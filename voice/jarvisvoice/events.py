"""Контракт событий между рабочими потоками, журналом и интерфейсом."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger(__name__)


@dataclass
class ExecResult:
    """Что стало с командой: разбор фразы сообщает сам помощник, выполнение — скрипт в игре."""

    label: str
    ok: bool
    message: str
    latency_ms: float | None = None
    deferred: bool = False
    info: bool = False

    @property
    def mark(self) -> str:
        return "·" if self.info else "✓" if self.ok else "✗"


class UiSink(Protocol):
    def status(self, key: str, text: str, ok: bool | None) -> None: ...
    def heard(self, text: str) -> None: ...
    def result(self, result: ExecResult) -> None: ...
    def level(self, value: float) -> None: ...
    def muted_changed(self, muted: bool) -> None: ...
    def awake_changed(self, awake: bool) -> None: ...
    def quit_requested(self) -> None: ...


class LogSink:
    """Дублирует всё происходящее в консоль и logs/jarvis.log."""

    def status(self, key: str, text: str, ok: bool | None) -> None:
        (log.warning if ok is False else log.info)("[%s] %s", key, text)

    def heard(self, text: str) -> None:
        # Обычная речь в лог-файл не пишется (только с -v).
        log.debug("услышал: «%s»", text)

    def result(self, result: ExecResult) -> None:
        latency = f" · {result.latency_ms:.0f} мс" if result.latency_ms is not None else ""
        if result.info or result.ok:
            (log.debug if result.info else log.info)("%s %s: %s%s", result.mark, result.label, result.message, latency)
        else:
            # Отказы попадают и в logs/errors.log: видно, что бот услышал и почему не выполнил.
            log.warning("%s %s: %s%s", result.mark, result.label, result.message, latency)

    def level(self, value: float) -> None:
        pass

    def muted_changed(self, muted: bool) -> None:
        log.info("мут: %s", "включён" if muted else "выключен")

    def awake_changed(self, awake: bool) -> None:
        pass  # пишет сам BotApp: там известно слово активации

    def quit_requested(self) -> None:
        pass
