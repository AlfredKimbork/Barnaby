from dataclasses import asdict
from uuid import UUID

from django.db import transaction
from pydantic import ValidationError

from game.checks import resolve_skill_check
from game.events import GameEvent
from game.models import ChallengeState, GameSession, GameTurn, PlayerState
from game.turns import create_turn, get_existing_turn


def get_challenge(
    session: GameSession,
    target_slug: str,
) -> dict | None:
    challenges = session.current_location.data.get(  # pyright:ignore
        "challenges",
        [],
    )

    return next(
        (challenge for challenge in challenges if challenge["id"] == target_slug), None
    )


def get_approach(challenge: dict, approach_slug: str) -> dict | None:
    return next(
        (approach for approach in challenge["approaches"] if approach["id"] == approach_slug),
        None,
    )


def apply_outcome(
    *,
    session: GameSession,
    player: PlayerState,
    outcome: dict,
) -> tuple[list[dict], list[GameEvent]]:
    consequences = []
    events = []

    damage = min(
        player.hp,
        outcome.get("damage", 0),
    )

    if damage:
        player.hp -= damage  # pyright:ignore

        player.save(update_fields=["hp"])

        consequences.append(
            {
                "type": "damage_taken",
                "damage": damage,
                "remaining_hp": player.hp,
            }
        )

    flags = dict(session.flags)  # pyright:ignore

    for flag, value in outcome.get("flags", {}).items():
        if flag in flags and flag[flag] == value:
            continue

        flags[flag] = value

        events.append(
            GameEvent(
                type="flag_changed",
                flag=flag,
                value=value,
            )
        )

    if flags != session.flags:
        session.flags = flags  # pyright:ignore
        session.save(update_fields=["flags"])

    return consequences, events


def resolve_challenge(
    *,
    session: GameSession,
    player: PlayerState,
    challenge: dict,
    approach: dict,
    result: dict,
) -> tuple[dict, list[GameEvent]]:
    state, _ = ChallengeState.objects.get_or_create(
        session=session,
        location=session.current_location,
        challenge_slug=challenge["id"],
        approach_slug=approach["id"],
    )

    if state.completed and not approach.get("repeatable", False):
        result["status"] = "already_completed"

        result["description"] = ("You have already completed this challenge",)

        return result, []

    skill = approach.get("skill")

    if skill is None:
        success = True
    else:
        check = resolve_skill_check(
            player=player,
            skill=skill,
            difficulty=approach["difficulty"],
        )

        result["check"] = asdict(check)

        success = check.success

    outcome = approach["success"] if success else approach["failure"]

    state.attempts += 1

    if success:
        state.successes += 1
    else:
        state.failures += 1

    state.save(
        update_fields=[
            "attempts",
            "successes",
            "failures",
        ],
    )

    consequences, events = apply_outcome(
        session=session,
        player=player,
        outcome=outcome,
    )

    result.update(
        {
            "status": "resolved",
            "success": success,
            "description": outcome["description"],
            "consequences": consequences,
        }
    )

    events.append(
        GameEvent(
            type=("challange_succeeded" if success else "challenge_failed"),
            target=challenge["id"],
            approach=approach["id"],
            location=session.current_location.slug,  # pyright:ignore
        )
    )

    return result, events


@transaction.atomic
def process_skill_check(
    *,
    session: GameSession,
    action_id: UUID,
    target_slug: str,
    approach_slug: str,
    intent: str,
    original_message: str = "",
) -> GameTurn:
    session = GameSession.objects.select_for_update().get(pk=session.pk)

    existing = get_existing_turn(
        session=session,
        action_id=action_id,
        action_type="skill_check",
        payload={
            "target": target_slug,
            "approach": approach_slug,
        },
    )

    if existing:
        return existing

    player = PlayerState.objects.select_for_update().get(session=session)

    if session.status != "active":
        raise ValidationError("Session is not active.")

    if player.hp <= 0:
        raise ValidationError("Player cannot perform actions.")

    result = {
        "type": "skill_check",
        "target": target_slug,
        "approach": approach_slug,
        "intent": intent,
        "status": "rejected",
        "success": False,
        "check": None,
        "consequences": [],
    }

    events = []

    challenge = get_challenge(
        session=session,
        target_slug=target_slug,
    )

    if challenge is None:
        result["description"] = ("That target is not available here.",)
    else:
        approach = get_approach(
            challenge=challenge,
            approach_slug=approach_slug,
        )

        if approach is None:
            result["description"] = ("that approach is not available.",)

        else:
            result, events = resolve_challenge(
                session=session,
                player=player,
                challenge=challenge,
                approach=approach,
                result=result,
            )
    return create_turn(
        session=session,  # pyright:ignore
        action_id=action_id,  # pyright:ignore
        player_action=intent,  # pyright:ignore
        original_message=original_message,  # pyright:ignore
        narration="",  # pyright:ignore
        mechanical_result=result,  # pyright:ignore
        events=events,  # pyright:ignore
    )


@transaction.atomic
def process_roleplay(
    *,
    session: GameSession,
    action_id: UUID,
    description: str,
    original_message: str,
) -> GameTurn:
    session = GameSession.objects.select_for_update().get(pk=session.pk)

    existing = get_existing_turn(
        session=session,
        action_id=action_id,
        action_type="roleplay",
        payload={
            "original_message": original_message,
        },
    )

    if existing:
        return existing

    player = PlayerState.objects.get(session=session)

    if session.status != "active":
        raise ValidationError("Session is not active.")

    if player.hp <= 0:
        raise ValidationError("Player cannot perform actions.")

    return create_turn(
        session=session,  # pyright:ignore
        action_id=action_id,  # pyright:ignore
        original_message=original_message,  # pyright:ignore
        player_action=description,  # pyright:ignore
        narration="",  # pyright:ignore
        mechanical_result={  # pyright:ignore
            "type": "roleplay",
            "description": description,
            "status": "narrative_only",
            "consequences": [],
        },
    )


def get_visible_challenges(session: GameSession) -> list[dict]:
    challenges = session.current_location.data.get(  # pyright:ignore
        "challenges",
        [],
    )
    states = {
        (state.challenge_slug, state.approach_slug): state
        for state in ChallengeState.objects.filter(
            session=session,
            location=session.current_location,
        )
    }

    visible = []

    for challenge in challenges:
        approaches = []

        for approach in challenge["approaches"]:
            state = states.get(
                (
                    challenge["id"],
                    approach["id"],
                )
            )

            approaches.append(
                {
                    "id": approach["id"],
                    "description": approach["description"],
                    "skill": approach.get("skill"),
                    "completed": bool(state and state.completed),
                    "repeatable": approach.get(
                        "repeatable",
                        False,
                    ),
                }
            )

        visible.append(
            {
                "id": challenge["id"],
                "description": challenge["description"],
                "approaches": approaches,
            }
        )

    return visible
