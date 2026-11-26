from django.core.exceptions import ValidationError
from django.db import transaction

from game.events import GameEvent
from game.models import GameSession, GameTurn, KnowledgeDiscovery, NPCState
from game.quests import process_game_events


def get_permitted_knowledge(
    session: GameSession, npc_state: NPCState
) -> list[str | dict[str, str]]:
    npc_data = npc_state.npc.data  # pyright: ignore

    permitted = list(npc_data.get("public_knowledge", []))

    for entry in npc_data.get("knowledge", []):
        conditions = entry.get("conditions", {})

        if npc_state.trust < conditions.get("min_trust", 0):
            continue

        if npc_state.hostility > conditions.get("max_hostility", 100):
            continue

        required_flags = conditions.get("required_flags", [])

        if not all(
            session.flags.get(flag, False)  # pyright: ignore
            for flag in required_flags
        ):
            continue

        permitted.append(
            {
                "id": entry["id"],
                "text": entry["text"],
            }
        )

    return permitted


def get_discovered_knowledge(
    session: GameSession,
) -> list[dict]:
    discoveries = (
        KnowledgeDiscovery.objects.filter(session=session)  # pyright:ignore
        .select_related("source__npc")
        .order_by("created_at")
    )

    return [
        {
            "id": discovery.knowledge_id,
            "text": discovery.text,
            "source": discovery.source.npc.name,
        }
        for discovery in discoveries
    ]


def get_discovered_knowledge_ids(session: GameSession) -> set[str]:
    return set(
        KnowledgeDiscovery.objects.filter(session=session).values_list(  # pyright:ignore
            "knowledge_ids",
            flat=True,
        )
    )


@transaction.atomic
def record_knowledge_disclosures(
    *,
    turn: GameTurn,
    npc_state: NPCState,
    permitted_knowledge: list[str | dict[str, str]],
    disclosed_ids: list[str],
) -> tuple[list[dict], list[GameEvent]]:
    if npc_state.session_id != turn.session_id:  # pyright:ignore
        raise ValidationError("NPC belongs to another session")

    permitted = {
        knowledge["id"]: knowledge
        for knowledge in permitted_knowledge
        if isinstance(knowledge, dict)
    }

    invalid_ids = set(disclosed_ids) - permitted.keys()

    if invalid_ids:
        raise ValidationError("AI attempted unauthorized knowledge disclosure.")

    discoveries = []
    events = []

    for knowledge_id in dict.fromkeys(disclosed_ids):
        knowledge = permitted[knowledge_id]

        discovery, created = KnowledgeDiscovery.objects.get_or_create(  # pyright:ignore
            session=turn.session,
            knowledge_id=knowledge_id,
            defaults={
                "text": knowledge["text"],
                "source": npc_state,
                "turn": turn,
            },
        )

        if not created:
            continue

        discoveries.append(
            {
                "id": discovery.id,
                "text": discovery.text,
                "source": npc_state.npc.name,
            }
        )

        events.append(
            GameEvent(
                type="knowledge_discovered",
                knowledge=knowledge_id,
                npc=npc_state.npc.slug,  # pyright:ignore
                target=knowledge_id,
                location=npc_state.current_location.slug,  # pyright:ignore
            )
        )

    return discoveries, events


@transaction.atomic
def finalize_dialogue(
    *, turn: GameTurn, npc_state: NPCState, dialogue: str, disclosed_ids: list[str]
) -> GameTurn:
    session = GameSession.objects.select_for_update().get(
        pk=turn.session_id,  # pyright:ignore
    )

    turn = GameTurn.objects.select_for_update().get(
        pk=turn.pk,
        session=session,
    )

    result = dict(turn.mechanical_result)  # pyright:ignore

    if result.get("dialogue_finalized"):  # pyright:ignore
        return turn

    npc_state = NPCState.objects.get(
        pk=npc_state.pk,
        session=session,
    )

    permitted_knowledge = get_permitted_knowledge(session=session, npc_state=npc_state)

    discoveries, events = record_knowledge_disclosures(
        turn=turn,  # pyright:ignore
        npc_state=npc_state,  # pyright:ignore
        permitted_knowledge=permitted_knowledge,  # pyright:ignore
        disclosed_ids=disclosed_ids,  # pyright:ignore
    )

    quest_updates, processed_events = process_game_events(
        session=session,  # pyright:ignore
        events=events,  # pyright:ignore
    )

    result["response"] = dialogue  # pyright:ignore
    result["knowledge_discoveries"] = discoveries  # pyright:ignore

    result["events"] = [  # pyright:ignore
        *result.get("events", []),  # pyright:ignore
        *[event.model_dump(mode="json") for event in processed_events],
    ]

    result["quest_updates"] = [  # pyright:ignore
        *result.get("quest_updates", []),  # pyright:ignore
        *quest_updates,
    ]

    result["dialogue_finalized"] = True  # pyright:ignore

    turn.mechanical_result = result  # pyright:ignore

    turn.save(
        update_fields=["mechanical_result"],
    )

    return turn
