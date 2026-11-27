from dataclasses import asdict
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import transaction

from game.combat import (
    advance_combat_turn,
    build_combat_narration,
    get_active_combat,
    get_combat_context,
    get_combat_end_events,
    process_npc_turns,
    require_outside_combat,
    resolve_player_attack,
    start_combat,
)
from game.companions import move_companions
from game.events import GameEvent
from game.interactables import get_visible_interactables
from game.interactions import resolve_interaction
from game.memory import remember
from game.models import (
    CombatParticipant,
    CombatStatus,
    CombatTeam,
    GameSession,
    GameTurn,
    NPCState,
    PlayerState,
)
from game.relationships import apply_relationship_event
from game.turns import create_turn, get_existing_turn
from world.models import Item, Location


@transaction.atomic
def process_attack(
    *,
    session: GameSession,
    action_id: UUID,
    target_slug: str,
    weapon_slug: str,
    original_message: str = "",
) -> GameTurn:
    session = GameSession.objects.select_for_update().get(pk=session.pk)

    existing = get_existing_turn(
        session=session,
        action_id=action_id,
        action_type="attack",
        payload={
            "target": target_slug,
            "weapon": weapon_slug,
        },
    )

    if existing:
        return existing

    if session.status != "active":
        raise ValidationError("Cannot process action for an inactive session.")

    player = PlayerState.objects.select_for_update().get(session=session)

    if player.hp <= 0:  # pyright: ignore
        raise ValidationError("Cannot process action for a dead character.")

    target = (
        NPCState.objects.select_for_update()
        .select_related("npc", "current_location")
        .filter(
            session=session,
            npc__slug=target_slug,
        )
        .first()
    )

    if target is None:
        raise ValidationError(f"Target NPC '{target_slug}' not found in the session.")

    if target.current_location_id != session.current_location_id:  # pyright: ignore
        raise ValidationError(
            f"Target NPC '{target_slug}' is not in the same location as the player."
        )

    if not target.alive or target.hp <= 0:
        raise ValidationError(f"Target NPC '{target_slug}' is already dead.")

    # Inventory entries are assumed to be in item slugs
    if weapon_slug not in player.inventory:  # pyright: ignore
        raise ValidationError(
            f"Player does not have weapon '{weapon_slug}' in inventory."
        )

    weapon = Item.objects.filter(campaign=session.campaign, slug=weapon_slug).first()

    if weapon is None:
        raise ValidationError(f"Weapon '{weapon_slug}' not found in the campaign.")

    if weapon.data.get("type") != "weapon":
        raise ValidationError(f"Item '{weapon_slug}' is not a weapon.")

    properties = weapon.data.get("properties", {})

    if not properties.get("damage_dice"):
        raise ValidationError(
            f"Weapon '{weapon_slug}' does not have damage properties."
        )

    events: list[GameEvent] = []
    combat_action: list[dict] = []

    encounter = get_active_combat(session)

    combat_started = encounter is None

    if encounter is None:
        encounter = start_combat(
            session=session,  # pyright: ignore
            enemies=[target],  # pyright: ignore
        )

        events.append(
            GameEvent(
                type="combat_started",
                encounter=encounter.pk,  # pyright: ignore
                location=session.current_location.slug,  # pyright: ignore
            )
        )

    else:
        particpant_exists = CombatParticipant.objects.filter(
            encounter=encounter,
            npc=target,
            team=CombatTeam.ENEMY,
            withdrawn=False,
        ).exists()

        if not particpant_exists:
            raise ValidationError(
                f"Target NPC '{target_slug}' is not a participant in the active combat encounter."
            )

    encounter = type(encounter).objects.select_for_update().get(pk=encounter.pk)  # pyright: ignore

    combat_action.extend(process_npc_turns(encounter=encounter))

    player.refresh_from_db()
    target.refresh_from_db()

    target_available = (
        CombatParticipant.objects.filter(
            encounter=encounter,
            npc=target,
            team=CombatTeam.ENEMY,
            withdrawn=False,
        ).exists()
        and target.alive
        and target.hp > 0
    )

    player_attack = None

    if encounter.status == CombatStatus.ACTIVE and player.hp > 0 and target_available:
        player_attack = resolve_player_attack(
            encounter=encounter,
            player=player,
            target=target,
            weapon_slug=weapon_slug,
            properties=properties,
        )

        combat_action.append(player_attack)

        apply_relationship_event(
            npc_state=target,  # pyright: ignore
            event="attacked_npc",  # pyright: ignore
        )

        target.refresh_from_db()

        if not target.alive:
            events.append(
                GameEvent(
                    type="npc_killed",
                    target=target_slug,
                    npc=target_slug,
                    location=target.current_location.slug,
                )
            )

        advance_combat_turn(encounter)

    combat_action.extend(process_npc_turns(encounter=encounter))

    player.refresh_from_db()
    target.refresh_from_db()

    narration = build_combat_narration(
        actions=combat_action,
        status=encounter.status,
    )

    turn = create_turn(
        session=session,  # pyright: ignore
        action_id=action_id,  # pyright: ignore
        player_action=f"Attack {target_slug} with {weapon_slug}",  # pyright: ignore
        original_message=original_message,  # pyright: ignore
        narration=narration,  # pyright: ignore
        mechanical_result={  # pyright: ignore
            "type": "attack",
            "target": target_slug,
            "weapon": weapon_slug,
            "combat_started": combat_started,
            "combat_id": encounter.pk,
            "combat_status": encounter.status,
            "combat_round": encounter.round_number,
            "combat_actions": combat_action,
            "attack_result": (
                player_attack["attack_result"] if player_attack is not None else None
            ),
            "player_attack_executed": (player_attack is not None),
            "player_remaining_hp": player.hp,
            "target_remaining_hp": target.hp,
            "target_alive": target.alive,
            "npc_trust": target.trust,
            "npc_hostility": target.hostility,
            "relationship_event": (
                "attacked_npc" if player_attack is not None else None
            ),
            "combat": get_combat_context(session),
            "target_available": target_available,
        },
        events=events + get_combat_end_events(encounter),  # pyright: ignore
    )

    apply_relationship_event(npc_state=target, event="attacked_npc")  # pyright: ignore
    remember(
        npc_state=target,
        turn=turn,  # pyright: ignore
        summary=f"Player attacked {target.npc.name} using {weapon_slug}",
        importance=5,
    )  # pyright: ignore

    return turn  # pyright: ignore


@transaction.atomic
def process_movement(
    *,
    session: GameSession,
    action_id: UUID,
    destination_slug: str,
) -> GameTurn:
    # Lock the session to serialize actions within the playthrough
    session = (
        GameSession.objects.select_for_update()
        .select_related("current_location")
        .get(pk=session.pk)
    )

    existing = get_existing_turn(
        session=session,
        action_id=action_id,
        action_type="move",
        payload={
            "destination": destination_slug,
        },
    )

    if existing:
        return existing

    require_outside_combat(session)

    if session.status != "active":
        raise ValidationError("Cannot process action for an inactive session.")

    if session.player_state.hp <= 0:  # pyright: ignore
        raise ValidationError("Cannot process action for a dead character.")

    current = session.current_location
    connections = current.data.get("connections", [])  # pyright: ignore

    if destination_slug not in connections:
        raise ValidationError(
            f"Destination location '{destination_slug}' is not connected to the current location '{current.name}'."
        )

    destination = Location.objects.filter(
        campaign=session.campaign,
        slug=destination_slug,
    ).first()

    if destination is None:
        raise ValidationError(
            f"Destination location '{destination_slug}' not found in the campaign."
        )

    session.current_location = destination  # pyright: ignore
    session.save(update_fields=["current_location", "updated_at"])

    following_companions = move_companions(
        session=session,
        origin=current,  # pyright: ignore
        destination=destination,
    )

    narration = f"You travel from {current.name} to {destination.name}."

    return create_turn(
        session=session,  # pyright: ignore
        action_id=action_id,  # pyright: ignore
        player_action=f"Move to {destination_slug}",  # pyright: ignore
        narration=narration,  # pyright: ignore
        mechanical_result={  # pyright: ignore
            "type": "move",
            "from": current.slug,  # pyright: ignore
            "destination": destination.slug,
            "following_companions": following_companions,
        },
        events=[  # pyright: ignore
            GameEvent(
                type="location_entered",  # pyright: ignore
                target=destination_slug,
                location=destination.slug,
            )
        ],
    )


@transaction.atomic
def process_inspect(
    *,
    session: GameSession,
    action_id: UUID,
) -> GameTurn:
    session = (
        GameSession.objects.select_for_update()
        .select_related("current_location")
        .get(pk=session.pk)
    )

    existing = get_existing_turn(
        session=session,
        action_id=action_id,
        action_type="inspect",
        payload={},
    )

    if existing:
        return existing

    if session.status != "active":
        raise ValidationError("Cannot process action for an inactive session.")

    if session.player_state.hp <= 0:  # pyright: ignore
        raise ValidationError("Cannot process action for a dead character.")

    location = session.current_location

    npcs = NPCState.objects.filter(
        session=session,
        current_location=location,
        alive=True,
    ).select_related("npc")

    visible_npcs = [
        {
            "slug": npc.npc.slug,
            "name": npc.npc.name,
            "hp": npc.hp,
            "max_hp": npc.max_hp,
            "alive": npc.alive,
            "trust": npc.trust,
            "hostility": npc.hostility,
        }
        for npc in npcs
    ]

    connections = location.data.get("connections", [])  # pyright: ignore
    interactables = get_visible_interactables(session)

    narration = f"You look around {location.name}. {location.description} "

    if visible_npcs:
        npc_names = ", ".join(npc["name"] for npc in visible_npcs)
        narration += f"You notice {npc_names} nearby."

    return create_turn(
        session=session,  # pyright: ignore
        action_id=action_id,  # pyright: ignore
        player_action="Inspect surroundings",  # pyright: ignore
        narration=narration,  # pyright: ignore
        mechanical_result={  # pyright: ignore
            "type": "inspect",
            "location": {
                "slug": location.slug,  # pyright: ignore
                "name": location.name,  # pyright: ignore
                "description": location.description,  # pyright: ignore
            },
            "npcs": visible_npcs,
            "connections": connections,
            "interactables": interactables,
        },
    )


def get_dialogue_response(npc_data: dict) -> str:
    examples = npc_data.get("examples", [])

    if examples:
        example = examples[0]

        if isinstance(example, str):
            return example

        if isinstance(example, dict):
            return str(
                example.get("response")
                or example.get("text")
                or "I have nothing to say."
            )

    return "I have nothing to say about that right now."


@transaction.atomic
def process_talk(
    *,
    session: GameSession,
    action_id: UUID,
    target_slug: str,
    intent: str,
    message: str,
) -> GameTurn:
    session = (
        GameSession.objects.select_for_update()
        .select_related("current_location")
        .get(pk=session.pk)
    )

    existing = get_existing_turn(
        session=session,
        action_id=action_id,
        action_type="talk",
        payload={
            "target": target_slug,
            "message": message,
        },
    )

    if existing:
        return existing

    require_outside_combat(session)

    if session.status != "active":
        raise ValidationError("Cannot process action for an inactive session.")

    if session.player_state.hp <= 0:  # pyright: ignore
        raise ValidationError("Cannot process action for a dead character.")

    npc_state = (
        NPCState.objects.select_for_update()
        .select_related("npc")
        .filter(
            session=session,
            npc__slug=target_slug,
            current_location=session.current_location,
            alive=True,
        )
        .first()
    )

    player = PlayerState.objects.select_for_update().get(session=session)

    if npc_state is None:
        raise ValidationError(f"Target NPC '{target_slug}' not found in the session.")

    narration = f"You speak to {npc_state.npc.name}."

    interaction = resolve_interaction(
        player=player,
        npc_state=npc_state,
        intent=intent,
    )

    return create_turn(  # pyright: ignore
        session=session,  # pyright: ignore
        action_id=action_id,  # pyright: ignore
        player_action=f"Talk to {target_slug}: {message}",  # pyright: ignore
        narration=narration,  # pyright: ignore
        mechanical_result={  # pyright: ignore
            "type": "talk",
            "target": target_slug,
            "message": message,
            "npc_name": npc_state.npc.name,
            "interaction": asdict(interaction),
            "npc_trust": npc_state.trust,
            "npc_hostility": npc_state.hostility,
        },
        events=[  # pyright: ignore
            GameEvent(
                type="npc_talked_to",
                target=target_slug,
                npc=target_slug,
                location=session.current_location.slug,  # pyright: ignore
            )
        ],
    )
