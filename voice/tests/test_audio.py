from __future__ import annotations

import sys
from types import SimpleNamespace

import numpy as np
import pytest

from jarvisvoice.audio import FRAME, FRAME_MS, SAMPLE_RATE, AudioListener, Segmenter, SileroVad, StreamResampler
from jarvisvoice.config import MicrophoneCfg


def feed(segmenter: Segmenter, probs: list[float]) -> list:
    utterances = []
    for i, prob in enumerate(probs):
        frame = np.full(FRAME, i, dtype=np.float32)
        utterance = segmenter.push(frame, prob, captured_at=i * FRAME_MS / 1000)
        if utterance is not None:
            utterances.append(utterance)
    return utterances


def test_phrase_is_cut_after_silence_with_preroll():
    cfg = MicrophoneCfg(vad_silence_ms=250, preroll_ms=100, min_speech_ms=90)
    segmenter = Segmenter(cfg)
    probs = [0.0] * 10 + [0.9] * 10 + [0.0] * 20
    (utterance,) = feed(segmenter, probs)
    frames = utterance.audio.reshape(-1, FRAME)[:, 0]
    assert frames[0] == 10 - segmenter.preroll_frames  # пре-ролл перед речью
    assert frames[segmenter.preroll_frames] == 10
    # Хвост тишины обрезан до ~100 мс.
    assert frames[-1] == 19 + segmenter.tail_frames
    assert utterance.speech_end_at == 19 * FRAME_MS / 1000


def test_short_noise_is_ignored():
    segmenter = Segmenter(MicrophoneCfg(min_speech_ms=150))
    assert feed(segmenter, [0.0, 0.9, 0.0] + [0.0] * 20) == []


def test_long_phrase_is_split_at_max_length():
    cfg = MicrophoneCfg(max_speech_ms=640)
    segmenter = Segmenter(cfg)
    utterances = feed(segmenter, [0.9] * 50)
    assert utterances and len(utterances[0].audio) <= segmenter.max_frames * FRAME


def test_hysteresis_keeps_phrase_together():
    segmenter = Segmenter(MicrophoneCfg(vad_threshold=0.5, vad_silence_ms=100))
    probs = [0.9] * 5 + [0.4] * 5 + [0.9] * 5 + [0.0] * 10  # 0.4 выше порога конца речи (0.35)
    assert len(feed(segmenter, probs)) == 1


def test_soft_short_word_counts_as_speech_after_onset():
    segmenter = Segmenter(MicrophoneCfg(vad_threshold=0.5, min_speech_ms=150))
    (utterance,) = feed(segmenter, [0.9] + [0.4] * 5 + [0.0] * 10)
    assert utterance.speech_end_at == 5 * FRAME_MS / 1000


def test_resampler_48k_to_16k_keeps_tone():
    source_rate = 48000
    t = np.arange(source_rate) / source_rate
    tone = np.sin(2 * np.pi * 440 * t).astype(np.float32)
    resampler = StreamResampler(source_rate)
    out = np.concatenate([resampler.process(chunk) for chunk in np.array_split(tone, 97)])
    assert abs(len(out) - SAMPLE_RATE) <= 2
    spectrum = np.abs(np.fft.rfft(out[1000:15000]))
    peak_hz = np.argmax(spectrum) * SAMPLE_RATE / len(out[1000:15000])
    assert abs(peak_hz - 440) < 5


def test_silero_vad_runs_on_silence():
    vad = SileroVad()
    probs = [vad(np.zeros(FRAME, dtype=np.float32)) for _ in range(10)]
    assert all(0.0 <= p < 0.3 for p in probs)


def test_empty_chunk_does_not_change_resampler_state():
    first = StreamResampler(48000)
    second = StreamResampler(48000)
    chunk = np.arange(512, dtype=np.float32)
    first.process(chunk)
    second.process(chunk)
    assert first.process(np.zeros(0, dtype=np.float32)).size == 0
    np.testing.assert_array_equal(first.process(chunk), second.process(chunk))


@pytest.mark.parametrize("src,dst,taps", [(0, 16000, 101), (16000, -1, 101), (48000, 16000, 2)])
def test_resampler_rejects_invalid_parameters(src, dst, taps):
    with pytest.raises(ValueError):
        StreamResampler(src, dst, taps)


def test_muting_flushes_captured_audio():
    listener = AudioListener(MicrophoneCfg(), lambda utterance: None)
    samples = np.zeros((FRAME, 1), dtype=np.float32)
    listener._callback(samples, FRAME, None, None)
    assert not listener._chunks.empty()
    listener.set_muted(True)
    listener._callback(samples, FRAME, None, None)
    assert listener._chunks.empty()
    listener.set_muted(False)
    listener._callback(samples, FRAME, None, None)
    assert listener._chunks.get_nowait()[2] == listener._generation


def test_stop_drains_full_capture_queue():
    listener = AudioListener(MicrophoneCfg(), lambda utterance: None)
    for _ in range(listener._chunks.maxsize):
        listener._chunks.put_nowait((np.zeros(0), 0.0, 0))
    listener.stop()
    assert listener._stopping.is_set()
    assert listener._chunks.get_nowait() is None
    assert listener._chunks.empty()


def test_stream_start_failure_closes_resources(monkeypatch):
    from jarvisvoice import audio

    closed = []

    def fail():
        raise OSError("microphone failed")

    stream = SimpleNamespace(start=fail, stop=lambda: None, close=lambda: closed.append(True))
    sounddevice = SimpleNamespace(
        query_devices=lambda device: {"name": "fake", "default_samplerate": 16000},
        default=SimpleNamespace(device=[0]),
        InputStream=lambda **kwargs: stream,
        PortAudioError=OSError,
    )
    monkeypatch.setitem(sys.modules, "sounddevice", sounddevice)
    monkeypatch.setattr(audio, "SileroVad", lambda: SimpleNamespace(reset=lambda: None))
    listener = AudioListener(MicrophoneCfg(), lambda utterance: None)
    with pytest.raises(OSError, match="microphone failed"):
        listener.start()
    assert listener._thread is None and listener._stream is None
    assert closed == [True]
    listener.stop()
    assert closed == [True]


def test_short_pause_sends_closing_partial_before_phrase_ends():
    from jarvisvoice.audio import Segmenter

    cfg = MicrophoneCfg(vad_silence_ms=250, early_silence_ms=90, partial_interval_ms=10_000, min_speech_ms=90)
    utterances, partials = [], []
    listener = AudioListener(cfg, utterances.append, on_partial=partials.append)
    probs = iter([0.9] * 10 + [0.0] * 12)

    class FakeVad:
        def reset(self):
            pass

        def __call__(self, frame):
            return next(probs)

    listener._vad = FakeVad()
    listener._segmenter = Segmenter(cfg)
    for i in range(22):
        listener._chunks.put_nowait((np.zeros(FRAME, dtype=np.float32), i * FRAME_MS / 1000, listener._generation))
    listener._chunks.put_nowait(None)
    listener._process()
    (closing,) = partials
    (utterance,) = utterances
    assert closing.closing and closing.id == utterance.id
    assert closing.speech_end_at == utterance.speech_end_at
    # Досрочно — через 3 кадра тишины, а не через 8.
    assert closing.captured_at < utterance.detected_at
