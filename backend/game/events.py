from typing import Literal

from pydantic import BaseModel, ConfigDict

EventType = Literal[
    "object_discovered",
    "object_unlocked",
    "item_collected",
    "location_entered",
    "npc_talked_to",
    "npc_killed",
    "knowledge_discovered",
    "flag_changed",
    "challange_succeeded",
    "challenge_failed",
]


class GameEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: EventType

    target: str | None = None
    approach: str | None = None
    location: str | None = None
    item: str | None = None
    npc: str | None = None
    knowledge: str | None = None
    flag: str | None = None
    value: str | bool | int | None = None


class EventCondition(GameEvent):
    def matched(self, event: GameEvent) -> bool:
        expected = self.model_dump(exclude_unset=True)
        actual = event.model_dump()

        return all(actual.get(key) == value for key, value in expected.items())
