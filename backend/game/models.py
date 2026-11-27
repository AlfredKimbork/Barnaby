from uuid import uuid4

from django.conf import settings
from django.db import models

from characters.mechanics import CharacterMechanicsMixin
from characters.models import Character
from world.models import NPC, Campaign, Location, Quest


class GameSession(models.Model):
    objects = models.Manager()
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="game_sessions",
        null=True,
        blank=True,
    )
    campaign = models.ForeignKey(
        Campaign,
        on_delete=models.PROTECT,
    )
    character = models.ForeignKey(
        Character,
        on_delete=models.PROTECT,
        related_name="game_sessions",
    )
    current_location = models.ForeignKey(
        Location,
        on_delete=models.PROTECT,
    )
    flags = models.JSONField(default=dict)
    status = models.CharField(max_length=20, default="active")
    turn_number = models.PositiveIntegerField(default=0)  # pyright: ignore
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class GameTurn(models.Model):
    objects = models.Manager()
    session = models.ForeignKey(
        GameSession,
        on_delete=models.CASCADE,
        related_name="turns",
    )
    number = models.PositiveIntegerField()  # pyright: ignore
    action_id = models.UUIDField(default=uuid4, null=True, blank=True)

    player_action = models.TextField()
    narration = models.TextField(blank=True)
    mechanical_result = models.JSONField(default=dict)

    original_message = models.TextField(
        blank=True,
        default="",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("number",)
        constraints = (
            models.UniqueConstraint(
                fields=["session", "number"],
                name="unique_session_turn_number",
            ),
            models.UniqueConstraint(
                fields=["session", "action_id"],
                name="unique_session_turn_action_id",
            ),
        )


class PlayerState(
    CharacterMechanicsMixin,
    models.Model,
):
    objects = models.Manager()
    session = models.OneToOneField(
        GameSession,
        on_delete=models.CASCADE,
        related_name="player_state",
    )

    hp = models.PositiveIntegerField(
        default=10,  # pyright: ignore
    )
    max_hp = models.PositiveIntegerField(
        default=10,  # pyright: ignore
    )
    gold = models.PositiveIntegerField(
        default=0,  # pyright: ignore
    )
    inventory = models.JSONField(
        default=list,
        blank=True,
    )

    level = models.PositiveIntegerField(
        default=1,  # pyright: ignore
    )
    experience = models.PositiveIntegerField(
        default=0,  # pyright: ignore
    )

    strength = models.PositiveSmallIntegerField(
        default=10,  # pyright: ignore
    )
    dexterity = models.PositiveSmallIntegerField(
        default=10,  # pyright: ignore
    )
    constitution = models.PositiveSmallIntegerField(
        default=10,  # pyright: ignore
    )
    intelligence = models.PositiveSmallIntegerField(
        default=10.0,  # pyright: ignore
    )
    wisdom = models.PositiveSmallIntegerField(
        default=10,  # pyright: ignore
    )
    charisma = models.PositiveSmallIntegerField(
        default=10,  # pyright: ignore
    )

    proficient_skills = models.JSONField(
        default=list,  # pyright: ignore
        blank=True,
    )

    def take_damage(self, amount: int) -> None:
        self.hp = max(self.hp - amount, 0)  # pyright: ignore
        self.save(update_fields=["hp"])

    def heal(self, amount: int) -> None:
        self.hp = min(self.hp + amount, self.max_hp)  # pyright: ignore
        self.save(update_fields=["hp"])

    def __str__(self):
        return f"Player state for session {self.session_id}"  # pyright: ignore


class NPCState(models.Model):
    objects = models.Manager()
    session = models.ForeignKey(
        GameSession,
        on_delete=models.CASCADE,
        related_name="npc_states",
    )
    npc = models.ForeignKey(
        NPC,
        on_delete=models.PROTECT,
    )
    current_location = models.ForeignKey(
        Location,
        on_delete=models.PROTECT,
    )
    hp = models.IntegerField()
    max_hp = models.IntegerField()
    alive = models.BooleanField(default=True)  # pyright: ignore
    trust = models.IntegerField(default=0)  # pyright: ignore
    hostility = models.IntegerField(default=0)  # pyright: ignore

    state = models.JSONField(default=dict)

    class Meta:
        constraints = (
            models.UniqueConstraint(
                fields=["session", "npc"],
                name="unique_session_npc",
            ),
        )


class NPCMemory(models.Model):
    objects = models.Manager()
    npc_state = models.ForeignKey(
        NPCState,
        on_delete=models.CASCADE,
        related_name="memories",
    )

    turn = models.ForeignKey(
        GameTurn,
        on_delete=models.CASCADE,
        related_name="npc_memories",
    )
    summary = models.TextField()

    importance = models.IntegerField(default=1)  # pyright: ignore

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)


class QuestStatus(models.TextChoices):
    HIDDEN = "hidden", "Hidden"  #   pyright: ignore
    ACTIVE = "active", "Active"  #   pyright: ignore
    COMPLETED = "completed", "Completed"  #   pyright: ignore
    FAILED = "failed", "Failed"  #   pyright: ignore


class QuestState(models.Model):
    objects = models.Manager()
    session = models.ForeignKey(
        GameSession,
        on_delete=models.CASCADE,
        related_name="quest_states",
    )
    quest = models.ForeignKey(
        Quest,
        on_delete=models.PROTECT,
    )
    status = models.CharField(
        max_length=20,
        choices=QuestStatus.choices,
        default=QuestStatus.HIDDEN,
    )
    completed_objectives = models.JSONField(
        default=list,
    )
    matched_events = models.JSONField(
        default=list,
        blank=True,
    )

    rewards_granted = models.BooleanField(default=False)  # pyright: ignore

    outcome = models.SlugField(
        max_length=150,
        blank=True,
        default="",
    )

    resolved_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    class Meta:
        constraints = (
            models.UniqueConstraint(
                fields=["session", "quest"],
                name="unique_session_quest",
            ),
        )


class InteractableState(models.Model):
    objects = models.Manager()

    session = models.ForeignKey(
        GameSession,
        on_delete=models.CASCADE,
        related_name="interactable_states",
    )

    location = models.ForeignKey(
        Location,
        on_delete=models.PROTECT,
    )

    slug = models.CharField(max_length=100)

    locked = models.BooleanField(default=False)  # pyright: ignore
    discovered = models.BooleanField(default=False)  # pyright: ignore

    contents = models.JSONField(default=list, blank=True)
    state = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = (
            models.UniqueConstraint(
                fields=["session", "location", "slug"],
                name="unique_session_location_interactable",
            ),
        )

    def __str__(self):
        return f"{self.location.slug}/{self.slug}"  # pyright: ignore


class KnowledgeDiscovery(models.Model):
    objects = models.Manager()

    session = models.ForeignKey(
        GameSession,
        on_delete=models.CASCADE,
        related_name="knowledge_discovery",
    )

    knowledge_id = models.SlugField(max_length=150)

    text = models.TextField()

    source = models.ForeignKey(
        NPCState,
        on_delete=models.PROTECT,
        related_name="knowledge_disclosure",
    )

    turn = models.ForeignKey(
        GameTurn,
        on_delete=models.CASCADE,
        related_name="knowledge_discoveries",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = (
            models.UniqueConstraint(
                fields=(
                    "session",
                    "knowledge_id",
                ),
                name="unique_session_knowledge",
            ),
        )

    def __str__(self):  # pyright:ignore
        return self.knowledge_id


class ChallengeState(models.Model):
    objects = models.Manager()

    session = models.ForeignKey(
        GameSession,
        on_delete=models.CASCADE,
        related_name="challenge_states",
    )

    location = models.ForeignKey(
        Location,
        on_delete=models.PROTECT,
    )

    challenge_slug = models.SlugField(max_length=150)

    approach_slug = models.SlugField(max_length=150)

    attempts = models.PositiveIntegerField(default=0)  # pyright:ignore

    successes = models.PositiveIntegerField(default=0)  # pyright:ignore
    failures = models.PositiveIntegerField(default=0)  # pyright:ignore

    class Meta:
        constraints = (
            models.UniqueConstraint(
                fields=(
                    "session",
                    "location",
                    "challenge_slug",
                    "approach_slug",
                ),
                name="unique_session_challange_approach",
            ),
        )

    @property
    def completed(self) -> bool:
        return self.successes > 0


class CombatStatus(models.TextChoices):
    ACTIVE = "active", "Active"  # pyright: ignore
    VICTORY = "victory", "Victory"  # pyright: ignore
    DEFEAT = "defeat", "Defeat"  # pyright: ignore
    ESCAPED = "escape", "Escape"  # pyright: ignore
    SURRENDERED = "surrender", "Surrender"  # pyright: ignore


class CombatTeam(models.TextChoices):
    PLAYER = "player", "Player"  # pyright: ignore
    ALLY = "ally", "Ally"  # pyright: ignore
    ENEMY = "enemy", "Enemy"  # pyright: ignore
    NEUTRAL = "neutral", "Neutral"  # pyright: ignore


class CombatEncounter(models.Model):
    objects = models.Manager()

    session = models.ForeignKey(
        GameSession,
        on_delete=models.CASCADE,
        related_name="combat_encounters",
    )

    location = models.ForeignKey(
        Location,
        on_delete=models.PROTECT,
    )

    status = models.CharField(
        max_length=20,
        choices=CombatStatus.choices,
        default=CombatStatus.ACTIVE,
    )

    round_number = models.PositiveIntegerField(default=1)  # pyright: ignore
    turn_index = models.PositiveIntegerField(default=0)  # pyright: ignore

    created_at = models.DateTimeField(auto_now_add=True)

    ended_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    class Meta:
        constraints = (
            models.UniqueConstraint(
                fields=["session"],
                name="unique_active_combat_per_session",
            ),
        )


class CombatParticipant(models.Model):
    objects = models.Manager()

    encounter = models.ForeignKey(
        CombatEncounter,
        on_delete=models.CASCADE,
        related_name="participants",
    )

    player = models.ForeignKey(
        PlayerState,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )

    npc = models.ForeignKey(
        NPCState,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )

    team = models.CharField(
        max_length=10,
        choices=CombatTeam.choices,
    )

    initiative = models.IntegerField()  # pyright: ignore
    initiative_modifier = models.IntegerField(default=0)  # pyright: ignore

    position = models.PositiveIntegerField()

    defending = models.BooleanField(default=False)  # pyright: ignore
    withdrawn = models.BooleanField(default=False)  # pyright: ignore

    state = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("position",)
        constraints = (
            models.CheckConstraint(
                condition=(
                    models.Q(
                        player__isnull=False,
                        npc__isnull=True,
                        team="player",
                    )
                    | models.Q(
                        player__isnull=True,
                        npc__isnull=False,
                        team__in=["enemy", "ally"],
                    )
                ),
                name="valid_combat_participant_actor",
            ),
            models.UniqueConstraint(
                fields=["encounter", "player"],
                name="unique_combat_player",
            ),
            models.UniqueConstraint(
                fields=["encounter", "npc"],
                name="unique_combat_npc",
            ),
            models.UniqueConstraint(
                fields=["encounter", "position"],
                name="unique_combat_position",
            ),
        )

    @property
    def is_player(self) -> bool:
        return self.player is not None

    @property
    def alive(self) -> bool:
        if self.player is not None:
            return self.player.hp > 0  # pyright: ignore

        if self.npc is not None:
            return self.npc.alive and self.npc.hp > 0  # pyright: ignore

        return False

    @property
    def can_act(self) -> bool:
        return self.alive and not self.withdrawn  #  pyright: ignore
