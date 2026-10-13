"""Native executable paths; overrides remain the responsibility of Settings."""
import os
import sys
from pathlib import Path
from shutil import which


def venv_python(root: Path) -> Path:
    return Path(root) / (".venv/Scripts/python.exe" if sys.platform == "win32" else ".venv/bin/python")


def chrome_executable() -> Path:
    if sys.platform == "darwin":
        return Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if sys.platform == "win32":
        candidates = [Path(os.environ.get(name, fallback)) / "Google/Chrome/Application/chrome.exe"
            for name, fallback in (("ProgramFiles", "C:/Program Files"),
                                   ("ProgramFiles(x86)", "C:/Program Files (x86)"),
                                   ("LOCALAPPDATA", str(Path.home() / "AppData/Local")))]
        return next((path for path in candidates if path.is_file()), candidates[0])
    return Path(which("google-chrome") or which("chromium") or "google-chrome")
