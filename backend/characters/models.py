from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from characters.skills import SKILL_ATTRIBUTES


class Character(models.Model):
    objects = models.Manager()
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="characters",
    )
    name = models.CharField(max_length=100)
    character_class = models.CharField(
        max_length=50,
        default="Fighter",
    )

    level = models.PositiveIntegerField(
        default=1,  # pyright: ignore
    )
    experience = models.PositiveIntegerField(
        default=0,  # pyright: ignore
    )

    strength = models.PositiveIntegerField(
        default=10,  # pyright: ignore
        validators=[
            MinValueValidator(1),
            MaxValueValidator(2),
        ],
    )
    dexterity = models.PositiveIntegerField(
        default=10,  # pyright: ignore
        validators=[
            MinValueValidator(1),
            MaxValueValidator(2),
        ],
    )
    constitution = models.PositiveIntegerField(
        default=10,  # pyright: ignore
        validators=[
            MinValueValidator(1),
            MaxValueValidator(2),
        ],
    )
    intelligence = models.PositiveIntegerField(
        default=10,  # pyright: ignore
        validators=[
            MinValueValidator(1),
            MaxValueValidator(2),
        ],
    )
    wisdom = models.PositiveIntegerField(
        default=10,  # pyright: ignore
        validators=[
            MinValueValidator(1),
            MaxValueValidator(2),
        ],
    )
    charisma = models.PositiveIntegerField(
        default=10,  # pyright: ignore
        validators=[
            MinValueValidator(1),
            MaxValueValidator(2),
        ],
    )

    proficient_skills = models.JSONField(
        default=list,
        blank=True,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    def __str__(self):
        return f"{self.name} (Level {self.level} {self.character_class})"

    def attribute_modifier(self, attribute: str) -> int:
        allowed = {
            "strength",
            "dexterity",
            "constitution",
            "intelligence",
            "wisdom",
            "charisma",
        }

        if attribute not in allowed:
            raise ValueError(f"Invalid attribute: {attribute}")

        score = getattr(self, attribute)
        return (score - 10) // 2

    @property
    def proficiency_bonus(self) -> int:
        return 2 + (self.level - 1) // 4  # pyright: ignore

    def is_proficient(self, skill: str) -> bool:
        if skill not in list(SKILL_ATTRIBUTES):
            raise ValueError(f"Invalid skill: {skill}")

        return skill in self.proficient_skills  # pyright: ignore

    def skill_modifier(self, skill: str, attribute: str | None = None) -> int:
        if skill not in SKILL_ATTRIBUTES:
            raise ValueError(f"Invalid skill: {skill}")

        attribute = attribute or SKILL_ATTRIBUTES[skill]

        modifier = self.attribute_modifier(attribute)

        if self.is_proficient(skill):
            modifier += self.proficiency_bonus

        return modifier
