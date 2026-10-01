"""Загрузка и проверка config.yaml."""

from __future__ import annotations

import math
from collections.abc import Iterator
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

import yaml

from .commands import (
    COMBO_SLOTS,
    LEARN_TARGETS,
    PRESS_TARGETS,
    SPELLS,
    ComboCmd,
    Command,
    ItemCmd,
    LearnCmd,
    PressCmd,
    SpellCmd,
    StanceCmd,
    SystemCmd,
)

SYSTEM_ACTIONS = {"mute": "мут", "unmute": "снять мут", "sleep": "сон", "quit": "выход"}
DEFAULT_WAKE_WORDS = ("джарвис", "жарвис", "джарвиз", "джервис", "jarvis")
DEFAULT_INVOKE_ONLY_PREFIXES = ("скастуй", "кастуй")
DEFAULT_LEARN_PREFIXES = ("качай", "прокачай")
LOCAL_SUFFIX = ".local"  # config.local.yaml рядом с config.yaml: личные настройки поверх общих


class ConfigError(ValueError):
    """Ошибка в config.yaml с указанием места."""


@dataclass
class MicrophoneCfg:
    device: str | None = None
    vad_threshold: float = 0.5
    vad_silence_ms: int = 250
    preroll_ms: int = 200
    min_speech_ms: int = 150
    max_speech_ms: int = 8000
    realtime: bool = True  # распознавать фразу, пока она звучит, и выполнять команды сразу
    partial_interval_ms: int = 300
    early_silence_ms: int = 90  # пауза, на которой фраза распознаётся досрочно, не дожидаясь vad_silence_ms


@dataclass
class RecognitionCfg:
    model: str = "small"
    device: str = "cuda"
    compute_type: str = "auto"
    language: str = "ru"
    beam_size: int = 1
    use_prompt: bool = True
    no_speech_threshold: float = 0.85  # шум у Whisper — 0.88 и выше, короткие чёткие слова — до 0.6
    log_prob_threshold: float = -1.5
    match_threshold: int = 80
    window_s: float = 30.0  # окно энкодера Whisper; меньше 30 — быстрее, фраза длиннее окна — как обычно
    save_audio: int = 0  # сколько последних фраз хранить в logs/audio (0 — не записывать)


@dataclass
class BridgeCfg:
    port: int = 52360  # тот же порт стоит в меню скрипта


@dataclass
class TimingCfg:
    invoke_only_window_ms: int = 3000


@dataclass
class WakeCfg:
    enabled: bool = True
    say: tuple[str, ...] = DEFAULT_WAKE_WORDS
    window_s: float = 10.0  # сколько бот слушает после обращения и после каждой команды


@dataclass(frozen=True)
class VoiceCommand:
    say: tuple[str, ...]
    command: Command


@dataclass
class Config:
    path: Path
    microphone: MicrophoneCfg
    recognition: RecognitionCfg
    bridge: BridgeCfg
    timing: TimingCfg
    wake: WakeCfg
    invoke_only_prefixes: tuple[str, ...]
    learn_prefixes: tuple[str, ...]
    commands: list[VoiceCommand]
    learn_targets: list[VoiceCommand]
    ignore_words: tuple[str, ...] = ()  # похожи на команды, но не команды: «блин» ≠ «блинк»


_TOP_LEVEL = (
    "microphone",
    "recognition",
    "bridge",
    "timing",
    "wake",
    "invoke_only_prefixes",
    "learn",
    "spells",
    "commands",
    "items",
    "ignore",
)


def _read_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"не удалось прочитать конфиг {path}: {exc}") from exc
    except UnicodeError as exc:
        raise ConfigError(f"{path.name}: ожидается текст UTF-8") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path.name}: ошибка YAML: {exc}") from None


def _merge(base: Any, extra: Any) -> Any:
    """Словари сливаются по ключам, всё остальное (числа, списки) заменяется целиком."""
    if isinstance(base, dict) and isinstance(extra, dict):
        merged = dict(base)
        for key, value in extra.items():
            merged[key] = _merge(base.get(key), value)
        return merged
    return extra


def load_config(path: str | Path) -> Config:
    path = Path(path)
    raw = _read_yaml(path)
    local = path.with_name(path.stem + LOCAL_SUFFIX + path.suffix)
    if local.is_file():
        raw = _merge(_dict(raw, "config"), _dict(_read_yaml(local), local.name))
    return parse_config(raw, path)


def parse_config(raw: Any, path: Path) -> Config:
    data = _dict(raw, "config")
    _check_keys(data, _TOP_LEVEL, "config")

    learn = _dict(data.get("learn"), "learn")
    _check_keys(learn, ("prefixes", "targets"), "learn")

    commands = _parse_spells(data.get("spells"))
    commands += _parse_commands(data.get("commands"))
    commands += _parse_items(data.get("items"))

    config = Config(
        path=path,
        microphone=_section(data, "microphone", MicrophoneCfg),
        recognition=_section(data, "recognition", RecognitionCfg),
        bridge=_section(data, "bridge", BridgeCfg),
        timing=_section(data, "timing", TimingCfg),
        wake=_parse_wake(data.get("wake")),
        invoke_only_prefixes=_aliases(
            data.get("invoke_only_prefixes", list(DEFAULT_INVOKE_ONLY_PREFIXES)), "invoke_only_prefixes"
        ),
        learn_prefixes=_aliases(learn.get("prefixes", list(DEFAULT_LEARN_PREFIXES)), "learn.prefixes"),
        commands=commands,
        learn_targets=_parse_learn_targets(learn.get("targets")),
        ignore_words=_aliases(data["ignore"], "ignore") if data.get("ignore") else (),
    )
    _validate_ranges(config)
    return config


def _validate_ranges(config: Config) -> None:
    checks = [
        (0.0 < config.microphone.vad_threshold < 1.0, "microphone.vad_threshold: от 0 до 1"),
        (config.microphone.vad_silence_ms >= 60, "microphone.vad_silence_ms: не меньше 60"),
        (config.microphone.max_speech_ms >= 500, "microphone.max_speech_ms: не меньше 500"),
        (config.microphone.preroll_ms >= 0, "microphone.preroll_ms: не может быть отрицательным"),
        (
            0 <= config.microphone.min_speech_ms <= config.microphone.max_speech_ms,
            "microphone.min_speech_ms: от 0 до max_speech_ms",
        ),
        (config.microphone.partial_interval_ms >= 100, "microphone.partial_interval_ms: не меньше 100"),
        (config.microphone.early_silence_ms >= 0, "microphone.early_silence_ms: не может быть отрицательным"),
        (config.recognition.device in ("cuda", "cpu"), "recognition.device: cuda или cpu"),
        (config.recognition.beam_size >= 1, "recognition.beam_size: не меньше 1"),
        (0 <= config.recognition.no_speech_threshold <= 1, "recognition.no_speech_threshold: от 0 до 1"),
        (config.recognition.log_prob_threshold <= 0, "recognition.log_prob_threshold: не больше 0"),
        (50 <= config.recognition.match_threshold <= 100, "recognition.match_threshold: от 50 до 100"),
        (config.recognition.save_audio >= 0, "recognition.save_audio: не может быть отрицательным"),
        (5 <= config.recognition.window_s <= 30, "recognition.window_s: от 5 до 30"),
        (1024 <= config.bridge.port <= 65535, "bridge.port: от 1024 до 65535"),
        (config.wake.window_s >= 1, "wake.window_s: не меньше 1"),
        (config.timing.invoke_only_window_ms >= 0, "timing.invoke_only_window_ms: не может быть отрицательным"),
    ]
    for ok, message in checks:
        if not ok:
            raise ConfigError(message)


def _dict(value: Any, ctx: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{ctx}: ожидается словарь")
    return value


def _list(value: Any, ctx: str) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(f"{ctx}: ожидается список")
    return value


def _check_keys(data: dict, allowed: tuple[str, ...] | list[str], ctx: str) -> None:
    extra = set(map(str, data)) - set(allowed)
    if extra:
        raise ConfigError(f"{ctx}: неизвестные параметры: {', '.join(sorted(extra))}")


def _bool(value: Any, ctx: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{ctx}: ожидается true или false")
    return value


def _section(raw: dict, key: str, cls: type) -> Any:
    data = _dict(raw.get(key), key)
    _check_keys(data, [f.name for f in fields(cls)], key)
    defaults = cls()
    return cls(
        **{name: _section_value(value, getattr(defaults, name), f"{key}.{name}") for name, value in data.items()}
    )


def _section_value(value: Any, default: Any, ctx: str) -> Any:
    if isinstance(default, bool):
        return _bool(value, ctx)
    if isinstance(default, int):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{ctx}: ожидается целое число")
        return value
    if isinstance(default, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ConfigError(f"{ctx}: ожидается конечное число")
        return float(value)
    if value is None and default is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ConfigError(f"{ctx}: ожидается строка")
    return str(value)


def _entries(value: Any, section: str, allowed: tuple[str, ...]) -> Iterator[tuple[dict, str]]:
    """Проверяет общую структуру списков голосовых команд."""
    for index, raw in enumerate(_list(value, section)):
        ctx = f"{section}[{index}]"
        entry = _dict(raw, ctx)
        _check_keys(entry, allowed, ctx)
        yield entry, ctx


def _target(value: Any, allowed: tuple[str, ...], ctx: str) -> str:
    name = str(value)
    if name not in allowed:
        raise ConfigError(f"{ctx}.press: неизвестная цель {name!r}; варианты: {', '.join(allowed)}")
    return name


def _aliases(value: Any, ctx: str) -> tuple[str, ...]:
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        value = [value]
    out = []
    for i, alias in enumerate(_list(value, ctx)):
        if isinstance(alias, int) and not isinstance(alias, bool):
            alias = str(alias)
        if not isinstance(alias, str) or not alias.strip():
            raise ConfigError(f"{ctx}[{i}]: пустая или не строковая фраза")
        out.append(alias.strip())
    if not out:
        raise ConfigError(f"{ctx}: нужна хотя бы одна фраза")
    return tuple(out)


def _label(value: Any, default: str, ctx: str) -> str:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ConfigError(f"{ctx}.label: ожидается строка")
    return str(value)


def _orbs(value: Any, ctx: str) -> str:
    orbs = str(value).strip().upper()
    if len(orbs) != 3 or any(orb not in "QWE" for orb in orbs):
        raise ConfigError(f"{ctx}: ожидаются 3 сферы из Q/W/E, например EEE")
    return orbs


def _combo_slot(value: Any, ctx: str) -> int:
    """Номер комбо в сборщике Umbrella; dynamic (0) — динамическое."""
    if value == "dynamic":
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= COMBO_SLOTS:
        raise ConfigError(f"{ctx}: ожидается номер комбо от 1 до {COMBO_SLOTS} или dynamic")
    return value


def _parse_spells(value: Any) -> list[VoiceCommand]:
    out = []
    for entry, ctx in _entries(value, "spells", ("spell", "say", "self_cast", "label")):
        spell = entry.get("spell")
        if not isinstance(spell, str) or spell not in SPELLS:
            raise ConfigError(f"{ctx}.spell: неизвестный спелл {spell!r}; варианты: {', '.join(SPELLS)}")
        self_cast = _bool(entry.get("self_cast", False), f"{ctx}.self_cast")
        default_label = SPELLS[spell].title + (" на себя" if self_cast else "")
        command = SpellCmd(spell, self_cast, False, _label(entry.get("label"), default_label, ctx))
        out.append(VoiceCommand(_aliases(entry.get("say"), f"{ctx}.say"), command))
    return out


def _parse_commands(value: Any) -> list[VoiceCommand]:
    out = []
    for entry, ctx in _entries(value, "commands", ("say", "press", "stance", "combo", "system", "label", "double")):
        say = _aliases(entry.get("say"), f"{ctx}.say")
        kinds = [kind for kind in ("press", "stance", "combo", "system") if kind in entry]
        if len(kinds) != 1:
            raise ConfigError(f"{ctx}: нужен ровно один из параметров press, stance, combo, system")
        kind = kinds[0]
        raw = entry[kind]
        label = entry.get("label")
        double = _bool(entry.get("double", False), f"{ctx}.double")
        if double and kind != "press":
            raise ConfigError(f"{ctx}.double: только для press")
        command: Command
        if kind == "press":
            command = PressCmd(_target(raw, PRESS_TARGETS, ctx), _label(label, say[0], ctx), double)
        elif kind == "stance":
            orbs = _orbs(raw, f"{ctx}.stance")
            command = StanceCmd(orbs, _label(label, f"сферы {orbs}", ctx))
        elif kind == "combo":
            slot = _combo_slot(raw, f"{ctx}.combo")
            command = ComboCmd(slot, _label(label, f"Комбо {slot}" if slot else "Комбо динамик", ctx))
        else:
            if raw not in SYSTEM_ACTIONS:
                raise ConfigError(f"{ctx}.system: неизвестное действие {raw!r}; варианты: {', '.join(SYSTEM_ACTIONS)}")
            command = SystemCmd(raw, _label(label, SYSTEM_ACTIONS[raw], ctx))
        out.append(VoiceCommand(say, command))
    return out


def _parse_wake(value: Any) -> WakeCfg:
    data = _dict(value, "wake")
    _check_keys(data, [f.name for f in fields(WakeCfg)], "wake")
    defaults = WakeCfg()
    return WakeCfg(
        enabled=_bool(data.get("enabled", defaults.enabled), "wake.enabled"),
        say=_aliases(data["say"], "wake.say") if "say" in data else defaults.say,
        window_s=_section_value(data.get("window_s", defaults.window_s), defaults.window_s, "wake.window_s"),
    )


def _parse_learn_targets(value: Any) -> list[VoiceCommand]:
    out = []
    for entry, ctx in _entries(value, "learn.targets", ("say", "press", "label")):
        say = _aliases(entry.get("say"), f"{ctx}.say")
        name = _target(entry.get("press"), LEARN_TARGETS, ctx)
        out.append(VoiceCommand(say, LearnCmd(name, _label(entry.get("label"), f"прокачка {say[0]}", ctx))))
    return out


def _parse_items(value: Any) -> list[VoiceCommand]:
    out = []
    for entry, ctx in _entries(value, "items", ("names", "say", "self_say", "label")):
        names = _aliases(entry.get("names"), f"{ctx}.names")
        for name in names:
            if not name.startswith("item_"):
                raise ConfigError(f"{ctx}.names: {name!r} — внутреннее имя предмета начинается с item_")
        say = _aliases(entry.get("say"), f"{ctx}.say")
        label = _label(entry.get("label"), say[0], ctx)
        out.append(VoiceCommand(say, ItemCmd(names, False, label)))
        if "self_say" in entry:
            self_say = _aliases(entry["self_say"], f"{ctx}.self_say")
            out.append(VoiceCommand(self_say, ItemCmd(names, True, f"{label} на себя")))
    return out
