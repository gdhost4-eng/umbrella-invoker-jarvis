"""Мост к скрипту в игре: локальный HTTP-сервер, который скрипт опрашивает длинными запросами.

Скрипт держит один запрос POST /poll. Ответ уходит сразу, как только появилась команда или сменилось
состояние бота; иначе запрос висит до wait_ms. В запросе скрипт сообщает, что стало с командами
(results), что нажато в его меню (actions) и какие клавиши нажать за игрока (keys): встроенное
комбо Umbrella запускается только клавишей, а сам скрипт нажать её не может.
"""

from __future__ import annotations

import json
import logging
import secrets
import socket
import threading
import time
from collections import deque
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

log = logging.getLogger(__name__)
_MAX_BODY_BYTES = 262_144
_MAX_WAIT_S = 2.0
_DEFAULT_WAIT_S = 1.0
_LEVEL_INTERVAL_S = 0.05  # громкость для кружка — не чаще 20 раз в секунду
_LEVEL_STEP = 0.02
_COMMAND_TTL_S = 10.0  # старше — не отдаём: скрипт всё равно не выполнит
_LINK_TIMEOUT_S = 3.0
ACTIONS = ("wake", "sleep", "mute", "unmute", "toggle_mute")


class _ExclusiveHTTPServer(ThreadingHTTPServer):
    """На Windows SO_REUSEADDR позволяет двум процессам занять один порт — запрещаем,
    чтобы второй запущенный помощник сразу сообщил «порт занят»."""

    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class Bridge:
    def __init__(
        self,
        port: int,
        on_results: Callable[[list[dict]], None] | None = None,
        on_actions: Callable[[list[str]], None] | None = None,
        on_keys: Callable[[list[dict], int | None], None] | None = None,
    ) -> None:
        self.port = port
        self.session = secrets.token_hex(4)  # новый запуск помощника — скрипт начинает счёт команд заново
        self._on_results = on_results
        self._on_actions = on_actions
        self._on_keys = on_keys
        self._cond = threading.Condition()
        self._commands: deque[dict] = deque(maxlen=64)
        self._seq = 0
        self._state: dict[str, Any] = {"activity": "loading", "error": "", "wake": ""}
        self._state_v = 0
        self._level = 0.0
        self._level_sent = 0.0
        self._last_response_at = 0.0
        self._last_poll_at = 0.0
        self._stopping = False
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # --- со стороны бота -------------------------------------------------------------

    def start(self) -> None:
        if self._httpd is not None:
            return
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802 - имя из http.server
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    self.send_error(400, "Invalid Content-Length")
                    return
                if not 0 <= length <= _MAX_BODY_BYTES:
                    self.send_error(413, "Invalid body size")
                    return
                self.connection.settimeout(2.0)
                try:
                    body = self.rfile.read(length)
                except OSError:
                    self.close_connection = True
                    return
                self._reply(_parse_body(body))

            def do_GET(self) -> None:  # noqa: N802
                self._reply(_parse_query(self.path))

            def _reply(self, request: dict | None) -> None:
                if request is None or urlsplit(self.path).path != "/poll":
                    self.send_error(400, "Bad request")
                    return
                payload = json.dumps(bridge.poll(request), ensure_ascii=False).encode("utf-8")
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except OSError:
                    self.close_connection = True

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                pass

        self._httpd = _ExclusiveHTTPServer(("127.0.0.1", self.port), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="bridge", daemon=True)
        self._thread.start()
        log.info("мост: слушаю http://127.0.0.1:%d/poll", self.port)

    def stop(self) -> None:
        with self._cond:
            self._stopping = True
            self._cond.notify_all()
        if self._httpd is not None:
            if self._thread is not None and self._thread.is_alive():
                self._httpd.shutdown()
                self._thread.join(timeout=2)
            self._httpd.server_close()
            self._httpd = None
            self._thread = None

    def push(self, command: dict, label: str, heard_at: float, speech_end_at: float | None = None) -> int:
        """Команда для игры; времена — time.monotonic() момента распознавания и конца речи."""
        with self._cond:
            self._seq += 1
            self._commands.append(
                {"seq": self._seq, "label": label, "heard_at": heard_at, "speech_end_at": speech_end_at, **command}
            )
            self._cond.notify_all()
            return self._seq

    def cancel(self) -> None:
        """Мут, перезагрузка конфига: скрипт бросает очередь и отложенный возврат сфер."""
        self.push({"kind": "cancel"}, "отмена", time.monotonic())

    def set_state(self, activity: str, error: str = "", wake: str = "") -> None:
        state = {"activity": activity, "error": error, "wake": wake}
        with self._cond:
            if state != self._state:
                self._state = state
                self._state_v += 1
                self._cond.notify_all()

    def set_level(self, value: float) -> None:
        with self._cond:
            self._level = value
            if self._state["activity"] == "listening" and abs(value - self._level_sent) >= _LEVEL_STEP:
                self._cond.notify_all()

    @property
    def connected(self) -> bool:
        """Опрашивал ли скрипт мост в последние секунды."""
        with self._cond:
            return self._last_poll_at > 0 and time.monotonic() - self._last_poll_at < _LINK_TIMEOUT_S

    # --- со стороны скрипта ----------------------------------------------------------

    def poll(self, request: dict) -> dict:
        self._deliver(request)
        same_session = request.get("session") == self.session
        if "session" in request and not same_session:
            # Скрипт перезагрузили: он уже не помнит, что просил держать клавишу комбо.
            self._send_keys([{"op": "reset"}])
        after = _int(request.get("after")) if same_session else None
        state_v = _int(request.get("state_v")) if same_session else -1
        wait_s = min(_MAX_WAIT_S, max(0.0, _int(request.get("wait_ms"), int(_DEFAULT_WAIT_S * 1000)) / 1000))
        deadline = time.monotonic() + wait_s
        with self._cond:
            self._last_poll_at = time.monotonic()
            if after is None:
                after = self._seq  # незнакомый скрипт начинает с текущего места: старое не повторяем
            while not self._stopping:
                now = time.monotonic()
                level_due = (
                    self._state["activity"] == "listening"
                    and abs(self._level - self._level_sent) >= _LEVEL_STEP
                    and now - self._last_response_at >= _LEVEL_INTERVAL_S
                )
                if self._seq > after or self._state_v != state_v or level_due or now >= deadline:
                    break
                self._cond.wait(min(deadline - now, _LEVEL_INTERVAL_S))
            now = time.monotonic()
            self._last_poll_at = self._last_response_at = now
            self._level_sent = self._level
            commands = [
                _wire(command, now)
                for command in self._commands
                if command["seq"] > after and now - command["heard_at"] <= _COMMAND_TTL_S
            ]
            return {
                "session": self.session,
                "seq": self._seq,
                "state_v": self._state_v,
                "state": {**self._state, "level": round(self._level, 3)},
                "commands": commands,
            }

    def _deliver(self, request: dict) -> None:
        results = request.get("results")
        if isinstance(results, list) and results and self._on_results is not None:
            try:
                self._on_results([entry for entry in results if isinstance(entry, dict)])
            except Exception:
                log.exception("мост: ошибка обработки результатов")
        actions = request.get("actions")
        if isinstance(actions, list) and self._on_actions is not None:
            known = [action for action in actions if action in ACTIONS]
            if known:
                try:
                    self._on_actions(known)
                except Exception:
                    log.exception("мост: ошибка обработки действий")
        keys = request.get("keys")
        if isinstance(keys, list):
            self._send_keys([step for step in keys if isinstance(step, dict)], _int(request.get("keys_seq")) or None)

    def _send_keys(self, steps: list[dict], seq: int | None = None) -> None:
        """seq — номер запроса у скрипта: два запроса подряд могут дойти не по порядку."""
        if steps and self._on_keys is not None:
            try:
                self._on_keys(steps, seq)
            except Exception:
                log.exception("мост: ошибка нажатия клавиш")


def _wire(command: dict, now: float) -> dict:
    out = {key: value for key, value in command.items() if key not in ("heard_at", "speech_end_at")}
    out["age_ms"] = round((now - command["heard_at"]) * 1000)
    speech_end_at = command["speech_end_at"]
    if speech_end_at is not None:
        out["speech_age_ms"] = round((now - speech_end_at) * 1000)
    return out


def _int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _parse_body(body: bytes) -> dict | None:
    if not body:
        return {}
    try:
        request = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return request if isinstance(request, dict) else None


def _parse_query(path: str) -> dict:
    """GET /poll?session=…&after=…&state_v=…&actions=wake,mute — то же, что POST, но без results."""
    query = {key: values[-1] for key, values in parse_qs(urlsplit(path).query).items()}
    request: dict[str, Any] = dict(query)
    if "actions" in query:
        request["actions"] = [action for action in query["actions"].split(",") if action]
    return request
