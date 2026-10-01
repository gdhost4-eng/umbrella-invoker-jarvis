"""Личные настройки, которые меняются из меню значка: микрофон и автозапуск."""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_NAME = "Jarvis"


def save_microphone(config_path: Path, device: str | None) -> None:
    """Записывает микрофон в config.local.yaml, не трогая остальные личные настройки."""
    local = config_path.with_name(config_path.stem + ".local" + config_path.suffix)
    data = yaml.safe_load(local.read_text(encoding="utf-8")) if local.is_file() else None
    data = data if isinstance(data, dict) else {}
    microphone = data.get("microphone")
    microphone = microphone if isinstance(microphone, dict) else {}
    if device:
        microphone["device"] = device
    else:
        microphone.pop("device", None)
    if microphone:
        data["microphone"] = microphone
    else:
        data.pop("microphone", None)
    header = "# Личные настройки поверх config.yaml (в git не попадает).\n"
    local.write_text(header + yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def unique_devices(devices: list[dict]) -> list[str]:
    """Имена микрофонов без повторов: один и тот же микрофон виден через MME, WASAPI и т. д."""
    seen: dict[str, str] = {}
    for device in devices:
        name = device["name"].strip()
        seen.setdefault(name.lower(), name)
    return list(seen.values())


def launch_command() -> list[str]:
    """Как запустить помощника заново: сам exe или python -m jarvisvoice."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-m", "jarvisvoice"]


def autostart_enabled() -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            winreg.QueryValueEx(key, _RUN_NAME)
        return True
    except OSError:
        return False


def set_autostart(enabled: bool) -> None:
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            command = " ".join(f'"{part}"' for part in launch_command())
            winreg.SetValueEx(key, _RUN_NAME, 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(key, _RUN_NAME)
            except OSError:
                pass
