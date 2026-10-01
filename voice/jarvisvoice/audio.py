"""Микрофон и Silero VAD: непрерывный поток звука → готовые фразы."""

from __future__ import annotations

import collections
import logging
import math
import queue
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

from .config import MicrophoneCfg

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
FRAME = 512  # 32 мс — окно Silero VAD
CONTEXT = 64
FRAME_MS = FRAME * 1000 / SAMPLE_RATE
_HOSTAPI_PRIORITY = ("MME", "Windows DirectSound", "Windows WASAPI", "Windows WDM-KS")


class AudioError(RuntimeError):
    pass


@dataclass
class Utterance:
    audio: np.ndarray  # float32, 16 кГц, моно
    speech_end_at: float  # time.monotonic() последнего кадра с речью
    detected_at: float  # когда фраза закрыта
    duration_s: float
    id: int = 0  # номер фразы; у промежуточных кусков тот же


@dataclass
class PartialUtterance:
    """Фраза, которая ещё звучит: для распознавания на лету."""

    id: int
    audio: np.ndarray
    captured_at: float
    closing: bool = False  # речь только что стихла: скорее всего, фраза закончена
    speech_end_at: float = 0.0  # последний кадр с речью; совпал с итоговым — звук тот же

    @property
    def duration_s(self) -> float:
        return len(self.audio) / SAMPLE_RATE


class SileroVad:
    """Потоковый Silero VAD v6 (модель из пакета faster-whisper)."""

    def __init__(self, model_path: Path | None = None) -> None:
        import onnxruntime as ort

        if model_path is None:
            from faster_whisper.utils import get_assets_path

            model_path = Path(get_assets_path()) / "silero_vad_v6.onnx"
        options = ort.SessionOptions()
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 1
        options.log_severity_level = 4
        self._session = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"], sess_options=options)
        self.reset()

    def reset(self) -> None:
        self._h = np.zeros((1, 1, 128), dtype=np.float32)
        self._c = np.zeros((1, 1, 128), dtype=np.float32)
        self._context = np.zeros(CONTEXT, dtype=np.float32)

    def __call__(self, frame: np.ndarray) -> float:
        batch = np.concatenate([self._context, frame]).reshape(1, FRAME + CONTEXT).astype(np.float32, copy=False)
        probs, self._h, self._c = self._session.run(None, {"input": batch, "h": self._h, "c": self._c})
        self._context = frame[-CONTEXT:].astype(np.float32, copy=True)
        return float(np.asarray(probs).reshape(-1)[0])


class Segmenter:
    """Собирает кадры в фразы по вероятности речи (с гистерезисом и пре-роллом)."""

    def __init__(self, cfg: MicrophoneCfg) -> None:
        self.threshold = cfg.vad_threshold
        self.neg_threshold = max(0.05, cfg.vad_threshold - 0.15)
        self.silence_frames = max(1, math.ceil(cfg.vad_silence_ms / FRAME_MS))
        self.preroll_frames = max(0, math.ceil(cfg.preroll_ms / FRAME_MS))
        self.min_speech_frames = max(1, math.ceil(cfg.min_speech_ms / FRAME_MS))
        self.max_frames = max(1, math.ceil(cfg.max_speech_ms / FRAME_MS))
        self.tail_frames = math.ceil(100 / FRAME_MS)
        self.utterance_id = 0
        self.reset()

    @property
    def in_speech(self) -> bool:
        return self._in_speech

    @property
    def silence(self) -> int:
        """Сколько кадров подряд без речи внутри фразы."""
        return self._silence

    @property
    def last_speech_at(self) -> float:
        return self._last_speech_at

    def current_audio(self) -> np.ndarray:
        return np.concatenate(self._frames).astype(np.float32, copy=False) if self._frames else np.zeros(0, np.float32)

    def reset(self) -> None:
        self._preroll: collections.deque[np.ndarray] = collections.deque(maxlen=self.preroll_frames or 1)
        self._frames: list[np.ndarray] = []
        self._in_speech = False
        self._silence = 0
        self._speech_frames = 0
        self._last_speech_at = 0.0

    def push(self, frame: np.ndarray, prob: float, captured_at: float) -> Utterance | None:
        if not self._in_speech:
            if prob >= self.threshold:
                self._in_speech = True
                self.utterance_id += 1
                self._frames = [*self._preroll, frame] if self.preroll_frames else [frame]
                self._preroll.clear()
                self._silence = 0
                self._speech_frames = 1
                self._last_speech_at = captured_at
            elif self.preroll_frames:
                self._preroll.append(frame)
            return None

        self._frames.append(frame)
        if prob >= self.neg_threshold:
            self._silence = 0
            self._speech_frames += 1
            self._last_speech_at = captured_at
        else:
            self._silence += 1
        if self._silence >= self.silence_frames or len(self._frames) >= self.max_frames:
            return self._finish(captured_at)
        return None

    def _finish(self, now: float) -> Utterance | None:
        frames = self._frames
        extra_silence = self._silence - self.tail_frames
        if extra_silence > 0:
            frames = frames[:-extra_silence]
        speech_frames, last_speech_at = self._speech_frames, self._last_speech_at
        self.reset()
        if speech_frames < self.min_speech_frames or not frames:
            return None
        audio = np.concatenate(frames).astype(np.float32, copy=False)
        return Utterance(audio, last_speech_at, now, len(audio) / SAMPLE_RATE, self.utterance_id)


class StreamResampler:
    """Потоковый ресемплер (ФНЧ + линейная интерполяция) для микрофонов без 16 кГц."""

    def __init__(self, src_rate: int, dst_rate: int = SAMPLE_RATE, taps: int = 101) -> None:
        if src_rate <= 0 or dst_rate <= 0 or taps < 3 or taps % 2 == 0:
            raise ValueError("частоты должны быть положительными, taps — нечётным числом не меньше 3")
        self.step = src_rate / dst_rate
        cutoff = 0.45 * min(1.0, dst_rate / src_rate)
        n = np.arange(taps) - (taps - 1) / 2
        kernel = 2 * cutoff * np.sinc(2 * cutoff * n) * np.hamming(taps)
        self._kernel = (kernel / kernel.sum()).astype(np.float32)
        self.reset()

    def reset(self) -> None:
        self._tail = np.zeros(len(self._kernel) - 1, dtype=np.float32)
        self._buffer = np.zeros(0, dtype=np.float32)
        self._position = 0.0

    def process(self, chunk: np.ndarray) -> np.ndarray:
        if not chunk.size:
            return np.zeros(0, dtype=np.float32)
        padded = np.concatenate([self._tail, chunk.astype(np.float32, copy=False)])
        filtered = np.convolve(padded, self._kernel, mode="valid").astype(np.float32)
        self._tail = padded[-(len(self._kernel) - 1) :]
        self._buffer = np.concatenate([self._buffer, filtered])
        positions = np.arange(self._position, len(self._buffer) - 1, self.step)
        out = np.interp(positions, np.arange(len(self._buffer)), self._buffer).astype(np.float32)
        next_position = self._position + len(positions) * self.step
        consumed = min(int(next_position), len(self._buffer))
        self._buffer = self._buffer[consumed:]
        self._position = next_position - consumed  # может быть ≥ 1, если шагнули за конец буфера
        return out


def list_input_devices() -> list[dict]:
    import sounddevice as sd

    hostapis = sd.query_hostapis()
    devices = []
    for index, device in enumerate(sd.query_devices()):
        if device["max_input_channels"] > 0:
            devices.append(
                {
                    "index": index,
                    "name": device["name"],
                    "hostapi": hostapis[device["hostapi"]]["name"],
                    "rate": int(device["default_samplerate"]),
                }
            )
    return devices


def find_input_device(name_part: str | None) -> int | None:
    if not name_part:
        return None
    matches = [d for d in list_input_devices() if name_part.lower() in d["name"].lower()]
    if not matches:
        raise AudioError(f"микрофон «{name_part}» не найден — список: python -m jarvisvoice devices")
    matches.sort(key=lambda d: _HOSTAPI_PRIORITY.index(d["hostapi"]) if d["hostapi"] in _HOSTAPI_PRIORITY else 99)
    return matches[0]["index"]


def load_wav(path: Path) -> np.ndarray:
    """WAV → float32 моно 16 кГц."""
    with wave.open(str(path), "rb") as wav:
        channels, width, rate = wav.getnchannels(), wav.getsampwidth(), wav.getframerate()
        data = wav.readframes(wav.getnframes())
    if width == 2:
        audio = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768
    elif width == 4:
        audio = np.frombuffer(data, dtype=np.int32).astype(np.float32) / 2147483648
    elif width == 1:
        audio = (np.frombuffer(data, dtype=np.uint8).astype(np.float32) - 128) / 128
    else:
        raise AudioError(f"{path.name}: неподдерживаемая разрядность WAV")
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    if rate != SAMPLE_RATE:
        audio = StreamResampler(rate).process(audio)
    return audio.astype(np.float32, copy=False)


class AudioListener:
    """Захват микрофона в отдельном потоке и нарезка на фразы."""

    def __init__(
        self,
        cfg: MicrophoneCfg,
        on_utterance: Callable[[Utterance], None],
        on_level: Callable[[float], None] | None = None,
        on_partial: Callable[[PartialUtterance], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.on_utterance = on_utterance
        self.on_level = on_level
        self.on_partial = on_partial
        self.device_name = ""
        self._chunks: queue.Queue[tuple[np.ndarray, float, int] | None] = queue.Queue(maxsize=400)
        self._muted = threading.Event()
        self._stopping = threading.Event()
        self._lifecycle_lock = threading.RLock()
        self._capture_lock = threading.Lock()
        self._generation = 0
        self._stream = None
        self._thread: threading.Thread | None = None
        self._resampler: StreamResampler | None = None
        # 0 — досрочного распознавания нет; не позже конца фразы, иначе оно бессмысленно.
        early = math.ceil(cfg.early_silence_ms / FRAME_MS) if cfg.early_silence_ms > 0 else 0
        self._early_frames = early if early < math.ceil(cfg.vad_silence_ms / FRAME_MS) else 0

    def set_muted(self, muted: bool) -> None:
        with self._capture_lock:
            self._generation += 1
            self._drain_chunks()
            if muted:
                self._muted.set()
            else:
                self._muted.clear()
        if muted and self.on_level is not None:
            self.on_level(0.0)

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._stream is not None or self._stopping.is_set():
                return
            try:
                self._start_stream()
            except Exception:
                self.stop()
                raise

    def _start_stream(self) -> None:
        import sounddevice as sd

        device = find_input_device(self.cfg.device)
        info = sd.query_devices(device if device is not None else sd.default.device[0])
        self.device_name = str(info["name"])
        self._vad = SileroVad()
        self._segmenter = Segmenter(self.cfg)
        try:
            self._stream = sd.InputStream(
                device=device,
                channels=1,
                samplerate=SAMPLE_RATE,
                dtype="float32",
                blocksize=FRAME,
                callback=self._callback,
            )
        except sd.PortAudioError:
            rate = int(info["default_samplerate"])
            log.info("микрофон не умеет 16 кГц, пишу в %d Гц и пересэмплирую", rate)
            self._resampler = StreamResampler(rate)
            self._stream = sd.InputStream(
                device=device,
                channels=1,
                samplerate=rate,
                dtype="float32",
                blocksize=int(rate * FRAME / SAMPLE_RATE),
                callback=self._callback,
            )
        self._thread = threading.Thread(target=self._process, name="vad", daemon=True)
        self._thread.start()
        self._stream.start()

    def stop(self) -> None:
        self._stopping.set()
        with self._lifecycle_lock:
            stream, self._stream = self._stream, None
            try:
                if stream is not None:
                    try:
                        stream.stop()
                    finally:
                        stream.close()
            finally:
                with self._capture_lock:
                    self._drain_chunks()
                    self._chunks.put_nowait(None)
                if self._thread is not None:
                    if self._thread is not threading.current_thread():
                        self._thread.join(timeout=2)
                    self._thread = None

    def _drain_chunks(self) -> None:
        while True:
            try:
                self._chunks.get_nowait()
            except queue.Empty:
                return

    def _callback(self, indata, frames, time_info, status) -> None:  # noqa: ANN001 - сигнатура sounddevice
        if status:
            log.debug("микрофон: %s", status)
        with self._capture_lock:
            if self._stopping.is_set() or self._muted.is_set():
                return
            if self._chunks.full():
                # Потерянный звук разрывает фразу; старое аудио не должно копить задержку.
                self._drain_chunks()
                self._generation += 1
            self._chunks.put_nowait((indata[:, 0].copy(), time.monotonic(), self._generation))

    def _process(self) -> None:
        buffer = np.zeros(0, dtype=np.float32)
        generation = -1
        partial_id, last_partial_at = 0, 0.0
        peak = 0.0
        last_level_at = 0.0
        while not self._stopping.is_set():
            item = self._chunks.get()
            if item is None:
                return
            chunk, captured_at, captured_generation = item
            if self._muted.is_set() or captured_generation != self._generation:
                continue
            if generation != captured_generation:
                generation = captured_generation
                buffer = np.zeros(0, dtype=np.float32)
                self._vad.reset()
                self._segmenter.reset()
                if self._resampler is not None:
                    self._resampler.reset()
                peak = 0.0
            if self._resampler is not None:
                chunk = self._resampler.process(chunk)
            buffer = np.concatenate([buffer, chunk])
            while len(buffer) >= FRAME:
                frame, buffer = buffer[:FRAME], buffer[FRAME:]
                if self._stopping.is_set() or self._muted.is_set() or generation != self._generation:
                    break
                rms = float(np.sqrt(np.mean(frame * frame)) + 1e-9)
                peak = max(peak, min(1.0, max(0.0, (20 * math.log10(rms) + 60) / 50)))
                utterance = self._segmenter.push(frame, self._vad(frame), captured_at)
                if generation != self._generation or self._stopping.is_set():
                    break
                if utterance is not None:
                    self.on_utterance(utterance)
                elif self.on_partial is not None and self._segmenter.in_speech:
                    segmenter = self._segmenter
                    if segmenter.utterance_id != partial_id:
                        partial_id, last_partial_at = segmenter.utterance_id, captured_at
                    elif segmenter.silence == 0:
                        if captured_at - last_partial_at >= self.cfg.partial_interval_ms / 1000:
                            last_partial_at = captured_at
                            self.on_partial(PartialUtterance(partial_id, segmenter.current_audio(), captured_at))
                    elif segmenter.silence == self._early_frames:
                        # Не ждём vad_silence_ms: слово уже сказано целиком, а распознавание займёт время.
                        last_partial_at = captured_at
                        self.on_partial(
                            PartialUtterance(
                                partial_id,
                                segmenter.current_audio(),
                                captured_at,
                                closing=True,
                                speech_end_at=segmenter.last_speech_at,
                            )
                        )
            if self.on_level is not None and captured_at - last_level_at >= 0.1:
                self.on_level(0.0 if self._muted.is_set() else peak)
                peak = 0.0
                last_level_at = captured_at
