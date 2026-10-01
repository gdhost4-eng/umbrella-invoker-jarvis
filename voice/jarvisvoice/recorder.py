"""Запись фраз, услышанных ботом: по ним распознавание проверяется без игры."""

from __future__ import annotations

import logging
import time
import wave
from pathlib import Path

import numpy as np

from .audio import SAMPLE_RATE

log = logging.getLogger(__name__)


class PhraseRecorder:
    """Последние limit фраз в WAV (16 кГц, моно); старые удаляются. limit = 0 — не записывать."""

    def __init__(self, directory: Path, limit: int) -> None:
        self.directory = directory
        self.limit = limit

    def save(self, audio: np.ndarray, phrase_id: int) -> str | None:
        """Имя записанного файла (оно же в строке лога) или None."""
        if self.limit <= 0 or not len(audio):
            return None
        name = f"{time.strftime('%Y%m%d-%H%M%S')}-{phrase_id:05d}.wav"  # по имени — по времени
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2")
            with wave.open(str(self.directory / name), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(SAMPLE_RATE)
                wav.writeframes(pcm.tobytes())
            self._trim()
        except OSError:
            log.exception("не удалось записать фразу в %s", self.directory)
            return None
        return name

    def _trim(self) -> None:
        files = sorted(self.directory.glob("*.wav"))
        for old in files[: max(0, len(files) - self.limit)]:
            old.unlink(missing_ok=True)
