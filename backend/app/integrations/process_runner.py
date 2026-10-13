from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

if os.name != "nt":
    import resource

EVENT_PREFIX = "FACTORY_EVENT "
BASE_ENV_KEYS = ("HOME", "PATH", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR",
                 "SystemRoot", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "PATHEXT", "ProgramFiles", "ProgramFiles(x86)")
LEGACY_KEY_NAMES = ("TIKTOK_AUTO_PRODUCT_VISION_API_KEY", "TIKTOK_AUTO_SEMANTIC_VISION_API_KEY")


def redact(value: str, secrets: Sequence[str]) -> str:
    for secret in sorted(set(secrets), key=len, reverse=True):
        if secret:
            value = value.replace(secret, "[REDACTED]")
            value = value.replace(json.dumps(secret, ensure_ascii=False)[1:-1], "[REDACTED]")
    return value


def legacy_environment(root: Path) -> dict[str, str]:
    values = {key: os.environ[key] for key in LEGACY_KEY_NAMES if key in os.environ}
    path = root / ".env"
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.strip().partition("=")
            if separator and key.strip() in LEGACY_KEY_NAMES:
                values.setdefault(key.strip(), value.strip().strip("\"'"))
    return values


@dataclass(frozen=True)
class ProcessResult:
    pid: int
    exit_code: int
    stdout: str
    stderr: str
    log_path: str
    duration_s: float
    timed_out: bool = False
    cancelled: bool = False
    started_at: float = 0
    finished_at: float = 0
    duration_ms: int = 0
    stdout_log_path: str = ""
    stderr_log_path: str = ""
    last_heartbeat: float = 0
    termination_signal: int | None = None
    resource_info: dict | None = None


class ProcessRunner:
    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        log_dir: Path,
        env: Mapping[str, str] | None = None,
        secrets: Sequence[str] = (),
        timeout_s: float = 1200,
        on_event: Callable[[dict], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> ProcessResult:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "collector-process.jsonl"
        stdout_path, stderr_path = log_dir / "collector-stdout.log", log_dir / "collector-stderr.log"
        child_env = {key: os.environ[key] for key in BASE_ENV_KEYS if key in os.environ}
        child_env.update({key: value for key, value in (env or {}).items() if key in LEGACY_KEY_NAMES})
        child_env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1")
        secret_values = tuple(secrets) + tuple(child_env.get(key, "") for key in LEGACY_KEY_NAMES)
        started = time.monotonic()
        started_at = time.time()
        if cancel_event and cancel_event.is_set():
            event = {"type": "process_finished", "exit_code": 130, "cancelled": True, "started_process": False}
            for path in (stdout_path, stderr_path):
                with path.open("a", encoding="utf-8"):
                    pass
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"at": started_at, **event}) + "\n")
            if on_event:
                on_event(event)
            return ProcessResult(0, 130, "", "", str(log_path), 0, False, True, started_at, time.time(), 0, str(stdout_path), str(stderr_path))
        process = subprocess.Popen(
            list(command), cwd=cwd, env=child_env, shell=False, start_new_session=os.name != "nt",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        lines: queue.Queue[tuple[str, str | None]] = queue.Queue()

        def read_stream(name: str, stream) -> None:
            try:
                for line in stream:
                    lines.put((name, line))
            finally:
                stream.close()
                lines.put((name, None))

        readers = [threading.Thread(target=read_stream, args=(name, stream), daemon=True)
                   for name, stream in (("stdout", process.stdout), ("stderr", process.stderr))]
        for reader in readers:
            reader.start()
        captured: dict[str, list[str]] = {"stdout": [], "stderr": []}
        ended_streams = 0
        timed_out = cancelled = False
        stop_sent_at: float | None = None
        heartbeat_at = time.time()
        heartbeat_due = time.monotonic() + 5
        failure = None

        def publish(event: dict, handle) -> None:
            safe = json.loads(redact(json.dumps(event, ensure_ascii=False, default=str), secret_values))
            handle.write(json.dumps({"at": time.time(), "pid": process.pid, **safe}, ensure_ascii=False) + "\n")
            handle.flush()
            if on_event:
                on_event(safe)

        def stop_child(*, force=False) -> None:
            try:
                if os.name == "nt":
                    process.kill() if force else process.terminate()
                else:
                    os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
            except ProcessLookupError:
                pass

        try:
            with log_path.open("a", encoding="utf-8") as handle, stdout_path.open("a", encoding="utf-8") as stdout_handle, stderr_path.open("a", encoding="utf-8") as stderr_handle:
                stream_handles = {"stdout": stdout_handle, "stderr": stderr_handle}
                publish({"type": "PROCESS_STARTED", "pid": process.pid, "command": list(command), "cwd": str(cwd), "started_at": started_at,
                         "stdout_log_path": str(stdout_path), "stderr_log_path": str(stderr_path), "log_path": str(log_path)}, handle)
                while ended_streams < 2 or process.poll() is None:
                    now = time.monotonic()
                    if now >= heartbeat_due and process.poll() is None:
                        heartbeat_at = time.time()
                        publish({"type": "PROCESS_HEARTBEAT", "pid": process.pid, "last_heartbeat": heartbeat_at,
                                 "elapsed_s": round(now - started, 3)}, handle)
                        heartbeat_due = now + 5
                    if stop_sent_at is None and process.poll() is None:
                        cancelled = bool(cancel_event and cancel_event.is_set())
                        timed_out = not cancelled and now - started >= timeout_s
                        if cancelled or timed_out:
                            stop_child()
                            stop_sent_at = now
                            publish({"type": "process_stopping", "reason": "cancelled" if cancelled else "timeout"}, handle)
                    elif stop_sent_at is not None and process.poll() is None and now - stop_sent_at >= 3:
                        stop_child(force=True)
                    try:
                        name, line = lines.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    if line is None:
                        ended_streams += 1
                        continue
                    safe_line = redact(line, secret_values)
                    stream_handles[name].write(safe_line)
                    stream_handles[name].flush()
                    captured[name].append(safe_line)
                    if name == "stdout" and safe_line.startswith(EVENT_PREFIX):
                        try:
                            event = json.loads(safe_line[len(EVENT_PREFIX):])
                        except ValueError:
                            event = {"type": "output", "stream": name, "text": safe_line.rstrip()}
                        if not isinstance(event, dict):
                            event = {"type": "output", "stream": name, "text": safe_line.rstrip()}
                    else:
                        event = {"type": "output", "stream": name, "text": safe_line.rstrip()}
                    publish(event, handle)
                process.wait()
        except BaseException as exc:
            failure = exc
            raise
        finally:
            if process.poll() is None:
                stop_child(force=True)
                process.wait()
            for reader in readers:
                reader.join(timeout=1)
            exit_code = process.returncode
            duration_s = round(time.monotonic() - started, 3)
            finished_at = time.time()
            if os.name == "nt":
                resources = {"scope": "not_available", "reason": "POSIX_GETRUSAGE_UNAVAILABLE_ON_WINDOWS"}
            else:
                usage = resource.getrusage(resource.RUSAGE_CHILDREN)
                resources = {"scope": "cumulative_child_processes", "max_rss": usage.ru_maxrss,
                             "user_cpu_s": usage.ru_utime, "system_cpu_s": usage.ru_stime}
            event = {"type": "process_finished", "exit_code": exit_code, "termination_signal": -exit_code if exit_code < 0 else None,
                "timed_out": timed_out, "cancelled": cancelled, "resource_info": resources,
                "started_at": started_at, "finished_at": finished_at, "duration_ms": round(duration_s * 1000),
                "last_heartbeat": heartbeat_at, "stdout_log_path": str(stdout_path), "stderr_log_path": str(stderr_path),
                "failure_reason": type(failure).__name__ if failure is not None else None}
            with log_path.open("a", encoding="utf-8") as handle:
                if failure is None:
                    publish(event, handle)
                else:
                    try:
                        publish(event, handle)
                    except Exception:
                        pass  # Keep the original observer/runner error; the terminal record was already flushed.
        return ProcessResult(process.pid, exit_code, "".join(captured["stdout"]), "".join(captured["stderr"]), str(log_path),
                             duration_s, timed_out, cancelled, started_at, finished_at, round(duration_s * 1000), str(stdout_path), str(stderr_path),
                             heartbeat_at, -exit_code if exit_code < 0 else None, resources)
