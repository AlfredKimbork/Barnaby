from dataclasses import dataclass

from pydantic import ValidationError

from game.models import PlayerState

EXPERIENCE_THRESHOLD = {
    1: 0,
    2: 300,
    3: 900,
    4: 2700,
    5: 6500,
    6: 14000,
    7: 23000,
    8: 34000,
    9: 48000,
    10: 64000,
    11: 85000,
    12: 100000,
    13: 120000,
    14: 140000,
    15: 165000,
    16: 195000,
    17: 225000,
    18: 265000,
    19: 305000,
    20: 355000,
}

MAX_LEVEL = max(EXPERIENCE_THRESHOLD)

CLASS_HP_GROWTH = {
    "barbarian": 7,
    "fighter": 6,
    "paladin": 6,
    "ranger": 6,
    "rogue": 5,
    "bard": 5,
    "cleric": 5,
    "druid": 5,
    "monk": 5,
    "warlock": 5,
    "wizard": 4,
    "sorcerer": 4,
}


@dataclass(frozen=True)
class ExperienceResult:
    gained: int
    previous_experience: int
    experience: int
    previous_level: int
    level: int
    levels_gained: int
    hp_gained: int


def get_level(experience: int) -> int:
    if experience < 0:
        raise ValueError("Experience cannot be negative")

    return max(
        level
        for level, threshold in EXPERIENCE_THRESHOLD.items()
        if experience >= threshold
    )


def experience_to_next_level(player: PlayerState) -> int | None:
    if player.level >= MAX_LEVEL:
        return None
    return max(0, EXPERIENCE_THRESHOLD[player.level + 1] - player.experience)  # pyright: ignore


def grant_experience(
    player: PlayerState,
    amount: int,
) -> ExperienceResult:
    if amount < 0:
        raise ValidationError("Experience amount cannot be negative")

    previous_level = player.level
    previous_experience = player.experience

    player.experience += amount  # pyright: ignore
    player.level = get_level(player.experience)  # pyright: ignore

    levels_gained = player.level - previous_level

    hp_gained = 0

    if levels_gained > 0:
        hp_gained = calculate_hp_growth(
            player=player,
            levels_gained=levels_gained,
        )

        player.max_hp += hp_gained  # pyright: ignore

        if player.hp > 0:
            player.hp += hp_gained  # pyright:ignore

    player.save(
        update_fields=[
            "experience",
            "level",
            "max_hp",
            "hp",
        ]
    )

    return ExperienceResult(
        gained=amount,
        previous_experience=previous_experience,  # pyright: ignore
        experience=player.experience,
        previous_level=previous_level,
        level=player.level,
        levels_gained=levels_gained,
        hp_gained=hp_gained,
    )


def calculate_hp_growth(
    player: PlayerState,
    levels_gained: int,
) -> int:
    character_class = player.session.character.character_class.lower()  # pyright:ignore

    base = CLASS_HP_GROWTH.get(character_class, 5)

    constitution = player.attribute_modifier(
        "constitution",
    )

    return max(1, base + constitution) * levels_gained
