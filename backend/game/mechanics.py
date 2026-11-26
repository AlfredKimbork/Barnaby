import re
from dataclasses import dataclass
from random import randint

from game.checks import RollMode, resolve_roll_mode, roll_d20
from game.models import NPCState, PlayerState


@dataclass(frozen=True)
class InitiativeResult:
    roll: int
    modifier: int
    total: int


def roll_initiative(modifier: int) -> InitiativeResult:
    roll, _ = roll_d20()

    return InitiativeResult(
        roll=roll,
        modifier=modifier,
        total=roll + modifier,
    )


DICE_PATTERN = re.compile(r"^([1-9]\d*)d([1-9]\d*)$")


def parse_damage_dice(expression: str) -> tuple[int, int]:
    match = DICE_PATTERN.fullmatch(expression.lower().strip())

    if match is None:
        raise ValueError(f"Invalid damage dice: {expression}")

    count, sides = map(int, match.groups())

    if count > 20 or sides > 100:
        raise ValueError(f"Damage dice out of bounds: {expression}")

    return count, sides


def roll_damage(expression: str, *, critical: bool = False) -> tuple[int, ...]:
    count, sides = parse_damage_dice(expression)

    if critical:
        count *= 2

    return tuple(randint(1, sides) for _ in range(count))


@dataclass(frozen=True)
class AttackResult:
    roll: int
    modifier: int
    total: int
    defense: int
    hit: bool
    damage: int


def resolve_attack(
    *,
    strength: int,
    defense: int,
    weapon_damage: int,
) -> AttackResult:
    roll, _rolls = roll_d20()
    modifier = (strength - 10) // 2
    total = roll + modifier

    # Natural 1 always misses; natural 20 always hits.
    hit = roll == 20 or (roll != 1 and total >= defense)

    damage = max(0, weapon_damage + modifier) if hit else 0

    return AttackResult(
        roll=roll,
        modifier=modifier,
        total=total,
        defense=defense,
        hit=hit,
        damage=damage,
    )


@dataclass(frozen=True)
class CombatAttackResult:
    roll: int
    rolls: tuple[int, ...]
    roll_mode: RollMode

    modifier: int
    total: int
    defense: int

    hit: bool
    critical: bool

    damage_dice: str
    damage_rolls: tuple[int, ...]
    damage_modifier: int
    damage: int


def resolve_combat_attack(
    *,
    attack_modifier: int,
    damage_modifier: int,
    damage_dice: str,
    defense: int,
    advantage: bool = False,
    disadvantage: bool = False,
) -> CombatAttackResult:
    parse_damage_dice(damage_dice)

    mode = resolve_roll_mode(advantage=advantage, disadvantage=disadvantage)

    roll, rolls = roll_d20(mode=mode)

    total = roll + attack_modifier
    critical = roll == 20

    hit = critical or (roll != 1 and total >= defense)

    damage_rolls: tuple[int, ...] = ()
    damage = 0

    if hit:
        damage_rolls = roll_damage(
            damage_dice,
            critical=critical,
        )

        damage = max(
            0,
            sum(damage_rolls) + damage_modifier,
        )

    return CombatAttackResult(
        roll=roll,
        rolls=rolls,
        roll_mode=mode,
        modifier=attack_modifier,
        total=total,
        defense=defense,
        hit=hit,
        critical=critical,
        damage_dice=damage_dice,
        damage_rolls=damage_rolls,
        damage_modifier=damage_modifier,
        damage=damage,
    )


def get_weapon_modifier(
    *,
    player: PlayerState,
    properties: dict,
) -> int:
    attribute = properties.get(
        "damage_attribute",
        "strength",
    )

    if attribute == "finesse":
        return max(
            player.attribute_modifier("strength"),
            player.attribute_modifier("dexterity"),
        )

    if attribute not in {"strength", "dexterity"}:
        raise ValueError(f"Invalid damage attribute: {attribute}")

    return player.attribute_modifier(attribute)


def get_player_defense(player: PlayerState) -> int:
    return max(
        1,
        10 + player.attribute_modifier("dexterity"),
    )


def get_npc_defense(
    npc_state: NPCState,
) -> int:
    return npc_state.state.get(  # pyright: ignore
        "defense",
        npc_state.npc.data.get("initial_state", {}).get("defense", 10),  # pyright: ignore
    )
