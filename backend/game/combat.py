from dataclasses import asdict

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from game.checks import roll_d20
from game.combat_ai import choose_npc_action, select_combat_target
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

    companions = list(
        NPCState.objects.select_for_update()
        .filter(
            session=session,
            current_location=session.current_location,
            state__recruited=True,
            alive=True,
            hp__gt=0,
        )
        .exclude(pk__in=enemy_ids)
        .select_related("npc")
        .order_by("pk")
    )

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

    for companion in companions:
        combat = companion.npc.data.get("combat", {})

        initiative = roll_initiative(combat.get("initiative_modifier", 0))

        participants.append(
            CombatParticipant(
                encounter=encounter,
                npc=companion,
                team=CombatTeam.ALLY,
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
    target: CombatParticipant | None = None,
) -> dict:
    if participant.npc is None:
        raise ValidationError("Combat participant is not an NPC.")

    if participant.encounter_id != encounter.pk:  # pyright: ignore
        raise ValidationError("Invalid combat participant.")

    if participant.position != encounter.turn_index:
        raise ValidationError("It is not this participant's turn.")

    if not participant.can_act:
        raise ValidationError("Participant cannot attack.")

    if target is None:
        target = select_combat_target(
            encounter=encounter,
            participant=participant,
        )

    if target is None:
        raise ValidationError("No valid combat target.")

    if target.encounter_id != encounter.pk:  # pyright: ignore
        raise ValidationError("Target belongs to another encounter.")

    opposing_teams = (
        {CombatTeam.PLAYER, CombatTeam.ALLY}
        if participant.team == CombatTeam.ENEMY
        else {CombatTeam.ENEMY}
    )

    if target.team not in opposing_teams:
        raise ValidationError("Target is not an opponent.")

    if not target.can_act:
        raise ValidationError("Target cannot be attacked.")

    npc = NPCState.objects.select_for_update().get(pk=participant.npc_id)  # pyright: ignore

    if not npc.alive or npc.hp <= 0:
        raise ValidationError("Dead NPCs cannot attack.")

    attack = select_npc_attack(npc)

    if target.is_player:
        actor = PlayerState.objects.select_for_update().get(pk=target.player_id)  # pyright: ignore

        target.player = actor

        target_slug = "player"
        target_name = "you"

    elif target.npc is not None:
        actor = NPCState.objects.select_for_update().get(pk=target.npc_id)  # pyright: ignore

        target.npc = actor

        target_slug = actor.npc.slug
        target_name = actor.npc.name

    else:
        raise ValidationError("Target has no actor.")

    if not target.can_act:
        raise ValidationError("Target is no longer available.")

    result = resolve_combat_attack(
        attack_modifier=attack.get("attack_bonus", 0),
        damage_modifier=attack.get("damage_modifier", 0),
        damage_dice=attack["damage_dice"],
        defense=get_combat_defense(target),
    )

    previous_hp = actor.hp

    if result.hit:
        if isinstance(actor, PlayerState):
            actor.take_damage(result.damage)

        else:
            actor.hp = max(
                0,
                actor.hp - result.damage,
            )

            actor.alive = actor.hp > 0

            actor.save(update_fields=["hp", "alive"])

    actor.refresh_from_db()

    return {
        "type": "npc_attack",
        "round": encounter.round_number,
        "attacker": npc.npc.slug,
        "attacker_name": npc.npc.name,
        "attacker_team": participant.team,
        "target": target_slug,
        "target_name": target_name,
        "target_team": target.team,
        "weapon": attack["id"],
        "weapon_name": attack["name"],
        "attack_result": asdict(result),
        "damage_applied": previous_hp - actor.hp,
        "target_remaining_hp": actor.hp,
        "target_alive": actor.hp > 0,
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
        "actor": participant.npc.npc.slug,  # pyright: ignore
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


def get_reinforcement_config(
    npc: NPCState,
) -> dict:
    return npc.npc.data.get("combat", {}).get("reinforcement", {})  # pyright: ignore


def process_reinforcements(
    *,
    encounter: CombatEncounter,
) -> list[dict]:
    if encounter.status != CombatStatus.ACTIVE:
        return []

    participants = list(
        CombatParticipant.objects.filter(encounter=encounter).select_related(
            "npc", "npc__npc"
        )
    )

    existing_ids = {
        participant.npc_id
        for participant in participants
        if participant.npc_id is not None
    }

    enemy_factions = set()

    for participant in participants:
        if participant.team != CombatTeam.ENEMY:
            continue

        if not participant.can_act:
            continue

        if participant.npc is None:
            continue

        faction = get_reinforcement_config(participant.npc).get("faction")

        if faction:
            enemy_factions.add(faction)

    if not enemy_factions:
        return []

    candidates = (
        NPCState.objects.select_for_update()
        .filter(
            session=encounter.session,
            current_location=encounter.location,
            alive=True,
            hp__gt=0,
        )
        .exclude(pk__in=existing_ids)
        .select_related("npc")
        .order_by("pk")
    )

    actions: list[dict] = []

    for npc in candidates:
        if npc.state.get("recruited", False):
            continue

        config = get_reinforcement_config(npc)

        if not config.get("assists_faction", False):
            continue

        if config.get("faction") not in enemy_factions:
            continue

        arrival_round = 1 + config.get("arrival_delay", 1)

        if encounter.round_number < arrival_round:
            continue

        participant = join_combat(
            encounter=encounter,  # pyright: ignore
            npc=npc,  # pyright: ignore
            team=CombatTeam.ENEMY,  # pyright: ignore
        )

        actions.append(
            {
                "type": "reinforcement_joined",
                "round": encounter.round_number,
                "actor": npc.npc.slug,
                "actor_name": npc.npc.name,
                "actor_team": participant.team,  # pyright: ignore
                "eligible_round": (participant.state["eligible_round"]),  # pyright: ignore
            }
        )

    return actions


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
        "actor_team": participant.team,
        "decision_reason": decision.reason,
    }


def process_npc_turns(
    *,
    encounter: CombatEncounter,
) -> list[dict]:
    actions: list[dict] = []

    while encounter.status == CombatStatus.ACTIVE:
        status = evaluate_combat_status(encounter)

        if status != CombatStatus.ACTIVE:
            end_encounter(
                encounter=encounter,
                status=status,
            )
            break

        actions.extend(
            process_reinforcements(
                encounter=encounter,
            )
        )

        participant = get_current_participant(encounter)

        if participant is None:
            break

        if not participant.can_act:
            advance_combat_turn(encounter)
            continue

        if participant.is_player:
            break

        actions.append(
            resolve_npc_turn(
                encounter=encounter,
                participant=participant,
            )
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
            event_type = (
                "companion_surrendered"
                if action.get("actor_team") == CombatTeam.ALLY
                else "npc_surrendered"
            )

            events.append(
                GameEvent(
                    type=event_type,
                    encounter=encounter.pk,
                    target=action["actor"],
                    npc=action["actor"],
                    location=encounter.location.slug,  # pyright: ignore
                )
            )

        elif action_type == "npc_flee" and action["success"]:
            event_type = (
                "companion_fled"
                if action.get("actor_team") == CombatTeam.ALLY
                else "npc_fled"
            )

            events.append(
                GameEvent(
                    type=event_type,
                    encounter=encounter.pk,
                    target=action["actor"],
                    npc=action["actor"],
                    location=encounter.location.slug,  # pyright: ignore
                    value=action["destination"],
                )
            )

        elif (
            action_type == "npc_attack"
            and not action["target_alive"]
            and action["target"] != "player"
        ):
            events.append(
                GameEvent(
                    type="npc_killed",
                    encounter=encounter.pk,
                    target=action["target"],
                    npc=action["target"],
                    location=encounter.location.slug,  # pyright: ignore
                )
            )

            if action["target_team"] == CombatTeam.ALLY:
                events.append(
                    GameEvent(
                        type="companion_killed",
                        encounter=encounter.pk,
                        target=action["target"],
                        npc=action["target"],
                        location=encounter.location.slug,  # pyright: ignore
                    )
                )

        elif action_type == "reinforcement_joined":
            events.append(
                GameEvent(
                    type="combat_reinforcement_joined",
                    encounter=encounter.pk,
                    target=action["actor"],
                    npc=action["actor"],
                    location=encounter.location.slug,  # pyright: ignore
                    value=action["actor_team"],
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
        if action["type"] == "reinforcement_joined":
            lines.append(f"{action['actor_name']} joins the combat as a reinforcement.")
            continue

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

                if action.get("escaped_companions"):
                    lines.append("Your companions escape the encounter with you.")

            else:
                lines.append("You attempt to flee from combat, but fail.")

            continue

        if action["type"] == "player_surrender":
            if not action["success"]:
                lines.append("Your opponents refuse your surrender.")
                continue

            opponent = action["opponent_name"]

            lines.append(f"{opponent} accepts your surrender.")

            if action["gold_lost"]:
                lines.append(f"You lose {action['gold_lost']} gold.")

            for item in action["items_lost"]:
                lines.append(f"Your {item.replace('_', ' ')} is confiscated.")

            if action["destination"]:
                lines.append("You are captured and taken away.")

            if action["surrendered_companions"]:
                lines.append("Your active companions cease fighting.")

            continue

        if action["type"] == "player_attack":
            attacker = "You"
            target = action["target_name"]

        elif action["type"] == "npc_attack":
            attacker = action["attacker_name"]
            target = action["target_name"]

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

        eligible_round = participant.state.get("eligible_round", 1)

        if encounter.round_number < eligible_round:
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


def retreat_companions(
    *,
    encounter: CombatEncounter,
    destination: Location,
) -> list[str]:
    participants = (
        CombatParticipant.objects.filter(
            encounter=encounter,
            team=CombatTeam.ALLY,
            withdrawn=False,
        )
        .select_related("npc", "npc__npc")
        .order_by("position")
    )

    escaped: list[str] = []

    for participant in participants:
        if not participant.can_act:
            continue

        if participant.npc is None:
            continue

        npc = participant.npc

        if npc.current_location_id != encounter.location_id:  #  pyright: ignore
            continue

        npc.current_location = destination

        npc.save(update_fields=["current_location"])

        participant.withdrawn = True
        participant.defending = False

        participant.state = {
            **participant.state,
            "withdrawal_reason": "party_escaped",
        }

        participant.save(
            update_fields=[
                "withdrawn",
                "defending",
                "state",
            ]
        )

        escaped.append(npc.npc.slug)

    return escaped


@transaction.atomic
def join_combat(
    *,
    encounter: CombatEncounter,
    npc: NPCState,
    team: CombatTeam,
) -> CombatParticipant:
    encounter = CombatEncounter.objects.select_for_update().get(pk=encounter.pk)

    if encounter.status != CombatStatus.ACTIVE:
        raise ValidationError("Cannot join a combat encounter that is not active.")

    if team not in {
        CombatTeam.ALLY,
        CombatTeam.ENEMY,
    }:
        raise ValidationError("Invalid team for joining combat.")

    npc = NPCState.objects.select_for_update().get(pk=npc.pk)

    if npc.session_id != encounter.session_id:  # pyright: ignore
        raise ValidationError(
            "NPC does not belong to the same session as the encounter."
        )

    if npc.current_location_id != encounter.location_id:  # pyright: ignore
        raise ValidationError("NPC is not in the same location as the encounter.")

    if not npc.alive or npc.hp <= 0:
        raise ValidationError("Dead NPCs cannot join combat.")

    if CombatParticipant.objects.filter(
        encounter=encounter,
        npc=npc,
    ).exists():
        raise ValidationError("NPC is already participating in this encounter.")

    last_position = CombatParticipant.objects.filter(encounter=encounter).aggregate(
        last=Max("position")
    )["last"]

    position = 0 if last_position is None else last_position + 1

    combat = npc.npc.data.get("combat", {})  # pyright: ignore

    initiative = roll_initiative(combat.get("initiative_modifier", 0))

    return CombatParticipant.objects.create(
        encounter=encounter,
        npc=npc,
        team=team,
        initiative=initiative.total,
        initiative_modifier=initiative.modifier,
        position=position,
        state={
            "joined_combat_round": encounter.round_number,
            "eligible_round": encounter.round_number + 1,  # pyright: ignore
        },
    )
