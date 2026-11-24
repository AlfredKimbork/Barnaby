from dataclasses import dataclass
from random import randint
from typing import Literal

from characters.models import Character

RollMode = Literal[
    "normal",
    "advantage",
    "disadvantage",
]


@dataclass(frozen=True)
class CheckResult:
    skill: str
    roll: int
    modifier: int
    total: int
    difficulty: int
    success: bool
    rolls: tuple[int, ...]
    roll_mode: RollMode


def resolve_roll_mode(
    *,
    advantage: bool = False,
    disadvantage: bool = False,
) -> RollMode:
    if advantage == disadvantage:
        return "normal"

    return "advantage" if advantage else "disadvantage"


def roll_d20(
    mode: RollMode = "normal",
) -> tuple[int, tuple[int, ...]]:
    if mode == "normal":
        roll = randint(1, 20)
        return roll, (roll,)

    if mode not in {"advantage", "disadvantage"}:
        raise ValueError(f"Invalid roll mode: {mode}")

    rolls = (randint(1, 20), randint(1, 20))

    selected_roll = max(rolls) if mode == "advantage" else min(rolls)

    return selected_roll, rolls


def resolve_skill_check(
    *,
    character: Character,
    skill: str,
    difficulty: int,
    attribute: str | None = None,
    bonus: int = 0,
    advantage: bool = False,
    disadvantage: bool = False,
) -> CheckResult:
    modifier = (
        character.skill_modifier(
            skill=skill,
            attribute=attribute,
        )
        + bonus
    )

    mode = resolve_roll_mode(
        advantage=advantage,
        disadvantage=disadvantage,
    )

    roll, rolls = roll_d20(mode=mode)
    total = roll + modifier

    return CheckResult(
        skill=skill,
        roll=roll,
        modifier=modifier,
        total=total,
        difficulty=difficulty,
        success=total >= difficulty,
        rolls=rolls,
        roll_mode=mode,
    )
