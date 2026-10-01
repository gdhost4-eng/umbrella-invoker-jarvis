from types import SimpleNamespace

import numpy as np
import pytest

from jarvisvoice.asr import WhisperRecognizer
from jarvisvoice.config import RecognitionCfg


def make_recognizer(segments):
    recognizer = WhisperRecognizer.__new__(WhisperRecognizer)
    recognizer.cfg = RecognitionCfg()
    recognizer.prompt = "емп, метеор"
    calls = []

    def transcribe(audio, **options):
        calls.append((audio, options))
        return iter(segments), None

    recognizer.model = SimpleNamespace(transcribe=transcribe)
    return recognizer, calls


def segment(text, no_speech=0.1, logprob=-0.2):
    return SimpleNamespace(text=text, no_speech_prob=no_speech, avg_logprob=logprob)


def test_bad_segments_cannot_pass_alongside_good_speech():
    recognizer, _ = make_recognizer(
        [
            segment("емп"),
            segment("бласт", no_speech=0.9),
            segment("метеор", logprob=-2.0),
        ]
    )
    result = recognizer.transcribe(np.zeros(16000, dtype=np.float32))
    assert result.text == "емп"
    assert result.raw_text == "емп бласт метеор"
    assert result.no_speech_prob == 0.9
    assert result.avg_logprob == -2.0


def test_all_noise_is_rejected():
    recognizer, _ = make_recognizer([segment("емп", no_speech=0.9)])
    assert recognizer.transcribe(np.zeros(16000)).text == ""


def test_empty_audio_does_not_call_model():
    recognizer, calls = make_recognizer([])
    assert recognizer.transcribe(np.zeros(0)).text == ""
    assert calls == []


@pytest.mark.parametrize("audio", [np.zeros((10, 2)), np.array([np.nan]), np.array([np.inf])])
def test_invalid_audio_is_rejected(audio):
    recognizer, calls = make_recognizer([])
    with pytest.raises(ValueError):
        recognizer.transcribe(audio)
    assert calls == []


def test_token_budget_scales_with_phrase_length():
    recognizer, calls = make_recognizer([])
    recognizer.transcribe(np.zeros(16000))
    recognizer.transcribe(np.zeros(8 * 16000))
    recognizer.transcribe(np.zeros(15 * 16000))
    # 16 токенов на секунду — вдвое больше самой быстрой речи: галлюцинация «ммм…» обрывается раньше.
    assert [call[1]["max_new_tokens"] for call in calls] == [32, 128, 192]
    assert calls[0][0].dtype == np.float32
    assert calls[0][1]["initial_prompt"] == "емп, метеор"


def test_cuda_directory_handles_are_retained_and_not_duplicated(monkeypatch, tmp_path):
    import ctypes

    from jarvisvoice import asr

    added = []
    handle = object()

    def add_directory(path):
        added.append(path)
        return handle

    monkeypatch.setattr(asr.sys, "platform", "win32")
    monkeypatch.setattr(asr, "_DLL_DIRECTORIES", {})
    monkeypatch.setattr(asr, "_nvidia_dll_dirs", lambda: [tmp_path])
    monkeypatch.setattr(asr.os, "add_dll_directory", add_directory, raising=False)
    monkeypatch.setattr(ctypes, "WinDLL", lambda dll: object(), raising=False)
    monkeypatch.setenv("PATH", "original")
    assert asr.prepare_cuda() is None
    assert asr.prepare_cuda() is None
    assert added == [str(tmp_path)]
    assert asr._DLL_DIRECTORIES[tmp_path] is handle


def test_cuda_warmup_failure_falls_back_to_cpu(monkeypatch, tmp_path):
    import ctranslate2
    import faster_whisper

    from jarvisvoice import asr

    devices = []

    def create_model(path, **kwargs):
        devices.append(kwargs["device"])
        return object()

    def warmup(self):
        if self.device == "cuda":
            raise RuntimeError("CUDA warmup failed")

    monkeypatch.setattr(asr, "prepare_cuda", lambda *args: None)
    monkeypatch.setattr(asr, "_model_path", lambda *args: "cached-model")
    monkeypatch.setattr(ctranslate2, "get_cuda_device_count", lambda: 1)
    monkeypatch.setattr(faster_whisper, "WhisperModel", create_model)
    monkeypatch.setattr(asr.WhisperRecognizer, "_warmup", warmup)
    recognizer = asr.WhisperRecognizer(RecognitionCfg(device="cuda"), tmp_path)
    assert devices == ["cuda", "cpu"]
    assert recognizer.device == "cpu"
    assert "CUDA warmup failed" in recognizer.warning


def test_short_clear_word_with_high_no_speech_is_kept():
    # У коротких чётких слов Whisper даёт no_speech до ~0.6 — раньше такие фразы терялись.
    recognizer, _ = make_recognizer([segment("бласт", no_speech=0.7, logprob=-0.3)])
    assert recognizer.transcribe(np.zeros(16000)).text == "бласт"


def test_whisper_noise_hallucinations_are_rejected():
    recognizer, _ = make_recognizer([segment("Субтитры субтитров А.Семкин", no_speech=0.5, logprob=-0.6)])
    result = recognizer.transcribe(np.zeros(16000))
    assert result.text == "" and result.raw_text.startswith("Субтитры")


class FakeTokenizer:
    def encode(self, text):
        return [len(word) for word in text.split()]

    def decode(self, tokens):
        return " санстрайк" if tokens else ""


def make_window_recognizer(window_s=20.0, frames=300):
    """Whisper с укороченным окном: энкодер и декодер вызываются напрямую."""
    recognizer = WhisperRecognizer.__new__(WhisperRecognizer)
    recognizer.cfg = RecognitionCfg(window_s=window_s)
    recognizer.prompt = "емп, метеор"
    recognizer._tokenizer = FakeTokenizer()
    recognizer._suppress = [7]
    calls = {"encode": [], "generate": [], "transcribe": 0}

    def generate(encoder_output, prompts, **options):
        calls["generate"].append((prompts[0], options))
        return [SimpleNamespace(sequences_ids=[[11, 12, 13]], scores=[-0.4], no_speech_prob=0.2)]

    def transcribe(audio, **options):
        calls["transcribe"] += 1
        return iter([segment("метеор")]), None

    recognizer.model = SimpleNamespace(
        feature_extractor=lambda audio: np.ones((80, frames), dtype=np.float32),
        encode=lambda features: calls["encode"].append(features.shape) or "encoded",
        get_prompt=lambda tokenizer, previous, without_timestamps: [50258, *previous],
        model=SimpleNamespace(generate=generate),
        transcribe=transcribe,
    )
    return recognizer, calls


def test_short_window_runs_encoder_on_20_seconds():
    recognizer, calls = make_window_recognizer()
    result = recognizer.transcribe(np.zeros(3 * 16000, dtype=np.float32))
    assert calls["encode"] == [(80, 2000)]
    prompt, options = calls["generate"][0]
    assert prompt == [50258, 4, 6]  # подсказка Whisper — как у faster-whisper
    assert options["max_length"] == len(prompt) + 48  # бюджет 16 токенов на секунду
    assert options["suppress_tokens"] == [7] and options["return_no_speech_prob"]
    assert result.text == "санстрайк"
    assert result.avg_logprob == -0.4 * 3 / 4
    assert calls["transcribe"] == 0


def test_phrase_longer_than_window_uses_full_whisper():
    recognizer, calls = make_window_recognizer(window_s=5.0, frames=600)
    assert recognizer.transcribe(np.zeros(6 * 16000, dtype=np.float32)).text == "метеор"
    assert calls["transcribe"] == 1 and calls["encode"] == []


def test_new_prompt_is_used_in_short_window():
    recognizer, calls = make_window_recognizer()
    recognizer.transcribe(np.zeros(16000, dtype=np.float32))
    recognizer.set_prompt("бласт")
    recognizer.transcribe(np.zeros(16000, dtype=np.float32))
    assert [call[0] for call in calls["generate"]] == [[50258, 4, 6], [50258, 5]]
