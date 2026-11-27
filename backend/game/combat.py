from dataclasses import asdict

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from game.checks import roll_d20
from game.combat_ai import choose_npc_action
from game.events import GameEvent
from game.mechanics import (
    get_npc_defense,
    get_player_defense,
    get_weapon_modifier,
    resolve_combat_attack,
    roll_initiative,
)
from game.models import (
    CombatEncounter,
    CombatParticipant,
    CombatStatus,
    CombatTeam,
    GameSession,
    NPCState,
    PlayerState,
)
from world.models import Location

DEFENDING_BONUS = 2
DEFAULT_NPC_ATTACK = {
    "id": "unarmed",
    "name": "Unarmed Strike",
    "attack_bonus": 0,
    "damage_modifier": 0,
    "damage_dice": "1d2",
    "damage_type": "bludgeoning",
}

NPC_FLEE_DIFFICULTY = 12


def get_active_combat(session: GameSession) -> CombatEncounter | None:
    return CombatEncounter.objects.filter(
        session=session,
        status=CombatStatus.ACTIVE,
    ).first()


@transaction.atomic
def start_combat(*, session: GameSession, enemies: list[NPCState]) -> CombatEncounter:
    session = GameSession.objects.select_for_update().get(id=session.pk)

    if get_active_combat(session) is not None:
        raise ValidationError("A combat encounter is already active for this session.")

    if session.status != "active":
        raise ValidationError("Cannot start combat for a session that is not active.")

    player = PlayerState.objects.select_for_update().get(session=session)

    if player.hp <= 0:
        raise ValidationError("Cannot start combat for a player that is dead.")

    if not enemies:
        raise ValidationError("Cannot start combat with no enemies.")

    enemy_ids = {enemy.pk for enemy in enemies}

    enemies = list(
        NPCState.objects.select_for_update()
        .filter(
            pk__in=enemy_ids,
            session=session,
            current_location=session.current_location,
            alive=True,
            hp__gt=0,
        )
        .select_related("npc")
        .order_by("pk")
    )

    if len(enemies) != len(enemy_ids):
        raise ValidationError("Some enemies are not valid for this session.")

    encounter = CombatEncounter.objects.create(
        session=session,
        location=session.current_location,
    )

    participants = []

    player_initiative = roll_initiative(player.attribute_modifier("dexterity"))

    participants.append(
        CombatParticipant(
            encounter=encounter,
            player=player,
            team=CombatTeam.PLAYER,
            initiative=player_initiative.total,
            initiative_modifier=player_initiative.modifier,
            position=0,
        )
    )

    for enemy in enemies:
        combat = enemy.npc.data.get("combat", {})  # pyright: ignore

        initiative = roll_initiative(combat.get("initiative_modifier", 0))

        participants.append(
            CombatParticipant(
                encounter=encounter,
                npc=enemy,
                team=CombatTeam.ENEMY,
                initiative=initiative.total,
                initiative_modifier=initiative.modifier,
                position=0,
            )
        )

    participants.sort(
        key=lambda participant: (
            participant.initiative,
            participant.initiative_modifier,
        ),
        reverse=True,
    )

    for position, participant in enumerate(participants):
        participant.position = position

    CombatParticipant.objects.bulk_create(participants)

    return encounter


def get_current_participant(encounter: CombatEncounter) -> CombatParticipant | None:
    if encounter.status != CombatStatus.ACTIVE:
        return None

    return (
        CombatParticipant.objects.filter(encounter=encounter)
        .select_related(
            "player",
            "npc",
            "npc__npc",
        )
        .get(position=encounter.turn_index)
    )


def evaluate_combat_status(encounter: CombatEncounter) -> CombatStatus:
    participants = CombatParticipant.objects.filter(encounter=encounter).select_related(
        "player",
        "npc",
    )

    player_alive = any(
        participant.is_player and participant.alive for participant in participants
    )

    enemies_remaining = any(
        participant.team == CombatTeam.ENEMY and participant.can_act
        for participant in participants
    )

    if not player_alive:
        return CombatStatus.DEFEAT

    if not enemies_remaining:
        return CombatStatus.VICTORY

    return CombatStatus.ACTIVE


def get_combat_defense(
    participant: CombatParticipant,
) -> int:
    if participant.player is not None:
        defense = get_player_defense(participant.player)  # pyright: ignore

    elif participant.npc is not None:
        defense = get_npc_defense(participant.npc)  # pyright: ignore

    else:
        raise RuntimeError("Combat participant has no actor.")

    if participant.defending:
        defense += DEFENDING_BONUS

    return defense


def end_encounter(*, encounter: CombatEncounter, status: CombatStatus) -> None:
    if status == CombatStatus.ACTIVE:
        raise ValidationError("Cannot end an encounter with status ACTIVE.")

    if encounter.status != CombatStatus.ACTIVE:
        return

    encounter.status = status  # pyright: ignore
    encounter.ended_at = timezone.now()  # pyright: ignore

    encounter.save(
        update_fields=[
            "status",
            "ended_at",
        ]
    )


def get_combat_context(session: GameSession) -> dict | None:
    encounter = get_active_combat(session)

    if encounter is None:
        return None

    participants = []

    states = (
        CombatParticipant.objects.filter(encounter=encounter)
        .select_related(
            "player",
            "npc",
            "npc__npc",
        )
        .order_by("position")
    )

    for participant in states:
        if participant.player is not None:
            actor = participant.player

            name = session.character.name
            slug = "player"

        elif participant.npc is not None:
            actor = participant.npc

            name = actor.npc.name
            slug = actor.npc.slug

        else:
            raise RuntimeError("Combat participant has no actor.")

        participants.append(
            {
                "name": name,
                "slug": slug,
                "team": participant.team,
                "initiative": participant.initiative,
                "position": participant.position,
                "current_turn": (participant.position == encounter.turn_index),
                "alive": participant.alive,
                "hp": actor.hp,
                "max_hp": actor.max_hp,
                "defending": participant.defending,
                "withdrawn": participant.withdrawn,
            }
        )

    return {
        "id": encounter.pk,
        "status": encounter.status,
        "round": encounter.round_number,
        "turn_index": encounter.turn_index,
        "participants": participants,
    }


def select_npc_attack(npc_state: NPCState) -> dict:
    combat = npc_state.npc.data.get("combat", {})  # pyright: ignore

    attacks = combat.get("attacks", [])

    if not attacks:
        return DEFAULT_NPC_ATTACK.copy()

    return attacks[0]


def resolve_npc_attack(
    *,
    encounter: CombatEncounter,
    participant: CombatParticipant,
) -> dict:
    if participant.npc is None:
        raise ValidationError("Combat participant is not an NPC.")

    if participant.team != CombatTeam.ENEMY:
        raise ValidationError("Combat participant is not an enemy.")

    if participant.position != encounter.turn_index:
        raise ValidationError("Combat participant is not the current turn.")

    npc = NPCState.objects.select_for_update().get(id=participant.npc_id)  # pyright: ignore

    player = PlayerState.objects.select_for_update().get(session=encounter.session)

    if not npc.alive or npc.hp <= 0:
        raise ValidationError("NPC is dead and cannot attack.")

    if player.hp <= 0:
        raise ValidationError("Player is dead and cannot be attacked.")

    attack = select_npc_attack(npc)

    player_participant = CombatParticipant.objects.select_related("player").get(
        encounter=encounter,
        player=player,
    )

    result = resolve_combat_attack(
        attack_modifier=attack.get("attack_bonus", 0),
        damage_modifier=attack.get("damage_modifier", 0),
        damage_dice=attack["damage_dice"],
        defense=get_combat_defense(player_participant),
    )

    previous_hp = player.hp

    if result.hit:
        player.take_damage(result.damage)

    player.refresh_from_db()

    return {
        "type": "npc_attack",
        "round": encounter.round_number,
        "attacker": npc.npc.slug,
        "attacker_name": npc.npc.name,
        "target": "player",
        "weapon": attack["id"],
        "weapon_name": attack["name"],
        "attack_result": asdict(result),
        "damage_applied": previous_hp - player.hp,
        "target_remaining_hp": player.hp,
        "target_alive": player.hp > 0,
    }


def resolve_npc_defend(
    *, encounter: CombatEncounter, participant: CombatParticipant
) -> dict:
    if participant.npc is None:
        raise ValidationError("Combat participant is not an NPC.")

    participant.defending = True  # pyright: ignore

    participant.state = {  # pyright: ignore
        **participant.state,  # pyright: ignore
        "last_defended_round": (encounter.round_number),
    }

    participant.save(
        update_fields=[
            "defending",
            "state",
        ]
    )

    return {
        "type": "npc_defend",
        "round": encounter.round_number,
        "actor": participant.pk,
        "actor_name": participant.npc.npc.name,  # pyright: ignore
        "defense_bonus": DEFENDING_BONUS,
    }


def resolve_npc_surrender(
    *,
    encounter: CombatEncounter,
    participant: CombatParticipant,
) -> dict:
    if participant.npc is None:
        raise ValidationError("Combat participant is not an NPC.")

    participant.withdrawn = True  # pyright: ignore
    participant.defending = False  # pyright: ignore

    participant.state = {  # pyright: ignore
        **participant.state,  # pyright: ignore
        "withdrawal_reason": "surrendered",
    }

    participant.save(
        update_fields=[
            "withdrawn",
            "defending",
            "state",
        ]
    )

    return {
        "type": "npc_surrender",
        "round": encounter.round_number,
        "actor": participant.npc.npc.slug,  # pyright: ignore
        "actor_name": participant.npc.npc.name,  # pyright: ignore
        "success": True,
    }


def get_npc_retreat_destination(
    encounter: CombatEncounter,
) -> Location | None:
    connections = encounter.location.data.get("connections", [])  # pyright: ignore

    return (
        Location.objects.filter(
            campaign=encounter.session.campaign,  # pyright: ignore
            slug__in=connections,
        )
        .order_by("slug")
        .first()
    )


def resolve_npc_flee(
    *, encounter: CombatEncounter, participant: CombatParticipant
) -> dict:
    if participant.npc is None:
        raise ValidationError("Combat participant is not an NPC.")

    npc = participant.npc

    destination = get_npc_retreat_destination(encounter)

    if destination is None:
        return {
            "type": "npc_flee",
            "round": encounter.round_number,
            "actor": npc.npc.slug,  # pyright: ignore
            "actor_name": npc.npc.name,  # pyright: ignore
            "success": False,
            "reason": "no_escape_route",
            "destination": None,
        }

    modifier = npc.npc.data.get("combat", {}).get("initiative_modifier", 0)  # pyright: ignore

    roll, rolls = roll_d20()
    total = roll + modifier

    success = total >= NPC_FLEE_DIFFICULTY

    if success:
        npc.current_location = destination  # pyright: ignore

        npc.save(update_fields=["current_location"])  # pyright: ignore

        participant.withdrawn = True  # pyright: ignore
        participant.defending = False  # pyright: ignore

        participant.state = {  # pyright: ignore
            **participant.state,  # pyright: ignore
            "withdrawal_reason": "fled",
        }

        participant.save(
            update_fields=[
                "withdrawn",
                "defending",
                "state",
            ]
        )

    return {
        "type": "npc_flee",
        "round": encounter.round_number,
        "actor": npc.npc.slug,  # pyright: ignore
        "actor_name": npc.npc.name,  # pyright: ignore
        "success": success,
        "destination": (destination.slug if success else None),
        "check": {
            "roll": roll,
            "rolls": rolls,
            "modifier": modifier,
            "total": total,
            "difficulty": NPC_FLEE_DIFFICULTY,
            "success": success,
        },
    }


def resolve_npc_turn(
    *,
    encounter: CombatEncounter,
    participant: CombatParticipant,
) -> dict:
    decision = choose_npc_action(
        encounter=encounter,
        participant=participant,
    )

    match decision.action:
        case "attack":
            result = resolve_npc_attack(
                encounter=encounter,
                participant=participant,
            )

        case "defend":
            result = resolve_npc_defend(
                encounter=encounter,
                participant=participant,
            )

        case "flee":
            result = resolve_npc_flee(
                encounter=encounter,
                participant=participant,
            )

        case "surrender":
            result = resolve_npc_surrender(
                encounter=encounter,
                participant=participant,
            )

    return {
        **result,  # pyright: ignore
        "decision_reason": decision.reason,
    }


def process_npc_turns(*, encounter: CombatEncounter) -> list[dict]:
    actions: list[dict] = []

    while encounter.status == CombatStatus.ACTIVE:
        participant = get_current_participant(encounter)

        if participant is None:
            break

        if not participant.can_act:
            advance_combat_turn(encounter)
            continue

        if participant.is_player:
            break

        if participant.team == CombatTeam.ENEMY:
            result = resolve_npc_turn(
                encounter=encounter,
                participant=participant,
            )
            actions.append(result)

        else:
            actions.append(
                {
                    "type": "skip",
                    "round": encounter.round_number,
                    "participant": participant.pk,
                }
            )

        advance_combat_turn(encounter)

    return actions


def get_npc_combat_events(
    *,
    encounter: CombatEncounter,
    actions: list[dict],
) -> list[GameEvent]:
    events: list[GameEvent] = []

    for action in actions:
        action_type = action["type"]

        if action_type == "npc_surrender":
            events.append(
                GameEvent(
                    type="npc_surrendered",
                    encounter=encounter.pk,  # pyright: ignore
                    target=action["actor"],
                    npc=action["actor"],
                    location=encounter.location.slug,  # pyright: ignore
                )
            )

        elif action_type == "npc_flee" and action["success"]:
            events.append(
                GameEvent(
                    type="npc_fled",
                    encounter=encounter.pk,  # pyright: ignore
                    target=action["actor"],
                    npc=action["actor"],
                    location=encounter.location.slug,  # pyright: ignore
                    value=action["destination"],
                )
            )

    return events


def resolve_player_attack(
    *,
    encounter: CombatEncounter,
    player: PlayerState,
    target: NPCState,
    weapon_slug: str,
    properties: dict,
) -> dict:
    participant = get_current_participant(encounter)

    if participant is None or not participant.is_player:
        raise ValidationError("It is not the player's turn.")

    if participant.player_id != player.pk:  # pyright: ignore
        raise ValidationError("Invalid player participant.")

    if not participant.can_act:
        raise ValidationError("Player cannot act this turn.")

    target_participant = CombatParticipant.objects.filter(
        encounter=encounter,
        npc=target,
        team=CombatTeam.ENEMY,
        withdrawn=False,
    ).first()

    if target_participant is None:
        raise ValidationError("Target is not an active enemy.")

    if not target.alive or target.hp <= 0:
        raise ValidationError("Target is already dead.")

    modifier = get_weapon_modifier(
        player=player,
        properties=properties,
    )

    result = resolve_combat_attack(
        attack_modifier=(modifier + player.proficiency_bonus),
        damage_modifier=modifier,
        damage_dice=properties["damage_dice"],
        defense=get_combat_defense(target_participant),
    )

    previous_hp = target.hp

    if result.hit:
        target.hp = max(  # pyright: ignore
            0,
            target.hp - result.damage,  # pyright: ignore
        )

        target.alive = target.hp > 0  # pyright: ignore

        target.save(update_fields=["hp", "alive"])

    return {
        "type": "player_attack",
        "round": encounter.round_number,
        "attacker": "player",
        "target": target.npc.slug,  # pyright: ignore
        "target_name": target.npc.name,
        "weapon": weapon_slug,
        "attack_result": asdict(result),
        "damage_applied": previous_hp - target.hp,  # pyright: ignore
        "target_remaining_hp": target.hp,
        "target_alive": target.alive,
    }


def get_combat_end_events(encounter: CombatEncounter) -> list[GameEvent]:
    if encounter.status == CombatStatus.ACTIVE:
        return []

    events = [
        GameEvent(
            type="combat_ended",
            encounter=encounter.pk,
            location=encounter.location.slug,  # pyright: ignore
            value=encounter.status,  # pyright: ignore
        )
    ]

    event_types = {
        CombatStatus.VICTORY: "combat_won",
        CombatStatus.DEFEAT: "combat_lost",
        CombatStatus.ESCAPED: "combat_escaped",
        CombatStatus.SURRENDERED: "player_surrendered",
    }

    event_type = event_types.get(encounter.status)  # pyright: ignore

    if event_type is not None:
        events.append(
            GameEvent(
                type=event_type,  # pyright: ignore
                encounter=encounter.pk,
                location=encounter.location.slug,  # pyright: ignore
            )
        )

    return events


def build_combat_narration(
    *,
    actions: list[dict],
    status: str,
) -> str:
    lines: list[str] = []

    for action in actions:
        if action["type"] == "npc_defend":
            lines.append(f"{action['actor_name']} takes a defensive stance.")
            continue

        if action["type"] == "npc_surrender":
            lines.append(f"{action['actor_name']} surrenders.")
            continue

        if action["type"] == "npc_flee":
            if action["success"]:
                lines.append(f"{action['actor_name']} escapes the encounter.")
            else:
                lines.append(f"{action['actor_name']} attempts to flee but fails.")

            continue

        if action["type"] == "player_defend":
            lines.append("You brace yourself against incoming attacks.")
            continue

        if action["type"] == "player_flee":
            if action["success"]:
                lines.append("You successfully flee from combat.")

            else:
                lines.append("You attempt to flee from combat, but fail.")
            continue

        if action["type"] == "player_surrender":
            lines.append("You surrender to your enemies.")
            continue

        if action["type"] == "player_attack":
            attacker = "You"
            target = action["target_name"]

        elif action["type"] == "npc_attack":
            attacker = action["attacker_name"]
            target = "you"

        else:
            continue

        result = action["attack_result"]

        if not result["hit"]:
            verb = "miss" if attacker == "You" else "misses"

            lines.append(f"{attacker} {verb} {target}.")

            continue

        damage = action["damage_applied"]

        verb = "hit" if attacker == "You" else "hits"

        lines.append(f"{attacker} {verb} {target} for {damage} damage.")

        if result["critical"]:
            lines.append("Critical hit!")

        if not action["target_alive"]:
            lines.append(
                "You fall in combat." if target == "you" else f"{target} falls."
            )

    if status == CombatStatus.VICTORY:
        lines.append("You have won the encounter.")

    elif status == CombatStatus.DEFEAT:
        lines.append("You have been defeated.")

    elif status == CombatStatus.ESCAPED:
        lines.append("You have escaped the encounter.")

    elif status == CombatStatus.SURRENDERED:
        lines.append("You have surrendered to your enemies.")

    return " ".join(lines)


def require_outside_combat(session: GameSession) -> None:
    if get_active_combat(session) is not None:
        raise ValidationError("Cannot perform this action during combat.")


def advance_combat_turn(
    encounter: CombatEncounter,
) -> CombatParticipant | None:
    if encounter.status != CombatStatus.ACTIVE:
        return None

    status = evaluate_combat_status(encounter)

    if status != CombatStatus.ACTIVE:
        end_encounter(
            encounter=encounter,
            status=status,
        )

        return None

    participants = list(
        CombatParticipant.objects.filter(encounter=encounter)
        .select_related(
            "player",
            "npc",
        )
        .order_by("position")
    )

    count = len(participants)

    for _ in range(count):
        encounter.turn_index += 1  # pyright: ignore

        if encounter.turn_index >= count:
            encounter.turn_index = 0  # pyright: ignore
            encounter.round_number += 1  # pyright: ignore

        participant = participants[encounter.turn_index]  # pyright: ignore

        if not participant.can_act:
            continue

        if participant.defending:
            participant.defending = False
            participant.save(update_fields=["defending"])

        encounter.save(
            update_fields=[
                "turn_index",
                "round_number",
            ]
        )

        return participant

    raise RuntimeError("No participants can act in this encounter.")
