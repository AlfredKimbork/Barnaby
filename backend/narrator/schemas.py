from typing import Annotated, Literal

from pydantic import BaseModel, Field, TypeAdapter, model_validator

ObjectOperation = Literal[
    "investigate",
    "unlock",
    "force_open",
    "take",
]

Interaction = Literal[
    "persuade",
    "intimidate",
    "deceive",
    "compliment",
    "normal",
]


class AttackAction(BaseModel):
    type: Literal["attack"]
    target_slug: str
    weapon_slug: str


class MoveAction(BaseModel):
    type: Literal["move"]
    destination_slug: str


class InspectAction(BaseModel):
    type: Literal["inspect"]


class TalkAction(BaseModel):
    type: Literal["talk"]
    target_slug: str
    message: str
    interaction_intent: Interaction = "normal"


class InteractionAction(BaseModel):
    type: Literal["interact"]
    target_slug: str
    operation: ObjectOperation
    item_slug: str | None = None


GameAction = Annotated[
    AttackAction | MoveAction | InspectAction | TalkAction | InteractionAction,
    Field(discriminator="type"),
]


class AIActionResponse(BaseModel):
    type: Literal[
        "attack",
        "move",
        "inspect",
        "talk",
        "interact",
    ]
    target_slug: str | None = None
    weapon_slug: str | None = None
    destination_slug: str | None = None
    message: str | None = None
    interaction_intent: Interaction = "normal"
    operation: ObjectOperation | None = None
    item_slug: str | None = None

    @model_validator(mode="after")
    def valide_action(self):
        required = {
            "attack": ["target_slug", "weapon_slug"],
            "move": ["destination_slug"],
            "inspect": (),
            "talk": ["target_slug", "message"],
            "interact": ["target_slug", "operation"],
        }

        for field in required[self.type]:
            if not getattr(self, field):
                raise ValueError(f"{field} is required for action type {self.type}")

        if self.type == "interact" and self.operation == "take" and not self.item_slug:
            raise ValueError(
                "item_slug is required for 'take' operation in 'interact' action"
            )

        return self

    def to_game_action(self):
        return TypeAdapter(GameAction).validate_python(
            self.model_dump(exclude_none=True)
        )
