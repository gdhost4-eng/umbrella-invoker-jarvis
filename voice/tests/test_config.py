from pathlib import Path

import pytest

from jarvisvoice.commands import ComboCmd
from jarvisvoice.config import ConfigError, load_config, parse_config


def minimal_config():
    return {}


@pytest.mark.parametrize(
    "section,values,message",
    [
        ("recognition", {"model": None}, "recognition.model"),
        ("recognition", {"log_prob_threshold": float("nan")}, "конечное число"),
        ("recognition", {"log_prob_threshold": float("-inf")}, "конечное число"),
        ("microphone", {"preroll_ms": -1}, "microphone.preroll_ms"),
        ("microphone", {"min_speech_ms": 9000}, "microphone.min_speech_ms"),
        ("bridge", {"port": 80}, "bridge.port"),
        ("wake", {"window_s": 0}, "wake.window_s"),
        ("wake", {"say": []}, "wake.say"),
        ("wake", {"enabled": "yes"}, "true или false"),
        ("wake", {"word": "джарвис"}, "неизвестные параметры"),
        ("timing", {"invoke_only_window_ms": True}, "целое число"),
        ("binds", {"quas": "Q"}, "неизвестные параметры"),
    ],
)
def test_invalid_values_report_their_location(section, values, message):
    raw = minimal_config()
    raw[section] = values
    with pytest.raises(ConfigError, match=message):
        parse_config(raw, Path("test.yaml"))


@pytest.mark.parametrize("value", [[], {}, 42])
def test_invalid_spell_name_is_a_config_error(value):
    raw = minimal_config()
    raw["spells"] = [{"spell": value, "say": "test"}]
    with pytest.raises(ConfigError, match="неизвестный спелл"):
        parse_config(raw, Path("test.yaml"))


@pytest.mark.parametrize("yaml", ["[]", "false", "0", '""'])
def test_falsey_yaml_roots_are_not_treated_as_empty_mapping(tmp_path, yaml):
    path = tmp_path / "config.yaml"
    path.write_text(yaml, encoding="utf-8")
    with pytest.raises(ConfigError, match="config: ожидается словарь"):
        load_config(path)


def test_unreadable_config_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="конфиг"):
        load_config(tmp_path)


def test_nullable_microphone_device_and_numeric_alias_are_supported():
    raw = minimal_config()
    raw["microphone"] = {"device": None}
    raw["commands"] = [{"press": "quas", "say": [1]}]
    config = parse_config(raw, Path("test.yaml"))
    assert config.microphone.device is None
    assert config.commands[0].say == ("1",)


@pytest.mark.parametrize("entry", [{"press": "courier", "say": "курьер"}, {"keys": "F3", "say": "курьер"}])
def test_press_accepts_only_known_targets(entry):
    raw = minimal_config()
    raw["commands"] = [entry]
    with pytest.raises(ConfigError):
        parse_config(raw, Path("test.yaml"))


def test_combo_is_a_slot_number_or_dynamic():
    raw = minimal_config()
    raw["commands"] = [{"combo": 12, "say": "комбо 12"}, {"combo": "dynamic", "say": "комбо динамик", "label": "Д"}]
    commands = [voice.command for voice in parse_config(raw, Path("test.yaml")).commands if "комбо" in voice.say[0]]
    assert commands == [ComboCmd(12, "Комбо 12"), ComboCmd(0, "Д")]


@pytest.mark.parametrize("slot", [0, 13, "3", True, None])
def test_combo_rejects_unknown_slots(slot):
    raw = minimal_config()
    raw["commands"] = [{"combo": slot, "say": "комбо"}]
    with pytest.raises(ConfigError, match=r"commands\[0\]\.combo"):
        parse_config(raw, Path("test.yaml"))


def test_local_config_overrides_sections_but_keeps_the_rest(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "microphone: {device: null, vad_threshold: 0.6}\nbridge: {port: 50000}\n", encoding="utf-8"
    )
    (tmp_path / "config.local.yaml").write_text("microphone: {device: HyperX}\n", encoding="utf-8")
    config = load_config(tmp_path / "config.yaml")
    assert config.microphone.device == "HyperX"
    assert config.microphone.vad_threshold == 0.6
    assert config.bridge.port == 50000
