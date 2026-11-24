from dataclasses import dataclass

from characters.models import Character
from game.checks import RollMode, resolve_skill_check
from game.models import NPCState
from game.relationships import apply_relationship_event


@dataclass(frozen=True)
class InteractionResult:
    intent: str
    success: bool
    roll: int | None
    modifier: int
    total: int | None
    difficulty: int | None
    relationship_event: str | None
    rolls: tuple[int, ...]
    roll_mode: RollMode


BASE_DIFFICULTIES = {
    "persuade": 12,
    "intimidate": 14,
    "deceive": 15,
    "compliment": 10,
}

INTERACTION_SKILLS = {
    "persuade": "persuasion",
    "intimidate": "intimidation",
    "deceive": "deception",
    "compliment": "persuasion",
}


def calculate_interaction_dc(
    *, npc_state: NPCState, intent: str, context_modifier: int = 0
) -> int:
    if intent not in BASE_DIFFICULTIES:
        raise ValueError(f"Invalid intent: {intent}")

    personality = npc_state.npc.data.get("personality", {})  # pyright: ignore

    resistance = personality.get("social_resistances", {})

    base = BASE_DIFFICULTIES[intent]
    personality_modifier = resistance.get(intent, 0)

    trust = npc_state.trust
    hostility = npc_state.hostility

    if intent in {"persuade", "deceive", "compliment"}:
        relationship_modifier = -int(trust // 20) + hostility // 20  # pyright: ignore
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
    character: Character,
    npc_state: NPCState,
    intent: str,
    modifier: int = 0,
    context_modifier: int = 0,
    advantage: bool = False,
    disadvantage: bool = False,
) -> InteractionResult:
    if intent not in BASE_DIFFICULTIES and intent != "normal":
        raise ValueError(f"Invalid intent: {intent}")

    difficulty = (
        calculate_interaction_dc(
            npc_state=npc_state,
            intent=intent,
            context_modifier=context_modifier,
        )
        if intent != "normal"
        else None
    )

    roll = None
    success = True
    check = None

    if difficulty is not None:
        check = resolve_skill_check(
            character=character,
            skill=INTERACTION_SKILLS[intent],
            difficulty=difficulty,
            bonus=modifier,
            advantage=advantage,
            disadvantage=disadvantage,
        )

        roll = check.roll
        success = check.success

    relationship_event = None

    match intent:
        case "intimidate":
            relationship_event = "threatened_npc"

        case "deceive":
            if not success:
                relationship_event = "lied_to_npc"

        case "compliment":
            if success:
                relationship_event = "praised_npc"

    if relationship_event:
        apply_relationship_event(
            npc_state=npc_state,  # pyright: ignore
            event=relationship_event,  # pyright: ignore
        )

    return InteractionResult(
        intent=intent,
        success=success,
        roll=roll,
        modifier=check.modifier if check else 0,
        total=check.total if check else None,
        difficulty=difficulty,
        relationship_event=relationship_event,
        rolls=check.rolls if check else (),
        roll_mode=check.roll_mode if check else "normal",
    )
