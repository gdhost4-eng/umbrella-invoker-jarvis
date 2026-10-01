"""Распознавание речи: faster-whisper на видеокарте (с откатом на CPU)."""

from __future__ import annotations

import logging
import math
import os
import site
import sys
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

from .audio import SAMPLE_RATE
from .config import RecognitionCfg
from .paths import CUDA_DIR

log = logging.getLogger(__name__)

# DLL, без которых ctranslate2 не запустит Whisper на CUDA (cuBLAS 12 + подбиблиотеки cuDNN 9).
_CUDA_DLLS = ("cublasLt64_12.dll", "cublas64_12.dll", "cudnn_graph64_9.dll", "cudnn_ops64_9.dll", "cudnn_cnn64_9.dll")
_WARMUP_SAMPLES = SAMPLE_RATE
_DLL_DIRECTORIES: dict[Path, object] = {}
# Что Whisper «слышит» в шуме и дыхании: титры из обучающих субтитров.
_HALLUCINATIONS = ("субтитр", "корректор", "продолжение следует", "спасибо за просмотр", "подписывайтесь")
_FRAMES_PER_S = 100  # признаки Whisper — кадр 10 мс
_FULL_WINDOW_S = 30.0  # окно, на котором Whisper обучен
_MAX_INITIAL_TIMESTAMP_INDEX = 50  # как у faster-whisper (1 с)


class _Segment(Protocol):
    text: str
    no_speech_prob: float
    avg_logprob: float


@dataclass
class _Decoded:
    text: str
    no_speech_prob: float
    avg_logprob: float


@dataclass
class Transcript:
    text: str  # пусто, если фраза отброшена как шум
    no_speech_prob: float
    avg_logprob: float
    elapsed_ms: float
    raw_text: str = ""  # что Whisper выдал до фильтра шума


class Recognizer(ABC):
    """Интерфейс движка распознавания — Whisper можно заменить другим движком."""

    device = "cpu"
    warning: str | None = None

    def set_prompt(self, prompt: str) -> None:
        pass

    @abstractmethod
    def transcribe(self, audio: np.ndarray) -> Transcript:
        raise NotImplementedError


def _nvidia_dll_dirs() -> list[Path]:
    roots = {Path(p) for p in site.getsitepackages()} if hasattr(site, "getsitepackages") else set()
    roots.add(Path(sys.prefix) / "Lib" / "site-packages")
    roots.add(CUDA_DIR)  # сюда докачивает cuda.download, если в окружении нет пакетов nvidia-*
    dirs: list[Path] = []
    for root in sorted(roots):
        nvidia = root / "nvidia"
        if nvidia.is_dir():
            dirs.extend(sorted(p for p in nvidia.glob("*/bin") if p.is_dir()))
    return dirs


def prepare_cuda(progress: Callable[[str], None] = lambda text: None) -> str | None:
    """Подключает DLL cuBLAS/cuDNN из pip-пакетов nvidia-* или из докачанной папки.
    Возвращает причину, если их нет и скачать не вышло."""
    if sys.platform != "win32":
        return None
    problem = _link_cuda()
    if problem is None:
        return None
    try:
        from .cuda import download, has_nvidia_gpu

        if not has_nvidia_gpu():
            return problem
        download(CUDA_DIR, progress)
    except Exception as exc:
        log.warning("не удалось докачать cuBLAS/cuDNN: %s", exc)
        return f"{problem}; докачать не вышло ({exc})"
    return _link_cuda()


def _link_cuda() -> str | None:
    import ctypes

    for directory in _nvidia_dll_dirs():
        if directory not in _DLL_DIRECTORIES:
            # Дескриптор должен жить столько же, сколько модель использует DLL.
            _DLL_DIRECTORIES[directory] = os.add_dll_directory(str(directory))
            os.environ["PATH"] = str(directory) + os.pathsep + os.environ.get("PATH", "")
    missing = []
    for dll in _CUDA_DLLS:
        try:
            ctypes.WinDLL(dll)
        except OSError:
            missing.append(dll)
    if missing:
        return "нет " + ", ".join(missing)
    return None


def _model_path(model: str, models_dir: Path, progress: Callable[[str], None] = lambda text: None) -> str:
    """Путь к модели: уже скачанная берётся без обращения к интернету."""
    from faster_whisper.utils import download_model

    try:
        return download_model(model, local_files_only=True, cache_dir=str(models_dir))
    except Exception:
        progress(f"скачиваю модель Whisper «{model}» (~0,5 ГБ, один раз)")
        log.info("скачиваю модель Whisper «%s» в %s …", model, models_dir)
        return download_model(model, cache_dir=str(models_dir))


class WhisperRecognizer(Recognizer):
    _tokenizer = None  # для окна короче 30 с: создаётся при первой фразе
    _suppress: list[int] = []
    _prompt_tokens: list[int] | None = None

    def __init__(
        self, cfg: RecognitionCfg, models_dir: Path, progress: Callable[[str], None] = lambda text: None
    ) -> None:
        self.cfg = cfg
        self.prompt = ""
        self.warning = None
        models_dir.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

        problem = prepare_cuda(progress) if cfg.device == "cuda" else None
        model_path = _model_path(cfg.model, models_dir, progress)
        self._load_model(model_path, problem)

    def _load_model(self, model_path: str, problem: str | None) -> None:
        from faster_whisper import WhisperModel

        cfg = self.cfg
        self.model = None
        if cfg.device == "cuda" and problem is None:
            try:
                import ctranslate2

                if ctranslate2.get_cuda_device_count() < 1:
                    raise RuntimeError("видеокарта NVIDIA не найдена")
                compute = cfg.compute_type if cfg.compute_type != "auto" else "float16"
                self.model = WhisperModel(model_path, device="cuda", compute_type=compute)
                self.device = "cuda"
                self._warmup()
            except Exception as exc:  # падение CUDA не должно ронять бота
                log.exception("Whisper на CUDA не запустился")
                problem = str(exc)
                self.model = None

        if self.model is None:
            self._tokenizer = None
            compute = cfg.compute_type if cfg.device == "cpu" and cfg.compute_type != "auto" else "int8"
            self.model = WhisperModel(
                model_path,
                device="cpu",
                compute_type=compute,
                cpu_threads=min(8, os.cpu_count() or 4),
            )
            self.device = "cpu"
            if cfg.device == "cuda":
                self.warning = f"Whisper работает на CPU (медленнее): {problem}"
            self._warmup()

    def set_prompt(self, prompt: str) -> None:
        self.prompt = prompt
        self._prompt_tokens = None

    def _warmup(self) -> None:
        started = time.perf_counter()
        self.transcribe(np.zeros(_WARMUP_SAMPLES, dtype=np.float32))
        log.info("Whisper прогрет на %s за %.0f мс", self.device, (time.perf_counter() - started) * 1000)

    def transcribe(self, audio: np.ndarray) -> Transcript:
        started = time.perf_counter()
        audio = np.asarray(audio, dtype=np.float32)
        if audio.ndim != 1 or not np.isfinite(audio).all():
            raise ValueError("ожидается конечный моно-сигнал при 16 кГц")
        if not audio.size:
            return Transcript("", 1.0, -10.0, 0.0)
        segments = list(self._decode(np.ascontiguousarray(audio)))
        # Одна уверенная часть фразы не должна пропускать шум из остальных.
        accepted = [segment for segment in segments if not self._is_noise(segment)]
        return Transcript(
            text=" ".join(s.text.strip() for s in accepted).strip(),
            no_speech_prob=max((s.no_speech_prob for s in segments), default=1.0),
            avg_logprob=min((s.avg_logprob for s in segments), default=-10.0),
            elapsed_ms=(time.perf_counter() - started) * 1000,
            raw_text=" ".join(s.text.strip() for s in segments).strip(),
        )

    def _is_noise(self, segment: _Segment) -> bool:
        text = segment.text.lower()
        return (
            segment.no_speech_prob > self.cfg.no_speech_threshold
            or segment.avg_logprob < self.cfg.log_prob_threshold
            or any(phrase in text for phrase in _HALLUCINATIONS)
        )

    def _decode(self, audio: np.ndarray) -> Sequence[_Segment]:
        """Общие параметры декодирования; длинные комбо получают больший бюджет токенов.

        Бюджет — вдвое больше, чем нужно самой быстрой речи: зацикленная галлюцинация
        («ммм…», «ойс, ойс, …») обрывается раньше и не держит распознавание следующих фраз.
        """
        token_budget = min(192, max(32, math.ceil(audio.size / SAMPLE_RATE * 16)))
        if self.cfg.window_s < _FULL_WINDOW_S and self.cfg.language:
            window = int(self.cfg.window_s * _FRAMES_PER_S)
            features = self.model.feature_extractor(audio)
            if features.shape[-1] <= window:
                return [self._decode_window(features, window, token_budget)]
        segments, _ = self.model.transcribe(
            audio,
            language=self.cfg.language,
            task="transcribe",
            beam_size=self.cfg.beam_size,
            best_of=1,
            temperature=0.0,
            condition_on_previous_text=False,
            without_timestamps=True,
            initial_prompt=self.prompt or None,
            vad_filter=False,
            no_speech_threshold=self.cfg.no_speech_threshold,
            log_prob_threshold=self.cfg.log_prob_threshold,
            max_new_tokens=token_budget,
        )
        return list(segments)

    def _decode_window(self, features: np.ndarray, window: int, token_budget: int) -> _Decoded:
        """Фраза в окне энкодера короче 30 с (recognition.window_s).

        Обычный Whisper дополняет любой звук тишиной до 30 с, и энкодер обрабатывает всё окно.
        Команды длятся секунды: с окном 20 с распознавание на треть быстрее, а на проверочных
        фразах точность та же и на шуме текста нет. Параметры — как в faster-whisper.
        """
        from faster_whisper.audio import pad_or_trim

        tokenizer = self._whisper_tokenizer()
        prompt = self._prompt_ids(tokenizer)
        encoder_output = self.model.encode(pad_or_trim(features, window))
        result = self.model.model.generate(
            encoder_output,
            [prompt],
            beam_size=self.cfg.beam_size,
            patience=1.0,
            length_penalty=1.0,
            max_length=len(prompt) + token_budget,
            return_scores=True,
            return_no_speech_prob=True,
            suppress_blank=True,
            suppress_tokens=self._suppress,
            max_initial_timestamp_index=_MAX_INITIAL_TIMESTAMP_INDEX,
        )[0]
        tokens = result.sequences_ids[0]
        # Оценка нормирована по длине; средний логарифм вероятности — как считает faster-whisper.
        avg_logprob = result.scores[0] * len(tokens) / (len(tokens) + 1)
        return _Decoded(tokenizer.decode(tokens), result.no_speech_prob, avg_logprob)

    def _whisper_tokenizer(self):  # noqa: ANN202 - faster_whisper.tokenizer.Tokenizer
        if self._tokenizer is None:
            from faster_whisper.tokenizer import Tokenizer
            from faster_whisper.transcribe import get_suppressed_tokens

            tokenizer = Tokenizer(
                self.model.hf_tokenizer, self.model.model.is_multilingual, task="transcribe", language=self.cfg.language
            )
            self._suppress = list(get_suppressed_tokens(tokenizer, [-1]))
            self._prompt_tokens = None
            self._tokenizer = tokenizer
        return self._tokenizer

    def _prompt_ids(self, tokenizer) -> list[int]:  # noqa: ANN001
        prompt = self._prompt_tokens
        if prompt is None:
            previous = tokenizer.encode(" " + self.prompt.strip()) if self.prompt else []
            prompt = self._prompt_tokens = self.model.get_prompt(tokenizer, previous, without_timestamps=True)
        return prompt
