from dataclasses import asdict
from typing import Literal
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import transaction

from game.checks import resolve_skill_check
from game.combat import require_outside_combat
from game.events import GameEvent
from game.memory import remember
from game.models import GameSession, GameTurn, NPCState, PlayerState
from game.turns import create_turn, get_existing_turn
from world.models import Location

CompanionAction = Literal[
    "recruit",
    "dismiss",
]

DEFAULT_RECRUITMENT = {
    "enabled": True,
    "minimum_trust": 10,
    "maximum_hostility": 10,
    "difficulty": 14,
    "retry_cooldown": 5,
    "required_flags": {},
    "refusal_reason": "",
}


def get_companions(
    session: GameSession,
    *,
    alive_only: bool = True,
):
    companions = NPCState.objects.filter(
        session=session,
        state__recruited=True,
    ).select_related(
        "npc",
        "current_location",
    )

    if alive_only:
        companions = companions.filter(
            alive=True,
            hp__gt=0,
        )

    return companions.order_by("pk")


def get_party_context(
    session: GameSession,
) -> list[dict]:
    return [
        {
            "slug": companion.npc.slug,
            "name": companion.npc.name,
            "hp": companion.hp,
            "max_hp": companion.max_hp,
            "alive": companion.alive,
            "trust": companion.trust,
            "hostility": companion.hostility,
            "location": companion.current_location.slug,
        }
        for companion in get_companions(
            session,
            alive_only=False,
        )
    ]


def get_recruitment_config(npc: NPCState) -> dict:
    return {
        **DEFAULT_RECRUITMENT,
        **npc.npc.data.get("recruitment", {}),  #  pyright: ignore
    }


def get_recruitment_refusal(
    *, session: GameSession, npc: NPCState, config: dict
) -> str | None:
    if not config["enabled"]:
        return "not_recruitable"

    if npc.hostility > config["maximum_hostility"]:
        return "too_hostile"

    if npc.trust < config["minimum_trust"]:
        return "insufficient_trust"

    for flag, expected in config["required_flags"].items():
        if session.flags.get(flag) != expected:  # pyright: ignore
            return "requirements_not_met"

    last_attempt = npc.state.get("last_recruitment_attempt")  # pyright: ignore

    next_turn = session.turn_number + 1  # pyright: ignore

    if last_attempt is not None:
        elapsed = next_turn - last_attempt

        if elapsed < config["retry_cooldown"]:
            return "recruitment_cooldown"

    return None


def resolve_recruitment(
    *,
    session: GameSession,
    npc: NPCState,
    player: PlayerState,
) -> dict:
    if npc.is_recruited:
        raise ValidationError("NPC is already recruited.")

    config = get_recruitment_config(npc)

    refusal = get_recruitment_refusal(
        session=session,
        npc=npc,
        config=config,
    )

    if refusal is not None:
        return {
            "success": False,
            "reason": refusal,
            "refusal_reasons": (
                config["refusal_reason"] if refusal == "not_recruitable" else ""
            ),
            "check": None,
        }

    trust_bonus = max(
        0,
        min(5, npc.trust // 10),  # pyright: ignore
    )

    check = resolve_skill_check(
        player=player,
        skill="persuasion",
        difficulty=config["difficulty"],
        bonus=trust_bonus,
    )

    npc.state = {  # pyright: ignore
        **npc.state,  # pyright: ignore
        "last_recruitment_attempt": (
            session.turn_number + 1  # pyright: ignore
        ),
    }

    if check.success:
        npc.state.update(  # pyright: ignore
            {
                "recruited": True,
                "recruited_at": session.turn_number + 1,  # pyright: ignore
            }
        )

    npc.save(update_fields=["state"])

    return {
        "success": check.success,
        "reason": "recruited" if check.success else "persuasion_failed",
        "trust_bonus": trust_bonus,
        "check": asdict(check),
    }


def resolve_dismissal(
    *,
    npc: NPCState,
) -> dict:
    if not npc.is_recruited:
        raise ValidationError("NPC is not recruited.")

    npc.state = {  # pyright: ignore
        **npc.state,  # pyright: ignore
        "recruited": False,
    }

    npc.save(update_fields=["state"])

    return {
        "success": True,
        "reason": "dismissed",
    }


@transaction.atomic
def process_companion_action(
    *,
    session: GameSession,
    action_id: UUID,
    action: CompanionAction,
    target_slug: str,
    original_message: str = "",
) -> GameTurn:
    session = GameSession.objects.select_for_update().get(pk=session.pk)

    if action not in {"recruit", "dismiss"}:
        raise ValidationError(f"Invalid companion action: {action}")

    existing = get_existing_turn(
        session=session,
        action_id=action_id,
        action_type=action,
        payload={
            "target": target_slug,
        },
    )

    if existing is not None:
        return existing

    require_outside_combat(session)

    if session.status != "active":
        raise ValidationError("Session is not active.")

    player = PlayerState.objects.select_for_update().get(session=session)

    if player.hp <= 0:
        raise ValidationError("Player cannot perform actions while dead.")

    npc = (
        NPCState.objects.select_for_update()
        .select_related("npc")
        .filter(
            session=session,
            npc__slug=target_slug,
            alive=True,
            hp__gt=0,
        )
        .first()
    )

    if npc is None:
        raise ValidationError(f"No living NPC found with slug: {target_slug}")

    events = []

    match action:
        case "recruit":
            result = resolve_recruitment(
                session=session,
                npc=npc,
                player=player,
            )

            if result["success"]:
                narration = f"{npc.npc.name} has joined your party."

                event_type = "npc_recruited"

            else:
                narration = f"{npc.npc.name} refuses to join your party."

                event_type = "recruitment_failed"
        case "dismiss":
            result = resolve_dismissal(
                npc=npc,
            )

            narration = f"{npc.npc.name} has been dismissed from your party."

            event_type = "npc_dismissed"

    events.append(
        GameEvent(
            type=event_type,
            target=target_slug,
            npc=target_slug,
            location=session.current_location.slug,  # pyright: ignore
            value=result["reason"],
        )
    )

    turn = create_turn(
        session=session,  # pyright: ignore
        action_id=action_id,  # pyright: ignore
        player_action=f"{action.capitalize()} {npc.npc.name}",  # pyright: ignore
        original_message=original_message,  # pyright: ignore
        narration=narration,  # pyright: ignore
        mechanical_result={  # pyright: ignore
            "type": action,
            "target": target_slug,
            "npc_name": npc.npc.name,
            "result": result,
            "party": get_party_context(session),
        },
        events=events,  # pyright: ignore
    )

    remember(
        npc_state=npc,
        turn=turn,  # pyright: ignore
        summary=narration,
        importance=3,
    )

    return turn  # pyright: ignore


def move_companions(
    *,
    session: GameSession,
    origin: Location,
    destination: Location,
) -> list[str]:
    companions = list(
        NPCState.objects.select_for_update()
        .filter(
            session=session,
            current_location=origin,
            state__recruited=True,
            alive=True,
            hp__gt=0,
        )
        .select_related("npc")
        .order_by("pk")
    )

    if not companions:
        return []

    for companion in companions:
        companion.current_location = destination  # pyright: ignore

    NPCState.objects.bulk_update(
        companions,
        ["current_location"],
    )

    return [companion.npc.slug for companion in companions]
