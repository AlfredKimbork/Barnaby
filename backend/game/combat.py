from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from game.mechanics import roll_initiative
from game.models import (
    CombatEncounter,
    CombatParticipant,
    CombatStatus,
    CombatTeam,
    GameSession,
    NPCState,
    PlayerState,
)


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
        participant.team == CombatTeam.ENEMY and participant.alive
        for participant in participants
    )

    if not player_alive:
        return CombatStatus.DEFEAT

    if not enemies_remaining:
        return CombatStatus.VICTORY

    return CombatStatus.ACTIVE


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

        encounter.save(
            update_fields=[
                "turn_index",
                "round_number",
            ]
        )

        return participant

    raise RuntimeError("No participants can act in this encounter.")


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
