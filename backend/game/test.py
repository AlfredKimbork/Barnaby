from functools import cached_property
from unittest.mock import Mock, patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from characters.models import Character
from game.checks import resolve_roll_mode, resolve_skill_check, roll_d20
from game.events import EventCondition, GameEvent
from game.interactions import calculate_interaction_dc
from game.knowledge import (
    get_permitted_knowledge,
    record_knowledge_disclosures,
)
from game.mechanics import resolve_attack
from game.models import GameTurn, KnowledgeDiscovery, NPCState, PlayerState, QuestState
from game.progression import get_level
from game.relationships import apply_relationship_event
from game.rewards import grant_experience, grant_quest_rewards
from game.turns import create_turn
from narrator.providers import GeminiAIProvider
from world.models import NPC, Campaign, Location, Quest

from .services import create_game_session

User = get_user_model()


@patch.dict("os.environ", {"AI_PROVIDER": "mock"})
class GameSessionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="testplayer",
            password="testpassword",
        )

        self.campaign = Campaign.objects.create(
            slug="test_campaign",
            title="Test Campaign",
            data={
                "starting_state": {
                    "location": "tavern",
                    "flags": {"quest_started": False},
                },
            },
        )

        self.quest = Quest.objects.create(
            campaign=self.campaign,
            slug="missing_villagers",
            title="The Missing Villagers",
            data={
                "description": "Investigate the missing villagers.",
                "activation": [
                    {
                        "type": "object_discovered",
                        "target": "notice_board",
                        "location": "lanterns_rest",
                    }
                ],
                "objectives": [
                    {
                        "id": "read_notice",
                        "description": "Read the notice board.",
                        "required": True,
                        "conditions": [
                            {
                                "type": "object_discovered",
                                "target": "notice_board",
                            }
                        ],
                    }
                ],
                "rewards": {
                    "experience": 300,
                    "gold": 25,
                    "items": [],
                },
            },
        )

        self.location = Location.objects.create(
            campaign=self.campaign, slug="tavern", name="The Tavern"
        )

        self.npc = NPC.objects.create(
            campaign=self.campaign,
            slug="garrick",
            name="Garrick",
            starting_location=self.location,
            data={
                "initial_state": {
                    "hp": 18,
                    "max_hp": 18,
                    "alive": True,
                    "trust": 0,
                    "hostility": 0,
                },
            },
        )

        self.quest = Quest.objects.create(
            campaign=self.campaign,
            slug="first_quest",
            title="The First Quest",
        )

        self.character = Character.objects.create(
            owner=self.user,
            name="Aric",
        )

        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

        self.session = create_game_session(
            owner=self.user,  # pyright: ignore
            character=self.character,  # pyright: ignore
            campaign=self.campaign,  # pyright: ignore
        )

    @cached_property
    def turn(self):
        return create_turn(
            session=self.session,  # pyright:ignore
            action_id=uuid4(),  # pyright:ignore
            player_action="Talk to Garrick",  # pyright:ignore
            narration="",  # pyright:ignore
            mechanical_result={"type": "talk"},  # pyright:ignore
        )

    @cached_property
    def npc_state(self):
        return NPCState.objects.get(session=self.session, npc=self.npc)

    def test_creates_complete_session(self):
        session = create_game_session(
            owner=self.user,  # pyright: ignore
            character=self.character,  # pyright: ignore
            campaign=self.campaign,  # pyright: ignore
        )

        self.assertEqual(session.current_location, self.location)  # pyright: ignore
        self.assertEqual(session.flags, {"quest_started": False})  # pyright: ignore

        player = PlayerState.objects.get(session=session)
        self.assertEqual(player.hp, 10)  # pyright: ignore
        self.assertEqual(player.max_hp, 10)  # pyright: ignore
        self.assertEqual(player.gold, 0)  # pyright: ignore
        self.assertEqual(player.inventory, [])  # pyright: ignore

        self.assertEqual(  # pyright: ignore
            NPCState.objects.filter(session=session, npc=self.npc).count(), 1
        )
        self.assertEqual(  # pyright: ignore
            QuestState.objects.filter(session=session, quest=self.quest).count(), 1
        )

    def test_session_have_independent_npc_states(self):
        first = create_game_session(
            owner=self.user,  # pyright: ignore
            character=self.character,  # pyright: ignore
            campaign=self.campaign,  # pyright: ignore
        )

        second = create_game_session(
            owner=self.user,  # pyright: ignore
            character=self.character,  # pyright: ignore
            campaign=self.campaign,  # pyright: ignore
        )

        first_npc = NPCState.objects.get(
            session=first,
            npc=self.npc,
        )

        first_npc.hp = 5
        first_npc.save(update_fields=["hp"])

        second_npc = NPCState.objects.get(
            session=second,
            npc=self.npc,
        )

        self.assertEqual(first_npc.hp, 5)  # pyright: ignore
        self.assertEqual(second_npc.hp, 18)  # pyright: ignore

    def test_cannot_use_another_users_character(self):
        other_user = User.objects.create_user(
            username="otherplayer",
            password="otherpassword",
        )

        with self.assertRaises(ValueError):
            create_game_session(
                owner=other_user,  # pyright: ignore
                character=self.character,  # pyright: ignore
                campaign=self.campaign,  # pyright: ignore
            )

    def test_duplicate_play_returns_same_turn(self):
        action_id = uuid4()
        url = reverse("session-play", args=[self.session.pk])  # pyright: ignore

        payload = {
            "action_id": str(action_id),
            "message": "I inspect the room.",
        }

        first = self.client.post(url, payload, format="json")
        second = self.client.post(url, payload, format="json")

        self.assertEqual(first.status_code, 201)  # pyright: ignore
        self.assertEqual(second.status_code, 201)  # pyright: ignore
        self.assertEqual(first.data, second.data)  # pyright: ignore

        self.session.refresh_from_db()  # pyright: ignore
        self.assertEqual(self.session.turn_number, 1)  # pyright: ignore

    def test_duplicate_play_rejects_different_message(self):
        action_id = uuid4()
        url = reverse("session-play", args=[self.session.pk])  # pyright: ignore

        first = self.client.post(
            url,
            {
                "action_id": str(action_id),
                "message": "I inspect the room.",
            },
            format="json",
        )

        with self.assertRaisesMessage(
            ValidationError,
            f"Existing turn with action_id {action_id} has a different original_message: I inspect the room. vs I attack the Garrick.",
        ):  # pyright: ignore
            self.client.post(
                url,
                {
                    "action_id": str(action_id),
                    "message": "I attack the Garrick.",
                },
                format="json",
            )

        self.assertEqual(first.status_code, 201)  # pyright: ignore

        self.session.refresh_from_db()  # pyright: ignore
        self.assertEqual(self.session.turn_number, 1)  # pyright: ignore

    def test_narrate_verified_result(self):
        client = Mock()

        client.models.generate_content.return_value.text = (
            "Your sword stikes Garrick, but he remains standing."
        )

        provider = GeminiAIProvider(
            client=client,
            model="test-model",
        )

        context = {
            "location": {
                "slug": "lanterns_rest",
                "name": "Lantern's Rest",
            },
            "action": {
                "original_message": "I attack Garrick.",
                "mechanical_result": {
                    "type": "attack",
                    "target": "garrick_voss",
                    "target_remaining_hp": 13,
                    "target_alive": True,
                },
            },
        }

        narration = provider.narrate(context)

        self.assertIn("Garrick", narration)
        self.assertIn("standing", narration)

    def test_quest_rewards(self):
        quest = QuestState.objects.get(
            session=self.session,
            quest__slug="missing_villagers",
        )

        quest.status = "completed"
        quest.save(update_fields=["status"])

        player = PlayerState.objects.get(session=self.session)

        previous_gold = player.gold
        previous_experience = player.experience

        result = grant_quest_rewards(quest)

        player.refresh_from_db()
        quest.refresh_from_db()

        self.assertIsNotNone(result)
        self.assertTrue(quest.rewards_granted)

        rewards = quest.quest.data["rewards"]

        self.assertEqual(  # pyright:ignore
            player.gold,
            previous_gold + rewards.get("gold", 0),
        )

        self.assertEqual(  # pyright:ignore
            player.experience,
            previous_experience + rewards.get("experience", 0),
        )

    def test_quest_rewards_cannot_be_granted_twice(self):
        quest = QuestState.objects.get(
            session=self.session,
            quest__slug="missing_villagers",
        )

        quest.status = "completed"
        quest.save(update_fields=["status"])

        first = grant_quest_rewards(quest)

        player = PlayerState.objects.get(session=self.session)

        gold = player.gold
        experience = player.experience
        inventory = list(player.inventory)

        second = grant_quest_rewards(quest)

        player.refresh_from_db()

        self.assertIsNotNone(first)
        self.assertIsNone(second)

        self.assertEqual(player.gold, gold)  # pyright:ignore
        self.assertEqual(player.experience, experience)  # pyright:ignore
        self.assertEqual(player.inventory, inventory)  # pyright:ignore

    def test_incomplete_quest_cannot_grant_rewards(self):
        quest = QuestState.objects.get(
            session=self.session,
            quest__slug="missing_villagers",
        )

        quest.status = "active"
        quest.save(update_fields=["status"])

        result = grant_quest_rewards(quest)

        self.assertIsNone(result)

    def test_experience_level_up(self):
        player = PlayerState.objects.get(session=self.session)

        player.level = 1
        player.experience = 0
        player.save()

        previous_max_hp = player.max_hp

        with transaction.atomic():  # pyright:ignore
            player = PlayerState.objects.select_for_update().get(pk=player.pk)

            result = grant_experience(player, 300)

        player.refresh_from_db()

        self.assertEqual(player.level, 2)  # pyright:ignore
        self.assertEqual(player.experience, 300)  # pyright:ignore
        self.assertEqual(result.levels_gained, 1)  # pyright:ignore

        self.assertGreater(
            player.max_hp,
            previous_max_hp,
        )

        self.assertEqual(  # pyright:ignore
            player.max_hp,
            previous_max_hp + result.hp_gained,
        )

    def test_knowledge_discovery(self):
        discoveries, events = record_knowledge_disclosures(
            turn=self.turn,  # pyright:ignore
            npc_state=self.npc_state,  # pyright:ignore
            permitted_knowledge=[  # pyright:ignore
                {
                    "id": "test_fact",
                    "text": "A test fact.",
                }
            ],
            disclosed_ids=["test_fact"],  # pyright:ignore
        )

        self.assertEqual(len(discoveries), 1)  # pyright:ignore
        self.assertEqual(len(events), 1)  # pyright:ignore

        self.assertEqual(  # pyright:ignore
            events[0].type,
            "knowledge_discovered",
        )

        self.assertTrue(
            KnowledgeDiscovery.objects.filter(  # pyright:ignore
                session=self.turn.session,  # pyright:ignore
                knowledge_id="test_fact",
            ).exists()
        )

    def test_unauthorized_knowledge(self):
        with self.assertRaisesMessage(  # pyright:ignore
            ValidationError, "AI attempted unauthorized knowledge disclosure."
        ):
            record_knowledge_disclosures(
                turn=self.turn,  # pyright:ignore
                npc_state=self.npc_state,  # pyright:ignore
                permitted_knowledge=[],  # pyright:ignore
                disclosed_ids=["secret_hideout"],  # pyright:ignore
            )

        self.assertFalse(
            KnowledgeDiscovery.objects.filter(
                session=self.turn.session,  # pyright:ignore
                knowledge_id="secret_hideout",
            ).exists()
        )

    def test_duplicate_knowledge(self):
        kwargs = {
            "turn": self.turn,
            "npc_state": self.npc_state,
            "permitted_knowledge": [
                {
                    "id": "test_fact",
                    "text": "A test fact.",
                }
            ],
            "disclosed_ids": ["test_fact"],
        }

        first_discoveries, first_events = record_knowledge_disclosures(**kwargs)  # pyright:ignore

        second_discoveries, second_events = record_knowledge_disclosures(**kwargs)  # pyright:ignore

        self.assertEqual(len(first_discoveries), 1)  # pyright:ignore
        self.assertEqual(len(first_events), 1)  # pyright:ignore

        self.assertEqual(second_discoveries, [])  # pyright:ignore
        self.assertEqual(second_events, [])  # pyright:ignore

        self.assertEqual(  # pyright:ignore
            KnowledgeDiscovery.objects.filter(
                session=self.turn.session,  # pyright:ignore
                knowledge_id="test_fact",
            ).count(),
            1,
        )


class AttackMechanicsTest(TestCase):
    @patch("game.mechanics.roll_d20", return_value=(15, (15,)))
    def test_successful_attack(self, mock_roll):
        result = resolve_attack(
            strength=14,
            weapon_damage=5,
            defense=12,
        )

        self.assertTrue(result.hit)
        self.assertEqual(result.total, 17)  # pyright: ignore
        self.assertEqual(result.damage, 7)  # pyright: ignore

    @patch("game.mechanics.roll_d20", return_value=(1, (1,)))
    def test_natural_one_always_miss(self, mock_roll):
        result = resolve_attack(
            strength=20,
            defense=5,
            weapon_damage=5,
        )

        self.assertFalse(result.hit)
        self.assertEqual(result.damage, 0)  # pyright: ignore


class AttackActionTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="testplayer",
            password="testpassword",
        )

        self.other_user = User.objects.create_user(
            username="otherplayer",
            password="otherpassword",
        )

        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

        self.campaign = Campaign.objects.create(
            slug="test_campaign",
            title="Test Campaign",
            data={
                "starting_state": {
                    "location": "tavern",
                    "flags": {"quest_started": False},
                },
            },
        )

        self.tavern = Location.objects.create(
            campaign=self.campaign,
            slug="tavern",
            name="The Tavern",
            data={
                "connections": ["square"],
            },
        )

        self.square = Location.objects.create(
            campaign=self.campaign,
            slug="square",
            name="Square",
            data={
                "connections": ["tavern"],
            },
        )

        self.npc = NPC.objects.create(
            campaign=self.campaign,
            slug="garrick",
            name="Garrick",
            starting_location=self.tavern,
            data={
                "initial_state": {
                    "hp": 18,
                    "max_hp": 18,
                    "alive": True,
                    "trust": 0,
                    "hostility": 0,
                },
            },
        )

        self.character = Character.objects.create(
            owner=self.user,
            name="Aric",
        )

        self.session = create_game_session(
            owner=self.user,  # pyright: ignore
            character=self.character,  # pyright: ignore
            campaign=self.campaign,  # pyright: ignore
        )

        player = PlayerState.objects.get(session=self.session)
        player.inventory = ["rusty_sword"]
        player.save(update_fields=["inventory"])

    def attack(self, *, action_id=None, target="garrick"):
        return self.client.post(
            reverse("session-actions", args=[self.session.pk]),  # pyright: ignore
            {
                "action_id": str(action_id or uuid4()),
                "type": "attack",
                "target": target,
                "weapon": "rusty_sword",
            },
            format="json",
        )

    @patch("game.mechanics.roll_d20", return_value=(15, (15,)))
    def test_attack_creates_turn_and_deals_damage(self, mock_roll):
        response = self.attack()

        self.assertEqual(response.status_code, 201)  # pyright: ignore

        self.session.refresh_from_db()  # pyright: ignore
        npc_state = NPCState.objects.get(
            session=self.session,
            npc=self.npc,
        )

        self.assertEqual(self.session.turn_number, 1)  # pyright: ignore
        self.assertEqual(npc_state.hp, 13)  # pyright: ignore
        self.assertEqual(GameTurn.objects.filter(session=self.session).count(), 1)  # pyright: ignore

    @patch("game.mechanics.roll_d20", return_value=(15, (15,)))
    def test_duplicate_action_does_not_deal_damage_twice(self, mock_roll):
        action_id = uuid4()

        first = self.attack(action_id=action_id)
        second = self.attack(action_id=action_id)

        self.assertEqual(first.status_code, 201)  # pyright: ignore
        self.assertEqual(second.status_code, 201)  # pyright: ignore
        self.assertEqual(first.data["id"], second.data["id"])  # pyright: ignore

        npc_state = NPCState.objects.get(
            session=self.session,
            npc=self.npc,
        )

        self.assertEqual(npc_state.hp, 13)  # pyright: ignore
        self.assertEqual(GameTurn.objects.filter(session=self.session).count(), 1)  # pyright: ignore

        self.session.refresh_from_db()  # pyright: ignore
        self.assertEqual(self.session.turn_number, 1)  # pyright: ignore

    def test_cannot_attack_npc_in_another_location(self):
        npc_state = NPCState.objects.get(
            session=self.session,
            npc=self.npc,
        )

        npc_state.current_location = self.square
        npc_state.save(update_fields=["current_location"])

        with self.assertRaisesMessage(ValidationError, "not in the same location"):  # pyright: ignore
            self.attack()

        self.session.refresh_from_db()  # pyright: ignore
        npc_state.refresh_from_db()

        self.assertEqual(self.session.turn_number, 0)  # pyright: ignore
        self.assertEqual(npc_state.hp, 18)  # pyright: ignore
        self.assertEqual(GameTurn.objects.filter(session=self.session).count(), 0)  # pyright: ignore

    def test_cannot_attack_without_weapon(self):
        player = PlayerState.objects.get(session=self.session)
        player.inventory = []
        player.save(update_fields=["inventory"])

        with self.assertRaisesMessage(ValidationError, "does not have weapon"):  # pyright: ignore
            self.attack()

        self.session.refresh_from_db()  # pyright: ignore
        npc_state = NPCState.objects.get(session=self.session, npc=self.npc)

        self.assertEqual(self.session.turn_number, 0)  # pyright: ignore
        self.assertEqual(npc_state.hp, 18)  # pyright: ignore
        self.assertEqual(GameTurn.objects.filter(session=self.session).count(), 0)  # pyright: ignore

    def test_cannot_attack_another_users_session(self):
        other_character = Character.objects.create(
            owner=self.other_user,
            name="Borin",
        )
        other_session = create_game_session(
            owner=self.other_user,  # pyright: ignore
            character=other_character,  # pyright: ignore
            campaign=self.campaign,  # pyright: ignore
        )

        response = self.client.post(
            reverse("session-actions", args=[other_session.pk]),  # pyright: ignore
            {
                "action_id": str(uuid4()),
                "type": "attack",
                "target": "garrick",
                "weapon": "rusty_sword",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 404)  # pyright: ignore
        self.assertEqual(GameTurn.objects.filter(session=other_session).count(), 0)  # pyright: ignore


class MovementActionTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="testplayer",
            password="testpassword",
        )

        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

        self.campaign = Campaign.objects.create(
            slug="test_campaign",
            title="Test Campaign",
            data={
                "starting_state": {
                    "location": "tavern",
                    "flags": {"quest_started": False},
                },
            },
        )

        self.tavern = Location.objects.create(
            campaign=self.campaign,
            slug="tavern",
            name="The Tavern",
            data={
                "connections": ["square"],
            },
        )

        self.square = Location.objects.create(
            campaign=self.campaign,
            slug="square",
            name="Square",
            data={
                "connections": ["tavern"],
            },
        )

        self.character = Character.objects.create(
            owner=self.user,
            name="Aric",
        )

        self.session = create_game_session(
            owner=self.user,  # pyright: ignore
            character=self.character,  # pyright: ignore
            campaign=self.campaign,  # pyright: ignore
        )

    def test_move_creates_turn_and_updates_location(self):
        action_id = uuid4()

        response = self.client.post(
            reverse("session-actions", args=[self.session.pk]),  # pyright: ignore
            {
                "action_id": str(action_id),
                "type": "move",
                "destination": "square",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201)  # pyright: ignore

        self.session.refresh_from_db()  # pyright: ignore
        self.assertEqual(self.session.current_location, self.square)  #  pyright: ignore
        self.assertEqual(self.session.turn_number, 1)  # pyright: ignore
        self.assertEqual(GameTurn.objects.filter(session=self.session).count(), 1)  # pyright: ignore

    def test_cannot_move_to_unconnected_location(self):
        action_id = uuid4()

        with self.assertRaisesMessage(ValidationError, "not connected"):  # pyright: ignore
            self.client.post(
                reverse("session-actions", args=[self.session.pk]),  # pyright: ignore
                {
                    "action_id": str(action_id),
                    "type": "move",
                    "destination": "nonexistent_location",
                },
                format="json",
            )

        self.session.refresh_from_db()  # pyright: ignore
        self.assertEqual(self.session.current_location, self.tavern)  # pyright: ignore
        self.assertEqual(self.session.turn_number, 0)  # pyright: ignore
        self.assertEqual(GameTurn.objects.filter(session=self.session).count(), 0)  # pyright: ignore

    def test_duplicate_move_action_does_not_increment_turn(self):
        action_id = uuid4()

        self.client.post(
            reverse("session-actions", args=[self.session.pk]),  # pyright: ignore
            {
                "action_id": str(action_id),
                "type": "move",
                "destination": "square",
            },
            format="json",
        )

        self.client.post(
            reverse("session-actions", args=[self.session.pk]),  # pyright: ignore
            {
                "action_id": str(action_id),
                "type": "move",
                "destination": "square",
            },
            format="json",
        )

        self.session.refresh_from_db()  # pyright: ignore
        self.assertEqual(self.session.current_location, self.square)  # pyright: ignore
        self.assertEqual(self.session.turn_number, 1)  # pyright: ignore
        self.assertEqual(GameTurn.objects.filter(session=self.session).count(), 1)  # pyright: ignore


class InspectActionTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="testplayer",
            password="testpassword",
        )

        self.other_user = User.objects.create_user(
            username="otherplayer",
            password="otherpassword",
        )

        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

        self.campaign = Campaign.objects.create(
            slug="test_campaign",
            title="Test Campaign",
            data={
                "starting_state": {
                    "location": "tavern",
                    "flags": {"quest_started": False},
                },
            },
        )

        self.tavern = Location.objects.create(
            campaign=self.campaign,
            slug="tavern",
            name="The Tavern",
            data={
                "connections": ["square"],
            },
        )

        self.square = Location.objects.create(
            campaign=self.campaign,
            slug="square",
            name="Square",
            data={
                "connections": ["tavern"],
            },
        )

        self.npc = NPC.objects.create(
            campaign=self.campaign,
            slug="garrick",
            name="Garrick",
            starting_location=self.tavern,
            data={
                "initial_state": {
                    "hp": 18,
                    "max_hp": 18,
                    "alive": True,
                    "trust": 0,
                    "hostility": 0,
                },
            },
        )

        self.character = Character.objects.create(
            owner=self.user,
            name="Aric",
        )

        self.session = create_game_session(
            owner=self.user,  # pyright: ignore
            character=self.character,  # pyright: ignore
            campaign=self.campaign,  # pyright: ignore
        )

    def inspect(self, action_id=None):
        return self.client.post(
            reverse("session-actions", args=[self.session.pk]),  # pyright: ignore
            {
                "action_id": str(action_id or uuid4()),
                "type": "inspect",
            },
            format="json",
        )

    def test_inspect_returns_location(self):
        response = self.inspect()

        self.assertEqual(response.status_code, 201)  #  pyright: ignore

        result = response.data["mechanical_result"]  # pyright: ignore

        self.assertEqual(result["type"], "inspect")  # pyright: ignore
        self.assertEqual(result["location"]["slug"], "tavern")  # pyright: ignore

    def test_inspect_returns_visible_npcs(self):
        response = self.inspect()

        self.assertEqual(response.status_code, 201)  # pyright: ignore

        npcs = response.data["mechanical_result"]["npcs"]  # pyright: ignore

        self.assertEqual(len(npcs), 1)  # pyright: ignore
        self.assertEqual(npcs[0]["slug"], "garrick")  # pyright: ignore

    def test_inspect_creates_turn(self):
        response = self.inspect()

        self.assertEqual(response.status_code, 201)  # pyright: ignore

        self.session.refresh_from_db()  # pyright: ignore

        self.assertEqual(self.session.turn_number, 1)  # pyright: ignore
        self.assertEqual(GameTurn.objects.filter(session=self.session).count(), 1)  # pyright: ignore

    def test_duplicate_inspect_action_does_not_increment_turn(self):
        action_id = uuid4()

        self.inspect(action_id=action_id)
        self.inspect(action_id=action_id)

        self.session.refresh_from_db()  # pyright: ignore

        self.assertEqual(self.session.turn_number, 1)  # pyright: ignore
        self.assertEqual(GameTurn.objects.filter(session=self.session).count(), 1)  # pyright: ignore


class TalkActionTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="testplayer",
            password="testpassword",
        )

        self.other_user = User.objects.create_user(
            username="otherplayer",
            password="otherpassword",
        )

        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

        self.campaign = Campaign.objects.create(
            slug="test_campaign",
            title="Test Campaign",
            data={
                "starting_state": {
                    "location": "tavern",
                    "flags": {"quest_started": False},
                },
            },
        )

        self.tavern = Location.objects.create(
            campaign=self.campaign,
            slug="tavern",
            name="The Tavern",
            data={
                "connections": ["square"],
            },
        )

        self.square = Location.objects.create(
            campaign=self.campaign,
            slug="square",
            name="Square",
            data={
                "connections": ["tavern"],
            },
        )

        self.npc = NPC.objects.create(
            campaign=self.campaign,
            slug="garrick",
            name="Garrick",
            starting_location=self.tavern,
            data={
                "initial_state": {
                    "hp": 18,
                    "max_hp": 18,
                    "alive": True,
                    "trust": 0,
                    "hostility": 0,
                },
            },
        )

        self.character = Character.objects.create(
            owner=self.user,
            name="Aric",
        )

        self.session = create_game_session(
            owner=self.user,  # pyright: ignore
            character=self.character,  # pyright: ignore
            campaign=self.campaign,  # pyright: ignore
        )

    def talk(self, action_id=None, message="Hello!"):
        return self.client.post(
            reverse("session-actions", args=[self.session.pk]),  # pyright: ignore
            {
                "action_id": str(action_id or uuid4()),
                "type": "talk",
                "target": "garrick",
                "message": message,
                "intent": "normal",
            },
            format="json",
        )

    def test_talk_creates_turn_and_returns_response(self):
        response = self.talk(message="Hello!")

        self.assertEqual(response.status_code, 201)  # pyright: ignore

        self.session.refresh_from_db()  # pyright: ignore

        self.assertEqual(self.session.turn_number, 1)  # pyright: ignore
        self.assertEqual(GameTurn.objects.filter(session=self.session).count(), 1)  # pyright: ignore

        result = response.data["mechanical_result"]  # pyright: ignore

        self.assertEqual(result["type"], "talk")  # pyright: ignore
        self.assertIn("message", result)

    def test_duplicate_talk_action_does_not_increment_turn(self):
        action_id = uuid4()

        self.talk(action_id=action_id, message="Hello!")
        self.talk(action_id=action_id, message="Hello!")

        self.session.refresh_from_db()  # pyright: ignore

        self.assertEqual(self.session.turn_number, 1)  # pyright: ignore
        self.assertEqual(GameTurn.objects.filter(session=self.session).count(), 1)  # pyright: ignore

    def test_cannot_talk_to_npc_in_another_location(self):
        npc_state = NPCState.objects.get(
            session=self.session,
            npc=self.npc,
        )

        npc_state.current_location = self.square
        npc_state.save(update_fields=["current_location"])

        with self.assertRaisesMessage(ValidationError, "not found in the session"):  # pyright: ignore
            self.talk(message="Hello!")

        self.session.refresh_from_db()  # pyright: ignore
        self.assertEqual(self.session.turn_number, 0)  # pyright: ignore
        self.assertEqual(GameTurn.objects.filter(session=self.session).count(), 0)  # pyright: ignore

    def test_cannot_talk_to_another_users_session(self):
        other_character = Character.objects.create(
            owner=self.other_user,
            name="Borin",
        )
        other_session = create_game_session(
            owner=self.other_user,  # pyright: ignore
            character=other_character,  # pyright: ignore
            campaign=self.campaign,  # pyright: ignore
        )

        response = self.client.post(
            reverse("session-actions", args=[other_session.pk]),  # pyright: ignore
            {
                "action_id": str(uuid4()),
                "type": "talk",
                "target": "garrick",
                "message": "Hello!",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 404)  # pyright: ignore
        self.assertEqual(GameTurn.objects.filter(session=other_session).count(), 0)  # pyright: ignore

    def test_talk_without_message_returns_error(self):
        response = self.talk(message="")

        self.assertEqual(response.status_code, 400)  # pyright: ignore
        self.assertEqual(GameTurn.objects.filter(session=self.session).count(), 0)  # pyright: ignore


class NPCKnowledgeTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="testplayer",
            password="testpassword",
        )

        self.campaign = Campaign.objects.create(
            slug="test_campaign",
            title="Test Campaign",
            data={
                "starting_state": {
                    "location": "tavern",
                    "flags": {"quest_started": False},
                },
            },
        )

        self.location = Location.objects.create(
            campaign=self.campaign, slug="tavern", name="The Tavern"
        )

        self.npc = NPC.objects.create(
            campaign=self.campaign,
            slug="garrick",
            name="Garrick",
            starting_location=self.location,
            data={
                "initial_state": {
                    "hp": 18,
                    "max_hp": 18,
                    "alive": True,
                    "trust": 0,
                    "hostility": 0,
                },
            },
        )
        self.character = Character.objects.create(
            owner=self.user,
            name="Aric",
        )

        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

        self.session = create_game_session(
            owner=self.user,  # pyright: ignore
            character=self.character,  # pyright: ignore
            campaign=self.campaign,  # pyright: ignore
        )

    def test_knowledge_unlock(self):
        npc_state = NPCState.objects.get(
            session=self.session,
            npc=self.npc,
        )

        npc_state.npc.data["knowledge"] = [
            {
                "id": "public",
                "text": "The village is quiet.",
                "conditions": {"min_trust": 0},
            },
            {
                "id": "private",
                "text": "I know something important.",
                "conditions": {"min_trust": 5},
            },
        ]

        npc_state.npc.save(update_fields=["data"])

        npc_state.trust = 0
        npc_state.save(update_fields=["trust"])

        self.assertEqual(  # pyright: ignore
            get_permitted_knowledge(npc_state.session, npc_state),
            [{"id": "public", "text": "The village is quiet."}],
        )

        npc_state.trust = 5
        npc_state.save(update_fields=["trust"])

        self.assertEqual(  # pyright: ignore
            get_permitted_knowledge(npc_state.session, npc_state),
            [
                {"id": "public", "text": "The village is quiet."},
                {"id": "private", "text": "I know something important."},
            ],
        )

    def test_attack_reduces_trust_and_increases_hostility(self):
        npc_state = NPCState.objects.get(
            session=self.session,
            npc=self.npc,
        )

        npc_state.trust = 0
        npc_state.hostility = 0
        npc_state.save(update_fields=["trust", "hostility"])

        apply_relationship_event(npc_state=npc_state, event="attacked_npc")  # pyright: ignore
        npc_state.refresh_from_db()  # pyright: ignore

        self.assertEqual(npc_state.trust, -5)  # pyright: ignore
        self.assertEqual(npc_state.hostility, 5)  # pyright: ignore

    def test_relationship_values_are_clamped(self):
        npc_state = NPCState.objects.get(
            session=self.session,
            npc=self.npc,
        )

        npc_state.trust = -99
        npc_state.hostility = 99
        npc_state.save(update_fields=["trust", "hostility"])

        apply_relationship_event(npc_state=npc_state, event="attacked_npc")  # pyright: ignore
        npc_state.refresh_from_db()  # pyright: ignore

        self.assertEqual(npc_state.trust, -100)  # pyright: ignore
        self.assertEqual(npc_state.hostility, 100)  # pyright: ignore

    def test_personality_affect_difficulty(self):
        npc_state = NPCState.objects.get(
            session=self.session,
            npc=self.npc,
        )

        npc_state.npc.data["personality"] = {
            "social_resistances": {
                "persuade": 5,
            }
        }
        dc = calculate_interaction_dc(npc_state=npc_state, intent="persuade")

        self.assertEqual(dc, 17)  # pyright: ignore

    def test_trust_reduces_persuasion_difficulty(self):
        npc_state = NPCState.objects.get(
            session=self.session,
            npc=self.npc,
        )

        npc_state.trust = 60
        npc_state.hostility = 0
        npc_state.save(update_fields=["trust", "hostility"])

        neutral_dc = 12

        trusted_dc = calculate_interaction_dc(
            npc_state=npc_state,
            intent="persuade",
        )

        self.assertLess(trusted_dc, neutral_dc)

    def test_hostility_increases_persuasion_difficulty(self):
        npc_state = NPCState.objects.get(
            session=self.session,
            npc=self.npc,
        )

        npc_state.trust = 0
        npc_state.hostility = 80
        npc_state.save(update_fields=["trust", "hostility"])

        neutral_dc = 12

        hostile_dc = calculate_interaction_dc(
            npc_state=npc_state,
            intent="persuade",
        )

        self.assertGreater(hostile_dc, neutral_dc)


class InteractionMechanicsTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="testplayer",
            password="testpassword",
        )

        self.campaign = Campaign.objects.create(
            slug="test_campaign",
            title="Test Campaign",
            data={
                "starting_state": {
                    "location": "tavern",
                    "flags": {"quest_started": False},
                },
            },
        )

        self.location = Location.objects.create(
            campaign=self.campaign, slug="tavern", name="The Tavern"
        )

        self.npc = NPC.objects.create(
            campaign=self.campaign,
            slug="garrick",
            name="Garrick",
            starting_location=self.location,
            data={
                "initial_state": {
                    "hp": 18,
                    "max_hp": 18,
                    "alive": True,
                    "trust": 0,
                    "hostility": 0,
                },
                "personality": {
                    "social_resistances": {
                        "persuade": 5,
                        "intimidate": 3,
                        "deceive": 4,
                        "compliment": 2,
                    }
                },
            },
        )

        self.character = Character.objects.create(
            owner=self.user,
            name="Aric",
        )

        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

        self.session = create_game_session(
            owner=self.user,  # pyright: ignore
            character=self.character,  # pyright: ignore
            campaign=self.campaign,  # pyright: ignore
        )

    def test_attribute_modifier(self):
        self.character.charisma = 16

        self.assertEqual(  # pyright: ignore
            self.character.attribute_modifier("charisma"),
            3,
        )

    def test_proficient_skill(self):
        self.character.charisma = 16
        self.character.level = 1
        self.character.proficient_skills = ["persuasion"]

        self.assertEqual(  # pyright: ignore
            self.character.skill_modifier("persuasion"),
            5,
        )

    def test_non_proficient_skill(self):
        self.character.charisma = 16
        self.character.proficient_skills = []

        self.assertEqual(  # pyright: ignore
            self.character.skill_modifier("persuasion"),
            3,
        )

    def test_strength_based_intimidation(self):
        self.character.strength = 18
        self.character.level = 1
        self.character.proficient_skills = ["intimidation"]

        self.assertEqual(  # pyright: ignore
            self.character.skill_modifier(
                "intimidation",
                attribute="strength",
            ),
            6,
        )

    @patch("game.checks.randint", return_value=12)
    def test_successful_skill_check(self, mock_roll):
        player_state = PlayerState.objects.get(session=self.session)
        player_state.charisma = 16
        player_state.level = 1
        player_state.proficient_skills = ["persuasion"]

        result = resolve_skill_check(
            player=player_state,
            skill="persuasion",
            difficulty=15,
        )

        self.assertEqual(result.roll, 12)  # pyright: ignore
        self.assertEqual(result.modifier, 5)  # pyright: ignore
        self.assertEqual(result.total, 17)  # pyright: ignore
        self.assertTrue(result.success)


class DiceRollTest(SimpleTestCase):
    @patch("game.checks.randint", return_value=12)
    def test_normal_roll(self, mock_roll):
        roll, rolls = roll_d20("normal")

        self.assertEqual(roll, 12)  # pyright: ignore
        self.assertEqual(rolls, (12,))  # pyright: ignore
        self.assertEqual(mock_roll.call_count, 1)  # pyright: ignore

    @patch("game.checks.randint", side_effect=[4, 17])
    def test_advantage_roll(self, mock_roll):
        roll, rolls = roll_d20("advantage")

        self.assertEqual(roll, 17)  # pyright: ignore
        self.assertEqual(rolls, (4, 17))  # pyright: ignore

    @patch("game.checks.randint", side_effect=[4, 17])
    def test_disadvantage_roll(self, mock_roll):
        roll, rolls = roll_d20("disadvantage")

        self.assertEqual(roll, 4)  # pyright: ignore
        self.assertEqual(rolls, (4, 17))  # pyright: ignore

    def test_advantage_and_disadvantage_cancel(self):
        mode = resolve_roll_mode(advantage=True, disadvantage=True)

        self.assertEqual(mode, "normal")  # pyright: ignore

    def test_invalid_roll_mode(self):
        with self.assertRaises(ValueError):
            roll_d20("invalid")  # pyright: ignore


class GameEventTest(SimpleTestCase):
    def test_matching_event(self):
        condition = EventCondition(
            type="object_discovered",
            target="notice_board",
        )

        event = GameEvent(
            type="object_discovered",
            target="notice_board",
            location="The player has discovered the notice board.",
        )

        self.assertTrue(condition.matched(event))

    def test_wrong_target(self):
        condition = EventCondition(
            type="object_discovered",
            target="notice_board",
        )

        event = GameEvent(
            type="object_discovered",
            target="fireplace",
        )

        self.assertFalse(condition.matched(event))

    def test_wrong_event_type(self):
        condition = EventCondition(
            type="object_discovered",
            target="locked_storage",
        )

        event = GameEvent(
            type="object_unlocked",
            target="locked_storage",
        )

        self.assertFalse(condition.matched(event))

    def test_wrong_location(self):
        condition = EventCondition(
            type="object_discovered",
            target="notice_board",
            location="lanterns_rest",
        )

        event = GameEvent(
            type="object_discovered",
            target="notice_board",
            location="hollowmere_sqaure",
        )

        self.assertFalse(condition.matched(event))


class ProgressionTests(SimpleTestCase):
    def test_starting_level(self):
        self.assertEqual(get_level(0), 1)  # pyright: ignore

    def test_below_threshold(self):
        self.assertEqual(get_level(299), 1)  # pyright: ignore

    def test_exact_threshold(self):
        self.assertEqual(get_level(300), 2)  # pyright: ignore

    def test_multiple_levels(self):
        self.assertEqual(get_level(2700), 4)  # pyright: ignore

    def test_maximum_lvel(self):
        self.assertEqual(get_level(999999), 20)  # pyright: ignore

    def test_negative_experience(self):
        with self.assertRaises(ValueError):
            get_level(-1)
