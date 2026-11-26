from dataclasses import asdict

from django.db import transaction

from game.models import PlayerState, QuestState
from game.progression import grant_experience


def grant_rewards(
    *,
    player: PlayerState,
    experience: int = 0,
    gold: int = 0,
    items: list[str] | None = None,
) -> dict:
    if experience < 0 or gold < 0:
        raise ValueError("Invalid reward amount.")

    items = items or []

    progression = grant_experience(
        player=player,
        amount=experience,
    )

    player.gold += gold  # pyright:ignore
    player.inventory.extend(items)  # pyright: ignore

    player.save(
        update_fields=[
            "gold",
            "inventory",
        ]
    )

    return {
        "experience": experience,
        "gold": gold,
        "items": items,
        "progression": asdict(progression),
    }


@transaction.atomic
def grant_quest_rewards(
    quest_state: QuestState,
) -> dict | None:
    quest_state = (
        QuestState.objects.select_for_update()
        .select_related("quest")
        .get(pk=quest_state.pk)
    )

    if quest_state.status not in {"completed", "failed"}:
        return None

    if quest_state.rewards_granted:
        return None

    player = PlayerState.objects.select_for_update().get(session=quest_state.session)

    quest_data = quest_state.quest.data  # pyright:ignore
    if quest_state.outcome:
        outcome = next(
            (
                outcome
                for outcome in quest_data.get("outcomes", [])
                if outcome["id"] == quest_state.outcome
            ),
            None,
        )
        if outcome is None:
            return None
        rewards = outcome.get("rewards") or {}
    elif quest_state.status == "completed":
        rewards = quest_data.get("rewards") or {}
    else:
        return None

    result = grant_rewards(
        player=player,
        experience=rewards.get("experience", 0),
        gold=rewards.get("gold", 0),
        items=rewards.get("items", []),
    )

    quest_state.rewards_granted = True  # pyright:ignore

    quest_state.save(update_fields=["rewards_granted"])

    return {
        "type": "quest_rewards_granted",
        "quest": quest_state.quest.slug,  # pyright:ignore
        **result,
    }
