import asyncio
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from fastapi import HTTPException

from app.domain import new_id, now
from app.integrations.process_runner import BASE_ENV_KEYS, ProcessRunner
from app.integrations.input_service import write_json
from app.integrations.legacy.video_adapter import LegacyPromptAdapter

STAGE_REQUIREMENTS = {
    "collector": {"Collector", "TikTok", "VidBee", "Storage", "Execution"},
    "analysis": {"Collector", "TikTok", "VidBee", "Storage", "Execution"},
    "download": {"Collector", "VidBee", "Storage", "Execution"},
    "cutout": {"ComfyUI", "Person Asset", "Configuration", "Quality", "Storage", "Execution"},
    "person": {"ComfyUI", "Person Asset", "Configuration", "Quality", "Storage", "Execution"},
    "prompt": {"AI API", "Configuration", "Storage", "Execution"},
    "background": {"RPA", "Configuration", "Storage", "Execution"},
    "compose": {"ComfyUI", "Configuration", "Quality", "Storage", "Execution"},
    "quality": {"Quality", "Storage", "Execution"},
}


class ProductionReadiness:
    def __init__(self, settings, collector, vidbee, video, prompt=None):
        self.settings, self.collector, self.vidbee, self.video = settings, collector, vidbee, video
        self.prompt = prompt
        self.cached = None
        self.checked = 0
        self.cache_scope = None
        self.lock = asyncio.Lock()
        self.preparation_id = None

    async def queue_tiktok_preparation(self):
        if self.preparation_id:
            return self.tiktok_preparation(self.preparation_id), False
        async with self.lock:
            if self.preparation_id:
                return self.tiktok_preparation(self.preparation_id), False
            operation_id = new_id("browser")
            directory = self.settings.storage_root / "browser_preparation" / operation_id
            directory.mkdir(parents=True)
            result = {"operation_id": operation_id, "action": "prepare_tiktok", "status": "QUEUED", "created_at": now(),
                      "search_executed": False, "paid_requests": 0}
            write_json(directory / "request.json", result)
            write_json(directory / "result.json", result)
            self.preparation_id = operation_id
            return result, True

    def tiktok_preparation(self, operation_id):
        if not isinstance(operation_id, str) or not operation_id.startswith("browser-") or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for character in operation_id):
            raise ValueError("INVALID_BROWSER_PREPARATION_ID")
        path = self.settings.storage_root / "browser_preparation" / operation_id / "result.json"
        return json.loads(path.read_text())

    async def run_tiktok_preparation(self, operation_id):
        async with self.lock:
            directory = self.settings.storage_root / "browser_preparation" / operation_id
            result_path = directory / "result.json"
            initial = self.tiktok_preparation(operation_id)
            initial["started_at"] = now()
            write_json(result_path, {**initial, "status": "RUNNING"})
            response = directory / "browser-result.json"
            command = [str(self.settings.legacy_tiktok_auto_python), "-B", str(Path(__file__).with_name("legacy") / "browser_prepare_worker.py"),
                       "--legacy-root", str(self.settings.legacy_tiktok_auto_root), "--result", str(response)]
            try:
                process = await asyncio.to_thread(ProcessRunner().run, command, cwd=self.settings.legacy_tiktok_auto_root,
                                                  log_dir=directory / "logs", timeout_s=180)
                value = json.loads(response.read_text()) if response.is_file() else {"status": "FAILED",
                    "error_code": "BROWSER_PREPARE_TIMEOUT" if process.timed_out else "BROWSER_PREPARE_NO_RESULT", "message": "浏览器准备未返回完整检查结果；未启动业务"}
                if value.get("action", "prepare_tiktok") != "prepare_tiktok" or value.get("search_executed", False) or value.get("paid_requests", 0):
                    raise ValueError("BROWSER_PREPARATION_RESULT_INVALID")
                if value.get("status") not in {"READY", "WAITING_MANUAL", "FAILED"}:
                    raise ValueError("BROWSER_PREPARATION_STATUS_INVALID")
                value = {key: value[key] for key in ("action", "status", "error_code", "message", "profile_path", "page_url", "page_host", "page_title",
                    "readable_character_count", "navigation_routes", "visible_video", "positive_tiktok_page", "login_page", "search_executed", "paid_requests") if key in value}
                write_json(result_path, {**initial, **value, "finished_at": now(), "pid": process.pid or None, "exit_code": process.exit_code,
                    "duration_ms": process.duration_ms, "stdout_log_path": process.stdout_log_path, "stderr_log_path": process.stderr_log_path})
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                write_json(result_path, {**initial, "status": "FAILED", "finished_at": now(), "error_code": "BROWSER_PREPARE_" + type(exc).__name__.upper(),
                                        "message": "浏览器准备失败；错误类型=" + type(exc).__name__})
            finally:
                self.preparation_id = None
                self.cached = None

    async def check(self, refresh=False, stage=None, task_id=None):
        if stage is not None and stage not in STAGE_REQUIREMENTS:
            raise HTTPException(422, "INVALID_PRODUCTION_STAGE")
        async with self.lock:
            scope = (stage, task_id)
            if self.cached and self.cache_scope == scope and not refresh and time.monotonic() - self.checked < 15:
                return self.cached
            required = STAGE_REQUIREMENTS.get(stage, set().union(*STAGE_REQUIREMENTS.values()))
            checks = []
            if "VidBee" in required:
                checks.append({"name": "VidBee", **await self.vidbee.readiness()})
            if "TikTok" in required:
                checks.append({"name": "TikTok", **await asyncio.to_thread(self._tiktok)})
            if "Collector" in required:
                evidence = await asyncio.to_thread(self.collector.dry_run, task_id)
                try:
                    snapshot = await asyncio.to_thread(self.collector.rules)
                    failures = [row for row in evidence.get("checks", []) if not row.get("ok", row.get("status") == "READY")]
                    checks.append({"name": "Collector", "status": evidence["status"],
                        "message": "；".join(row.get("message") or row.get("error") or row["name"] for row in failures) if failures else
                            "Task 输入、旧环境、API 配置及产品图已只读检查" if task_id else "旧 Python、模块、Chrome、CDP 与生产配置已只读检查；产品图随 Task 校验",
                        "sha256": snapshot["sha256"], "input_checks": evidence.get("checks", []),
                        "frontend_node_required": False, "error_code": failures[0].get("error_code") if failures else None})
                except (OSError, ValueError, RuntimeError) as exc:
                    checks.append({"name": "Collector", "status": "MISSING", "message": "旧生产规则读取失败：" + type(exc).__name__,
                                   "error_code": "LEGACY_RULES_UNAVAILABLE", "input_checks": evidence.get("checks", []), "frontend_node_required": False})
            video = {}
            if required & {"ComfyUI", "Person Asset"}:
                video = await self.video.assets.readiness()
            for row in video.get("metadata", {}).get("checks", []):
                message = "配置已就绪，未发送付费请求" if row["name"] == "AI API" and row["status"] == "READY" else row.get("message") or row.get("error") or ("已连接" if row["status"] == "READY" else "尚未连接")
                checks.append({**row, "message": message})
            if self.prompt and required & {"AI API", "RPA"}:
                legacy = await self.prompt.readiness(refresh=True) if refresh and "RPA" in required and isinstance(self.prompt, LegacyPromptAdapter) else await self.prompt.readiness()
                for row in legacy.get("metadata", {}).get("checks", []):
                    if row["name"] in {"AI API", "RPA", "Configuration"} and row["name"] in required:
                        if row["name"] == "Configuration" and any(check["name"] == "Configuration" for check in checks):
                            continue
                        checks = [check for check in checks if check["name"] != row["name"]]
                        checks.append(row)
            if "Quality" in required:
                checks.append({"name": "Quality", **await asyncio.to_thread(self._quality)})
            directory = self.settings.storage_root
            parent = directory if directory.is_dir() else directory.parent
            writable = parent.is_dir() and os.access(parent, os.W_OK)
            free = shutil.disk_usage(parent).free if parent.is_dir() else 0
            checks.append({"name": "Storage", "status": "READY" if writable and free > 1024**3 else "MISSING", "required": True,
                           "message": "可写且剩余空间大于 1 GiB" if writable and free > 1024**3 else "存储不可写或空间不足", "free_bytes": free})
            execution = self.settings.app_mode == "integration" and not self.settings.integration_dry_run
            checks.append({"name": "Execution", "status": "READY" if execution else "BLOCKED", "required": True,
                "message": "真实执行模式；检查未执行业务" if execution else "Factory 当前非真实执行模式；不会执行生产"})
            present = {row["name"] for row in checks}
            checks += [{"name": name, "status": "MISSING", "message": "依赖未返回可验证检查结果"} for name in sorted(required - present)]
            checks = [{**row, "required": row["name"] in required} for row in checks]
            missing = [row for row in checks if row["required"] and row["status"] != "READY"]
            self.cached = {"ready": all(row["status"] == "READY" for row in checks if row["required"]),
                           "stage": stage, "task_id": task_id, "checks": checks, "checked_at": now(),
                           "status": "READY" if not missing else "WAITING_MANUAL" if any(row["status"] == "WAITING_MANUAL" for row in missing) else "MISSING",
                           "business_executed": False, "paid_requests": 0}
            self.checked = time.monotonic()
            self.cache_scope = scope
            return self.cached

    def _quality(self):
        try:
            process = subprocess.run([str(self.settings.quality_python), "-B", "-c", "import cv2, numpy"],
                env={key: os.environ[key] for key in BASE_ENV_KEYS if key in os.environ}, capture_output=True, timeout=20)
            return {"status": "READY" if process.returncode == 0 else "MISSING", "message": "OpenCV / NumPy 可用" if process.returncode == 0 else "质检环境不可用"}
        except (OSError, subprocess.SubprocessError):
            return {"status": "MISSING", "message": "质检 Python 不可用"}

    def _tiktok(self):
        command = [str(self.settings.legacy_tiktok_auto_python), "-B", str(Path(__file__).with_name("legacy") / "readiness_worker.py"),
                   str(self.settings.legacy_tiktok_auto_root)]
        try:
            result = subprocess.run(command, cwd=self.settings.legacy_tiktok_auto_root,
                env={key: os.environ[key] for key in BASE_ENV_KEYS if key in os.environ}, capture_output=True, text=True, timeout=20)
            return json.loads(result.stdout) if result.returncode == 0 else {"status": "ERROR", "message": "TikTok 当前页面检查失败", "error_code": "TIKTOK_INSPECTION_FAILED"}
        except (OSError, ValueError, subprocess.SubprocessError):
            return {"status": "MISSING", "message": "现有 Chrome/CDP 检查无法执行；请查看 Collector 的 Python 与 CDP 检查", "error_code": "TIKTOK_ENVIRONMENT_UNAVAILABLE"}
