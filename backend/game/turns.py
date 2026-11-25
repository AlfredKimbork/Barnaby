from uuid import UUID

from django.db import transaction
from django.db.models import Max

from game.events import GameEvent
from game.models import GameSession, GameTurn
from game.quests import process_game_events


def get_existing_turn(
    *,
    session: GameSession,
    action_id: UUID,
    action_type: str,
    payload: dict,
) -> GameTurn | None:
    existing = GameTurn.objects.filter(
        session=session,
        action_id=action_id,
    ).first()

    if existing is None:
        return None

    result = existing.mechanical_result

    if result.get("type") != action_type:
        raise ValueError(
            f"Existing turn with action_id {action_id} has a different action_type: {result.get('action_type')}"
        )

    for key, value in payload.items():
        if result.get(key) != value:
            raise ValueError(
                f"Existing turn with action_id {action_id} has a different value for '{key}': {result.get(key)} vs {value}"
            )

    return existing


@transaction.atomic
def create_turn(
    *,
    session: GameSession,
    action_id: UUID,
    player_action: str,
    narration: str,
    mechanical_result: dict,
    original_message: str = "",
    events: list[GameEvent] | None = None,
) -> GameTurn:
    last_turn_number = (
        GameTurn.objects.filter(session=session).aggregate(last=Max("number"))["last"]
        or 0
    )

    next_turn_number = max(session.turn_number, last_turn_number) + 1

    events = events or []

    quest_updates = process_game_events(
        session=session,
        events=events,
    )

    mechanical_result = {
        **mechanical_result,
        "events": [event.model_dump(mode="json") for event in events],
        "quest_updates": quest_updates,
    }

    turn = GameTurn.objects.create(
        session=session,
        number=next_turn_number,
        action_id=action_id,
        player_action=player_action,
        narration=narration,
        mechanical_result=mechanical_result,
        original_message=original_message,
    )

    session.turn_number = next_turn_number  # pyright: ignore
    session.save(update_fields=["turn_number", "updated_at", "created_at"])

    return turn
