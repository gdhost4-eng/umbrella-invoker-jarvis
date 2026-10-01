"""Докачка cuBLAS и cuDNN для видеокарты NVIDIA: в exe их нет, чтобы он не весил гигабайты."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)
# Версии те же, что у ctranslate2 4.8.2 (cudnn64_9.dll 9.10.2.21).
PACKAGES = ("nvidia-cublas-cu12", "nvidia-cudnn-cu12==9.10.2.21")
_TIMEOUT_S = 60


def has_nvidia_gpu() -> bool:
    try:
        return subprocess.run(["nvidia-smi", "-L"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _wheel_url(requirement: str) -> str:
    name, _, version = requirement.partition("==")
    path = f"{name}/{version}" if version else name
    with urllib.request.urlopen(f"https://pypi.org/pypi/{path}/json", timeout=_TIMEOUT_S) as reply:
        info = json.load(reply)
    for entry in info["urls"]:
        if entry["filename"].endswith("win_amd64.whl"):
            return entry["url"]
    raise RuntimeError(f"для {requirement} нет колеса под Windows")


def download(target: Path, progress: Callable[[str], None] = lambda text: None) -> None:
    """Скачивает колеса nvidia-* и достает из них DLL в target/<пакет>/bin."""
    for requirement in PACKAGES:
        name = requirement.split("==")[0]
        if any((target / "nvidia" / name.split("-")[1] / "bin").glob("*.dll")):
            continue  # уже скачано при прошлой попытке
        progress(f"качаю {name} (один раз)")
        log.info("качаю %s для видеокарты…", name)
        with tempfile.TemporaryFile() as wheel_file:  # колесо cuDNN весит сотни МБ — в память не читаем
            with urllib.request.urlopen(_wheel_url(requirement), timeout=_TIMEOUT_S) as reply:
                total = int(reply.headers.get("Content-Length") or 0)
                done = 0
                while chunk := reply.read(1 << 20):
                    wheel_file.write(chunk)
                    done += len(chunk)
                    mb = f"{done >> 20} из {total >> 20} МБ" if total else f"{done >> 20} МБ"
                    progress(f"качаю библиотеки видеокарты ({name}): {mb}")
            with zipfile.ZipFile(wheel_file) as wheel:
                for member in wheel.namelist():
                    if member.startswith("nvidia/") and member.endswith(".dll"):
                        destination = target / member
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        with wheel.open(member) as source, destination.open("wb") as out:
                            shutil.copyfileobj(source, out)
