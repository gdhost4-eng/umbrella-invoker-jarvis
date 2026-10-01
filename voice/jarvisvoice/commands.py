"""Типы команд и таблица спеллов Инвокера (данные клиента 6933, 15.09.2026)."""

from __future__ import annotations

from dataclasses import dataclass

ITEM_SLOTS = 6
# Что можно «нажать» голосом: выполняет скрипт в игре, клавиши Доты тут ни при чём.
PRESS_TARGETS = (
    "quas",
    "wex",
    "exort",
    "slot_d",
    "slot_f",
    "invoke",
    *(f"item{i + 1}" for i in range(ITEM_SLOTS)),
    "tp",
    "neutral",
    "stop",
    "hold",
)
LEARN_TARGETS = ("quas", "wex", "exort")
COMBO_SLOTS = 12  # столько комбо в сборщике встроенного скрипта Invoker в Umbrella (режим «12»)


@dataclass(frozen=True)
class Spell:
    id: str
    ability: str  # внутреннее имя способности в игре
    orbs: str  # комбинация сфер, например "EEW"
    title: str


SPELLS: dict[str, Spell] = {
    s.id: s
    for s in (
        Spell("cold_snap", "invoker_cold_snap", "QQQ", "Cold Snap"),
        Spell("ghost_walk", "invoker_ghost_walk", "QQW", "Ghost Walk"),
        Spell("ice_wall", "invoker_ice_wall", "QQE", "Ice Wall"),
        Spell("emp", "invoker_emp", "WWW", "EMP"),
        Spell("tornado", "invoker_tornado", "WWQ", "Tornado"),
        Spell("alacrity", "invoker_alacrity", "WWE", "Alacrity"),
        Spell("sun_strike", "invoker_sun_strike", "EEE", "Sun Strike"),
        Spell("forge_spirit", "invoker_forge_spirit", "EEQ", "Forge Spirit"),
        Spell("chaos_meteor", "invoker_chaos_meteor", "EEW", "Chaos Meteor"),
        Spell("deafening_blast", "invoker_deafening_blast", "QWE", "Deafening Blast"),
    )
}


@dataclass(frozen=True)
class SpellCmd:
    """«санстрайк» — вызвать и применить; invoke_only — только вызвать («скастуй …»)."""

    spell: str
    self_cast: bool = False
    invoke_only: bool = False
    label: str = ""


@dataclass(frozen=True)
class PressCmd:
    """Применить способность или предмет из слота (quas, item1, tp, …); double — на себя («тпаут»)."""

    bind: str
    label: str = ""
    double: bool = False


@dataclass(frozen=True)
class ItemCmd:
    """Предмет по названию: скрипт сам находит его в инвентаре."""

    names: tuple[str, ...]
    self_cast: bool = False
    label: str = ""


@dataclass(frozen=True)
class StanceCmd:
    """Поставить три сферы («экзорт» = EEE) и запомнить стойку."""

    orbs: str
    label: str = ""


@dataclass(frozen=True)
class LearnCmd:
    """Прокачка способности."""

    bind: str
    label: str = ""


@dataclass(frozen=True)
class ComboCmd:
    """Комбо из сборщика Umbrella: slot 1…12, 0 — динамическое. Прокаст ведёт встроенный скрипт Invoker."""

    slot: int
    label: str = ""


@dataclass(frozen=True)
class SystemCmd:
    """Команда самому боту (например, мут микрофона)."""

    action: str
    label: str = ""


Command = SpellCmd | PressCmd | ItemCmd | StanceCmd | LearnCmd | ComboCmd | SystemCmd


def to_wire(command: Command) -> dict:
    """Команда в виде, который понимает скрипт в игре."""
    match command:
        case SpellCmd():
            return {
                "kind": "spell",
                "spell": command.spell,
                "self_cast": command.self_cast,
                "invoke_only": command.invoke_only,
            }
        case PressCmd():
            return {"kind": "press", "bind": command.bind, "double": command.double}
        case ItemCmd():
            return {"kind": "item", "names": list(command.names), "self_cast": command.self_cast}
        case StanceCmd():
            return {"kind": "stance", "orbs": command.orbs}
        case LearnCmd():
            return {"kind": "learn", "bind": command.bind}
        case ComboCmd():
            return {"kind": "combo", "slot": command.slot}
    raise ValueError(f"команда не передаётся в игру: {command!r}")
