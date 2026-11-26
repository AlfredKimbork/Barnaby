from collections import deque

from django.db import transaction
from django.utils import timezone

from game.events import EventCondition, GameEvent
from game.models import GameSession, QuestState
from game.rewards import grant_quest_rewards


def matches_any(
    conditions: list[dict],
    event: GameEvent,
) -> bool:
    return any(
        EventCondition.model_validate(condition).matches(event)
        for condition in conditions
    )


def has_matching_event(
    conditions: list[dict],
    events: list[GameEvent],
) -> bool:
    return any(matches_any(conditions, event) for event in events)


def conditions_satisfied(
    conditions: list[dict],
    events: list[GameEvent],
    mode: str = "any",
) -> bool:
    if not conditions:
        return False

    matches = [
        any(EventCondition.model_validate(condition).matches(event) for event in events)
        for condition in conditions
    ]

    if mode == "all":
        return all(matches)

    if mode == "any":
        return any(matches)

    raise ValueError(f"Unknown condition mode {mode}")


def apply_quest_consequences(
    *,
    session: GameSession,
    outcome: dict,
) -> list[GameEvent]:
    consequences = outcome.get("consequences", {})

    flags = dict(session.flags)  # pyright: ignore
    events = []

    for flag, value in consequences.get("flags", {}).items():
        if flag in flags and flags[flag] == value:
            continue

        flags[flag] = value

        events.append(
            GameEvent(
                type="flag_changed",
                flag=flag,
                value=value,
            )
        )

    if flags != session.flags:
        session.flags = flags  # pyright: ignore

        session.save(update_fields=["flags"])

    return events


def resolve_quest(
    *,
    quest_state: QuestState,
    outcome: dict | None = None,
) -> tuple[list[dict], list[GameEvent]]:
    if quest_state.status != "active":
        return [], []

    if outcome is None:
        quest_state.status = "completed"  # pyright: ignore
    else:
        quest_state.status = outcome["status"]
        quest_state.outcome = outcome["id"]

    quest_state.resolved_at = timezone.now()  # pyright: ignore

    quest_state.save(
        update_fields=[
            "status",
            "outcome",
            "resolved_at",
            "completed_objectives",
            "matched_events",
        ]
    )

    event_type = (
        "quest_completed" if quest_state.status == "completed" else "quest_failed"
    )

    updates = [
        {
            "type": event_type,
            "quest": quest_state.quest.slug,  # pyright: ignore
            "name": quest_state.quest.title,
            "outcome": quest_state.outcome or None,
            "outcome_name": (outcome["name"] if outcome else None),
            "description": (outcome["description"] if outcome else None),
        }
    ]

    events = [
        GameEvent(
            type="quest_resolved",
            quest=quest_state.quest.slug,  # pyright: ignore
            outcome=quest_state.outcome or None,  # pyright: ignore
        ),
        GameEvent(
            type=event_type,
            quest=quest_state.quest.slug,  # pyright: ignore
            outcome=quest_state.outcome or None,  # pyright: ignore
        ),
    ]

    if outcome is not None:
        events.extend(
            apply_quest_consequences(
                session=quest_state.session,  # pyright: ignore
                outcome=outcome,
            )
        )

    reward_update = grant_quest_rewards(quest_state)  # pyright:ignore

    if reward_update is not None:
        updates.append(reward_update)  # pyright:ignore

    return updates, events


def select_quest_outcome(
    *,
    quest_state: QuestState,
    history: list[GameEvent],
) -> dict | None:
    outcomes = quest_state.quest.data.get(  # pyright:ignore
        "outcomes",
        [],
    )

    completed = set(quest_state.completed_objectives)  # pyright:ignore

    ordered_outcomes = sorted(
        outcomes,
        key=lambda outcome: outcome.get("priority", 0),
        reverse=True,
    )

    for outcome in ordered_outcomes:
        required = set(outcome.get("required_objectives", []))

        if not required.issubset(completed):
            continue

        conditions = outcome.get("conditions", [])

        if conditions and not conditions_satisfied(
            conditions=conditions,
            events=history,
            mode=outcome.get("conditions_mode", outcome.get("condition_mode", "all")),
        ):
            continue

        return outcome

    return None


def process_quest_event(
    *,
    quest_state: QuestState,
    events: list[GameEvent],
) -> tuple[list[dict], list[GameEvent]]:
    if quest_state.status in {"completed", "failed"}:
        return [], []

    definition = quest_state.quest.data  # pyright:ignore

    activation = definition.get("activation", [])
    objectives = definition.get("objectives", [])
    outcomes = definition.get("outcomes", [])

    updates: list[dict] = []
    generated_events: list[GameEvent] = []

    history = [GameEvent.model_validate(event) for event in quest_state.matched_events]  # pyright:ignore

    # Record relevant events.
    for event in events:
        relevant = (
            matches_any(activation, event)
            or any(
                matches_any(
                    objective.get("conditions", []),
                    event,
                )
                for objective in objectives
            )
            or any(
                matches_any(
                    outcome.get("conditions", []),
                    event,
                )
                for outcome in outcomes
            )
        )

        if relevant and event not in history:
            history.append(event)

    quest_state.matched_events = [event.model_dump(mode="json") for event in history]  # pyright:ignore

    # Activate hidden quests.
    if quest_state.status in {"hidden", "not_started"} and (
        not activation or has_matching_event(activation, history)
    ):
        quest_state.status = "active"  # pyright:ignore

        updates.append(
            {
                "type": "quest_activated",
                "quest": quest_state.quest.slug,  # pyright:ignore
                "name": quest_state.quest.title,  # pyright:ignore
            }
        )

    # Evaluate objectives.
    if quest_state.status == "active":
        completed = set(quest_state.completed_objectives)  # pyright:ignore

        changed = True

        while changed:
            changed = False

            for objective in objectives:
                objective_id = objective["id"]

                if objective_id in completed:
                    continue

                prerequisites = set(objective.get("prerequisites", []))

                if not prerequisites.issubset(completed):
                    continue

                if not conditions_satisfied(
                    conditions=objective.get("conditions", []),
                    events=history,
                    mode=objective.get("condition_mode", "any"),
                ):
                    continue

                completed.add(objective_id)
                changed = True

                updates.append(
                    {
                        "type": "objective_completed",
                        "quest": quest_state.quest.slug,  # pyright:ignore
                        "objective": objective_id,
                        "description": objective.get("description", ""),
                    }
                )

        quest_state.completed_objectives = sorted(completed)  # pyright:ignore

        # Resolve branching quests.
        if outcomes:
            outcome = select_quest_outcome(
                quest_state=quest_state,
                history=history,
            )

            if outcome is not None:
                resolution_updates, resolution_events = resolve_quest(
                    quest_state=quest_state,
                    outcome=outcome,
                )

                updates.extend(resolution_updates)
                generated_events.extend(resolution_events)

        # Resolve traditional quests.
        else:
            required = {
                objective["id"]
                for objective in objectives
                if objective.get("required", True)
            }

            if required and required.issubset(completed):
                resolution_updates, resolution_events = resolve_quest(
                    quest_state=quest_state,
                )

                updates.extend(resolution_updates)
                generated_events.extend(resolution_events)

    quest_state.save(
        update_fields=[
            "status",
            "completed_objectives",
            "matched_events",
        ]
    )

    return updates, generated_events


@transaction.atomic
def process_game_events(
    *,
    session: GameSession,
    events: list[GameEvent],
) -> tuple[list[dict], list[GameEvent]]:
    if not events:
        return [], []

    session = GameSession.objects.select_for_update().get(pk=session.pk)

    pending = deque(events)
    processed: set[str] = set()

    updates: list[dict] = []
    all_events: list[GameEvent] = []

    while pending:
        batch: list[GameEvent] = []

        while pending:
            event = pending.popleft()
            key = event.model_dump_json()

            if key in processed:
                continue

            processed.add(key)
            batch.append(event)
            all_events.append(event)

        if not batch:
            continue

        quest_states = (
            QuestState.objects.select_for_update()
            .filter(session=session)
            .select_related("quest")
            .order_by("pk")
        )

        for quest_state in quest_states:
            quest_updates, generated_events = process_quest_event(
                quest_state=quest_state,
                events=batch,
            )

            updates.extend(quest_updates)
            pending.extend(generated_events)

    return updates, all_events


def get_visible_quests(
    session: GameSession,
) -> list[dict]:
    states = (
        QuestState.objects.filter(session=session)
        .exclude(status__in=["hidden", "not_started"])
        .select_related("quest")
    )

    quests = []

    for state in states:
        completed = set(state.completed_objectives)
        objectives = []

        for objective in state.quest.data.get("objectives", []):
            objective_id = objective["id"]

            if objective.get("hidden", False) and objective_id not in completed:
                continue

            prequisites = set(objective.get("prerequisites", []))

            if not prequisites.issubset(completed):
                continue

            objectives.append(
                {
                    "id": objective_id,
                    "description": objective.get("description", ""),
                    "completed": objective_id in completed,
                    "required": objective.get("required", True),
                }
            )
        quests.append(
            {
                "slug": state.quest.slug,  # pyright: ignore
                "title": state.quest.title,  # pyright: ignore
                "description": state.quest.description,  # pyright: ignore
                "status": state.status,  # pyright: ignore
                "objectives": objectives,
            }
        )
    return quests
