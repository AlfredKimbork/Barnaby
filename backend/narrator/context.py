from game.combat import get_combat_context
from game.companions import get_party_context
from game.general_actions import get_visible_challenges
from game.interactables import get_visible_interactables
from game.knowledge import (
    get_discovered_knowledge,
    get_discovered_knowledge_ids,
    get_permitted_knowledge,
)
from game.memory import get_npc_memories
from game.models import GameSession, GameTurn, NPCState
from game.quests import get_visible_quests
from narrator.schemas import AIActionResponse


def build_interpreter_context(session: GameSession):
    location = session.current_location

    npcs = NPCState.objects.filter(
        session=session,
        current_location=location,
        alive=True,
    ).select_related("npc")

    return {
        "location": {
            "slug": location.slug,  # pyright: ignore
            "name": location.name,
            "description": location.description,
        },
        "interactables": get_visible_interactables(session),
        "connections": location.data.get("connections", []),  # pyright: ignore
        "npcs": [
            {
                "slug": npc.npc.slug,
                "name": npc.npc.name,
            }
            for npc in npcs
        ],
        "inventory": session.player_state.inventory,  # pyright: ignore
        "available_challenges": get_visible_challenges(session),
        "combat": get_combat_context(session),
        "party": get_party_context(session),
    }


def build_dialogue_context(
    session: GameSession, action: AIActionResponse, turn: GameTurn
):
    target_slug = action.target_slug

    npc = (
        NPCState.objects.filter(
            session=session,
            npc__slug=target_slug,
        )
        .select_related("npc")
        .first()
    )

    memories = get_npc_memories(npc)

    npc_data = npc.npc.data

    return {
        "npc": {
            "name": npc.npc.name,
            "personality": npc_data.get("personality", ""),
            "role": npc_data.get("role", ""),
            "hostility": npc.hostility,
            "trust": npc.trust,
            "recruited": npc.is_recruited,
        },
        "memories": memories,
        "permitted_knowledge": get_permitted_knowledge(
            session,
            npc,
        ),
        "known_knowledge_ids": sorted(
            get_discovered_knowledge_ids(session),
        ),
        "player_message": action.message,
        "location": {
            "slug": session.current_location.slug,  # pyright: ignore
            "name": session.current_location.name,
        },
        "interaction_result": turn.mechanical_result.get("interaction", {}),  # pyright: ignore
    }


def build_narrator_context(session: GameSession, turn: GameTurn):
    location = session.current_location

    npcs = NPCState.objects.filter(
        session=session,
        current_location=location,
    ).select_related("npc")

    visible_npcs = []

    for state in npcs:
        if not state.alive:
            continue

        npc_data = state.npc.data

        visible_npcs.append(
            {
                "slug": state.npc.slug,
                "name": state.npc.name,
                "personality": npc_data.get("personality", ""),
                "role": npc_data.get("role", ""),
                "hostility": state.hostility,
                "trust": state.trust,
                "public_knowledge": npc_data.get("public_knowledge", []),
                "recruited": state.is_recruited,
            }
        )

    previous_turns = GameTurn.objects.filter(
        session=session,
        number__lt=turn.number,
    ).order_by("-number")[:15]

    history = [
        {
            "player_action": previous.player_action,
            "narration": previous.narration,
        }
        for previous in reversed(list(previous_turns))
    ]

    return {
        "location": {
            "slug": location.slug,  # pyright: ignore
            "name": location.name,
            "description": location.description,
        },
        "interactables": get_visible_interactables(session),
        "npcs": visible_npcs,
        "player": {
            "level": session.player_state.level,  # pyright:ignore
            "experience": session.player_state.experience,  # pyright:ignore
            "hp": session.player_state.hp,  # pyright: ignore
            "max_hp": session.player_state.max_hp,  # pyright: ignore
            "gold": session.player_state.gold,  # pyright:ignore
            "proficiency_bonus": session.player_state.proficiency_bonus,  # pyright:ignore
            "inventory": session.player_state.inventory,  # pyright: ignore
        },
        "history": history,
        "action": {
            "original_message": turn.original_message,
            "player_action": turn.player_action,
            "mechanical_result": turn.mechanical_result,
        },
        "quests": get_visible_quests(session),
        "discovered_knowledge": get_discovered_knowledge(session),
        "combat": get_combat_context(session),
        "party": get_party_context(session),
    }
