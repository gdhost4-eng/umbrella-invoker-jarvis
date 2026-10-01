"""Пути к ресурсам приложения, независимые от текущего рабочего каталога.

Модели, логи, библиотеки видеокарты и личные настройки лежат в %LOCALAPPDATA%\\Jarvis: и у Jarvis.exe,
и при запуске из исходников. Рядом с exe и в папке проекта ничего не появляется.
"""

import os
import shutil
import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))
PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Jarvis"
MODELS_DIR = DATA_DIR / "models"
LOGS_DIR = DATA_DIR / "logs"
CUDA_DIR = DATA_DIR / "cuda"


def default_config() -> Path:
    """config.yaml для запуска: копия в папке данных, рядом с ней личный config.local.yaml.
    Сам config.yaml обновляется вместе с программой, поэтому переписывается при каждом старте."""
    source = Path(getattr(sys, "_MEIPASS", PROJECT_DIR)) / "config.yaml"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    target = DATA_DIR / "config.yaml"
    shutil.copyfile(source, target)
    return target
