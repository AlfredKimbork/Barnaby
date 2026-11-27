from dataclasses import dataclass
from typing import Literal

from pydantic import ValidationError

from game.models import CombatEncounter, CombatParticipant, CombatTeam

NPCAction = Literal[
    "attack",
    "defend",
    "flee",
    "surrender",
]


@dataclass(frozen=True)
class CombatDecision:
    action: NPCAction
    reason: str


BEHAVIOUR_DEFAULTS = {
    "aggressive": {
        "defend_threshold": 0.0,
        "flee_threshold": 0.0,
        "surrender_threshold": 0.0,
    },
    "balanced": {
        "defend_threshold": 0.4,
        "flee_threshold": 0.0,
        "surrender_threshold": 0.2,
    },
    "cautious": {
        "defend_threshold": 0.5,
        "flee_threshold": 0.25,
        "surrender_threshold": 0.0,
    },
    "cowardly": {
        "defend_threshold": 0.0,
        "flee_threshold": 0.6,
        "surrender_threshold": 0.2,
    },
    "protective": {
        "defend_threshold": 0.4,
        "flee_threshold": 0.0,
        "surrender_threshold": 0.0,
    },
}


def get_behavior(
    participant: CombatParticipant,
) -> dict:
    if participant.npc is None:
        raise ValidationError("Participant is not an NPC")

    combat = participant.npc.npc.data.get("combat", {})  # pyright: ignore

    configuration = combat.get("behaviour", {})

    style = configuration.get("style", "aggressive")

    behaviour = {
        **BEHAVIOUR_DEFAULTS[style],
        "style": style,
        "defend_cooldown": 2,
        "can_flee": True,
        "can_surrender": True,
    }

    behaviour.update(
        {key: value for key, value in configuration.items() if value is not None}
    )

    return behaviour


def get_health_ratio(participant: CombatParticipant) -> float:
    actor = participant.npc if participant.npc is not None else participant.player

    if actor is None:
        raise ValidationError("Participant has no actor")

    if actor.max_hp <= 0:  #  pyright: ignore
        return 0.0

    return actor.hp / actor.max_hp  # pyright: ignore


def can_defend(
    *,
    encounter: CombatEncounter,
    participant: CombatParticipant,
    cooldown: int,
) -> bool:
    last_round = participant.state.get("last_defended_round")  # pyright: ignore

    if last_round is None:
        return True

    return encounter.round_number - last_round >= cooldown


def is_outnumbered(
    *,
    encounter: CombatEncounter,
    participant: CombatParticipant,
) -> bool:
    participants = CombatParticipant.objects.filter(encounter=encounter).select_related(
        "npc", "player"
    )

    friendly_teams = (
        {CombatTeam.ENEMY}
        if participant.team == CombatTeam.ENEMY
        else {CombatTeam.PLAYER, CombatTeam.ALLY}
    )

    friendly = 0
    opposing = 0

    for other in participants:
        if not other.can_act:
            continue

        if other.team in friendly_teams:
            friendly += 1
        else:
            opposing += 1

    return opposing > friendly


def choose_npc_action(
    *,
    encounter: CombatEncounter,
    participant: CombatParticipant,
) -> CombatDecision:
    if participant.npc is None:
        raise ValidationError("Participant is not an NPC")

    if not participant.can_act:
        return CombatDecision(action="defend", reason="Cannot act")

    behaviour = get_behavior(participant)
    health = get_health_ratio(participant)

    surrender_threshold = behaviour["surrender_threshold"]

    flee_threshold = behaviour["flee_threshold"]

    defend_threshold = behaviour["defend_threshold"]

    if behaviour.get("style") in {
        "cautious",
        "cowardly",
    } and is_outnumbered(
        encounter=encounter,
        participant=participant,
    ):
        flee_threshold = min(1.0, flee_threshold + 0.15)

    if behaviour.get("can_surrender") and health <= surrender_threshold:
        return CombatDecision(
            action="surrender",
            reason="critical_health",
        )

    if behaviour.get("can_flee") and health <= flee_threshold:
        return CombatDecision(
            action="flee",
            reason="self_preservation",
        )

    if health <= defend_threshold and can_defend(
        encounter=encounter,
        participant=participant,
        cooldown=behaviour["defend_cooldown"],
    ):
        return CombatDecision(
            action="defend",
            reason="low_health",
        )

    return CombatDecision(
        action="attack",
        reason="continue_fight",
    )
