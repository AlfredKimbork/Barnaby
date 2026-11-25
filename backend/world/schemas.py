from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from characters.skills import SKILL_ATTRIBUTES
from game.events import EventCondition


class Schema(BaseModel):
    model_config = ConfigDict(extra="allow")


class CampaignSchema(Schema):
    id: str
    title: str
    version: str = "1.0"
    description: str = ""
    starting_state: dict[str, Any]
    dm_secret: dict[str, Any] = Field(default_factory=dict)


class InteractableSchema(Schema):
    id: str
    description: str

    locked: bool = False
    unlock_dc: int = Field(default=14, ge=5, le=30)

    discovery: str | None = None
    investigation_dc: int = Field(default=10, ge=5, le=30)

    contents: list[str] = Field(default_factory=list)


class ChallengeOutcomeShema(Schema):
    description: str

    flags: dict[str, bool | int | str] = Field(default_factory=dict)

    damage: int = Field(default=0, ge=0)


class ChallengeApproachSchema(Schema):
    id: str
    description: str

    skill: str | None = None

    difficulty: int | None = Field(
        default=None,
        ge=1,
        le=30,
    )

    repeatable: bool = False

    success: ChallengeOutcomeShema
    failure: ChallengeOutcomeShema

    @model_validator(mode="after")
    def validate_skill_check(self):
        if self.skill is None:
            if self.difficulty is not None:
                raise ValueError("Difficulty requires a skill.")

            return self

        if self.skill not in SKILL_ATTRIBUTES:
            raise ValueError(f"Unknown skill: {self.skill}")

        if self.difficulty is None:
            raise ValueError("Skill checks require a difficulty.")

        if self.failure is None:
            raise ValueError("Skill checks require a failure.")

        return self


class ChallengeSchema(Schema):
    id: str
    description: str

    approaches: list[ChallengeApproachSchema] = Field(
        min_length=1,
    )

    @model_validator(mode="after")
    def validate_approaches(self):
        ids = [approach.id for approach in self.approaches]

        if len(ids) != len(set(ids)):
            raise ValidationError("Duplicate challenge approach IDs.")

        return self


class LocationSchema(Schema):
    id: str
    name: str
    type: str
    description: str = ""
    npcs: list[str] = Field(default_factory=list)
    interactables: list[InteractableSchema] = Field(default_factory=list)
    connections: list[str] = Field(default_factory=list)
    encounters: list[str] = Field(default_factory=list)

    challenges: list[ChallengeSchema] = Field(default_factory=list)


class NPCInitialStateSchema(Schema):
    hp: int = Field(gt=0)
    max_hp: int = Field(gt=0)
    defense: int = Field(gt=0)
    hostility: int = Field(ge=0)
    trust: int = 0
    alive: bool = True
    recruited: bool = False

    @model_validator(mode="after")
    def validate_hp(self):
        if self.hp > self.max_hp:
            raise ValueError("HP cannot be greater than max HP")
        return self


class SecretSchema(Schema):
    id: str
    description: str
    reveal_condition: dict[str, Any] = Field(default_factory=dict)


class NPCSchema(Schema):
    id: str
    name: str
    age: int | None = None
    species: str
    role: str
    location: str

    appearance: str
    background: str

    personality: dict[str, Any] = Field(default_factory=dict)
    goals: list[str] = Field(default_factory=list)
    public_knowledge: list[str] = Field(default_factory=list)

    secrets: list[SecretSchema]

    relationships: dict[str, str] = Field(default_factory=dict)
    initial_state: NPCInitialStateSchema

    dialogue_examples: dict[str, str] = Field(default_factory=dict)


class QuestObjectiveSchema(Schema):
    id: str
    description: str

    required: bool = True
    hidden: bool = False

    prerequisites: list[str] = Field(default_factory=list)

    conditions: list[dict[str, Any]] = Field(default_factory=list)


class QuestRewardSchema(Schema):
    experience: int = Field(default=0, ge=0)
    gold: int = Field(default=0, ge=0)
    items: list[str] = Field(default_factory=list)


class QuestSchema(Schema):
    id: str
    title: str
    description: str = ""

    activation: list[EventCondition] = Field(default_factory=list)

    objectives: list[QuestObjectiveSchema] = Field(default_factory=list)

    rewards: QuestRewardSchema = Field(default_factory=QuestRewardSchema)

    @model_validator(mode="after")
    def validate_objectives(self):
        ids = [objective.id for objective in self.objectives]

        if len(ids) != len(set(ids)):
            raise ValidationError(f"Duplicate objective IDs found in Quest '{self.id}'")

        graph = {objective.id: objective.prerequisites for objective in self.objectives}

        visited = set()
        visiting = set()

        def visit(objective_id: str):
            if objective_id not in graph:
                raise ValidationError(
                    f"Objective '{objective_id}' not found in Quest '{self.id}'"
                )
            if objective_id in visiting:
                raise ValidationError(
                    f"Cycle detected in prerequisites of Quest '{self.id}'"
                )
            if objective_id in visited:
                return

            visiting.add(objective_id)

            for prerequisite in graph[objective_id]:
                visit(prerequisite)

            visiting.remove(objective_id)
            visited.add(objective_id)

        for objective_id in ids:
            visit(objective_id)

        return self


class ItemSchema(Schema):
    id: str
    name: str
    type: str
    description: str = ""
    properties: dict[str, Any] = Field(default_factory=dict)


class EnemiesSchema(Schema):
    id: str
    name: str
    hp: int
    defense: int
    attack_bonus: int
    damage_dice: str


class EncounterSchema(Schema):
    id: str
    name: str
    location: str
    enemies: list[EnemiesSchema] = Field(default_factory=list)


class EndingSchema(Schema):
    id: str
    title: str
    requirements: dict[str, Any] = Field(default_factory=dict)
    consequences: dict[str, Any] = Field(default_factory=dict)


class CampaignBundle(BaseModel):
    campaign: CampaignSchema
    locations: list[LocationSchema]
    npcs: list[NPCSchema]
    quests: list[QuestSchema]
    items: list[ItemSchema]
    encounters: list[EncounterSchema]
    endings: list[EndingSchema]

    @model_validator(mode="after")
    def validate_references(self):
        locations = {location.id for location in self.locations}
        npcs = {npc.id for npc in self.npcs}
        encounters = {encounter.id for encounter in self.encounters}

        for group in (
            self.locations,
            self.npcs,
            self.quests,
            self.items,
            self.encounters,
            self.endings,
        ):
            ids = [object.id for object in group]
            if len(ids) != len(set(ids)):
                raise ValueError(f"Duplicate IDs found in {group.__class__.__name__}")

        start = self.campaign.starting_state["location"]
        if start not in locations:
            raise ValueError(f"Starting location '{start}' not found in locations")

        items = {item.id for item in self.items}

        for location in self.locations:
            for target in location.connections:
                if target not in locations:
                    raise ValueError(
                        f"Location '{location.id}' has a connection to non-existent location '{target}'"
                    )
            for npc_id in location.npcs:
                if npc_id not in npcs:
                    raise ValueError(
                        f"Location '{location.id}' has a reference to non-existent NPC '{npc_id}'"
                    )
            for encounter_id in location.encounters:
                if encounter_id not in encounters:
                    raise ValueError(
                        f"Location '{location.id}' has a reference to non-existent Encounter '{encounter_id}'"
                    )
            for npc in self.npcs:
                if npc.location not in locations:
                    raise ValueError(
                        f"NPC '{npc.id}' has a reference to non-existent Location '{npc.location}'"
                    )
            for encounter in self.encounters:
                if encounter.location not in locations:
                    raise ValueError(
                        f"Encounter '{encounter.id}' has a reference to non-existent Location '{encounter.location}'"
                    )

            object_id = [obj.id for obj in location.interactables]

            if len(object_id) != len(set(object_id)):
                raise ValueError(
                    f"Duplicate Interactable IDs found in Location '{location.id}'"
                )

            for obj in location.interactables:
                for item_id in obj.contents:
                    if item_id not in items:
                        raise ValueError(
                            f"Interactable '{obj.id}' in Location '{location.id}' has a reference to non-existent Item '{item_id}'"
                        )
        return self
