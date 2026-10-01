"""Сборка компонентов, жизненный цикл и обработка распознанных фраз."""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path

from .asr import Recognizer, Transcript
from .audio import AudioListener, PartialUtterance, Utterance
from .bridge import Bridge
from .commands import SystemCmd, to_wire
from .config import Config, ConfigError, load_config
from .diagnostics import describe_parse
from .events import ExecResult, LogSink, UiSink
from .keys import KeyHolder
from .nlu import CommandParser, ParsedCommand, WakeWord, build_prompt
from .paths import LOGS_DIR, MODELS_DIR
from .recorder import PhraseRecorder
from .streaming import StreamingCommands, too_many_commands

log = logging.getLogger(__name__)
_MAX_UTTERANCE_AGE_S = 2.0
_AWAKE_CHECK_S = 0.25
_WAKE_MIN_SPEECH_S = 0.3  # «Джарвис» из шума принимается, только если детектор речи слышал слово целиком
_NO_AUDIO = Transcript("", 1.0, -10.0, 0.0)
AudioEvent = Utterance | PartialUtterance


@dataclass(frozen=True)
class _Frozen:
    """Начало фразы до паузы после команды: распознано окончательно и заново не распознаётся.

    Whisper пишет текст по токену, и длинное комбо целиком стоит сотни миллисекунд. С кусками
    промежуточные гипотезы и итог распознают только звук после последней такой паузы,
    а уже выполненное начало фразы не может переписаться («блинк» → «балинг»).
    """

    utterance_id: int
    samples: int = 0  # столько звука от начала фразы уже распознано
    texts: tuple[str, ...] = ()
    raw_texts: tuple[str, ...] = ()

    def join(self, tail: Transcript) -> Transcript:
        return replace(
            tail,
            text=" ".join(t for t in (*self.texts, tail.text) if t),
            raw_text=" ".join(t for t in (*self.raw_texts, tail.raw_text) if t),
        )

    def extend(self, samples: int, tail: Transcript) -> _Frozen:
        return _Frozen(self.utterance_id, samples, (*self.texts, tail.text), (*self.raw_texts, tail.raw_text))


def _wake_word(config: Config) -> WakeWord:
    return WakeWord(config.wake.say, config.recognition.match_threshold)


class BotApp:
    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path
        self.config = load_config(config_path)
        self.sinks: list[UiSink] = [LogSink()]
        self.statuses: dict[str, tuple[str, bool | None]] = {}
        self._muted = False
        self._ready = False  # Whisper загружен и микрофон запущен
        self._fatal = False  # бот не может работать: Whisper или микрофон не запустились
        self._awake_until = 0.0
        self._awake = False
        self._utterance_awake: tuple[int, bool] | None = None
        self._closing: tuple[int, float, Transcript, int] | None = None  # распознано на паузе в конце фразы
        self._frozen: _Frozen | None = None
        self._quit = threading.Event()
        self._started = False
        self._stopping = threading.Event()
        self._lifecycle_lock = threading.RLock()
        self._recognition_lock = threading.RLock()
        self._generation = 0
        self._linked: bool | None = None  # опрашивает ли мост скрипт в игре

        self.parser = CommandParser(self.config)
        self.recorder = PhraseRecorder(LOGS_DIR / "audio", self.config.recognition.save_audio)
        self.wake_word = _wake_word(self.config)
        self.keys = KeyHolder(lambda: self.bridge.connected)
        self.bridge = Bridge(self.config.bridge.port, self._on_game_results, self._on_game_actions, self.keys.submit)
        self._utterances: queue.Queue[tuple[AudioEvent, int] | None] = queue.Queue(maxsize=32)
        self._streaming = StreamingCommands()
        on_partial = self._utterances_put if self.config.microphone.realtime else None
        self.audio = AudioListener(self.config.microphone, self._utterances_put, self._on_level, on_partial)
        self.recognizer: Recognizer | None = None
        self._asr_thread = threading.Thread(target=self._asr_loop, name="asr", daemon=True)

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._started or self._stopping.is_set():
                return
            self._started = True
            self._report_conflicts()
            try:
                self.bridge.start()
            except OSError as exc:
                # Без моста команды некому выполнять; обычно это второй запущенный помощник.
                self.bridge.stop()
                self._fatal = True
                self._status("bridge", f"порт {self.config.bridge.port} занят: {exc}", False)
                return
            self._status("game", "жду скрипт в игре", None)
            self.keys.start()
            self._asr_thread.start()

    def stop(self) -> None:
        if self._stopping.is_set():
            return
        self._stopping.set()
        with self._recognition_lock:
            self._invalidate_recognition()
            self._utterances.put_nowait(None)
        with self._lifecycle_lock:
            try:
                self.audio.stop()
            except Exception:
                log.exception("ошибка при остановке микрофона")
            self.bridge.stop()
            self.keys.stop()  # зажатая клавиша комбо не остаётся нажатой после выхода
        if self._asr_thread.is_alive() and self._asr_thread is not threading.current_thread():
            self._asr_thread.join(timeout=2)

    def set_muted(self, muted: bool) -> None:
        """Мягкий мут: микрофон слушает дальше, но бот ждёт только «Джарвис, включись»."""
        with self._recognition_lock:
            self._muted = muted
            self._invalidate_recognition()
            if muted:
                self.bridge.cancel()
                self.keys.submit([{"op": "up"}])
            self._set_awake_until(0.0)
            self._publish_state()
        self._notify("muted_changed", muted)

    @property
    def muted(self) -> bool:
        return self._muted

    @property
    def wake_name(self) -> str:
        """Как звать бота: первое слово из wake.say."""
        return self.config.wake.say[0].capitalize()

    @property
    def awake(self) -> bool:
        """Слушает ли бот команды без обращения."""
        if not self.config.wake.enabled:
            return not self._muted
        return time.monotonic() < self._awake_until

    def wake(self) -> None:
        """Начать слушать без обращения, например по клику на значок в трее."""
        with self._recognition_lock:
            if not self._muted:
                self._extend_awake(time.monotonic())

    def sleep(self) -> None:
        with self._recognition_lock:
            self._set_awake_until(0.0)

    def activity(self) -> str:
        """Что показывать кружком: loading | error | muted | listening | sleeping."""
        if self._fatal:
            return "error"
        if not self._ready:
            return "loading"
        if self._muted:
            return "muted"
        return "listening" if self.awake else "sleeping"

    def game_linked(self) -> bool:
        """Опрашивает ли мост скрипт в игре: без него команды некому выполнять."""
        return self.bridge.connected

    def fatal_text(self) -> str:
        for key in ("bridge", "asr", "mic"):
            text, ok = self.statuses.get(key, ("", None))
            if ok is False:
                return text
        return "подробности в logs/errors.log"

    def _publish_state(self) -> None:
        """Состояние для кружка в игре."""
        activity = self.activity()
        self.bridge.set_state(activity, self.fatal_text() if activity == "error" else "", self.wake_name)

    def _check_link(self) -> None:
        linked = self.bridge.connected
        if linked != self._linked:
            self._linked = linked
            self._status("game", "скрипт в игре подключён" if linked else "нет связи со скриптом в игре", None)

    def request_quit(self) -> None:
        log.info("выход по голосовой команде")
        self._quit.set()
        self._notify("quit_requested")

    def wait_for_quit(self, timeout: float) -> bool:
        return self._quit.wait(timeout)

    def reload_config(self) -> None:
        try:
            config = load_config(self.config_path)
            parser = CommandParser(config)
        except ConfigError as exc:
            self._status("config", f"ошибка в конфиге: {exc}", False)
            return
        with self._recognition_lock:
            self._invalidate_recognition()
            self.bridge.cancel()
            self.config = config
            self.parser = parser
            self.wake_word = _wake_word(config)
            self.recorder.limit = config.recognition.save_audio
            if self.recognizer is not None:
                self.recognizer.set_prompt(build_prompt(config) if config.recognition.use_prompt else "")
            self._check_awake()
        if not self._report_conflicts():
            self._status("config", "конфиг перечитан (микрофон, модель и порт моста — после перезапуска)", True)

    def _report_conflicts(self) -> bool:
        if self.parser.conflicts:
            self._status("config", "; ".join(self.parser.conflicts), False)
            return True
        return False

    def _invalidate_recognition(self) -> None:
        self._generation += 1
        self._streaming.reset()
        self.parser.reset()
        self._utterance_awake = None
        self._closing = None
        self._frozen = None
        while True:
            try:
                self._utterances.get_nowait()
            except queue.Empty:
                return

    # --- слово активации -----------------------------------------------------------

    def _extend_awake(self, now: float) -> None:
        self._set_awake_until(max(self._awake_until, now + self.config.wake.window_s))

    def _set_awake_until(self, until: float) -> None:
        self._awake_until = until
        self._check_awake()

    def _check_awake(self) -> None:
        """Сообщает о пробуждении и засыпании; вызывается и по таймеру из потока распознавания."""
        awake = self.awake
        self._publish_state()
        if awake == self._awake:
            return
        self._awake = awake
        if self.config.wake.enabled and not self._muted:
            log.info("«%s»: %s", self.wake_name, "слушаю" if awake else "сплю")
        self._notify("awake_changed", awake)

    def _utterance_is_awake(self, item: AudioEvent) -> bool:
        """Слушал ли бот, когда фраза началась. Решение запоминается на всю фразу,
        чтобы промежуточные и итоговый результаты разбирались одинаково."""
        if self._utterance_awake is not None and self._utterance_awake[0] == item.id:
            awake = self._utterance_awake[1]
        else:
            end = item.captured_at if isinstance(item, PartialUtterance) else item.speech_end_at
            awake = not self._muted and end - item.duration_s < self._awake_until
        self._utterance_awake = (item.id, awake) if isinstance(item, PartialUtterance) else None
        return awake

    def _addressed_text(self, item: AudioEvent, text: str, now: float) -> str | None:
        """Часть фразы, адресованная боту; None — бот спит и его не звали."""
        if not self.config.wake.enabled:
            return text
        awake = self._utterance_is_awake(item)
        found, rest = self.wake_word.split(text, anywhere=awake)
        if not found:
            return text if awake else None
        if not self._muted:
            self._extend_awake(now)
        return rest

    # --- распознавание -------------------------------------------------------------

    def _initialize_recognition(self) -> bool:
        from .asr import WhisperRecognizer

        recognition = self.config.recognition
        self._status("asr", f"загружаю Whisper «{recognition.model}»…", None)
        try:
            recognizer = WhisperRecognizer(
                recognition, MODELS_DIR, progress=lambda text: self._status("asr", text, None)
            )
        except Exception as exc:
            log.exception("Whisper не загрузился")
            self._fatal = True
            self._status("asr", f"Whisper не загрузился: {exc}", False)
            self._publish_state()
            return False
        with self._recognition_lock:
            if self._stopping.is_set():
                return False
            self.recognizer = recognizer
            recognizer.set_prompt(build_prompt(self.config) if self.config.recognition.use_prompt else "")
        if recognizer.warning:
            self._status("asr", recognizer.warning, False)
        else:
            self._status("asr", f"Whisper {recognition.model} на {recognizer.device.upper()}", True)

        with self._lifecycle_lock:
            if self._stopping.is_set():
                return False
            try:
                self.audio.start()
            except Exception as exc:
                log.exception("микрофон не запустился")
                self._fatal = True
                self._status("mic", f"микрофон: {exc}", False)
                self._publish_state()
                return False
        self._ready = True
        self._publish_state()
        self._status("mic", f"слушаю: {self.audio.device_name}", True)
        if self.config.wake.enabled:
            log.info("жду обращения «%s»", self.wake_name)
        return True

    def _asr_loop(self) -> None:
        if not self._initialize_recognition():
            return
        while not self._stopping.is_set():
            try:
                queued = self._utterances.get(timeout=_AWAKE_CHECK_S)
            except queue.Empty:
                queued = False
            with self._recognition_lock:
                self._check_awake()
            self._check_link()
            if queued is False:
                continue
            if queued is None:
                return
            item, generation = queued
            if generation != self._generation:
                continue
            try:
                if isinstance(item, PartialUtterance):
                    # Гипотеза устарела, если в очереди звук новее; кусок на паузе нужен всегда —
                    # после него фраза распознаётся с этого места.
                    if item.closing or self._utterances.empty():
                        self._handle_audio(item, generation)
                elif time.monotonic() - item.detected_at > _MAX_UTTERANCE_AGE_S:
                    log.info("пропускаю фразу, пролежавшую в очереди слишком долго")
                    with self._recognition_lock:
                        self._streaming.reset()
                        self._utterance_awake = None
                        self._frozen = None
                else:
                    self._handle_audio(item, generation)
            except Exception:
                log.exception("ошибка распознавания")

    def _handle_partial(self, partial: PartialUtterance) -> None:
        self._handle_audio(partial)

    def _handle_utterance(self, utterance: Utterance) -> None:
        self._handle_audio(utterance)

    def _discarded(self, item: AudioEvent, generation: int) -> bool:
        # В муте нужна только законченная фраза: вдруг это «Джарвис, включись».
        muted_partial = self._muted and isinstance(item, PartialUtterance)
        return muted_partial or self._stopping.is_set() or generation != self._generation

    def _reuse_closing(self, item: AudioEvent) -> tuple[Transcript, int] | None:
        """Итог фразы, распознанной ещё на паузе: после неё речи не было — звук тот же, Whisper не нужен."""
        closing, self._closing = self._closing, None
        if isinstance(item, Utterance) and closing is not None and closing[:2] == (item.id, item.speech_end_at):
            return closing[2], closing[3]
        return None

    def _frozen_part(self, item: AudioEvent) -> _Frozen:
        frozen = self._frozen
        if frozen is None or frozen.utterance_id != item.id or frozen.samples > len(item.audio):
            return _Frozen(item.id)
        return frozen

    def _freeze(self, item: PartialUtterance, frozen: _Frozen, tail: Transcript) -> None:
        """Кусок до паузы больше не распознаётся, если кончается командой. Обрывок слова, «блин»
        или «скастуй» остаются: Whisper услышит их вместе с продолжением."""
        if tail.text and self.parser.parse(tail.text, time.monotonic(), commit=False).ends_with_command:
            self._frozen = frozen.extend(len(item.audio), tail)

    def _handle_audio(self, item: AudioEvent, generation: int | None = None) -> None:
        with self._recognition_lock:
            generation = self._generation if generation is None else generation
            if self._discarded(item, generation):
                return
            recognizer = self.recognizer
            reused = self._reuse_closing(item)
            frozen = self._frozen_part(item)
        if recognizer is None:
            return
        if reused is not None:
            transcript, parts = reused
        else:
            tail_audio = item.audio[frozen.samples :]
            # Начало фразы Whisper не подсказывается: на шуме он повторил бы его, и комбо сработало бы дважды.
            tail = recognizer.transcribe(tail_audio) if len(tail_audio) else _NO_AUDIO
            transcript, parts = frozen.join(tail), len(frozen.texts) + 1
        with self._recognition_lock:
            if self._discarded(item, generation):
                return
            partial = isinstance(item, PartialUtterance)
            if partial and item.closing:
                self._closing = (item.id, item.speech_end_at, transcript, parts)
                if reused is None:
                    self._freeze(item, frozen, tail)
            elif not partial:
                self._frozen = None
            if not transcript.text and self._wake_in_noise(item, transcript):
                # Одиночное «Джарвис» Whisper часто считает шумом: у коротких слов no_speech высокий.
                if not partial:
                    log.info("«%s» → обращение (no_speech=%.2f)", transcript.raw_text, transcript.no_speech_prob)
                if not self._muted:
                    self._extend_awake(time.monotonic())
                if partial:
                    self._streaming.partial(item.id, [])
                else:
                    self._streaming.reset()
                    self._utterance_awake = None
                return
            if not transcript.text:
                if partial:
                    self._streaming.partial(item.id, [])
                else:
                    self._streaming.reset()
                    self._log_noise(item, transcript)
                    self._utterance_awake = None
                return
            now = time.monotonic()
            text = self._addressed_text(item, transcript.text, now)
            if text is None:
                if not partial:
                    self._streaming.reset()
                    # В логе видно, что Whisper услышал вместо «Джарвис», если обращение не сработало.
                    log.info("сплю, фраза без обращения: «%s»", transcript.text)
                return
            if self._muted:
                self._handle_muted(transcript.text, text, now)
                return
            if not text:  # одно обращение: бот проснулся, команд пока нет
                if partial:
                    self._streaming.partial(item.id, [])
                else:
                    self._streaming.reset()
                return
            result = self.parser.parse(text, now, commit=False)
            self._notify("heard", transcript.text + (" …" if partial else ""))
            if self._muted or self._stopping.is_set() or generation != self._generation:
                return
            if too_many_commands(result.commands, item.duration_s):
                if partial:
                    self._streaming.partial(item.id, [])
                else:
                    self._streaming.reset()
                    self._on_result(
                        ExecResult(transcript.text, False, "слишком много команд для короткой фразы — пропущено")
                    )
                return
            if isinstance(item, PartialUtterance):
                pending = self._streaming.partial(item.id, result.commands, item.closing, result.ends_with_command)
                _log_late(self._streaming.late)
                self._submit(pending, now, item.speech_end_at if item.closing else item.captured_at)
                return

            finished = self._streaming.finish(item.id, result.commands)
            early = len(result.commands) - len(finished.pending) - len(finished.skipped)
            log.info(
                "«%s» → %s · Whisper %.0f мс%s · от конца речи %.0f мс%s",
                transcript.text,
                describe_parse(result),
                transcript.elapsed_ms,
                " (на паузе)" if reused else "",
                (now - item.speech_end_at) * 1000,
                _details(parts, early, len(result.commands), self.recorder.save(item.audio, item.id)),
            )
            if finished.skipped:
                labels = ", ".join(parsed.label for parsed in finished.skipped)
                message = "распознано позже следующих команд вместо выполненной — пропущено"
                self._on_result(ExecResult(labels, False, message))
            _log_late(finished.late)
            self.parser.commit(result)
            if not result.commands:
                message = "жду спелл для «скастуй»…" if result.waiting_for_spell else "не команда"
                self._on_result(ExecResult(transcript.text, False, message, info=True))
            self._submit(finished.pending, now, item.speech_end_at)

    def _wake_in_noise(self, item: AudioEvent, transcript: Transcript) -> bool:
        """Обращение во фразе, отброшенной только из-за no_speech: Whisper в тексте уверен."""
        return (
            self.config.wake.enabled
            and bool(transcript.raw_text)
            and item.duration_s >= _WAKE_MIN_SPEECH_S
            and transcript.avg_logprob >= self.config.recognition.log_prob_threshold
            and self.wake_word.split(transcript.raw_text, anywhere=False)[0]
        )

    def _log_noise(self, item: Utterance, transcript: Transcript) -> None:
        # Отброшенное, пока бот слушает, видно в логе: так понятно, почему команда «не услышана».
        listening = not self._muted and (not self.config.wake.enabled or self._utterance_is_awake(item))
        heard = bool(transcript.raw_text) and listening
        saved = self.recorder.save(item.audio, item.id) if heard else None
        log.log(
            logging.INFO if heard else logging.DEBUG,
            "«%s» → шум, пропущено (no_speech=%.2f, logprob=%.2f)%s",
            transcript.raw_text,
            transcript.no_speech_prob,
            transcript.avg_logprob,
            f" · звук {saved}" if saved else "",
        )

    def _handle_muted(self, heard: str, text: str, now: float) -> None:
        self._streaming.reset()
        result = self.parser.parse(text, now, commit=False)
        if any(
            isinstance(parsed.command, SystemCmd) and parsed.command.action == "unmute" for parsed in result.commands
        ):
            log.info("«%s» → снять мут", heard)
            self.set_muted(False)
            self._extend_awake(now)
        else:
            log.debug("мут, жду «%s, включись»: «%s»", self.wake_name, heard)

    def _submit(self, commands: list[ParsedCommand], heard_at: float, speech_end_at: float) -> None:
        for parsed in commands:
            if self._muted or self._stopping.is_set():
                break
            command = parsed.command
            if isinstance(command, SystemCmd):
                if command.action == "quit" and parsed.score < 100:
                    # Случайно закрыть бота посреди игры хуже, чем переспросить.
                    log.warning("похоже на «%s», но не точно («%s») — бот не выключен", parsed.alias, parsed.heard)
                    continue
                if self._system(command.action):
                    break
                continue
            self._extend_awake(time.monotonic())
            self.bridge.push(to_wire(command), parsed.label, heard_at, speech_end_at)

    def _system(self, action: str) -> bool:
        """Команда самому боту. True — остаток фразы не выполняется."""
        if action == "mute":
            self.set_muted(True)
        elif action == "sleep":
            self.sleep()
        elif action == "quit":
            self.request_quit()
        else:  # unmute: бот и так слушает
            return False
        return True

    def _utterances_put(self, utterance: AudioEvent) -> None:
        with self._recognition_lock:
            if self._stopping.is_set() or (self._muted and isinstance(utterance, PartialUtterance)):
                return
            try:
                self._utterances.put_nowait((utterance, self._generation))
            except queue.Full:
                log.debug("очередь распознавания заполнена — пропускаю аудио")

    # --- скрипт в игре ---------------------------------------------------------------

    def _on_game_results(self, results: list[dict]) -> None:
        """Что скрипт сделал с командами: в журнал, отказы — ещё и в logs/errors.log."""
        for entry in results:
            latency = entry.get("latency_ms")
            self._on_result(
                ExecResult(
                    str(entry.get("label", "?")),
                    bool(entry.get("ok")),
                    str(entry.get("message", "")),
                    float(latency) if isinstance(latency, (int, float)) and not isinstance(latency, bool) else None,
                    deferred=bool(entry.get("deferred")),
                    info=bool(entry.get("info")),
                )
            )

    def _on_game_actions(self, actions: list[str]) -> None:
        """Клавиши из меню скрипта: разбудить, усыпить, мут."""
        for action in actions:
            if action == "wake":
                self.wake()
            elif action == "sleep":
                self.sleep()
            elif action == "mute" or (action == "toggle_mute" and not self._muted):
                self.set_muted(True)
            else:  # unmute
                self.set_muted(False)
                self.wake()

    def _on_level(self, value: float) -> None:
        self.bridge.set_level(value)
        self._notify("level", value)

    def _on_result(self, result: ExecResult) -> None:
        self._notify("result", result)

    def _status(self, key: str, text: str, ok: bool | None) -> None:
        self.statuses[key] = (text, ok)
        self._notify("status", key, text, ok)

    def _notify(self, method: str, *args: object) -> None:
        for sink in tuple(self.sinks):
            try:
                getattr(sink, method)(*args)
            except Exception:
                log.exception("ошибка обработчика интерфейса: %s", method)


def _log_late(late: list[ParsedCommand]) -> None:
    if late:
        labels = ", ".join(parsed.label for parsed in late)
        log.info("%s: распознано позже следующих команд — выполняю с опозданием", labels)


def _details(parts: int, early: int, total: int, saved: str | None = None) -> str:
    """Хвост строки лога: из скольких кусков распознана фраза, сколько команд выполнено до её конца
    и в каком файле logs/audio её звук."""
    details = []
    if parts > 1:
        details.append(f"кусков {parts}")
    if total > 1:
        details.append(f"на лету {early}/{total}")
    if saved:
        details.append(f"звук {saved}")
    return "".join(f" · {detail}" for detail in details)
