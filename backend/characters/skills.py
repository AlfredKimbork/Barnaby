from typing import Literal

Attributes = Literal[
    "strength",
    "dexterity",
    "constitution",
    "intelligence",
    "wisdom",
    "charisma",
]

SKILL_ATTRIBUTES: dict[str, Attributes] = {
    # Strength
    "athletics": "strength",
    # Dexterity
    "acrobatics": "dexterity",
    "sleight_of_hand": "dexterity",
    "stealth": "dexterity",
    # Intelligence
    "arcana": "intelligence",
    "history": "intelligence",
    "investigation": "intelligence",
    "nature": "intelligence",
    "religion": "intelligence",
    # Wisdom
    "animal_handling": "wisdom",
    "insight": "wisdom",
    "medicine": "wisdom",
    "perception": "wisdom",
    "survival": "wisdom",
    # Charisma
    "deception": "charisma",
    "performance": "charisma",
    "persuasion": "charisma",
    "intimidation": "charisma",
}
