from django.core.exceptions import ValidationError

from game.combat import end_encounter
from game.events import GameEvent
from game.models import (
    CombatEncounter,
    CombatParticipant,
    CombatStatus,
    CombatTeam,
    GameSession,
    NPCState,
    PlayerState,
)
from world.models import Item, Location
from world.schemas import NPCSurrenderSchema


def get_surrender_config(
    npc: NPCState,
) -> NPCSurrenderSchema:
    configuration = npc.npc.data.get("combat", {}).get("surrender", {})  # pyright: ignore

    return NPCSurrenderSchema.model_validate(configuration)


def accepts_surrender(
    *,
    npc: NPCState,
    config: NPCSurrenderSchema,
) -> bool:
    if config.outcome == "refusal":
        return False

    return (
        config.minimum_trust is None
        or npc.trust < config.minimum_trust
        and config.maximum_hostility is None
        or npc.hostility < config.maximum_hostility
    )


def get_surrender_opponents(
    encounter: CombatEncounter,
) -> list[CombatParticipant]:
    participants = (
        CombatParticipant.objects.filter(
            encounter=encounter,
            team=CombatTeam.ENEMY,
            withdrawn=False,
        )
        .select_related("npc", "npc__npc")
        .order_by("position")
    )

    return [
        participant
        for participant in participants
        if participant.can_act and participant.npc is not None
    ]


def evaluate_surrender(
    *,
    encounter: CombatEncounter,
) -> dict:
    opponents = get_surrender_opponents(encounter=encounter)

    if not opponents:
        raise ValueError("No opponents available to surrender to.")

    refusing = []
    candidates = []

    for participant in opponents:
        npc = participant.npc

        if npc is None:
            continue

        config = get_surrender_config(npc=npc)  # pyright: ignore

        if not accepts_surrender(npc=npc, config=config):  # pyright: ignore
            refusing.append(npc.npc.slug)  # pyright: ignore

        candidates.append((participant, config))

    if refusing:
        return {
            "accepted": False,
            "refusing_opponents": refusing,
            "opponent": None,
            "config": None,
        }

    leader, config = max(
        candidates,
        key=lambda candidate: (
            candidate[1].priority,  # pyright: ignore
            -candidate[0].position,  # pyright: ignore
        ),
    )

    return {
        "accepted": True,
        "refusing_opponents": [],
        "opponent": leader.npc,
        "config": config,
    }


def confiscate_gold(
    *,
    player: PlayerState,
    amount: int,
) -> int:
    removed = min(
        player.gold,
        amount,
    )

    player.gold -= removed  # pyright: ignore

    player.save(update_fields=["gold"])

    return removed  # pyright: ignore


def confiscate_weapon(
    *,
    session: GameSession,
    player: PlayerState,
) -> str | None:
    inventory = list(player.inventory)  # pyright: ignore

    weapon_slugs = set(
        Item.objects.filter(
            campaign=session.campaign,
            slug__in=inventory,
            data__type="weapon",
        ).values_list(
            "slug",
            flat=True,
        )
    )

    weapon = next(
        (slug for slug in inventory if slug in weapon_slugs),
        None,
    )

    if weapon is None:
        return None

    inventory.remove(weapon)

    player.inventory = inventory  # pyright: ignore

    player.save(update_fields=["inventory"])

    return weapon


def relocate_captured_player(
    *,
    session: GameSession,
    destination_slug: str,
) -> Location:
    destination = Location.objects.filter(
        campaign=session.campaign,
        slug=destination_slug,
    ).first()

    if destination is None:
        raise ValidationError("Invalid captivity destination.")

    session.current_location = destination

    session.save(
        update_fields=[
            "current_location",
            "updated_at",
        ]
    )

    return destination


def resolve_player_surrender(
    *,
    session: GameSession,
    encounter: CombatEncounter,
    participant: CombatParticipant,
    player: PlayerState,
) -> tuple[dict, list[GameEvent]]:
    decision = evaluate_surrender(
        encounter=encounter,
    )

    result = {
        "type": "player_surrender",
        "round": encounter.round_number,
        "success": decision["accepted"],
        "outcome": "refusal",
        "opponent": None,
        "opponent_name": None,
        "refusing_opponents": (decision["refusing_opponents"]),
        "gold_lost": 0,
        "items_lost": [],
        "destination": None,
        "surrendered_companions": [],
    }

    events: list[GameEvent] = []

    if not decision["accepted"]:
        events.append(
            GameEvent(
                type="surrender_refused",
                encounter=encounter.pk,
                location=encounter.location.slug,  # pyright: ignore
            )
        )

        return result, events

    opponent = decision["opponent"]
    config = decision["config"]

    result.update(
        {
            "outcome": config.outcome,
            "opponent": opponent.npc.slug,
            "opponent_name": opponent.npc.name,
        }
    )

    match config.outcome:
        case "mercy":
            pass

        case "extortion":
            result["gold_lost"] = confiscate_gold(
                player=player,
                amount=config.gold_amount,
            )

        case "confiscation":
            weapon = confiscate_weapon(
                session=session,
                player=player,
            )

            if weapon is not None:
                result["items_lost"].append(weapon)

        case "captivity":
            if config.destination is None:
                raise ValidationError("Missing captivity destination.")

            destination = relocate_captured_player(
                session=session,
                destination_slug=config.destination,
            )

            result["destination"] = destination.slug

            events.append(
                GameEvent(
                    type="player_captured",
                    encounter=encounter.pk,
                    npc=opponent.npc.slug,
                    location=encounter.location.slug,  # pyright: ignore
                    value=destination.slug,  # pyright: ignore
                )
            )

            events.append(
                GameEvent(
                    type="location_entered",
                    target=destination.slug,  # pyright: ignore
                    location=destination.slug,  # pyright: ignore
                )
            )

    participant.withdrawn = True  # pyright: ignore
    participant.defending = False  # pyright: ignore

    participant.save(
        update_fields=[
            "withdrawn",
            "defending",
        ]
    )

    result["surrendered_companions"] = surrender_companions(
        encounter=encounter,
    )

    end_encounter(
        encounter=encounter,
        status=CombatStatus.SURRENDERED,
    )

    events.append(
        GameEvent(
            type="surrender_accepted",
            encounter=encounter.pk,
            npc=opponent.npc.slug,
            location=encounter.location.slug,  # pyright: ignore
            value=config.outcome,
        )
    )

    return result, events


def surrender_companions(
    *,
    encounter: CombatEncounter,
) -> list[str]:
    participants = CombatParticipant.objects.filter(
        encounter=encounter,
        team=CombatTeam.ALLY,
        withdrawn=False,
    ).select_related("npc", "npc__npc")

    surrendered = []

    for participant in participants:
        if not participant.can_act:
            continue

        if participant.npc is None:
            continue

        participant.withdrawn = True
        participant.defending = False

        participant.state = {
            **participant.state,
            "withdrawal_reason": "party_surrendered",
        }

        participant.save(
            update_fields=[
                "withdrawn",
                "defending",
                "state",
            ]
        )

        surrendered.append(participant.npc.npc.slug)

    return surrendered
