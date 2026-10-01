from __future__ import annotations

import subprocess
import sys

import pytest

from jarvisvoice import cli


@pytest.mark.parametrize("argv", [[], ["--no-tray"], ["run", "--no-tray"]])
def test_default_run_dispatch(argv, monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "_setup_console", lambda: None)
    monkeypatch.setattr(cli, "_setup_logging", lambda verbose: None)
    monkeypatch.setattr(cli, "_run", lambda *args: calls.append(args) or 0)
    assert cli.main(argv) == 0
    assert len(calls) == 1
    assert calls[0][1:] == ("--no-tray" in argv,)


def test_help_does_not_import_audio_gui_or_native_input():
    result = subprocess.run(
        [sys.executable, "-c", "import jarvisvoice.cli, sys; print(','.join(sys.modules))"],
        capture_output=True,
        text=True,
        check=True,
    )
    imported = result.stdout.strip().split(",")
    assert all(name not in imported for name in ("jarvisvoice.app", "sounddevice", "PySide6", "jarvisvoice.winapi"))
    help_result = subprocess.run([sys.executable, "-m", "jarvisvoice", "--help"], capture_output=True, check=True)
    assert b"mic-test" in help_result.stdout and b"setup-gsi" not in help_result.stdout


def test_config_error_has_exit_code_two(tmp_path, monkeypatch):
    path = tmp_path / "broken.yaml"
    path.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(cli, "_setup_console", lambda: None)
    monkeypatch.setattr(cli, "_setup_logging", lambda verbose: None)
    assert cli.main(["devices", "--config", str(path)]) == 2


def test_glob_expansion_is_sorted_and_keeps_missing_paths(tmp_path):
    for name in ("b.wav", "a.wav"):
        (tmp_path / name).touch()
    missing = tmp_path / "missing.wav"
    assert cli._expand_globs([tmp_path / "*.wav", missing]) == [tmp_path / "a.wav", tmp_path / "b.wav", missing]


def test_headless_start_failure_still_stops_app(config, monkeypatch):
    from jarvisvoice import app

    stopped = []

    class BrokenApp:
        def __init__(self, *args, **kwargs):
            self.config = config

        def start(self):
            raise RuntimeError("start failed")

        def stop(self):
            stopped.append(True)

    monkeypatch.setattr(app, "BotApp", BrokenApp)
    with pytest.raises(RuntimeError, match="start failed"):
        cli._run(config.path, no_tray=True)
    assert stopped == [True]
