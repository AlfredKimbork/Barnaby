from copy import deepcopy
from dataclasses import asdict
from uuid import UUID

from django.db import transaction
from pydantic import ValidationError

from game.checks import resolve_skill_check
from game.models import GameSession, GameTurn, InteractableState, PlayerState
from game.turns import create_turn, get_existing_turn
from world.models import Location

OBJECT_SKILLS = {
    "investigate": "investigate",
    "unlock": "sleight_of_hand",
    "force_open": "athletics",
}


def initialize_interactables(session: GameSession) -> None:
    locations = Location.objects.filter(
        campaign=session.campaign,
    )

    for location in locations:
        for definition in location.data.get("interactables", []):
            InteractableState.objects.get_or_create(
                session=session,
                location=location,
                slug=definition["id"],
                defaults={
                    "locked": definition.get("locked", False),
                    "discovered": False,
                    "context": deepcopy(definition.get("context", [])),
                    "state": {},
                },
            )


def get_visible_interactables(session: GameSession) -> list[dict]:
    location = session.current_location

    definitions = {
        obj["id"]: obj
        for obj in location.data.get("interactables", [])  # pyright: ignore
    }

    states = InteractableState.objects.filter(
        session=session,
        location=location,
    )

    visible_interactables = []

    for state in states:
        definition = definitions.get(state.slug)
        if not definition:
            continue

        obj = {
            "slug": state.slug,
            "description": definition["description"],  # pyright: ignore
            "locked": state.locked,  # pyright: ignore
            "discovered": state.discovered,  # pyright: ignore
        }

        if state.discovered and not state.locked:
            obj["discovery"] = definition.get("discovery")  # pyright: ignore
            obj["contents"] = state.contents

        visible_interactables.append(obj)

    return visible_interactables


@transaction.atomic
def process_interactable(
    *,
    session: GameSession,
    action_id: UUID,
    target_slug: str,
    operation: str,
    item_slug: str | None = None,
) -> GameTurn:
    session = (
        GameSession.objects.select_for_update()
        .select_related("character")
        .get(id=session.pk)
    )

    existing = get_existing_turn(
        session=session,
        action_id=action_id,
        action_type="interact",
        payload={
            "target": target_slug,
            "operation": operation,
            "item": item_slug,
        },
    )

    if existing:
        return existing

    if session.status != "active":
        raise ValidationError("Cannot process interactable in a non-active session.")

    player = PlayerState.objects.select_for_update().get(session=session)
    character = session.character

    if player.hp <= 0:
        raise ValidationError("Cannot process interactable when player is dead.")

    if operation not in {*OBJECT_SKILLS, "take"}:
        raise ValidationError(f"Invalid operation '{operation}' for interactable.")

    location = session.current_location

    definition = next(
        (
            obj
            for obj in location.data.get("interactables", [])  # pyright: ignore
            if obj["id"] == target_slug
        ),
        None,
    )

    if definition is None:
        raise ValidationError(f"Interactable '{target_slug}' not found in location.")

    state = InteractableState.objects.select_for_update().get(
        session=session,
        location=location,
        slug=target_slug,
    )

    result = {
        "type": "interact",
        "target": target_slug,
        "operation": operation,
        "item": item_slug,
        "success": False,
        "check": None,
        "consequences": [],
    }

    if operation == "take":
        if state.locked or not state.discovered:
            raise ValidationError(f"Cannot take from interactable '{target_slug}'.")

        if not item_slug or item_slug not in state.contents:
            raise ValidationError(
                f"Item '{item_slug}' not found in interactable '{target_slug}'."
            )

        state.contents.remove(item_slug)
        player.inventory.append(item_slug)

        state.save(update_fields=["contents"])
        player.save(update_fields=["inventory"])

        result["success"] = True
        result["consequences"].append(
            {
                "type": "item_collected",
                "item": item_slug,
            }
        )
    elif operation == "investigate":
        if state.locked:
            raise ValidationError(
                f"Cannot investigate locked interactable '{target_slug}'."
            )
        if state.discovered:
            result["success"] = True
        else:
            check = resolve_skill_check(
                character=character,  # pyright: ignore
                skill="investigation",
                difficulty=definition.get("difficulty", 10),
            )

            result["check"] = asdict(check)
            result["success"] = check.success

            if check.success:
                state.discovered = True
                state.save(update_fields=["discovered"])

                result["consequences"].append(
                    {
                        "type": "object_discovered",
                        "slug": target_slug,
                    }
                )
        if result["success"]:
            result["discovery"] = definition.get("discovery")
            result["contents"] = list(state.contents)
    elif operation in {"unlock", "force_open"}:
        if not state.locked:
            raise ValidationError(
                f"Cannot {operation} an already unlocked interactable '{target_slug}'."
            )

        difficulty = definition.get("unlock_dc", 14)

        if operation == "force_open":
            difficulty += 2

        check = resolve_skill_check(
            character=character,  # pyright: ignore
            skill=OBJECT_SKILLS[operation],
            difficulty=difficulty,
        )

        result["check"] = asdict(check)
        result["success"] = check.success

        if check.success:
            state.locked = False
            state.save(update_fields=["locked"])

            result["consequences"].append(
                {
                    "type": "object_unlocked",
                    "slug": target_slug,
                }
            )

    result["locked"] = state.locked
    result["discovered"] = state.discovered

    return create_turn(
        session=session,
        action_id=action_id,
        player_action=f"{operation} {target_slug}",
        narration="",
        mechanical_result=result,
    )
