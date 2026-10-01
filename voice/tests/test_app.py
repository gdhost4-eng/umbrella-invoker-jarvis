from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import numpy as np

from jarvisvoice.app import BotApp
from jarvisvoice.asr import Transcript
from jarvisvoice.audio import PartialUtterance, Utterance

from .conftest import ROOT


class FakeRecognizer:
    def __init__(self, texts: list[str]) -> None:
        self.texts = iter(texts)
        self.lengths: list[int] = []  # сколько звука получил Whisper при каждом вызове

    def transcribe(self, audio) -> Transcript:  # noqa: ANN001
        self.lengths.append(len(audio))
        return Transcript(next(self.texts), 0.0, -0.1, 10.0)

    def set_prompt(self, prompt):
        self.prompt = prompt


def make_bot(texts: list[str], awake: bool = True) -> tuple[BotApp, list[str]]:
    """awake=True — бота уже позвали («Джарвис»), и команды идут без обращения."""
    bot = BotApp(ROOT / "config.yaml")
    bot.sinks = []
    submitted: list[str] = []
    bot.bridge.start = lambda: None  # порт в тестах не занимаем
    bot.bridge.push = lambda command, label, *times: submitted.append(label)
    bot.bridge.cancel = lambda: None
    bot.recognizer = FakeRecognizer(texts)
    if awake:
        bot.wake()
    return bot, submitted


def say(bot: BotApp, utterance_id: int = 0, duration_s: float = 1.0) -> None:
    now = time.monotonic()
    bot._handle_utterance(Utterance(np.zeros(16000), now, now, duration_s, id=utterance_id))


def test_realtime_fires_commands_while_phrase_continues():
    bot, submitted = make_bot(["атос санстрайк", "атос санстрайк метеор", "атос санстрайк метеор бласт"])
    audio = np.zeros(1600, dtype=np.float32)
    now = time.monotonic()

    bot._handle_partial(PartialUtterance(1, audio, now))
    assert submitted == ["Atos"]  # выполняем с первого результата, без повторного подтверждения
    audio = np.zeros(32000, dtype=np.float32)
    bot._handle_partial(PartialUtterance(1, audio, now))
    assert submitted == ["Atos", "Sun Strike"]
    bot._handle_utterance(Utterance(audio, now, now, 2.5, id=1))
    assert submitted == ["Atos", "Sun Strike", "Chaos Meteor", "Deafening Blast"]


def test_last_word_waits_for_end_of_phrase():
    bot, submitted = make_bot(["алакрити", "алакрити на союзника"])
    audio = np.zeros(1600, dtype=np.float32)
    now = time.monotonic()
    bot._handle_partial(PartialUtterance(2, audio, now))
    assert submitted == []
    bot._handle_utterance(Utterance(audio, now, now, 1.0, id=2))
    assert submitted == ["Alacrity на союзника"]


def test_rewritten_final_still_runs_its_new_commands():
    bot, submitted = make_bot(["атос санстрайк", "атос санстрайк", "метеор бласт"])
    audio = np.zeros(32000, dtype=np.float32)
    now = time.monotonic()
    for _ in range(2):
        bot._handle_partial(PartialUtterance(1, audio, now))
    assert submitted == ["Atos"]
    bot._handle_utterance(Utterance(audio, now, now, 2.0, id=1))
    assert submitted == ["Atos", "Chaos Meteor", "Deafening Blast"]


def test_final_that_dropped_a_command_keeps_the_rest_of_the_combo():
    partial = "блинк санстрайк метеор бласт атос хекс"
    bot, submitted = make_bot([partial, "блинк санстрайк метеор бласта тос хекс рефрешер"])
    audio = np.zeros(48000, dtype=np.float32)
    now = time.monotonic()
    bot._handle_partial(PartialUtterance(1, audio, now))
    assert submitted == ["Blink", "Sun Strike", "Chaos Meteor", "Deafening Blast", "Atos"]
    bot._handle_utterance(Utterance(audio, now, now, 3.0, id=1))
    assert submitted[5:] == ["Hex", "Refresher"]


def test_pause_fires_last_command_and_final_reuses_the_transcript():
    bot, submitted = make_bot(["санстрайк"])  # второго вызова Whisper не будет: StopIteration
    audio = np.zeros(16000, dtype=np.float32)
    now = time.monotonic()
    bot._handle_partial(PartialUtterance(1, audio, now, closing=True, speech_end_at=now - 0.1))
    assert submitted == ["Sun Strike"]
    bot._handle_utterance(Utterance(audio, now - 0.1, now, 1.0, id=1))
    assert submitted == ["Sun Strike"]


def test_speech_after_the_pause_is_transcribed_again():
    bot, submitted = make_bot(["санстрайк", "метеор"])
    now = time.monotonic()
    bot._handle_partial(PartialUtterance(1, np.zeros(16000, np.float32), now, closing=True, speech_end_at=now - 0.5))
    bot._handle_utterance(Utterance(np.zeros(24000, np.float32), now - 0.1, now, 1.5, id=1))
    assert submitted == ["Sun Strike", "Chaos Meteor"]
    assert bot.recognizer.lengths == [16000, 8000]  # после паузы — только новый звук


def test_combo_is_recognized_piece_by_piece_after_pauses():
    bot, submitted = make_bot(["Блинк, катаклизм.", "мет", "метеор, колд", "метеор, колд снэп."])
    now = time.monotonic()
    bot._handle_partial(PartialUtterance(1, np.zeros(16000, np.float32), now, closing=True, speech_end_at=now))
    assert submitted == ["Blink", "Cataclysm"]
    bot._handle_partial(PartialUtterance(1, np.zeros(20800, np.float32), now))
    bot._handle_partial(PartialUtterance(1, np.zeros(32000, np.float32), now))
    assert submitted == ["Blink", "Cataclysm", "Chaos Meteor"]
    bot._handle_partial(PartialUtterance(1, np.zeros(40000, np.float32), now, closing=True, speech_end_at=now + 1))
    bot._handle_utterance(Utterance(np.zeros(41000, np.float32), now + 1, now + 1, 2.6, id=1))
    assert submitted == ["Blink", "Cataclysm", "Chaos Meteor", "Cold Snap"]
    assert bot.recognizer.lengths == [16000, 4800, 16000, 24000]  # итог взят с паузы, Whisper не нужен


def test_piece_ending_with_a_fragment_is_heard_again_with_its_continuation():
    # «кат…» на паузе может оказаться началом «катаклизма»: этот кусок распознаётся заново.
    bot, submitted = make_bot(["Блинк, кат.", "блинк, катаклизм"])
    now = time.monotonic()
    bot._handle_partial(PartialUtterance(1, np.zeros(16000, np.float32), now, closing=True, speech_end_at=now))
    assert submitted == ["Blink"]
    bot._handle_utterance(Utterance(np.zeros(24000, np.float32), now + 0.4, now + 0.6, 1.5, id=1))
    assert submitted == ["Blink", "Cataclysm"]
    assert bot.recognizer.lengths == [16000, 24000]


def test_next_phrase_starts_without_pieces_of_the_previous_one():
    bot, submitted = make_bot(["санстрайк", "метеор"])
    now = time.monotonic()
    bot._handle_partial(PartialUtterance(1, np.zeros(16000, np.float32), now, closing=True, speech_end_at=now))
    bot._handle_utterance(Utterance(np.zeros(16000, np.float32), now, now, 1.0, id=1))
    bot._handle_utterance(Utterance(np.zeros(16000, np.float32), now, now, 1.0, id=2))
    assert submitted == ["Sun Strike", "Chaos Meteor"]
    assert bot.recognizer.lengths == [16000, 16000]


def test_partial_command_density_is_checked():
    bot, submitted = make_bot(["емп метеор бласт"] * 2)
    for _ in range(2):
        bot._handle_partial(PartialUtterance(1, np.zeros(1600, dtype=np.float32), time.monotonic()))
    assert submitted == []


def test_muting_during_transcription_discards_result():
    bot, submitted = make_bot([])

    def transcribe(audio):
        bot.set_muted(True)
        return Transcript("емп", 0.0, -0.1, 10.0)

    bot.recognizer.transcribe = transcribe
    now = time.monotonic()
    bot._handle_utterance(Utterance(np.zeros(16000), now, now, 1.0, id=1))
    assert submitted == []


def test_mute_command_stops_remaining_commands():
    bot, submitted = make_bot(["бот мут емп"])
    now = time.monotonic()
    bot._handle_utterance(Utterance(np.zeros(32000), now, now, 2.0, id=1))
    assert bot.muted
    assert submitted == []


def test_mute_then_unmute_during_transcription_discards_old_result():
    bot, submitted = make_bot([])

    def transcribe(audio):
        bot.set_muted(True)
        bot.set_muted(False)
        return Transcript("емп", 0.0, -0.1, 10.0)

    bot.recognizer.transcribe = transcribe
    now = time.monotonic()
    bot._handle_utterance(Utterance(np.zeros(16000), now, now, 1.0))
    assert not bot.muted and submitted == []


def test_reload_during_transcription_discards_old_result():
    bot, submitted = make_bot([])

    def transcribe(audio):
        bot.reload_config()
        return Transcript("емп", 0.0, -0.1, 10.0)

    bot.recognizer.transcribe = transcribe
    now = time.monotonic()
    bot._handle_utterance(Utterance(np.zeros(16000), now, now, 1.0))
    assert submitted == []


def test_accepted_phrase_is_parsed_once(monkeypatch):
    bot, submitted = make_bot(["скастуй", "метеор"])
    calls = []
    parse = bot.parser.parse

    def counted_parse(*args, **kwargs):
        calls.append(args[0])
        return parse(*args, **kwargs)

    monkeypatch.setattr(bot.parser, "parse", counted_parse)
    jobs = []
    monkeypatch.setattr(bot.bridge, "push", lambda command, *rest: jobs.append(command))
    now = time.monotonic()
    for index in range(2):
        bot._handle_utterance(Utterance(np.zeros(16000), now, now, 1.0, index))
    assert calls == ["скастуй", "метеор"]
    assert len(jobs) == 1 and jobs[0]["invoke_only"]


def test_muting_clears_audio_queue_and_pending_prefix():
    bot, _ = make_bot([])
    now = time.monotonic()
    bot.parser.parse("скастуй", now)
    bot._utterances_put(Utterance(np.zeros(16000), now, now, 1.0))
    bot.set_muted(True)
    bot.set_muted(False)
    assert bot._utterances.empty()
    assert not bot.parser.parse("метеор", now).commands[0].command.invoke_only


def test_bad_config_reload_preserves_running_configuration(tmp_path):
    bot, _ = make_bot([])
    config, parser = bot.config, bot.parser
    bot.config_path = tmp_path / "config.yaml"
    bot.config_path.write_text("[broken", encoding="utf-8")
    bot.reload_config()
    assert bot.config is config and bot.parser is parser
    assert bot.statuses["config"][1] is False


def test_failed_ui_sink_does_not_block_other_sinks():
    bot, _ = make_bot([])
    levels = []

    def fail(value):
        raise RuntimeError("UI failed")

    bot.sinks = [SimpleNamespace(level=fail), SimpleNamespace(level=levels.append)]
    bot._on_level(0.5)
    assert levels == [0.5]


def test_ui_callback_can_invalidate_current_phrase():
    bot, submitted = make_bot(["емп"])

    def heard(text):
        bot.set_muted(True)
        bot.set_muted(False)

    bot.sinks = [
        SimpleNamespace(
            heard=heard, muted_changed=lambda muted: None, level=lambda value: None, awake_changed=lambda awake: None
        )
    ]
    now = time.monotonic()
    bot._handle_utterance(Utterance(np.zeros(16000), now, now, 1.0))
    assert submitted == []


def test_stopping_during_model_load_does_not_start_microphone(monkeypatch):
    from jarvisvoice import asr

    bot, _ = make_bot([])
    loading, finish_loading = threading.Event(), threading.Event()
    started_audio = []

    def load(*args, **kwargs):
        loading.set()
        assert finish_loading.wait(3)
        return SimpleNamespace(set_prompt=lambda prompt: None, warning=None, device="cpu")

    monkeypatch.setattr(asr, "WhisperRecognizer", load)
    monkeypatch.setattr(bot.audio, "start", lambda: started_audio.append(True))
    bot.start()
    assert loading.wait(2)
    stopping = threading.Thread(target=bot.stop)
    stopping.start()
    assert bot._stopping.wait(2)
    finish_loading.set()
    stopping.join(3)
    assert not stopping.is_alive() and not bot._asr_thread.is_alive()
    assert started_audio == []
    bot.stop()


def test_asleep_bot_ignores_commands_without_wake_word():
    bot, submitted = make_bot(["санстрайк"], awake=False)
    say(bot)
    assert submitted == [] and not bot.awake


def test_wake_word_runs_rest_of_phrase_and_opens_window():
    bot, submitted = make_bot(["Джарвис, санстрайк", "метеор"], awake=False)
    say(bot, 1)
    assert submitted == ["Sun Strike"] and bot.awake
    say(bot, 2)  # в окне прослушивания обращение не нужно
    assert submitted == ["Sun Strike", "Chaos Meteor"]


def test_wake_word_alone_wakes_without_not_a_command_result():
    bot, submitted = make_bot(["Джарвис."], awake=False)
    results = []
    bot.sinks = [SimpleNamespace(result=results.append, awake_changed=lambda awake: None)]
    say(bot)
    assert bot.awake and submitted == [] and results == []


class NoiseRecognizer(FakeRecognizer):
    """Whisper распознал текст уверенно, но счёл фразу шумом по no_speech."""

    def transcribe(self, audio) -> Transcript:  # noqa: ANN001
        return Transcript("", 0.9, -0.3, 10.0, raw_text=next(self.texts))


def test_wake_word_rejected_as_noise_still_wakes():
    bot, submitted = make_bot([], awake=False)
    bot.recognizer = NoiseRecognizer(["Джарвис.", "Джарвис, санстрайк"])
    say(bot, 1)
    assert bot.awake and submitted == []
    bot._set_awake_until(0.0)
    say(bot, 2)  # команды из «шумной» фразы не выполняются — только пробуждение
    assert bot.awake and submitted == []


def test_noise_without_wake_word_or_too_short_does_not_wake():
    bot, submitted = make_bot([], awake=False)
    bot.recognizer = NoiseRecognizer(["Санстрайк.", "Джарвис."])
    say(bot, 1)
    say(bot, 2, duration_s=0.2)
    assert not bot.awake and submitted == []


def test_speech_before_wake_word_is_not_for_the_bot():
    bot, submitted = make_bot(["блинк джарвис торнадо"], awake=False)
    say(bot)
    assert submitted == ["Tornado"]


def test_wake_decision_is_kept_for_the_whole_phrase():
    bot, submitted = make_bot(["блинк джарвис атос санстрайк", "блинк джарвис атос санстрайк метеор"], awake=False)
    audio = np.zeros(32000, dtype=np.float32)
    now = time.monotonic()
    bot._handle_partial(PartialUtterance(1, audio, now))
    assert submitted == ["Atos"] and bot.awake
    # Бот уже проснулся, но начало этой фразы («блинк») всё равно было сказано не ему.
    bot._handle_utterance(Utterance(audio, now, now, 2.0, id=1))
    assert submitted == ["Atos", "Sun Strike", "Chaos Meteor"]


def test_window_closes_after_silence_and_notifies():
    bot, submitted = make_bot(["санстрайк"])
    changes = []
    bot.sinks = [SimpleNamespace(awake_changed=changes.append)]
    bot._awake_until = time.monotonic() - 5
    bot._check_awake()
    assert changes == [False]
    say(bot)
    assert submitted == []


def test_phrase_started_inside_window_is_finished_after_it_closes():
    bot, submitted = make_bot(["санстрайк метеор"])
    now = time.monotonic()
    bot._awake_until = now - 0.5  # окно закрылось посреди фразы длиной 2 с
    bot._handle_utterance(Utterance(np.zeros(32000), now, now, 2.0, id=1))
    assert submitted == ["Sun Strike", "Chaos Meteor"]


def test_commands_extend_the_window():
    bot, submitted = make_bot(["метеор"])
    bot._awake_until = time.monotonic() + 1
    say(bot)
    assert submitted == ["Chaos Meteor"]
    assert bot._awake_until > time.monotonic() + bot.config.wake.window_s - 1


def test_soft_mute_accepts_only_unmute_with_wake_word():
    bot, submitted = make_bot(["санстрайк", "включись", "джарвис включись"])
    bot.set_muted(True)
    assert not bot.awake
    say(bot, 1)
    say(bot, 2)
    assert bot.muted and submitted == []
    say(bot, 3)
    assert not bot.muted and bot.awake and submitted == []


def test_muted_bot_skips_partials_but_keeps_listening():
    bot, _ = make_bot([])
    bot.set_muted(True)
    now = time.monotonic()
    bot._utterances_put(PartialUtterance(1, np.zeros(1600, dtype=np.float32), now))
    assert bot._utterances.empty()
    bot._utterances_put(Utterance(np.zeros(16000), now, now, 1.0, id=1))
    assert bot._utterances.qsize() == 1


def test_sleep_command_stops_listening_and_skips_rest():
    bot, submitted = make_bot(["спи метеор"])
    say(bot)
    assert not bot.awake and submitted == []


def test_voice_quit_needs_exact_phrase():
    bot, submitted = make_bot(["выключи", "выключись"])
    quits = []
    bot.sinks = [SimpleNamespace(quit_requested=lambda: quits.append(True))]
    say(bot, 1)
    assert not bot.wait_for_quit(0) and quits == []
    say(bot, 2)
    assert bot.wait_for_quit(0) and quits == [True]
    assert submitted == []


def test_activity_reflects_bot_state():
    bot, _ = make_bot([], awake=False)
    assert bot.activity() == "loading"
    bot._ready = True
    assert bot.activity() == "sleeping"
    bot.wake()
    assert bot.activity() == "listening"
    bot.set_muted(True)
    assert bot.activity() == "muted"
    bot.wake()  # в муте разбудить можно только «Джарвис, включись»
    assert bot.activity() == "muted"
    bot._fatal = True
    assert bot.activity() == "error"


def test_wake_word_can_be_disabled():
    bot, submitted = make_bot(["санстрайк", "включись"], awake=False)
    bot.config.wake.enabled = False
    assert bot.awake
    say(bot, 1)
    assert submitted == ["Sun Strike"]
    bot.set_muted(True)
    say(bot, 2)
    assert not bot.muted


def test_command_heard_late_on_the_pause_is_still_pressed():
    # Лог 19.09 22:53: промежуточная гипотеза пропустила «хекс», гипотеза на паузе его услышала.
    bot, submitted = make_bot(["блинк атос катаклизм метеор", "блинк атос хекс катаклизм метеор"])
    now = time.monotonic()
    bot._handle_partial(PartialUtterance(1, np.zeros(32000, np.float32), now))
    assert submitted == ["Blink", "Atos", "Cataclysm"]
    bot._handle_partial(PartialUtterance(1, np.zeros(40000, np.float32), now, closing=True, speech_end_at=now))
    assert submitted == ["Blink", "Atos", "Cataclysm", "Hex", "Chaos Meteor"]
    bot._handle_utterance(Utterance(np.zeros(40000, np.float32), now, now, 2.5, id=1))
    assert submitted == ["Blink", "Atos", "Cataclysm", "Hex", "Chaos Meteor"]


def test_heard_phrases_are_recorded_with_the_file_in_the_log(tmp_path, caplog):
    bot, submitted = make_bot(["санстрайк"])
    bot.recorder.directory = tmp_path
    bot.recorder.limit = 5
    with caplog.at_level("INFO", logger="jarvisvoice.app"):
        say(bot, utterance_id=7)
    (saved,) = tmp_path.glob("*.wav")
    assert saved.name.endswith("-00007.wav")
    assert f"звук {saved.name}" in caplog.text
