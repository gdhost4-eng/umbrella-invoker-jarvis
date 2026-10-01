"""Точка входа для сборки Jarvis.exe (PyInstaller)."""

import sys

from jarvisvoice.cli import main

if __name__ == "__main__":
    sys.exit(main())
