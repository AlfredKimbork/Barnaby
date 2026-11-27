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
from game.actions import process_attack, process_movement
from game.checks import resolve_roll_mode, resolve_skill_check, roll_d20
from game.combat import (
    advance_combat_turn,
    evaluate_combat_status,
    get_active_combat,
    get_combat_defense,
    get_current_participant,
    get_npc_combat_events,
    join_combat,
    resolve_npc_attack,
    resolve_npc_defend,
    resolve_npc_flee,
    resolve_npc_surrender,
    retreat_companions,
    start_combat,
)
from game.combat_actions import process_combat_action
from game.combat_ai import choose_npc_action
from game.companions import get_companions, process_companion_action
from game.events import EventCondition, GameEvent
from game.general_actions import process_skill_check
from game.interactions import calculate_interaction_dc
from game.knowledge import (
    get_permitted_knowledge,
    record_knowledge_disclosures,
)
from game.mechanics import (
    parse_damage_dice,
    resolve_attack,
    resolve_combat_attack,
    roll_damage,
    roll_initiative,
)
from game.models import (
    ChallengeState,
    CombatEncounter,
    CombatParticipant,
    CombatStatus,
    CombatTeam,
    GameTurn,
    KnowledgeDiscovery,
    NPCState,
    PlayerState,
    QuestState,
)
from game.progression import get_level
from game.quests import conditions_satisfied, process_game_events
from game.relationships import apply_relationship_event
from game.rewards import grant_experience, grant_quest_rewards
from game.turns import create_turn
from narrator.providers import GeminiAIProvider
from world.models import NPC, Campaign, Item, Location, Quest

from .services import create_game_session

User = get_user_model()


def equip_rusty_sword(session):
    Item.objects.create(
        campaign=session.campaign,
        slug="rusty_sword",
        name="Rusty Sword",
        data={"type": "weapon", "properties": {"damage_dice": "1d6"}},
    )
    player = PlayerState.objects.get(session=session)
    player.inventory = ["rusty_sword"]
    player.save(update_fields=["inventory"])


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

        self.missing_villagers = Quest.objects.create(
            campaign=self.campaign,
            slug="missing_villagers",
            title="The Missing Villagers",
            data={
                "description": "Investigate the missing villagers.",
                "activation": [
                    {
                        "type": "object_discovered",
                        "target": "notice_board",
                        "location": "tavern",
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
                "outcomes": [
                    {
                        "id": "betrayal",
                        "name": "Betrayal",
                        "description": "Accept the mercenary bribe.",
                        "status": "failed",
                        "conditions": [
                            {
                                "type": "flag_changed",
                                "flag": "accepted_mercenary_bribe",
                                "value": True,
                            }
                        ],
                        "rewards": {"gold": 150, "experience": 100},
                        "consequences": {"flags": {"betrayed_village": True}},
                    },
                    {
                        "id": "negotiated_release",
                        "name": "Negotiated Release",
                        "description": "Negotiate the villagers' release.",
                        "status": "completed",
                        "conditions": [
                            {
                                "type": "flag_changed",
                                "flag": "villagers_release_negotiated",
                                "value": True,
                            }
                        ],
                    },
                ],
            },
        )

        self.location = Location.objects.create(
            campaign=self.campaign,
            slug="tavern",
            name="The Tavern",
            data={"connections": ["square"]},
        )
        self.square = Location.objects.create(
            campaign=self.campaign, slug="square", name="Village Square"
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

        self.followup_quest = Quest.objects.create(
            campaign=self.campaign,
            slug="a_village_divided",
            title="A Village Divided",
            data={
                "activation": [
                    {
                        "type": "quest_resolved",
                        "quest": self.missing_villagers.slug,
                        "outcome": "betrayal",
                    }
                ],
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

        equip_rusty_sword(self.session)

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
        self.assertCountEqual(
            QuestState.objects.filter(session=session).values_list("quest", "status"),
            [
                (self.missing_villagers.pk, "hidden"),
                (self.followup_quest.pk, "hidden"),
            ],
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

    def test_betrayal_outcome(self):
        quest = QuestState.objects.get(
            session=self.session,
            quest__slug="missing_villagers",
        )

        quest.status = "active"
        quest.save(update_fields=["status"])

        player = PlayerState.objects.get(session=self.session)

        previous_gold = player.gold
        previous_experience = player.experience

        _updates, events = process_game_events(
            session=self.session,  # pyright:ignore
            events=[  # pyright:ignore
                GameEvent(
                    type="flag_changed",
                    flag="accepted_mercenary_bribe",
                    value=True,
                )
            ],
        )

        quest.refresh_from_db()
        player.refresh_from_db()
        self.session.refresh_from_db()  # pyright:ignore

        self.assertEqual(quest.status, "failed")  # pyright:ignore
        self.assertEqual(quest.outcome, "betrayal")  # pyright:ignore
        self.assertTrue(quest.rewards_granted)

        self.assertEqual(player.gold, previous_gold + 150)  # pyright:ignore

        self.assertEqual(player.experience, previous_experience + 100)  # pyright:ignore

        self.assertTrue(self.session.flags["betrayed_village"])  # pyright:ignore

        self.assertTrue(
            any(
                event.type == "quest_resolved" and event.outcome == "betrayal"
                for event in events
            )
        )

    def test_resolved_quest_cannot_change(self):
        quest = QuestState.objects.get(
            session=self.session,
            quest__slug="missing_villagers",
        )

        quest.status = "active"
        quest.save(update_fields=["status"])

        process_game_events(
            session=self.session,  # pyright:ignore
            events=[  # pyright:ignore
                GameEvent(
                    type="flag_changed",
                    flag="accepted_mercenary_bribe",
                    value=True,
                )
            ],
        )

        process_game_events(
            session=self.session,  # pyright:ignore
            events=[  # pyright:ignore
                GameEvent(
                    type="flag_changed",
                    flag="villagers_release_negotiated",
                    value=True,
                )
            ],
        )

        quest.refresh_from_db()

        self.assertEqual(quest.outcome, "betrayal")  # pyright:ignore
        self.assertEqual(quest.status, "failed")  # pyright:ignore

    def test_followup_quest_activation(self):
        quest = QuestState.objects.get(
            session=self.session,
            quest__slug="missing_villagers",
        )

        quest.status = "active"
        quest.save(update_fields=["status"])

        process_game_events(
            session=self.session,  # pyright:ignore
            events=[  # pyright:ignore
                GameEvent(
                    type="flag_changed",
                    flag="accepted_mercenary_bribe",
                    value=True,
                )
            ],
        )

        followup = QuestState.objects.get(
            session=self.session,
            quest=self.followup_quest,
        )

        self.assertEqual(followup.status, "active")  # pyright:ignore

    def test_start_combat(self):
        encounter = start_combat(
            session=self.session,  # pyright:ignore
            enemies=[self.npc_state],  # pyright:ignore
        )

        self.assertEqual(encounter.status, "active")  # pyright:ignore
        self.assertEqual(encounter.round_number, 1)  # pyright:ignore
        self.assertEqual(encounter.turn_index, 0)  # pyright:ignore

        self.assertEqual(  # pyright:ignore
            encounter.participants.count(),  # pyright:ignore
            2,
        )

    @patch(
        "game.checks.randint",
        side_effect=[5, 18],
    )
    def test_combat_initiative_order(
        self,
        mock_roll,
    ):
        player = PlayerState.objects.get(session=self.session)

        player.dexterity = 10
        player.save(update_fields=["dexterity"])

        encounter = start_combat(
            session=self.session,  # pyright:ignore
            enemies=[self.npc_state],  # pyright:ignore
        )

        participant = get_current_participant(encounter)  # pyright:ignore

        self.assertIsNotNone(participant)

        self.assertEqual(  # pyright:ignore
            participant.npc_id,  # pyright:ignore
            self.npc_state.pk,
        )

    def test_duplicate_combat(self):
        start_combat(
            session=self.session,  # pyright:ignore
            enemies=[self.npc_state],  # pyright:ignore
        )

        with self.assertRaisesMessage(  #  pyright: ignore
            ValidationError,
            "A combat encounter is already active for this session.",
        ):
            start_combat(
                session=self.session,  # pyright:ignore
                enemies=[self.npc_state],  # pyright:ignore
            )

    def test_combat_round_progression(self):
        encounter = start_combat(
            session=self.session,  # pyright:ignore
            enemies=[self.npc_state],  # pyright:ignore
        )

        advance_combat_turn(encounter)  # pyright:ignore

        self.assertEqual(encounter.turn_index, 1)  # pyright:ignore
        self.assertEqual(encounter.round_number, 1)  # pyright:ignore

        advance_combat_turn(encounter)  # pyright:ignore

        self.assertEqual(encounter.turn_index, 0)  # pyright:ignore
        self.assertEqual(encounter.round_number, 2)  # pyright:ignore

    def test_combat_victory(self):
        encounter = start_combat(
            session=self.session,  # pyright:ignore
            enemies=[self.npc_state],  # pyright:ignore
        )

        self.npc_state.hp = 0
        self.npc_state.alive = False

        self.npc_state.save(update_fields=["hp", "alive"])

        participant = advance_combat_turn(
            encounter  # pyright:ignore
        )

        encounter.refresh_from_db()  # pyright:ignore

        self.assertIsNone(participant)
        self.assertEqual(encounter.status, "victory")  # pyright:ignore
        self.assertIsNotNone(encounter.ended_at)  # pyright:ignore

        self.assertIsNone(
            get_active_combat(self.session)  # pyright:ignore
        )

    def test_combat_defeat(self):
        encounter = start_combat(
            session=self.session,  # pyright:ignore
            enemies=[self.npc_state],  # pyright:ignore
        )

        player = PlayerState.objects.get(session=self.session)

        player.take_damage(player.hp)

        participant = advance_combat_turn(
            encounter  # pyright:ignore
        )

        encounter.refresh_from_db()  # pyright:ignore

        self.assertIsNone(participant)
        self.assertEqual(encounter.status, "defeat")  # pyright:ignore

    def test_combat_rejects_other_session_npc(self):
        other_session = create_game_session(
            owner=self.user,  # pyright: ignore
            character=self.character,  # pyright: ignore
            campaign=self.campaign,  # pyright: ignore
        )

        other_npc = NPCState.objects.get(
            session=other_session,
            npc=self.npc,
        )

        with self.assertRaisesMessage(  # pyright: ignore
            ValidationError, "Some enemies are not valid for this session."
        ):
            start_combat(
                session=self.session,  # pyright: ignore
                enemies=[other_npc],  # pyright: ignore
            )

    @patch(
        "game.checks.randint",
        side_effect=[20, 1, 10, 1],
    )
    @patch(
        "game.mechanics.randint",
        return_value=1,
    )
    def test_attack_starts_combat(
        self,
        mock_damage,
        mock_d20,
    ):
        turn = process_attack(
            session=self.session,  # pyright:ignore
            action_id=uuid4(),  # pyright:ignore
            target_slug=self.npc.slug,  # pyright:ignore
            weapon_slug="rusty_sword",  # pyright:ignore
        )

        result = turn.mechanical_result  # pyright:ignore

        self.assertTrue(result["combat_started"])

        self.assertTrue(result["player_attack_executed"])

        self.assertEqual(  # pyright:ignore
            result["combat_status"],
            "active",
        )

        self.assertIsNotNone(
            get_active_combat(self.session)  # pyright:ignore
        )

        self.assertEqual(  # pyright:ignore
            result["combat_actions"][0]["type"],
            "player_attack",
        )

    @patch(
        "game.checks.randint",
        side_effect=[20, 1, 1, 20],
    )
    @patch(
        "game.mechanics.randint",
        return_value=1,
    )
    def test_enemy_retaliates(
        self,
        mock_damage,
        mock_d20,
    ):
        player = PlayerState.objects.get(session=self.session)

        previous_hp = player.hp

        turn = process_attack(
            session=self.session,  # pyright:ignore
            action_id=uuid4(),  # pyright:ignore
            target_slug=self.npc.slug,  # pyright:ignore
            weapon_slug="rusty_sword",  # pyright:ignore
        )

        player.refresh_from_db()

        actions = turn.mechanical_result[  # pyright:ignore
            "combat_actions"
        ]

        self.assertEqual(len(actions), 2)  # pyright:ignore

        self.assertEqual(  # pyright:ignore
            actions[0]["type"],
            "player_attack",
        )

        self.assertEqual(  # pyright:ignore
            actions[1]["type"],
            "npc_attack",
        )

        self.assertLess(
            player.hp,
            previous_hp,
        )

    @patch(
        "game.checks.randint",
        side_effect=[20, 1, 1, 1],
    )
    def test_duplicate_combat_action(
        self,
        mock_d20,
    ):
        action_id = uuid4()

        kwargs = {
            "session": self.session,
            "action_id": action_id,
            "target_slug": self.npc.slug,
            "weapon_slug": "rusty_sword",
        }

        first = process_attack(**kwargs)

        player = PlayerState.objects.get(session=self.session)

        player_hp = player.hp

        self.npc_state.refresh_from_db()

        npc_hp = self.npc_state.hp

        second = process_attack(**kwargs)

        player.refresh_from_db()
        self.npc_state.refresh_from_db()

        self.assertEqual(first.pk, second.pk)  # pyright: ignore

        self.assertEqual(player.hp, player_hp)  # pyright: ignore

        self.assertEqual(  # pyright: ignore
            self.npc_state.hp,
            npc_hp,
        )

        self.assertEqual(  # pyright: ignore
            GameTurn.objects.filter(
                session=self.session,
                action_id=action_id,
            ).count(),
            1,
        )

        self.assertEqual(  # pyright: ignore
            CombatEncounter.objects.filter(
                session=self.session,
            ).count(),
            1,
        )

        self.assertEqual(mock_d20.call_count, 4)  # pyright: ignore

    @patch(
        "game.checks.randint",
        side_effect=[20, 1, 20],
    )
    @patch(
        "game.mechanics.randint",
        return_value=6,
    )
    def test_attack_combat_victory(
        self,
        mock_damage,
        mock_d20,
    ):
        self.npc_state.hp = 1

        self.npc_state.save(update_fields=["hp"])

        turn = process_attack(
            session=self.session,  # pyright: ignore
            action_id=uuid4(),  # pyright: ignore
            target_slug=self.npc.slug,  # pyright: ignore
            weapon_slug="rusty_sword",  # pyright: ignore
        )

        result = turn.mechanical_result  # pyright: ignore

        self.assertEqual(  # pyright: ignore
            result["combat_status"],
            "victory",
        )

        self.assertFalse(result["target_alive"])

        self.assertIsNone(
            get_active_combat(self.session)  # pyright: ignore
        )

        event_types = {event["type"] for event in result["events"]}

        self.assertIn("npc_killed", event_types)
        self.assertIn("combat_won", event_types)
        self.assertIn("combat_ended", event_types)

    def test_defending_increases_defense(self):
        encounter = start_combat(
            session=self.session,  # pyright: ignore
            enemies=[self.npc_state],  # pyright: ignore
        )

        participant = CombatParticipant.objects.get(
            encounter=encounter,
            team="player",
        )

        base_defense = get_combat_defense(participant)

        participant.defending = True
        participant.save(update_fields=["defending"])

        self.assertEqual(  # pyright: ignore
            get_combat_defense(participant),
            base_defense + 2,
        )

    @patch(
        "game.checks.randint",
        side_effect=[20, 1, 1],
    )
    def test_defend_consumes_turn(
        self,
        mock_roll,
    ):
        encounter = start_combat(
            session=self.session,  # pyright: ignore
            enemies=[self.npc_state],  # pyright: ignore
        )

        turn = process_combat_action(
            session=self.session,  # pyright: ignore
            action_id=uuid4(),  # pyright: ignore
            action="defend",  # pyright: ignore
        )

        encounter.refresh_from_db()  # pyright: ignore

        actions = turn.mechanical_result["combat_actions"]  # pyright: ignore

        self.assertEqual(  # pyright: ignore
            actions[0]["type"],
            "player_defend",
        )

        self.assertEqual(  # pyright: ignore
            actions[1]["type"],
            "npc_attack",
        )

        self.assertEqual(  # pyright: ignore
            encounter.round_number,  # pyright: ignore
            2,
        )

        participant = get_current_participant(encounter)  # pyright: ignore

        self.assertTrue(participant.is_player)  # pyright: ignore
        self.assertFalse(participant.defending)  # pyright: ignore

    @patch(
        "game.checks.randint",
        side_effect=[20, 1, 20],
    )
    def test_successful_flee(
        self,
        mock_roll,
    ):
        destination = self.square.slug

        encounter = start_combat(
            session=self.session,  # pyright: ignore
            enemies=[self.npc_state],  # pyright: ignore
        )

        turn = process_combat_action(
            session=self.session,  # pyright: ignore
            action_id=uuid4(),  # pyright: ignore
            action="flee",  # pyright: ignore
            destination_slug=destination,  # pyright: ignore
        )

        encounter.refresh_from_db()  # pyright: ignore
        self.session.refresh_from_db()  # pyright: ignore

        self.assertEqual(  # pyright: ignore
            encounter.status,  # pyright: ignore
            CombatStatus.ESCAPED,
        )

        self.assertEqual(  # pyright: ignore
            self.session.current_location.slug,  # pyright: ignore
            destination,
        )

        self.assertTrue(
            turn.mechanical_result[  # pyright: ignore
                "player_result"
            ]["success"]
        )

        event_types = {
            event["type"]
            for event in turn.mechanical_result["events"]  # pyright: ignore
        }

        self.assertIn(
            "combat_escaped",
            event_types,
        )

        self.assertIn(
            "location_entered",
            event_types,
        )

    @patch(
        "game.checks.randint",
        side_effect=[20, 1, 1, 1],
    )
    def test_failed_flee(
        self,
        mock_roll,
    ):
        original_location = (
            self.session.current_location  # pyright: ignore
        )

        destination = self.square.slug

        encounter = start_combat(
            session=self.session,  # pyright: ignore
            enemies=[self.npc_state],  # pyright: ignore
        )

        turn = process_combat_action(
            session=self.session,  # pyright: ignore
            action_id=uuid4(),  # pyright: ignore
            action="flee",  # pyright: ignore
            destination_slug=destination,  # pyright: ignore
        )

        encounter.refresh_from_db()  # pyright: ignore
        self.session.refresh_from_db()  # pyright: ignore

        self.assertEqual(  # pyright: ignore
            encounter.status,  # pyright: ignore
            "active",
        )

        self.assertEqual(  # pyright: ignore
            self.session.current_location,  # pyright: ignore
            original_location,
        )

        self.assertFalse(
            turn.mechanical_result[  # pyright: ignore
                "player_result"
            ]["success"]
        )

        self.assertEqual(  # pyright: ignore
            turn.mechanical_result[  # pyright: ignore
                "combat_actions"
            ][1]["type"],
            "npc_attack",
        )

    @patch(
        "game.checks.randint",
        side_effect=[20, 1],
    )
    def test_surrender(
        self,
        mock_roll,
    ):
        encounter = start_combat(
            session=self.session,  # pyright: ignore
            enemies=[self.npc_state],  # pyright: ignore
        )

        turn = process_combat_action(
            session=self.session,  # pyright: ignore
            action_id=uuid4(),  # pyright: ignore
            action="surrender",  # pyright: ignore
        )

        encounter.refresh_from_db()  # pyright: ignore

        self.assertEqual(  # pyright: ignore
            encounter.status,  # pyright: ignore
            CombatStatus.SURRENDERED,
        )

        self.assertEqual(  # pyright: ignore
            len(
                turn.mechanical_result[  # pyright: ignore
                    "combat_actions"
                ]
            ),
            1,
        )

        participant = CombatParticipant.objects.get(
            encounter=encounter,
            team="player",
        )

        self.assertTrue(participant.withdrawn)

    @patch(
        "game.checks.randint",
        side_effect=[20, 1, 1],
    )
    def test_duplicate_defend(
        self,
        mock_roll,
    ):
        start_combat(
            session=self.session,  # pyright: ignore
            enemies=[self.npc_state],  # pyright: ignore
        )

        action_id = uuid4()

        kwargs = {
            "session": self.session,
            "action_id": action_id,
            "action": "defend",
        }

        first = process_combat_action(**kwargs)
        second = process_combat_action(**kwargs)

        self.assertEqual(  # pyright: ignore
            first.pk,  # pyright: ignore
            second.pk,  # pyright: ignore
        )

        self.assertEqual(  # pyright: ignore
            mock_roll.call_count,
            3,
        )

    def test_defend_outside_combat(self):
        with self.assertRaises(ValidationError):
            process_combat_action(
                session=self.session,  # pyright: ignore
                action_id=uuid4(),  # pyright: ignore
                action="defend",  # pyright: ignore
            )

    def configure_npc_behaviour(
        self,
        *,
        style: str,
        hp: int,
        **overrides,
    ):
        self.npc.data = {
            **self.npc.data,
            "combat": {
                **self.npc.data.get("combat", {}),
                "behaviour": {
                    "style": style,
                    **overrides,
                },
            },
        }

        self.npc.save(update_fields=["data"])

        self.npc_state.max_hp = 20
        self.npc_state.hp = hp

        self.npc_state.save(update_fields=["hp", "max_hp"])

        encounter = start_combat(
            session=self.session,  # pyright: ignore
            enemies=[self.npc_state],  # pyright: ignore
        )

        participant = CombatParticipant.objects.select_related("npc", "npc__npc").get(
            encounter=encounter,
            npc=self.npc_state,
        )

        return encounter, participant

    def test_healthy_npc_attacks(self):
        encounter, participant = self.configure_npc_behaviour(
            style="balanced",
            hp=20,
        )

        decision = choose_npc_action(
            encounter=encounter,  # pyright: ignore
            participant=participant,
        )

        self.assertEqual(  # pyright: ignore
            decision.action,
            "attack",
        )

    def test_injured_npc_defends(self):
        encounter, participant = self.configure_npc_behaviour(
            style="balanced",
            hp=6,
        )

        decision = choose_npc_action(
            encounter=encounter,  # pyright: ignore
            participant=participant,
        )

        self.assertEqual(  # pyright: ignore
            decision.action,
            "defend",
        )

    def test_npc_defensive_cooldown(self):
        encounter, participant = self.configure_npc_behaviour(
            style="balanced",
            hp=6,
        )

        resolve_npc_defend(
            encounter=encounter,  # pyright: ignore
            participant=participant,
        )

        decision = choose_npc_action(
            encounter=encounter,  # pyright: ignore
            participant=participant,
        )

        self.assertEqual(  # pyright: ignore
            decision.action,
            "attack",
        )

    def test_cautious_npc_flees(self):
        encounter, participant = self.configure_npc_behaviour(
            style="cautious",
            hp=4,
        )

        decision = choose_npc_action(
            encounter=encounter,  # pyright: ignore
            participant=participant,
        )

        self.assertEqual(  # pyright: ignore
            decision.action,
            "flee",
        )

    def test_npc_surrender_decision(self):
        encounter, participant = self.configure_npc_behaviour(
            style="balanced",
            hp=2,
        )

        decision = choose_npc_action(
            encounter=encounter,  # pyright: ignore
            participant=participant,
        )

        self.assertEqual(  # pyright: ignore
            decision.action,
            "surrender",
        )

    def test_aggressive_npc_continues_fighting(self):
        encounter, participant = self.configure_npc_behaviour(
            style="aggressive",
            hp=1,
        )

        decision = choose_npc_action(
            encounter=encounter,  # pyright: ignore
            participant=participant,
        )

        self.assertEqual(  # pyright: ignore
            decision.action,
            "attack",
        )

    def test_npc_surrender_preserves_life(self):
        encounter, participant = self.configure_npc_behaviour(
            style="balanced",
            hp=2,
        )

        resolve_npc_surrender(
            encounter=encounter,  # pyright: ignore
            participant=participant,
        )

        self.npc_state.refresh_from_db()
        participant.refresh_from_db()

        self.assertTrue(self.npc_state.alive)
        self.assertEqual(self.npc_state.hp, 2)  # pyright: ignore
        self.assertTrue(participant.withdrawn)

        self.assertEqual(  # pyright: ignore
            evaluate_combat_status(encounter),  # pyright: ignore
            CombatStatus.VICTORY,
        )

    def test_npc_surrender_event(self):
        encounter, participant = self.configure_npc_behaviour(
            style="balanced",
            hp=2,
        )

        result = resolve_npc_surrender(
            encounter=encounter,  # pyright: ignore
            participant=participant,
        )

        events = get_npc_combat_events(
            encounter=encounter,  # pyright: ignore
            actions=[result],
        )

        self.assertEqual(len(events), 1)  # pyright: ignore

        self.assertEqual(  # pyright: ignore
            events[0].type,
            "npc_surrendered",
        )

        self.assertEqual(  # pyright: ignore
            events[0].npc,
            self.npc.slug,
        )

    def test_npc_successful_escape(self):
        encounter, participant = self.configure_npc_behaviour(
            style="cautious",
            hp=2,
        )

        original_location = self.npc_state.current_location

        with patch(
            "game.checks.randint",
            return_value=20,
        ):
            result = resolve_npc_flee(
                encounter=encounter,  # pyright: ignore
                participant=participant,
            )

        self.npc_state.refresh_from_db()
        participant.refresh_from_db()

        self.assertTrue(result["success"])
        self.assertTrue(participant.withdrawn)
        self.assertTrue(self.npc_state.alive)

        self.assertNotEqual(
            self.npc_state.current_location,
            original_location,
        )

    @patch(
        "game.checks.randint",
        return_value=20,
    )
    def test_recruit_companion(
        self,
        mock_roll,
    ):
        self.npc_state.trust = 30
        self.npc_state.save(update_fields=["trust"])

        turn = process_companion_action(
            session=self.session,  # pyright: ignore
            action_id=uuid4(),  # pyright: ignore
            action="recruit",  # pyright: ignore
            target_slug=self.npc.slug,  # pyright: ignore
        )

        self.npc_state.refresh_from_db()

        self.assertTrue(self.npc_state.is_recruited)

        self.assertTrue(
            turn.mechanical_result["result"]["success"]  # pyright: ignore
        )

        self.assertEqual(  # pyright: ignore
            get_companions(self.session).count(),  # pyright: ignore
            1,
        )

    @patch("game.checks.randint")
    def test_recruitment_requires_trust(
        self,
        mock_roll,
    ):
        self.npc_state.trust = 0
        self.npc_state.save(update_fields=["trust"])

        turn = process_companion_action(
            session=self.session,  # pyright: ignore
            action_id=uuid4(),  # pyright: ignore
            action="recruit",  # pyright: ignore
            target_slug=self.npc.slug,  # pyright: ignore
        )

        self.npc_state.refresh_from_db()

        self.assertFalse(self.npc_state.is_recruited)

        self.assertEqual(  # pyright: ignore
            turn.mechanical_result["result"]["reason"],  # pyright: ignore
            "insufficient_trust",
        )

        mock_roll.assert_not_called()

    @patch(
        "game.checks.randint",
        return_value=1,
    )
    def test_failed_recruitment(
        self,
        mock_roll,
    ):
        self.npc_state.trust = 10
        self.npc_state.save(update_fields=["trust"])

        turn = process_companion_action(
            session=self.session,  # pyright: ignore
            action_id=uuid4(),  # pyright: ignore
            action="recruit",  # pyright: ignore
            target_slug=self.npc.slug,  # pyright: ignore
        )

        self.npc_state.refresh_from_db()

        self.assertFalse(self.npc_state.is_recruited)

        self.assertEqual(  # pyright: ignore
            turn.mechanical_result["result"]["reason"],  # pyright: ignore
            "persuasion_failed",
        )

    @patch(
        "game.checks.randint",
        return_value=1,
    )
    def test_recruitment_cooldown(
        self,
        mock_roll,
    ):
        self.npc_state.trust = 10
        self.npc_state.save(update_fields=["trust"])

        kwargs = {
            "session": self.session,
            "action": "recruit",
            "target_slug": self.npc.slug,
        }

        process_companion_action(
            action_id=uuid4(),  # pyright: ignore
            **kwargs,
        )

        second = process_companion_action(
            action_id=uuid4(),  # pyright: ignore
            **kwargs,
        )

        self.assertEqual(  # pyright: ignore
            second.mechanical_result["result"]["reason"],  # pyright: ignore
            "recruitment_cooldown",
        )

        self.assertEqual(  # pyright: ignore
            mock_roll.call_count,
            1,
        )

    def test_dismiss_companion(self):
        self.npc_state.state = {
            **self.npc_state.state,
            "recruited": True,
        }

        self.npc_state.save(update_fields=["state"])

        turn = process_companion_action(
            session=self.session,  # pyright: ignore
            action_id=uuid4(),  # pyright: ignore
            action="dismiss",  # pyright: ignore
            target_slug=self.npc.slug,  # pyright: ignore
        )

        self.npc_state.refresh_from_db()

        self.assertFalse(self.npc_state.is_recruited)

        self.assertTrue(
            turn.mechanical_result["result"]["success"]  # pyright: ignore
        )

        self.assertEqual(  # pyright: ignore
            get_companions(self.session).count(),  # pyright: ignore
            0,
        )

    def test_companion_follows_player(self):
        self.npc_state.state = {
            **self.npc_state.state,
            "recruited": True,
        }

        self.npc_state.save(update_fields=["state"])

        destination = self.session.current_location.data["connections"][0]  # pyright: ignore

        turn = process_movement(
            session=self.session,  # pyright: ignore
            action_id=uuid4(),  # pyright: ignore
            destination_slug=destination,  # pyright: ignore
        )

        self.session.refresh_from_db()  # pyright: ignore
        self.npc_state.refresh_from_db()

        self.assertEqual(  # pyright: ignore
            self.npc_state.current_location,
            self.session.current_location,  # pyright: ignore
        )

        self.assertIn(
            self.npc.slug,
            turn.mechanical_result[  # pyright: ignore
                "following_companions"
            ],
        )

    def test_dismissed_npc_does_not_follow(self):
        original_location = self.npc_state.current_location

        self.npc_state.state = {
            **self.npc_state.state,
            "recruited": False,
        }

        self.npc_state.save(update_fields=["state"])

        destination = self.session.current_location.data["connections"][0]  # pyright: ignore

        process_movement(
            session=self.session,  # pyright: ignore
            action_id=uuid4(),  # pyright: ignore
            destination_slug=destination,  # pyright: ignore
        )

        self.npc_state.refresh_from_db()

        self.assertEqual(  # pyright: ignore
            self.npc_state.current_location,
            original_location,
        )

    @patch(
        "game.checks.randint",
        return_value=20,
    )
    def test_duplicate_recruitment(
        self,
        mock_roll,
    ):
        self.npc_state.trust = 30
        self.npc_state.save(update_fields=["trust"])

        kwargs = {
            "session": self.session,
            "action_id": uuid4(),
            "action": "recruit",
            "target_slug": self.npc.slug,
        }

        first = process_companion_action(**kwargs)
        second = process_companion_action(**kwargs)

        self.assertEqual(  #  pyright: ignore
            first.pk,  #  pyright: ignore
            second.pk,  #  pyright: ignore
        )

        self.assertEqual(  #  pyright: ignore
            mock_roll.call_count,
            1,
        )

    def test_cannot_recruit_during_combat(self):
        start_combat(
            session=self.session,  # pyright: ignore
            enemies=[self.npc_state],  # pyright: ignore
        )

        with self.assertRaises(ValidationError):
            process_companion_action(
                session=self.session,  # pyright: ignore
                action_id=uuid4(),  # pyright: ignore
                action="recruit",  # pyright: ignore
                target_slug=self.npc.slug,  # pyright: ignore
            )

    def create_test_companion(self):
        npc = NPC.objects.create(
            campaign=self.session.campaign,  # pyright: ignore
            slug="test_companion",
            name="Test Companion",
            starting_location=self.session.current_location,  # pyright: ignore
            data={
                "combat": {
                    "initiative_modifier": 0,
                    "attacks": [
                        {
                            "id": "sword",
                            "name": "Sword",
                            "attack_bonus": 3,
                            "damage_modifier": 2,
                            "damage_dice": "1d6",
                        }
                    ],
                },
            },
        )

        return NPCState.objects.create(
            session=self.session,  # pyright: ignore
            npc=npc,
            current_location=(self.session.current_location),  # pyright: ignore
            hp=16,
            max_hp=16,
            alive=True,
            trust=50,
            hostility=0,
            state={
                "recruited": True,
                "defense": 12,
            },
        )

    def test_companion_joins_combat(self):
        companion = self.create_test_companion()

        encounter = start_combat(  # pyright: ignore
            session=self.session,  # pyright: ignore
            enemies=[self.npc_state],  # pyright: ignore
        )

        participant = CombatParticipant.objects.get(
            encounter=encounter,
            npc=companion,
        )

        self.assertEqual(  # pyright: ignore
            participant.team,
            CombatTeam.ALLY,
        )

        self.assertEqual(  # pyright: ignore
            encounter.participants.count(),  # pyright: ignore
            3,
        )

    @patch(
        "game.checks.randint",
        return_value=20,
    )
    @patch(
        "game.mechanics.randint",
        return_value=3,
    )
    def test_companion_attacks_enemy(
        self,
        mock_damage,
        mock_roll,
    ):
        companion = self.create_test_companion()

        encounter = start_combat(
            session=self.session,  # pyright: ignore
            enemies=[self.npc_state],  # pyright: ignore
        )

        attacker = CombatParticipant.objects.get(
            encounter=encounter,
            npc=companion,
        )

        target = CombatParticipant.objects.get(
            encounter=encounter,  # pyright: ignore
            npc=self.npc_state,  # pyright: ignore
        )

        encounter.turn_index = attacker.position  # pyright: ignore

        encounter.save(update_fields=["turn_index"])  # pyright: ignore

        previous_hp = self.npc_state.hp  # pyright: ignore

        result = resolve_npc_attack(
            encounter=encounter,  # pyright: ignore
            participant=attacker,
            target=target,
        )

        self.npc_state.refresh_from_db()  # pyright: ignore

        self.assertEqual(  # pyright: ignore
            result["attacker_team"],
            CombatTeam.ALLY,
        )

        self.assertEqual(  # pyright: ignore
            result["target"],
            self.npc.slug,  # pyright: ignore
        )

        self.assertLess(
            self.npc_state.hp,  # pyright: ignore
            previous_hp,
        )

    @patch(
        "game.checks.randint",
        return_value=20,
    )
    @patch(
        "game.mechanics.randint",
        return_value=2,
    )
    def test_enemy_attacks_companion(
        self,
        mock_damage,
        mock_roll,
    ):
        companion = self.create_test_companion()

        encounter = start_combat(
            session=self.session,  # pyright: ignore
            enemies=[self.npc_state],  # pyright: ignore
        )

        attacker = CombatParticipant.objects.get(
            encounter=encounter,
            npc=self.npc_state,  # pyright: ignore
        )

        target = CombatParticipant.objects.get(
            encounter=encounter,
            npc=companion,
        )

        encounter.turn_index = attacker.position  # pyright: ignore

        encounter.save(update_fields=["turn_index"])  # pyright: ignore

        previous_hp = companion.hp

        result = resolve_npc_attack(
            encounter=encounter,  # pyright: ignore
            participant=attacker,
            target=target,
        )

        companion.refresh_from_db()

        self.assertEqual(  # pyright: ignore
            result["target"],
            companion.npc.slug,
        )

        self.assertLess(
            companion.hp,
            previous_hp,
        )

    @patch(
        "game.checks.randint",
        return_value=20,
    )
    @patch(
        "game.mechanics.randint",
        return_value=2,
    )
    def test_companion_death(
        self,
        mock_damage,
        mock_roll,
    ):
        companion = self.create_test_companion()

        companion.hp = 1
        companion.save(update_fields=["hp"])

        encounter = start_combat(
            session=self.session,  # pyright: ignore
            enemies=[self.npc_state],  # pyright: ignore
        )

        attacker = CombatParticipant.objects.get(
            encounter=encounter,
            npc=self.npc_state,  # pyright: ignore
        )

        target = CombatParticipant.objects.get(
            encounter=encounter,
            npc=companion,
        )

        encounter.turn_index = attacker.position  # pyright: ignore

        encounter.save(update_fields=["turn_index"])  # pyright: ignore

        result = resolve_npc_attack(
            encounter=encounter,  # pyright: ignore
            participant=attacker,
            target=target,
        )

        companion.refresh_from_db()

        self.assertFalse(companion.alive)  # pyright: ignore
        self.assertEqual(companion.hp, 0)  # pyright: ignore

        events = get_npc_combat_events(
            encounter=encounter,  # pyright: ignore
            actions=[result],
        )

        self.assertIn(
            "companion_killed",
            {event.type for event in events},
        )

    def test_companion_retreats_with_player(self):
        companion = self.create_test_companion()

        encounter = start_combat(
            session=self.session,  # pyright: ignore
            enemies=[self.npc_state],  # pyright: ignore
        )

        destination_slug = self.session.current_location.data["connections"][0]  # pyright: ignore

        destination = Location.objects.get(
            campaign=self.session.campaign,  # pyright: ignore
            slug=destination_slug,
        )

        escaped = retreat_companions(
            encounter=encounter,  # pyright: ignore
            destination=destination,
        )

        companion.refresh_from_db()

        participant = CombatParticipant.objects.get(
            encounter=encounter,
            npc=companion,
        )

        self.assertEqual(  # pyright: ignore
            companion.current_location,
            destination,
        )

        self.assertTrue(participant.withdrawn)

        self.assertIn(
            companion.npc.slug,
            escaped,
        )

    def test_reinforcement_joins_next_round(self):
        reinforcement = self.create_test_companion()

        reinforcement.state = {
            **reinforcement.state,
            "recruited": False,
        }

        reinforcement.save(update_fields=["state"])

        encounter = start_combat(
            session=self.session,  # pyright: ignore
            enemies=[self.npc_state],  # pyright: ignore
        )

        previous_count = encounter.participants.count()  # pyright: ignore

        participant = join_combat(
            encounter=encounter,  # pyright: ignore
            npc=reinforcement,  # pyright: ignore
            team=CombatTeam.ENEMY,  # pyright: ignore
        )

        self.assertEqual(  # pyright: ignore
            participant.position,  # pyright: ignore
            previous_count,
        )

        self.assertEqual(  # pyright: ignore
            participant.state["eligible_round"],  # pyright: ignore
            encounter.round_number + 1,  # pyright: ignore
        )

        self.assertEqual(  # pyright: ignore
            encounter.participants.count(),  # pyright: ignore
            previous_count + 1,
        )

    @patch(
        "game.checks.randint",
        side_effect=[20, 1, 10],
    )
    def test_reinforcement_skips_arrival_round(
        self,
        mock_roll,
    ):
        reinforcement = self.create_test_companion()

        reinforcement.state = {
            **reinforcement.state,
            "recruited": False,
        }

        reinforcement.save(update_fields=["state"])

        encounter = start_combat(
            session=self.session,  # pyright: ignore
            enemies=[self.npc_state],  # pyright: ignore
        )

        participant = join_combat(
            encounter=encounter,  # pyright: ignore
            npc=reinforcement,  # pyright: ignore
            team=CombatTeam.ENEMY,  # pyright: ignore
        )

        encounter.turn_index = participant.position - 1  # pyright: ignore

        encounter.save(update_fields=["turn_index"])  # pyright: ignore

        next_participant = advance_combat_turn(encounter)  # pyright: ignore

        self.assertEqual(  # pyright: ignore
            encounter.round_number,  # pyright: ignore
            2,
        )

        self.assertNotEqual(
            next_participant.pk,  # pyright: ignore
            participant.pk,  # pyright: ignore
        )

    def test_reinforcement_cannot_join_twice(self):
        reinforcement = self.create_test_companion()

        reinforcement.state = {
            **reinforcement.state,
            "recruited": False,
        }

        reinforcement.save(update_fields=["state"])

        encounter = start_combat(
            session=self.session,  # pyright: ignore
            enemies=[self.npc_state],  # pyright: ignore
        )

        kwargs = {
            "encounter": encounter,
            "npc": reinforcement,
            "team": CombatTeam.ENEMY,
        }

        join_combat(**kwargs)

        with self.assertRaises(ValidationError):
            join_combat(**kwargs)

    def configure_surrender(self, **config):
        self.npc.data = {
            **self.npc.data,
            "combat": {
                **self.npc.data.get("combat", {}),
                "surrender": config,
            },
        }

        self.npc.save(update_fields=["data"])

    @patch(
        "game.checks.randint",
        side_effect=[20, 1, 1],
    )
    def test_surrender_refused(self, mock_roll):
        self.configure_surrender(
            outcome="refusal",
        )

        encounter = start_combat(
            session=self.session,  # pyright: ignore
            enemies=[self.npc_state],  # pyright: ignore
        )

        turn = process_combat_action(
            session=self.session,  # pyright: ignore
            action_id=uuid4(),  # pyright: ignore
            action="surrender",  # pyright: ignore
        )

        encounter.refresh_from_db()  # pyright: ignore

        result = turn.mechanical_result  # pyright: ignore

        self.assertFalse(result["player_result"]["success"])
        self.assertEqual(  # pyright: ignore
            result["player_result"]["refusing_opponents"],
            [self.npc.slug],
        )

        self.assertEqual(  # pyright: ignore
            encounter.status,  # pyright: ignore
            CombatStatus.ACTIVE,
        )

        self.assertEqual(  # pyright: ignore
            result["combat_actions"][1]["type"],
            "npc_attack",
        )

    @patch(
        "game.checks.randint",
        side_effect=[20, 1],
    )
    def test_surrender_extortion(self, mock_roll):
        self.configure_surrender(
            outcome="extortion",
            gold_amount=25,
        )

        player = self.session.player_state  # pyright: ignore

        player.gold = 40
        player.save(update_fields=["gold"])

        encounter = start_combat(
            session=self.session,  # pyright: ignore
            enemies=[self.npc_state],  # pyright: ignore
        )

        turn = process_combat_action(
            session=self.session,  # pyright: ignore
            action_id=uuid4(),  # pyright: ignore
            action="surrender",  # pyright: ignore
        )

        player.refresh_from_db()
        encounter.refresh_from_db()  # pyright: ignore

        self.assertEqual(player.gold, 15)  # pyright: ignore

        self.assertEqual(  # pyright: ignore
            encounter.status,  # pyright: ignore
            CombatStatus.SURRENDERED,
        )

        self.assertEqual(  # pyright: ignore
            turn.mechanical_result["player_result"]["gold_lost"],  # pyright: ignore
            25,
        )

    @patch(
        "game.checks.randint",
        side_effect=[20, 1],
    )
    def test_surrender_cannot_create_negative_gold(
        self,
        mock_roll,
    ):
        self.configure_surrender(
            outcome="extortion",
            gold_amount=25,
        )

        player = self.session.player_state  # pyright: ignore

        player.gold = 8
        player.save(update_fields=["gold"])

        start_combat(
            session=self.session,  # pyright: ignore
            enemies=[self.npc_state],  # pyright: ignore
        )

        turn = process_combat_action(
            session=self.session,  # pyright: ignore
            action_id=uuid4(),  # pyright: ignore
            action="surrender",  # pyright: ignore
        )

        player.refresh_from_db()

        self.assertEqual(player.gold, 0)  # pyright: ignore

        self.assertEqual(  # pyright: ignore
            turn.mechanical_result["player_result"]["gold_lost"],  # pyright: ignore
            8,
        )

    @patch(
        "game.checks.randint",
        side_effect=[20, 1],
    )
    def test_duplicate_surrender_penalty(
        self,
        mock_roll,
    ):
        self.configure_surrender(
            outcome="extortion",
            gold_amount=25,
        )

        player = self.session.player_state  # pyright: ignore

        player.gold = 100
        player.save(update_fields=["gold"])

        start_combat(
            session=self.session,  # pyright: ignore
            enemies=[self.npc_state],  # pyright: ignore
        )

        kwargs = {
            "session": self.session,
            "action_id": uuid4(),
            "action": "surrender",
        }

        first = process_combat_action(**kwargs)
        second = process_combat_action(**kwargs)

        player.refresh_from_db()

        self.assertEqual(first.pk, second.pk)  # pyright: ignore
        self.assertEqual(player.gold, 75)  # pyright: ignore


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

        equip_rusty_sword(self.session)

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

    @patch("game.mechanics.randint", return_value=5)
    @patch("game.checks.randint", side_effect=[20, 1, 15, 1])
    def test_attack_creates_turn_and_deals_damage(self, mock_roll, mock_damage):
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

    @patch("game.mechanics.randint", return_value=5)
    @patch("game.checks.randint", side_effect=[20, 1, 15, 1])
    def test_duplicate_action_does_not_deal_damage_twice(self, mock_roll, mock_damage):
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
            campaign=self.campaign,
            slug="tavern",
            name="The Tavern",
            data={
                "challenges": [
                    {
                        "id": "muddy_footprints",
                        "description": "Muddy footprints lead away from the tavern.",
                        "approaches": [
                            {
                                "id": "track",
                                "description": "Determine where the footprints lead.",
                                "skill": "survival",
                                "difficulty": 12,
                                "repeatable": False,
                                "success": {
                                    "description": "The footprints lead north.",
                                    "flags": {"mercenary_tracks_found": True},
                                },
                                "failure": {
                                    "description": "The footprints are too disturbed to follow.",
                                },
                            }
                        ],
                    }
                ],
            },
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

    @patch("game.checks.randint", return_value=20)
    def test_successful_challenge(self, mock_roll):
        turn = process_skill_check(
            session=self.session,  # pyright:ignore
            action_id=uuid4(),  # pyright:ignore
            target_slug="muddy_footprints",  # pyright:ignore
            approach_slug="track",  # pyright:ignore
            intent="Follow the tracks.",  # pyright:ignore
        )

        self.session.refresh_from_db()  # pyright:ignore

        self.assertTrue(
            turn.mechanical_result["success"]  # pyright:ignore
        )

        self.assertTrue(
            self.session.flags["mercenary_tracks_found"]  # pyright:ignore
        )

        state = ChallengeState.objects.get(
            session=self.session,
            challenge_slug="muddy_footprints",
            approach_slug="track",
        )

        self.assertEqual(state.attempts, 1)  # pyright:ignore
        self.assertEqual(state.successes, 1)  # pyright:ignore
        self.assertEqual(state.failures, 0)  # pyright:ignore

    @patch("game.checks.randint", return_value=1)
    def test_failed_challenge(self, mock_roll):
        turn = process_skill_check(
            session=self.session,  # pyright:ignore
            action_id=uuid4(),  # pyright:ignore
            target_slug="muddy_footprints",  # pyright:ignore
            approach_slug="track",  # pyright:ignore
            intent="Follow the tracks.",  # pyright:ignore
        )

        self.session.refresh_from_db()  # pyright:ignore

        self.assertFalse(
            turn.mechanical_result["success"]  # pyright:ignore
        )

        self.assertNotIn(
            "mercenary_tracks_found",
            self.session.flags,  # pyright:ignore
        )

        state = ChallengeState.objects.get(
            session=self.session,
            challenge_slug="muddy_footprints",
            approach_slug="track",
        )

        self.assertEqual(state.failures, 1)  # pyright:ignore

    def test_invalid_challenge(self):
        turn = process_skill_check(
            session=self.session,  # pyright:ignore
            action_id=uuid4(),  # pyright:ignore
            target_slug="nonexistent_tower",  # pyright:ignore
            approach_slug="climb",  # pyright:ignore
            intent="Climb the tower.",  # pyright:ignore
        )

        self.assertEqual(  # pyright:ignore
            turn.mechanical_result["status"],  # pyright:ignore
            "rejected",
        )

        self.assertIsNone(
            turn.mechanical_result["check"]  # pyright:ignore
        )

        self.assertFalse(
            ChallengeState.objects.filter(
                session=self.session,
                challenge_slug="nonexistent_tower",
            ).exists()
        )


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


class QuestConditionTests(SimpleTestCase):
    def setUp(self):
        self.condition = [
            {
                "type": "npc_killed",
                "npc": "mercenary_captain",
            },
            {
                "type": "npc_killed",
                "npc": "mercenary_lieutenant",
            },
        ]

        self.events = [
            GameEvent(
                type="npc_killed",
                npc="mercenary_captain",
            ),
        ]

    def test_any_condition(self):
        self.assertTrue(
            conditions_satisfied(
                self.condition,
                self.events,
                mode="any",
            )
        )

    def test_all_conditions_incomplete(self):
        self.assertFalse(
            conditions_satisfied(
                self.condition,
                self.events,
                mode="all",
            )
        )

    def test_all_conditions_completed(self):
        self.events.append(
            GameEvent(
                type="npc_killed",
                npc="mercenary_lieutenant",
            )
        )

        self.assertTrue(
            conditions_satisfied(
                self.condition,
                self.events,
                mode="all",
            )
        )


class CombatMechanicsTests(SimpleTestCase):
    def test_parse_damage_dice(self):
        self.assertEqual(  # pyright: ignore
            parse_damage_dice("2d6"),
            (2, 6),
        )

    def test_invalid_damage_dice(self):
        with self.assertRaises(ValueError):
            parse_damage_dice("invalid")

    @patch(
        "game.mechanics.randint",
        return_value=4,
    )
    def test_damage_roll(self, mock_roll):
        self.assertEqual(  # pyright: ignore
            roll_damage("2d6"),
            (4, 4),
        )

    @patch(
        "game.mechanics.randint",
        return_value=4,
    )
    def test_critical_damage(self, mock_roll):
        self.assertEqual(  # pyright: ignore
            roll_damage("2d6", critical=True),
            (4, 4, 4, 4),
        )

    @patch(
        "game.checks.randint",
        return_value=15,
    )
    def test_initiative(self, mock_roll):
        result = roll_initiative(3)

        self.assertEqual(result.roll, 15)  # pyright: ignore
        self.assertEqual(result.total, 18)  # pyright: ignore

    @patch(
        "game.mechanics.randint",
        return_value=5,
    )
    @patch(
        "game.checks.randint",
        return_value=15,
    )
    def test_successful_attack(
        self,
        mock_attack,
        mock_damage,
    ):
        result = resolve_combat_attack(
            attack_modifier=5,
            damage_modifier=3,
            damage_dice="1d6",
            defense=14,
        )

        self.assertTrue(result.hit)
        self.assertFalse(result.critical)
        self.assertEqual(result.total, 20)  # pyright: ignore
        self.assertEqual(result.damage, 8)  # pyright: ignore

    @patch(
        "game.checks.randint",
        return_value=1,
    )
    def test_natural_one(self, mock_roll):
        result = resolve_combat_attack(
            attack_modifier=20,
            damage_modifier=3,
            damage_dice="1d6",
            defense=10,
        )

        self.assertFalse(result.hit)
        self.assertEqual(result.damage, 0)  # pyright: ignore

    @patch(
        "game.mechanics.randint",
        return_value=4,
    )
    @patch(
        "game.checks.randint",
        return_value=20,
    )
    def test_natural_twenty(
        self,
        mock_attack,
        mock_damage,
    ):
        result = resolve_combat_attack(
            attack_modifier=0,
            damage_modifier=3,
            damage_dice="1d6",
            defense=30,
        )

        self.assertTrue(result.hit)
        self.assertTrue(result.critical)  # pyright: ignore
        self.assertEqual(result.damage, 11)  # pyright: ignore
        self.assertEqual(  # pyright: ignore
            result.damage_rolls,
            (4, 4),
        )
