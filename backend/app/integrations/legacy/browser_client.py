"""Factory-only Windows launch boundary for the unchanged legacy CDP client."""
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from app.runtime_paths import chrome_executable


class WindowsChromeLaunch:
    def _start_debug_chrome(self):
        endpoint = urlsplit(str(self.config["cdp_endpoint"]))
        if (endpoint.scheme != "http" or endpoint.hostname != "127.0.0.1" or not endpoint.port
                or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment):
            raise ValueError("EXISTING_LOCAL_CDP_REQUIRED")
        profile = Path(self.config["profile_path"]).resolve()
        if not profile.is_dir():
            raise RuntimeError("EXISTING_CHROME_PROFILE_REQUIRED")
        chrome = chrome_executable()
        if not chrome.is_file():
            raise RuntimeError(f"CHROME_EXECUTABLE_MISSING: {chrome}")
        self.logger.info("starting existing Chrome profile with localhost CDP: %s", profile)
        self._chrome_process = subprocess.Popen([
            str(chrome), f"--user-data-dir={profile}", "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={endpoint.port}", "--no-first-run", "--no-default-browser-check"],
            shell=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", endpoint.port), timeout=.5):
                    return
            except OSError:
                if self._chrome_process.poll() is not None:
                    break
                time.sleep(.2)
        raise RuntimeError("CHROME_CDP_UNAVAILABLE: preserve profile and inspect the existing Chrome window")


def native_browser_class(legacy_class):
    if sys.platform != "win32":
        return legacy_class
    return type("FactoryWindowsBrowserClient", (WindowsChromeLaunch, legacy_class), {})
