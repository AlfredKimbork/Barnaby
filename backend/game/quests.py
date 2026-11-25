from game.events import EventCondition, GameEvent
from game.models import GameSession, QuestState
from game.rewards import grant_quest_rewards


def matches_any(
    conditions: list[dict],
    event: GameEvent,
) -> bool:
    return any(
        EventCondition.model_validate(condition).matched(event)
        for condition in conditions
    )


def has_matching_event(
    conditions: list[dict],
    events: list[GameEvent],
) -> bool:
    return any(matches_any(conditions, event) for event in events)


def process_quest_event(
    *,
    quest_state: QuestState,
    events: list[GameEvent],
) -> list[dict]:
    if quest_state.status in {"completed", "failed"}:
        return []

    definition = quest_state.quest.data  #  pyright: ignore

    activation = definition.get("activation", [])
    objectives = definition.get("objectives", [])

    updates = []

    history = [
        GameEvent.model_validate(event)
        for event in quest_state.mathed_events  # pyright: ignore
    ]

    for event in events:
        relevant = matches_any(activation, event) or any(
            matches_any(objective.get("conditions", []), event)
            for objective in objectives
        )

        if relevant and event not in history:
            history.append(event)

    quest_state.matched_events = [event.model_dump(mode="json") for event in history]  # pyright: ignore

    if (
        quest_state.status == "hidden"
        and not activation
        or has_matching_event(activation, history)
    ):
        quest_state.status = "active"  # pyright: ignore
        updates.append(
            {
                "type": "quest_activated",
                "quest": quest_state.quest.slug,  # pyright: ignore
                "name": quest_state.quest.title,  # pyright: ignore
            }
        )
    if quest_state.status == "active":
        completed = set(
            quest_state.completed_objectives  # pyright: ignore
        )

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

                if not has_matching_event(
                    objective.get("conditions", []),
                    history,
                ):
                    continue

                completed.add(objective_id)
                changed = True

                updates.append(
                    {
                        "type": "objective_completed",
                        "quest": quest_state.quest.slug,  # pyright: ignore
                        "objective": objective_id,
                        "description": objective.get("description", ""),  # pyright: ignore
                    }
                )

        quest_state.completed_objectives = sorted(completed)  # pyright: ignore

        required = {
            objective["id"]
            for objective in objectives
            if objective.get("required", True)
        }

        if required and required.issubset(completed):
            quest_state.status = "completed"  # pyright: ignore

            updates.append(
                {
                    "type": "quest_completed",
                    "quest": quest_state.quest.slug,  # pyright: ignore
                    "name": quest_state.quest.title,  # pyright: ignore
                }
            )

    quest_state.save(
        update_fields=[
            "status",
            "completed_objectives",
            "matched_events",
        ]
    )  # pyright: ignore

    if quest_state.status == "completed":
        reward_update = grant_quest_rewards(
            quest_state=quest_state  # pyright:ignore
        )

        if reward_update is not None:
            updates.append(reward_update)

    return updates


def process_game_events(*, session: GameSession, events: list[GameEvent]) -> list[dict]:
    if not events:
        return []

    updates = []

    quest_states = (
        QuestState.objects.select_for_update()
        .filter(session=session)
        .select_related("quest")
        .order_by("pk")
    )

    for quest_state in quest_states:
        updates.extend(
            process_quest_event(
                quest_state=quest_state,
                events=events,
            )
        )

    return updates


def get_visible_quests(
    session: GameSession,
) -> list[dict]:
    states = (
        QuestState.objects.filter(session=session)
        .exclude(status="hidden")
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
