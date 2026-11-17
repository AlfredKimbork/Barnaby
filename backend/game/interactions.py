from dataclasses import dataclass
from random import randint
from typing import Literal

from game.models import NPCState
from game.relationships import apply_relationship_event


@dataclass(frozen=True)
class InteractionResult:
    intent: str
    success: bool
    roll: int | None
    difficulty: int | None
    relationship_event: str | None


BASE_DIFFICULTIES = {
    "persuade": 12,
    "intimidate": 14,
    "decieve": 15,
    "compliment": 10,
}


def calculate_interaction_dc(
    *, npc_state: NPCState, intent: str, context_modifier: int = 0
) -> int:
    if intent not in BASE_DIFFICULTIES:
        raise ValueError(f"Invalid intent: {intent}")

    personality = npc_state.npc.data.get("personality", {})  # pyright: ignore

    resistance = personality.get("social_resistance", {})

    base = BASE_DIFFICULTIES[intent]
    personality_modifier = resistance.get(intent, 0)

    trust = npc_state.trust
    hostility = npc_state.hostility

    if intent in {"persuade", "deceive", "compliment"}:
        relationship_modifier = -(trust // 20) + (hostility // 20)  # pyright: ignore
    else:
        relationship_modifier = hostility // 20  # pyright: ignore

    return max(
        5,
        min(
            30,
            base + personality_modifier + relationship_modifier + context_modifier,
        ),
    )


def resolve_interaction(
    *,
    npc_state: NPCState,
    intent: str,
    modifier: int = 0,
) -> InteractionResult:
    if intent not in BASE_DIFFICULTIES and intent != "normal":
        raise ValueError(f"Invalid intent: {intent}")

    difficulty = (
        calculate_interaction_dc(npc_state=npc_state, intent=intent)
        if intent != "normal"
        else None
    )

    roll = None
    success = True

    if difficulty is not None:
        roll = randint(1, 20) + modifier
        success = roll >= difficulty

    relationship_event = None

    match intent:
        case "persuade":
            relationship_event = "helped_npc" if success else "ignored_npc"
        case "intimidate":
            relationship_event = "threatened_npc" if success else "mocked_npc"
        case "decieve":
            relationship_event = "lied_to_npc" if success else "praised_npc"
        case "compliment":
            relationship_event = "praised_npc" if success else "mocked_npc"

    if relationship_event:
        apply_relationship_event(
            npc_state=npc_state,  # pyright: ignore
            event=relationship_event,  # pyright: ignore
        )

    return InteractionResult(
        intent=intent,
        success=success,
        roll=roll,
        difficulty=difficulty,
        relationship_event=relationship_event,
    )
