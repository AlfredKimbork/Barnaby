from dataclasses import asdict
from typing import Literal
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import transaction

from game.checks import resolve_skill_check
from game.combat import (
    DEFENDING_BONUS,
    advance_combat_turn,
    build_combat_narration,
    end_encounter,
    get_active_combat,
    get_combat_context,
    get_combat_end_events,
    get_current_participant,
    get_npc_combat_events,
    process_npc_turns,
    retreat_companions,
)
from game.events import GameEvent
from game.models import (
    CombatEncounter,
    CombatParticipant,
    CombatStatus,
    GameSession,
    GameTurn,
    PlayerState,
)
from game.turns import create_turn, get_existing_turn
from world.models import Location

CombatAction = Literal[
    "defend",
    "flee",
    "surrender",
]

FLEE_DIFFICULTY = 12


def resolve_defend(
    *,
    encounter: CombatEncounter,
    participant: CombatParticipant,
) -> dict:
    participant.defending = True  # pyright: ignore

    participant.save(update_fields=["defending"])

    return {
        "type": "player_defend",
        "round": encounter.round_number,
        "success": True,
        "defense": DEFENDING_BONUS,
    }


def get_escape_destination(*, session: GameSession, destination_slug: str) -> Location:
    connections = session.current_location.data.get("connections", [])  # pyright: ignore

    if destination_slug not in connections:
        raise ValidationError(
            f"Destination '{destination_slug}' is not connected to the current location."
        )

    destination = Location.objects.filter(
        campaign=session.campaign, slug=destination_slug
    ).first()

    if destination is None:
        raise ValidationError(
            f"Destination '{destination_slug}' does not exist in the campaign."
        )

    return destination


def resolve_flee(
    *,
    session: GameSession,
    encounter: CombatEncounter,
    participant: CombatParticipant,
    player: PlayerState,
    destination: Location,
) -> tuple[dict, list[GameEvent]]:
    skill = max(
        ("athletics", "acrobatics"),
        key=lambda skill: player.skill_modifier(skill),
    )

    check = resolve_skill_check(player=player, skill=skill, difficulty=FLEE_DIFFICULTY)

    result = {
        "type": "player_flee",
        "round": encounter.round_number,
        "success": check.success,
        "destination": destination.slug,
        "escaped_companions": [],
        "check": asdict(check),
    }

    events: list[GameEvent] = []

    if check.success:
        participant.withdrawn = True  # pyright: ignore

        participant.save(update_fields=["withdrawn"])

        result["escaped_companions"] = retreat_companions(
            encounter=encounter,
            destination=destination,
        )

        end_encounter(
            encounter=encounter,
            status=CombatStatus.ESCAPED,
        )

        session.current_location = destination  # pyright: ignore

        session.save(
            update_fields=[
                "current_location",
                "updated_at",
            ]
        )

        events.append(
            GameEvent(
                type="location_entered",
                target=destination.slug,  # pyright: ignore
                location=destination.slug,  # pyright: ignore
            )
        )

    return result, events


def resolve_surrender(
    *,
    encounter: CombatEncounter,
    participant: CombatParticipant,
) -> dict:
    participant.withdrawn = True  # pyright: ignore

    participant.save(update_fields=["withdrawn"])

    end_encounter(encounter=encounter, status=CombatStatus.SURRENDERED)

    return {
        "type": "player_surrender",
        "round": encounter.round_number,
        "success": True,
    }


@transaction.atomic
def process_combat_action(
    *,
    session: GameSession,
    action_id: UUID,
    action: CombatAction,
    destination_slug: str | None = None,
    original_message: str = "",
) -> GameTurn:
    session = GameSession.objects.select_for_update().get(id=session.pk)

    if action not in {
        "defend",
        "flee",
        "surrender",
    }:
        raise ValidationError(f"Invalid combat action: {action}")

    existing = get_existing_turn(
        session=session,
        action_id=action_id,
        action_type=action,
        payload={
            "destination": destination_slug,
        },
    )

    if existing is not None:
        if existing.original_message != original_message:
            raise ValidationError("Action ID has a different message.")

        return existing

    if session.status != "active":
        raise ValidationError("Game session is not active.")

    encounter = get_active_combat(session)

    if encounter is None:
        raise ValidationError("No active combat encounter.")

    encounter = CombatEncounter.objects.select_for_update().get(id=encounter.pk)

    player = PlayerState.objects.select_for_update().get(session=session)

    if player.hp <= 0:
        raise ValidationError("Player is incapacitated and cannot act.")

    destination = None

    if action == "flee":
        if not destination_slug:
            raise ValidationError("Destination slug is required for fleeing.")

        destination = get_escape_destination(
            session=session,
            destination_slug=destination_slug,
        )

    elif destination_slug is not None:
        raise ValidationError(
            "Destination slug should not be provided for this action."
        )

    events: list[GameEvent] = []

    combat_actions = process_npc_turns(encounter=encounter)

    player.refresh_from_db()

    player_result = None

    if encounter.status == CombatStatus.ACTIVE and player.hp > 0:
        participant = get_current_participant(encounter=encounter)

        if participant is None or not participant.is_player or not participant.can_act:
            raise ValidationError("It's not the player's turn.")

        match action:
            case "defend":
                player_result = resolve_defend(
                    encounter=encounter,
                    participant=participant,
                )
            case "flee":
                if destination is None:
                    raise ValidationError("Destination is required for fleeing.")

                player_result, flee_events = resolve_flee(
                    session=session,
                    encounter=encounter,
                    participant=participant,
                    player=player,
                    destination=destination,
                )

                events.extend(flee_events)
            case "surrender":
                player_result = resolve_surrender(
                    encounter=encounter,
                    participant=participant,
                )

        combat_actions.append(player_result)

        if encounter.status == CombatStatus.ACTIVE:
            advance_combat_turn(encounter=encounter)

            combat_actions.extend(process_npc_turns(encounter=encounter))

    events.extend(
        get_npc_combat_events(
            encounter=encounter,
            actions=combat_actions,
        )
    )
    events.extend(
        get_combat_end_events(
            encounter=encounter,
        )
    )

    player.refresh_from_db()

    return create_turn(
        session=session,  #  pyright: ignore
        action_id=action_id,  #  pyright: ignore
        player_action=(  #  pyright: ignore
            f"Flee to {destination.slug}" if action == "flee" else action.capitalize()  #  pyright: ignore
        ),
        original_message=original_message,  #  pyright: ignore
        narration=build_combat_narration(  #  pyright: ignore
            actions=combat_actions,
            status=encounter.status,
        ),
        mechanical_result={  #  pyright: ignore
            "type": action,
            "destination": destination_slug,
            "combat_id": encounter.pk,
            "combat_status": encounter.status,
            "combat_round": encounter.round_number,
            "combat_actions": combat_actions,
            "player_action_executed": (player_result is not None),
            "player_result": player_result,
            "player_remaining_hp": player.hp,
            "combat": get_combat_context(session=session),
        },
        events=events,  #  pyright: ignore
    )
