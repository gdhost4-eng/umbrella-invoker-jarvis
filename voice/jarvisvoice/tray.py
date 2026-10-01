"""Значок в трее и его меню. Кружок поверх игры рисует сам скрипт Umbrella.

Цвет значка показывает состояние: перламутровый — слушаю, янтарный — слушаю, но скрипт в игре
не подключён, серый — мут, красноватый — ошибка.
"""

from __future__ import annotations

import logging
import math
import os
import signal
import subprocess
import sys
from dataclasses import dataclass

from PySide6.QtCore import QObject, QPointF, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QImage, QPainter, QPixmap, QRadialGradient
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from .app import BotApp
from .paths import LOGS_DIR
from .settings import autostart_enabled, launch_command, save_microphone, set_autostart, unique_devices

log = logging.getLogger(__name__)

_FATAL_KEYS = ("bridge", "asr", "mic")
_SPOT_STOPS = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)


def _rgba(r: int, g: int, b: int, a: float = 1.0) -> QColor:
    color = QColor(r, g, b)
    color.setAlphaF(a)
    return color


def _alpha(color: QColor, a: float) -> QColor:
    out = QColor(color)
    out.setAlphaF(max(0.0, min(1.0, a)))
    return out


@dataclass(frozen=True)
class Palette:
    blobs: tuple[QColor, ...]  # переливы внутри шара
    core: QColor


PEARL = Palette(
    (_rgba(255, 255, 255, 0.7), _rgba(165, 185, 255, 0.85), _rgba(255, 185, 225, 0.75)),
    _rgba(111, 127, 184),
)
GREY = Palette(
    (_rgba(225, 225, 232, 0.45), _rgba(150, 155, 170, 0.55), _rgba(195, 195, 205, 0.45)),
    _rgba(71, 74, 83),
)
RED = Palette(
    (_rgba(255, 215, 215, 0.65), _rgba(255, 110, 120, 0.85), _rgba(255, 165, 150, 0.75)),
    _rgba(132, 64, 76),
)
AMBER = Palette(
    (_rgba(255, 245, 220, 0.65), _rgba(255, 190, 100, 0.85), _rgba(255, 215, 165, 0.7)),
    _rgba(134, 102, 63),
)
WHITE = QColor(255, 255, 255)
_TRAY_ICONS = {"listening": PEARL, "unlinked": AMBER, "muted": GREY, "error": RED}


def orb_icon(palette: Palette, size: int = 64) -> QIcon:
    """Значок для трея: тот же шар, что рисует скрипт в игре, только без свечения."""
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    center, r, phase = QPointF(size / 2, size / 2), size * 0.44, 0.8
    p = QPainter(image)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(palette.core)
    p.drawEllipse(center, r, r)
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceAtop)
    for i, color in enumerate(palette.blobs):
        angle = phase * (-1 if i % 2 else 1) + i * 2.1
        distance = r * (0.38 + 0.15 * math.sin(phase * 2.2 + i))
        spot = QPointF(center.x() + math.cos(angle) * distance, center.y() + math.sin(angle) * distance)
        gradient = QRadialGradient(spot, r * 0.85)
        for stop in _SPOT_STOPS:
            gradient.setColorAt(stop, _alpha(color, color.alphaF() * (1 - stop) ** 2))
        p.setBrush(gradient)
        p.drawEllipse(spot, r * 0.85, r * 0.85)
    shine_at = QPointF(center.x() - r * 0.35, center.y() - r * 0.4)
    shine = QRadialGradient(shine_at, r * 0.8)
    shine.setColorAt(0.0, _alpha(WHITE, 0.32))
    shine.setColorAt(1.0, _alpha(WHITE, 0.0))
    p.setBrush(shine)
    p.drawEllipse(shine_at, r * 0.8, r * 0.8)
    p.end()
    return QIcon(QPixmap.fromImage(image))


class _Signals(QObject):
    """Сигналы из рабочих потоков в поток интерфейса."""

    changed = Signal()
    quit = Signal()


class _TraySink:
    def __init__(self, signals: _Signals) -> None:
        self._signals = signals

    def status(self, key: str, text: str, ok: bool | None) -> None:
        self._signals.changed.emit()

    def heard(self, text: str) -> None:
        pass

    def result(self, result: object) -> None:
        pass

    def level(self, value: float) -> None:
        pass

    def muted_changed(self, muted: bool) -> None:
        self._signals.changed.emit()

    def awake_changed(self, awake: bool) -> None:
        self._signals.changed.emit()

    def quit_requested(self) -> None:
        self._signals.quit.emit()


MENU_STYLE = """
QMenu {
    background: rgba(22, 24, 34, 246);
    color: #E8EAF2;
    border: 1px solid rgba(255, 255, 255, 28);
    border-radius: 10px;
    padding: 6px;
}
QMenu::item { padding: 6px 22px 6px 14px; border-radius: 6px; }
QMenu::item:selected { background: rgba(165, 185, 255, 48); }
QMenu::item:disabled { color: rgba(232, 234, 242, 120); }
QMenu::separator { height: 1px; background: rgba(255, 255, 255, 22); margin: 5px 8px; }
"""


class TrayUi(QObject):
    def __init__(self, app: BotApp) -> None:
        super().__init__()
        self.app = app
        self.signals = _Signals()
        self.sink = _TraySink(self.signals)
        self.menu = self._build_menu()
        self._icons = {name: orb_icon(palette) for name, palette in _TRAY_ICONS.items()}
        self.tray = self._build_tray()
        self._tray_state: tuple[str, str] | None = None

        self.signals.changed.connect(self.refresh)
        self.signals.quit.connect(QApplication.quit)
        self._timer = QTimer(self)
        self._timer.setInterval(500)  # засыпание по тишине и связь со скриптом проверяются по таймеру
        self._timer.timeout.connect(self.refresh)

    def start(self) -> None:
        if self.tray is not None:
            self.tray.show()
        self.refresh()
        self._timer.start()

    def close(self) -> None:
        self._timer.stop()
        if self.tray is not None:
            self.tray.hide()

    def look(self) -> str:
        activity = self.app.activity()
        if activity in ("listening", "sleeping") and not self.app.game_linked():
            return "unlinked"
        return activity

    def refresh(self) -> None:
        look = self.look()
        tooltip = f"Jarvis · {self.describe(look)}"[:120]
        if self.tray is not None and self._tray_state != (look, tooltip):
            self._tray_state = (look, tooltip)
            self.tray.setIcon(self._icons.get(look, self._icons["listening"]))
            self.tray.setToolTip(tooltip)

    def describe(self, look: str) -> str:
        name = self.app.wake_name
        if look == "loading":
            text, _ = self.app.statuses.get("asr", ("", None))
            return text if text.startswith(("качаю", "скачиваю")) else "загружаюсь…"
        if look == "error":
            return f"ошибка: {self.app.fatal_text()}"
        if look == "muted":
            return f"мут — скажи «{name}, включись»"
        if look == "unlinked":
            return "скрипт в игре не подключён"
        if look == "listening":
            return "слушаю"
        return f"сплю — скажи «{name}»"

    # --- меню ----------------------------------------------------------------------

    def _build_menu(self) -> QMenu:
        menu = QMenu()
        menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        menu.setWindowFlags(
            menu.windowFlags() | Qt.WindowType.FramelessWindowHint | Qt.WindowType.NoDropShadowWindowHint
        )
        menu.setStyleSheet(MENU_STYLE)
        self._state_action = menu.addAction("")
        self._state_action.setEnabled(False)
        self._warning_actions: list[QAction] = []
        self._info_end = menu.addSeparator()
        menu.addAction("Разбудить").triggered.connect(self.app.wake)
        self._mute_action = menu.addAction("Мут")
        self._mute_action.triggered.connect(lambda: self.app.set_muted(not self.app.muted))
        menu.addSeparator()
        self._mic_menu = menu.addMenu("Микрофон")
        self._mic_menu.setStyleSheet(MENU_STYLE)
        self.restart_requested = False
        self._autostart_action = menu.addAction("Запускать вместе с Windows")
        self._autostart_action.setCheckable(True)
        self._autostart_action.triggered.connect(self._toggle_autostart)
        menu.addAction("Перечитать config.yaml").triggered.connect(self.app.reload_config)
        menu.addAction("Открыть логи").triggered.connect(self._open_logs)
        menu.addSeparator()
        menu.addAction("Выход").triggered.connect(QApplication.quit)
        menu.aboutToShow.connect(self._update_menu)
        return menu

    def _update_menu(self) -> None:
        self._state_action.setText(self.describe(self.look()))
        self._mute_action.setText("Снять мут" if self.app.muted else "Мут")
        self._autostart_action.setChecked(autostart_enabled())
        self._fill_mic_menu()
        for action in self._warning_actions:
            self.menu.removeAction(action)
        self._warning_actions = []
        # Не мешающие работе проблемы: CPU вместо видеокарты, конфликты в конфиге.
        for key, (text, ok) in list(self.app.statuses.items()):
            if ok is False and key not in _FATAL_KEYS:
                action = QAction(f"⚠ {text[:90]}", self.menu)
                action.setEnabled(False)
                self.menu.insertAction(self._info_end, action)
                self._warning_actions.append(action)

    def _fill_mic_menu(self) -> None:
        from .audio import list_input_devices

        self._mic_menu.clear()
        current = self.app.config.microphone.device
        try:
            names = unique_devices(list_input_devices())
        except Exception:
            log.exception("не удалось получить список микрофонов")
            names = []
        for label, name in [("По умолчанию в Windows", None), *[(n, n) for n in names]]:
            action = self._mic_menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(name == current)
            action.triggered.connect(lambda _=False, name=name: self._choose_microphone(name))

    def _choose_microphone(self, name: str | None) -> None:
        save_microphone(self.app.config_path, name)
        self.restart_requested = True  # микрофон применяется при запуске: перезапускаемся
        QApplication.quit()

    def _toggle_autostart(self, enabled: bool) -> None:
        try:
            set_autostart(enabled)
        except Exception:
            log.exception("не удалось изменить автозапуск")

    def _open_logs(self) -> None:
        LOGS_DIR.mkdir(exist_ok=True)
        if hasattr(os, "startfile"):
            os.startfile(LOGS_DIR)  # noqa: S606

    def _build_tray(self) -> QSystemTrayIcon | None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            log.warning("системный трей недоступен: управление — голосом и из меню скрипта в игре")
            return None
        tray = QSystemTrayIcon(self._icons["listening"], self)
        tray.setContextMenu(self.menu)
        tray.activated.connect(self._on_tray)
        return tray

    def _on_tray(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.app.wake()


def run_tray(app: BotApp) -> int:
    qt_app = QApplication.instance() or QApplication(sys.argv)
    qt_app.setQuitOnLastWindowClosed(False)  # окон нет вовсе — это не выход
    ui = TrayUi(app)
    app.sinks.append(ui.sink)
    previous_sigint = signal.signal(signal.SIGINT, lambda *_: QApplication.quit())
    # Периодически отдаём управление Python, чтобы Ctrl+C в консоли срабатывал.
    heartbeat = QTimer()
    heartbeat.timeout.connect(lambda: None)
    heartbeat.start(250)
    try:
        ui.start()
        app.start()
        return qt_app.exec()
    finally:
        heartbeat.stop()
        signal.signal(signal.SIGINT, previous_sigint)
        try:
            app.stop()
        finally:
            app.sinks.remove(ui.sink)
            ui.close()
            if ui.restart_requested:
                subprocess.Popen(launch_command(), close_fds=True)  # noqa: S603
