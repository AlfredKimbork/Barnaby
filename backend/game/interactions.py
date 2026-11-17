from dataclasses import dataclass
from random import randint

from game.models import NPCState
from game.relationships import apply_relationship_event


@dataclass(frozen=True)
class InteractionResult:
    intent: str
    success: bool
    roll: int | None
    difficulty: int | None
    relationship_event: str | None


INTERACTION_RULES = {
    "persuade": {"difficulty": 12},
    "intimidate": {"difficulty": 14},
    "decieve": {"difficulty": 15},
    "compliment": {"difficulty": None},
    "normal": {"difficulty": None},
}


def resolve_interaction(
    *,
    npc_state: NPCState,
    intent: str,
    modifier: int = 0,
) -> InteractionResult:
    if intent not in INTERACTION_RULES:
        raise ValueError(f"Invalid intent: {intent}")

    difficulty = INTERACTION_RULES[intent]["difficulty"]

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
