"""Аргументы командной строки и запуск выбранного режима."""

from __future__ import annotations

import argparse
import glob
import logging
import logging.handlers
import os
import sys
import threading
from pathlib import Path

from .paths import LOGS_DIR, default_config

log = logging.getLogger("jarvisvoice")
_SUBCOMMANDS = ("run", "mic-test", "devices")
_LOG_HANDLERS: list[logging.Handler] = []


def _setup_console() -> None:
    # Jarvis.exe и pythonw работают без консоли: stdout/stderr равны None.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _setup_logging(verbose: bool) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for handler in _LOG_HANDLERS:
        root.removeHandler(handler)
        handler.close()
    _LOG_HANDLERS.clear()
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)
    logfile = logging.handlers.RotatingFileHandler(
        LOGS_DIR / "jarvis.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    logfile.setFormatter(formatter)
    root.addHandler(logfile)
    # Только предупреждения и ошибки, с датой: сюда смотреть, если бот что-то не сделал.
    errors = logging.handlers.RotatingFileHandler(
        LOGS_DIR / "errors.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8"
    )
    errors.setLevel(logging.WARNING)
    # Советы huggingface_hub (hf_xet, HF_TOKEN) при скачивании модели проблемами не являются.
    errors.addFilter(lambda record: not record.name.startswith("huggingface_hub"))
    errors.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"))
    root.addHandler(errors)
    _LOG_HANDLERS.extend((console, logfile, errors))
    for noisy in ("httpx", "httpcore", "faster_whisper", "urllib3", "filelock"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("huggingface_hub").setLevel(logging.ERROR)
    _log_uncaught_exceptions()


def _log_uncaught_exceptions() -> None:
    """Без консоли (pythonw) необработанные исключения иначе пропадают бесследно."""

    def excepthook(kind, value, traceback) -> None:  # noqa: ANN001
        if issubclass(kind, KeyboardInterrupt):
            sys.__excepthook__(kind, value, traceback)
            return
        log.critical("необработанная ошибка", exc_info=(kind, value, traceback))

    def thread_excepthook(args: threading.ExceptHookArgs) -> None:
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread is not None else "?"
        log.critical(
            "необработанная ошибка в потоке %s", name, exc_info=(args.exc_type, args.exc_value, args.exc_traceback)
        )

    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook


def _build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, default=None, help="путь к config.yaml")
    common.add_argument("-v", "--verbose", action="store_true", help="подробный лог")

    parser = argparse.ArgumentParser(
        prog="python -m jarvisvoice", description="Голосовой помощник для скрипта Jarvis (Invoker, Umbrella)"
    )
    sub = parser.add_subparsers(dest="command", metavar="команда")
    run = sub.add_parser("run", parents=[common], help="запустить помощника (по умолчанию)")
    run.add_argument("--no-tray", action="store_true", help="без значка в трее, только консоль")
    mic = sub.add_parser("mic-test", parents=[common], help="проверка распознавания без игры")
    mic.add_argument("wav", nargs="*", type=Path, help="распознать WAV-файлы вместо микрофона")
    sub.add_parser("devices", parents=[common], help="список микрофонов")
    return parser


def main(argv: list[str] | None = None) -> int:
    _setup_console()
    args_list = list(sys.argv[1:] if argv is None else argv)
    if not args_list or args_list[0] not in (*_SUBCOMMANDS, "-h", "--help"):
        args_list.insert(0, "run")
    args = _build_parser().parse_args(args_list)
    args.config = args.config or default_config()
    _setup_logging(args.verbose)

    from .config import ConfigError, load_config

    try:
        if args.command == "devices":
            return _devices(load_config(args.config).microphone.device)
        if args.command == "mic-test":
            from .diagnostics import mic_test

            _keep_responsive()
            return mic_test(args.config, _expand_globs(args.wav))
        return _run(args.config, args.no_tray)
    except ConfigError as exc:
        log.error("Ошибка в конфиге: %s", exc)
        return 2


def _expand_globs(paths: list[Path]) -> list[Path]:
    """PowerShell не раскрывает *.wav сам — раскрываем здесь."""
    expanded: list[Path] = []
    for path in paths:
        matches = sorted(glob.glob(str(path))) if glob.has_magic(str(path)) else []
        if matches:
            expanded.extend(Path(match) for match in matches)
        else:
            expanded.append(path)
    return expanded


def _keep_responsive() -> None:
    from .winapi import keep_responsive

    problems = keep_responsive()
    if problems:
        log.warning("процесс бота: %s", "; ".join(problems))


def _run(config_path: Path, no_tray: bool) -> int:
    from .app import BotApp

    _keep_responsive()
    app = BotApp(config_path)
    if no_tray:
        try:
            app.start()
            log.info("Помощник запущен без значка в трее. Ctrl+C — выход.")
            while not app.wait_for_quit(0.5):
                pass
            return 0
        except KeyboardInterrupt:
            return 0
        finally:
            app.stop()

    from .tray import run_tray

    return run_tray(app)


def _devices(configured: str | None) -> int:
    from .audio import AudioError, find_input_device, list_input_devices

    try:
        chosen = find_input_device(configured)
    except AudioError as exc:
        print(f"⚠ {exc}")
        chosen = None
    for device in list_input_devices():
        mark = "→" if device["index"] == chosen else " "
        print(f"{mark} [{device['index']:>2}] {device['name']}  ({device['hostapi']}, {device['rate']} Гц)")
    print(f"\nВ конфиге: microphone.device = {configured!r} (ищется по части имени)")
    return 0
