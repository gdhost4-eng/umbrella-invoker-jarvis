from __future__ import annotations

from pathlib import Path

import pytest

from jarvisvoice.config import Config, load_config

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def config() -> Config:
    return load_config(ROOT / "config.yaml")


@pytest.fixture(autouse=True)
def _phrases_not_recorded_to_real_logs(monkeypatch, tmp_path):
    """В config.yaml запись фраз включена — тесты пишут звук во временную папку, а не в logs/audio."""
    monkeypatch.setattr("jarvisvoice.app.LOGS_DIR", tmp_path)
