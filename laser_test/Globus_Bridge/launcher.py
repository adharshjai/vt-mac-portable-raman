"""Small Windows launcher; all runtime files remain in Globus_Bridge."""
import ctypes
from pathlib import Path
import subprocess
import sys


def main():
    root = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
    python = root / ".venv/Scripts/pythonw.exe"
    if not python.exists():
        ctypes.windll.user32.MessageBoxW(None, "Run Setup_Globus.cmd first. Keep the EXE in the Globus_Bridge folder.", "Globus Bridge", 16)
        return
    args = sys.argv[1:] if len(sys.argv) == 3 and sys.argv[1] == "--preview-image" else []
    try:
        with (root / "launcher.log").open("a", encoding="utf-8") as log:
            subprocess.Popen([str(python), str(root / "app.py"), *args], cwd=str(root),
                             stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)
    except OSError as exc:
        ctypes.windll.user32.MessageBoxW(None, str(exc), "Globus Bridge", 16)


if __name__ == "__main__":
    main()
