from pydantic import ValidationError

from characters.skills import SKILL_ATTRIBUTES


class CharacterMechanicsMixin:
    level: int
    proficient_skills: list[str]

    def attribute_modifier(self, attribute: str) -> int:
        attributes = {
            "strength",
            "dexterity",
            "constitution",
            "intelligence",
            "wisdom",
            "charisma",
        }

        if attribute not in attributes:
            raise ValidationError(
                f"Invalid attribute: {attribute}. Must be one of {attributes}"
            )

        return (getattr(self, attribute) - 10) // 2

    @property
    def proficiency_bonus(self) -> int:
        return 2 + (self.level - 1) // 4

    def is_proficient(self, skill: str) -> bool:
        return skill in self.proficient_skills

    def skill_modifier(self, skill: str, attribute: str | None = None) -> int:
        if skill not in SKILL_ATTRIBUTES:
            raise ValidationError(
                f"Invalid skill: {skill}. Must be one of {list(SKILL_ATTRIBUTES.keys())}"
            )

        attribute = attribute or SKILL_ATTRIBUTES[skill]

        modifier = self.attribute_modifier(attribute)

        if self.is_proficient(skill):
            modifier += self.proficiency_bonus

        return modifier
