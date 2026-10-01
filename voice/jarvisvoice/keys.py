"""Нажатие клавиш за игрока.

Комбо встроенного скрипта Invoker в Umbrella запускается только удержанием его клавиши, и из скрипта
её не нажать. Поэтому скрипт называет клавиши (имена из Enum.ButtonCode), а нажимает их помощник.
Всё остальное скрипт делает сам, без клавиш.
"""

from __future__ import annotations

import ctypes
import logging
import queue
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)
_TAP_S = 0.04  # столько держится клавиша слота: скрипт Umbrella видит её хотя бы один кадр
_GAP_S = 0.05  # пауза после неё: слот должен смениться раньше, чем зажмётся клавиша комбо
_LINK_CHECK_S = 0.25
GAME_EXE = "dota2.exe"
OPS = ("tap", "down", "up", "reset")


@dataclass(frozen=True)
class Button:
    vk: int = 0
    extended: bool = False
    mouse: int = 0  # 1–5 — кнопка мыши, 0 — клавиша


def _buttons() -> dict[str, Button]:
    """Имена Enum.ButtonCode из Umbrella → клавиши Windows. Колеса и индикаторов Caps/Num/Scroll нет: их не зажать."""
    plain = {
        "PAD_MULTIPLY": 0x6A,
        "PAD_MINUS": 0x6D,
        "PAD_PLUS": 0x6B,
        "PAD_DECIMAL": 0x6E,
        "LBRACKET": 0xDB,
        "RBRACKET": 0xDD,
        "SEMICOLON": 0xBA,
        "APOSTROPHE": 0xDE,
        "BACKQUOTE": 0xC0,
        "COMMA": 0xBC,
        "PERIOD": 0xBE,
        "SLASH": 0xBF,
        "BACKSLASH": 0xDC,
        "MINUS": 0xBD,
        "EQUAL": 0xBB,
        "ENTER": 0x0D,
        "SPACE": 0x20,
        "BACKSPACE": 0x08,
        "TAB": 0x09,
        "CAPSLOCK": 0x14,
        "NUMLOCK": 0x90,
        "ESCAPE": 0x1B,
        "SCROLLLOCK": 0x91,
        "BREAK": 0x13,
        "LSHIFT": 0xA0,
        "RSHIFT": 0xA1,
        "LALT": 0xA4,
        "LCONTROL": 0xA2,
    }
    extended = {
        "PAD_DIVIDE": 0x6F,
        "PAD_ENTER": 0x0D,
        "INSERT": 0x2D,
        "DELETE": 0x2E,
        "HOME": 0x24,
        "END": 0x23,
        "PAGEUP": 0x21,
        "PAGEDOWN": 0x22,
        "RALT": 0xA5,
        "RCONTROL": 0xA3,
        "LWIN": 0x5B,
        "RWIN": 0x5C,
        "APP": 0x5D,
        "UP": 0x26,
        "LEFT": 0x25,
        "DOWN": 0x28,
        "RIGHT": 0x27,
    }
    buttons = {f"KEY_{name}": Button(vk) for name, vk in plain.items()}
    buttons.update({f"KEY_{name}": Button(vk, extended=True) for name, vk in extended.items()})
    buttons.update({f"KEY_{digit}": Button(0x30 + digit) for digit in range(10)})
    buttons.update({f"KEY_PAD_{digit}": Button(0x60 + digit) for digit in range(10)})
    buttons.update({f"KEY_{chr(code)}": Button(code) for code in range(ord("A"), ord("Z") + 1)})
    buttons.update({f"KEY_F{number}": Button(0x70 + number - 1) for number in range(1, 25)})
    buttons.update({f"KEY_MOUSE{number}": Button(mouse=number) for number in range(1, 6)})
    return buttons


BUTTONS = _buttons()

if sys.platform == "win32":
    from ctypes import wintypes as wt

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ULONG_PTR = ctypes.c_size_t

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = (
            ("dx", wt.LONG),
            ("dy", wt.LONG),
            ("mouseData", wt.DWORD),
            ("dwFlags", wt.DWORD),
            ("time", wt.DWORD),
            ("dwExtraInfo", _ULONG_PTR),
        )

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = (
            ("wVk", wt.WORD),
            ("wScan", wt.WORD),
            ("dwFlags", wt.DWORD),
            ("time", wt.DWORD),
            ("dwExtraInfo", _ULONG_PTR),
        )

    class _INPUTUNION(ctypes.Union):
        _fields_ = (("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT))

    class _INPUT(ctypes.Structure):
        _fields_ = (("type", wt.DWORD), ("u", _INPUTUNION))

    _INPUT_MOUSE, _INPUT_KEYBOARD = 0, 1
    _KEYEVENTF_EXTENDEDKEY, _KEYEVENTF_KEYUP = 0x1, 0x2
    # кнопка мыши → (флаг нажатия, флаг отпускания, mouseData)
    _MOUSE_FLAGS = {
        1: (0x0002, 0x0004, 0),
        2: (0x0008, 0x0010, 0),
        3: (0x0020, 0x0040, 0),
        4: (0x0080, 0x0100, 1),
        5: (0x0080, 0x0100, 2),
    }
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _user32.SendInput.argtypes = (wt.UINT, ctypes.POINTER(_INPUT), ctypes.c_int)
    _user32.SendInput.restype = wt.UINT
    _user32.MapVirtualKeyW.argtypes = (wt.UINT, wt.UINT)
    _user32.MapVirtualKeyW.restype = wt.UINT
    _user32.GetForegroundWindow.restype = wt.HWND
    _user32.GetWindowThreadProcessId.argtypes = (wt.HWND, ctypes.POINTER(wt.DWORD))
    _user32.GetWindowThreadProcessId.restype = wt.DWORD
    _kernel32.OpenProcess.argtypes = (wt.DWORD, wt.BOOL, wt.DWORD)
    _kernel32.OpenProcess.restype = wt.HANDLE
    _kernel32.QueryFullProcessImageNameW.argtypes = (wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD))
    _kernel32.QueryFullProcessImageNameW.restype = wt.BOOL
    _kernel32.CloseHandle.argtypes = (wt.HANDLE,)
    _kernel32.CloseHandle.restype = wt.BOOL

    def send_button(button: Button, down: bool) -> bool:
        """Нажимает или отпускает клавишу. False — Windows не приняла нажатие."""
        event = _INPUT()
        if button.mouse:
            press, release, data = _MOUSE_FLAGS[button.mouse]
            event.type = _INPUT_MOUSE
            event.u.mi = _MOUSEINPUT(0, 0, data, press if down else release, 0, 0)
        else:
            flags = (_KEYEVENTF_EXTENDEDKEY if button.extended else 0) | (0 if down else _KEYEVENTF_KEYUP)
            event.type = _INPUT_KEYBOARD
            event.u.ki = _KEYBDINPUT(button.vk, _user32.MapVirtualKeyW(button.vk, 0), flags, 0, 0)
        return _user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(_INPUT)) == 1

    def game_is_foreground() -> bool:
        """False — на переднем плане точно не Дота, и клавиши ушли бы в чужое окно."""
        window = _user32.GetForegroundWindow()
        pid = wt.DWORD()
        if not window or not _user32.GetWindowThreadProcessId(window, ctypes.byref(pid)):
            return True
        process = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not process:
            return True
        try:
            size = wt.DWORD(1024)
            path = ctypes.create_unicode_buffer(size.value)
            if not _kernel32.QueryFullProcessImageNameW(process, 0, path, ctypes.byref(size)):
                return True
            return path.value.replace("/", "\\").rsplit("\\", 1)[-1].lower() == GAME_EXE
        finally:
            _kernel32.CloseHandle(process)

else:

    def send_button(button: Button, down: bool) -> bool:
        return False

    def game_is_foreground() -> bool:
        return False


class KeyHolder:
    """Нажатия по просьбе скрипта, в своём потоке: мост отвечает сразу, паузы между клавишами игру не задерживают.

    Зажатое отпускается по просьбе скрипта, при выходе и когда скрипт перестал опрашивать мост.
    """

    def __init__(
        self,
        linked: Callable[[], bool],
        send: Callable[[Button, bool], bool] = send_button,
        foreground: Callable[[], bool] = game_is_foreground,
    ) -> None:
        self._linked = linked
        self._send = send
        self._foreground = foreground
        self._queue: queue.Queue[tuple[list[dict], int | None] | None] = queue.Queue()
        self._lock = threading.Lock()
        self._held: list[str] = []
        self._seq = 0  # номер последнего запроса скрипта
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="keys", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        if self._thread is not None:
            self._queue.put(None)
            self._thread.join(timeout=2)
            self._thread = None
        self.release()

    def submit(self, ops: list[dict], seq: int | None = None) -> None:
        """ops — шаги {op: tap | down | up | reset, keys: [имена]}; up без клавиш отпускает всё зажатое,
        reset ещё и начинает счёт запросов заново. seq — номер запроса скрипта: запрос, пришедший позже
        следующего, ничего не нажимает, иначе клавиша осталась бы зажатой после отмены."""
        self._queue.put((ops, seq))

    @property
    def held(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._held)

    def run(self, ops: list[dict], seq: int | None = None) -> None:
        steps = [(op.get("op"), _names(op.get("keys"))) for op in ops if isinstance(op, dict) and op.get("op") in OPS]
        unknown = sorted({name for _, names in steps for name in names if name not in BUTTONS})
        pressing = any(kind in ("tap", "down") and names for kind, names in steps)
        late = seq is not None and seq < self._seq
        if seq is not None and not late:
            self._seq = seq
        if unknown:
            log.warning("клавиши: не знаю, как нажать %s", ", ".join(unknown))
            pressing = False
        elif pressing and late:
            log.info("клавиши: запрос %d пришёл позже запроса %d, ничего не нажимаю", seq, self._seq)
            pressing = False
        elif pressing and not self._foreground():
            log.warning("клавиши: на переднем плане не Дота, ничего не нажимаю")
            pressing = False
        for kind, names in steps:
            if kind == "reset":
                self._seq = 0
                self.release()
            elif kind == "up":
                self.release([name for name in names if name in BUTTONS] if names else None)
            elif pressing and kind == "tap":
                self._press(names)
                time.sleep(_TAP_S)
                self.release(names)
                time.sleep(_GAP_S)
            elif pressing and kind == "down":
                self._press(names)

    def release(self, names: list[str] | None = None) -> None:
        with self._lock:
            for name in reversed(list(self._held) if names is None else names):
                self._send(BUTTONS[name], False)
                if name in self._held:
                    self._held.remove(name)

    def _press(self, names: list[str]) -> None:
        with self._lock:
            for name in names:
                if not self._send(BUTTONS[name], True):
                    log.warning(
                        "клавиши: Windows не дала нажать %s (если Дота запущена от администратора, "
                        "так же запусти и помощника)",
                        name,
                    )
                if name not in self._held:
                    self._held.append(name)

    def _loop(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=_LINK_CHECK_S)
            except queue.Empty:
                if self.held and not self._linked():
                    log.info("клавиши: скрипт в игре не отвечает, отпускаю %s", ", ".join(self.held))
                    self.release()
                continue
            if item is None:
                return
            try:
                self.run(*item)
            except Exception:
                log.exception("клавиши: ошибка нажатия")


def _names(value: object) -> list[str]:
    return [name for name in value if isinstance(name, str)] if isinstance(value, list) else []
