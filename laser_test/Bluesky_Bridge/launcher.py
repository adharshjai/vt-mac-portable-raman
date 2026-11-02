"""Small EXE entry point. The local environment and app stay beside the EXE."""
import ctypes
import os
from pathlib import Path
import subprocess
import sys


def main():
    root = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
    python = root / ".venv/Scripts/pythonw.exe"
    if not python.exists():
        ctypes.windll.user32.MessageBoxW(None, "Run Setup_Bluesky.cmd first. Keep the EXE in the Bluesky_Bridge folder.", "Bluesky Bridge", 16)
        return
    log = root / "launcher.log"
    args = []
    if len(sys.argv) == 3 and sys.argv[1] == "--preview-image":
        args = sys.argv[1:]
    try:
        with log.open("a", encoding="utf-8") as stream:
            subprocess.Popen([str(python), str(root / "app.py"), *args], cwd=str(root),
                             stdout=stream, stderr=stream, creationflags=subprocess.CREATE_NO_WINDOW)
    except OSError as exc:
        ctypes.windll.user32.MessageBoxW(None, str(exc), "Bluesky Bridge", 16)


if __name__ == "__main__":
    main()
