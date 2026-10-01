"""Проверка микрофона и распознавания без игры."""

from __future__ import annotations

import queue
import time
from pathlib import Path

from .asr import Transcript
from .audio import Utterance
from .commands import SpellCmd
from .config import load_config
from .nlu import CommandParser, ParseResult, WakeWord, build_prompt
from .paths import MODELS_DIR


def describe_parse(result: ParseResult) -> str:
    if not result.commands:
        return "жду спелл после «скастуй»" if result.waiting_for_spell else "не команда"
    parts = []
    for parsed in result.commands:
        label = parsed.label
        if isinstance(parsed.command, SpellCmd) and parsed.command.invoke_only:
            label += " (только вызвать)"
        parts.append(f"{label} [{parsed.score:.0f}]")
    return ", ".join(parts)


def _describe_transcript(transcript: Transcript, parser: CommandParser, now: float, wake: WakeWord) -> str:
    if not transcript.text:
        return f"шум, пропущено (no_speech={transcript.no_speech_prob:.2f}: «{transcript.raw_text}»)"
    # Обращение необязательно: mic-test проверяет и сами команды, и слово активации.
    addressed, text = wake.split(transcript.text, anywhere=False)
    if not addressed:
        text = transcript.text
    mark = "[обращение] " if addressed else ""
    return f"«{transcript.text}» → {mark}{describe_parse(parser.parse(text, now))}"


def mic_test(config_path: Path, wav_files: list[Path]) -> int:
    """Распознавание без игры: печатает текст, команды и задержки."""
    from .asr import WhisperRecognizer
    from .audio import AudioListener, load_wav

    config = load_config(config_path)
    parser = CommandParser(config)
    wake = WakeWord(config.wake.say, config.recognition.match_threshold)
    for conflict in parser.conflicts:
        print(f"⚠ {conflict}")
    print(f"Загружаю Whisper «{config.recognition.model}»…")
    recognizer = WhisperRecognizer(config.recognition, MODELS_DIR)
    recognizer.set_prompt(build_prompt(config) if config.recognition.use_prompt else "")
    print(f"Whisper работает на {recognizer.device.upper()}")
    if recognizer.warning:
        print(f"⚠ {recognizer.warning}")

    if wav_files:
        for path in wav_files:
            transcript = recognizer.transcribe(load_wav(path))
            described = _describe_transcript(transcript, parser, time.monotonic(), wake)
            print(f"{path.name}: {described} · {transcript.elapsed_ms:.0f} мс")
        return 0

    utterances: queue.Queue[Utterance] = queue.Queue()
    listener = AudioListener(config.microphone, utterances.put)
    listener.start()
    print(f"Микрофон: {listener.device_name}")
    print("Говори команды. Нажатий не будет. Ctrl+C — выход.\n")
    try:
        while True:
            utterance = utterances.get()
            transcript = recognizer.transcribe(utterance.audio)
            now = time.monotonic()
            print(
                f"[{utterance.duration_s:.1f} с] {_describe_transcript(transcript, parser, now, wake)}"
                f" · Whisper {transcript.elapsed_ms:.0f} мс"
                f" · от конца речи {(now - utterance.speech_end_at) * 1000:.0f} мс"
            )
    except KeyboardInterrupt:
        return 0
    finally:
        listener.stop()
