"""Read/poll the existing downloader; submission remains owned by the collector."""
import asyncio
import json
import os
import subprocess
from dataclasses import asdict
from pathlib import Path

from app.integrations.process_runner import BASE_ENV_KEYS, ProcessRunner


class VidbeeDownloaderAdapter:
    def __init__(self, settings, runner=None):
        self.settings = settings
        self.runner = runner or ProcessRunner()

    async def verify(self, context, row, directory, on_event=None, cancel_event=None):
        return await asyncio.to_thread(self._execute, context, row, Path(directory), on_event, cancel_event, False)

    async def verify_completed_once(self, context, row, directory):
        return await asyncio.to_thread(self._execute, context, row, Path(directory), None, None, False, True)

    async def readiness(self):
        return await asyncio.to_thread(self._execute, {}, {}, None, None, None, True)

    def _execute(self, context, row, directory, on_event, cancel_event, health, completed_once=False):
        command = [str(self.settings.legacy_tiktok_auto_python), "-B", str(Path(__file__).with_name("vidbee_worker.py")),
                   "--legacy-root", str(self.settings.legacy_tiktok_auto_root)]
        if health:
            # A readiness read never queues a download or opens the app.
            try:
                process = subprocess.run(command + ["--health"], cwd=self.settings.legacy_tiktok_auto_root,
                    env={key: os.environ[key] for key in BASE_ENV_KEYS if key in os.environ},
                    capture_output=True, text=True, timeout=20)
                return json.loads(process.stdout) if process.returncode == 0 else {"status": "ERROR", "message": "VidBee 健康检查失败"}
            except (OSError, ValueError, subprocess.SubprocessError):
                return {"status": "MISSING", "message": "VidBee Automation API 未连接"}
        directory.mkdir(parents=True, exist_ok=True)
        input_path, result_path = directory / "download-input.json", directory / "download-result.json"
        input_path.write_text(json.dumps({"context": context, "row": row}, ensure_ascii=False), encoding="utf-8")
        arguments = ["--input", str(input_path), "--result", str(result_path)] + (["--require-completed"] if completed_once else [])
        process = self.runner.run(command + arguments,
            cwd=self.settings.legacy_tiktok_auto_root, log_dir=directory / "logs",
            timeout_s=min(30, self.settings.legacy_collector_timeout_seconds) if completed_once else self.settings.legacy_collector_timeout_seconds, on_event=on_event, cancel_event=cancel_event)
        result = json.loads(result_path.read_text()) if result_path.is_file() else {
            "status": "FAILED", "error_code": "DOWNLOAD_POLL_INTERRUPTED", "message": "下载检查中断；原 VidBee 任务保留", "retryable": True}
        result["_process"] = {key: value for key, value in asdict(process).items() if key not in {"stdout", "stderr"}}
        result["_process"].update(command=command + arguments, cwd=str(self.settings.legacy_tiktok_auto_root))
        return result
