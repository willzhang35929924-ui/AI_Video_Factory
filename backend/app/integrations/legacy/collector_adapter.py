from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import os
import re
import subprocess
import threading
import urllib.error
import urllib.request
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urlsplit
from uuid import uuid4

from app.integrations.process_runner import BASE_ENV_KEYS, ProcessRunner, legacy_environment, redact
from app.integrations.legacy.collector_worker import semantic_retry_ready, source_verification_ready
from app.storage import StoragePaths
from app.runtime_paths import chrome_executable, venv_python

CONTEXT_FIELDS = ("task_id", "project_id", "country", "keyword", "product_id", "product_images", "source_document", "source_row", "context_id", "video_url", "video_id", "factory_rules")
CHROME_EXECUTABLE = chrome_executable()


class LegacyCollectorAdapter:
    def __init__(self, legacy_root: Path, storage_root: Path, runner: ProcessRunner | None = None,
                 python_path: Path | None = None, timeout_s: float = 1200):
        self.legacy_root = Path(legacy_root).resolve()
        self.storage_root = Path(storage_root).resolve()
        self.runner = runner or ProcessRunner()
        self.python_path = Path(python_path) if python_path else venv_python(self.legacy_root)
        self.timeout_s = timeout_s

    def _paths(self, context: Mapping) -> dict[str, str]:
        return StoragePaths(self.storage_root).task_paths(str(context["project_id"]), str(context["task_id"]))

    def checkpoint(self, context: Mapping) -> dict:
        path = Path(self._paths(context)["metadata"]) / "collector-state.json"
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"phase": "MISSING", "path": str(path)}
        payload = {key: context[key] for key in CONTEXT_FIELDS if key in context}
        identity = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        if state.get("identity") != identity:
            return {"phase": "CONFLICT", "path": str(path)}
        result = state.get("result") or {}
        if state.get("status") == "external_inflight" or result.get("error_code") == "UNKNOWN_EXTERNAL_REQUEST":
            return {"phase": "UNKNOWN_EXTERNAL_REQUEST", "path": str(path)}
        row, evidence = state.get("row") or {}, state.get("recovery_evidence") or {}
        bound = bool(row.get("vidbee_task_id") and evidence.get("vidbee_task_id") == row["vidbee_task_id"]
                     and row.get("context_id") == context["context_id"] and evidence.get("config_sha256")
                     and evidence.get("reference_image_fingerprint") and evidence.get("screening_config_fingerprint"))
        if bound and source_verification_ready(state):
            return {"phase": "SOURCE_VERIFICATION_READY", "path": str(path), "video_id": row.get("video_id"), "vidbee_task_id": row["vidbee_task_id"]}
        if bound and semantic_retry_ready(state):
            return {"phase": "SEMANTIC_RETRY_READY", "path": str(path), "video_id": row.get("video_id"), "vidbee_task_id": row["vidbee_task_id"]}
        if bound and (state.get("status") in {"download_wait", "download_verifying"} or row.get("reason") == "vidbee_download_pending"):
            return {"phase": "DOWNLOAD_PENDING", "path": str(path), "video_id": row.get("video_id"), "vidbee_task_id": row["vidbee_task_id"]}
        if bound and state.get("status") == "result_received" and row.get("analysis_status") == "completed":
            return {"phase": "RESULT_READY", "path": str(path), "video_id": row.get("video_id"), "vidbee_task_id": row["vidbee_task_id"]}
        return {"phase": "NOT_SUBMITTED" if not state.get("legacy_processing_entered") else "CONFLICT", "path": str(path)}

    def dry_run(self, context: Mapping | None = None) -> dict:
        context = context or {"task_id": None}
        try:
            return self._dry_run(context)
        except (OSError, ValueError, KeyError) as exc:
            return {"task_id": context.get("task_id"), "mode": "dry_run", "status": "ERROR", "ok": False,
                    "checks": [{"name": "dry_run", "ok": False, "status": "ERROR", "error": type(exc).__name__}],
                    "business_executed": False, "paid_requests": 0, "single_hit_budget": 1}

    def _dry_run(self, context: Mapping) -> dict:
        if context.get("task_id"):
            self._paths(context)
        checks = []
        for name in ("src/pipeline.py", "src/config.py", "src/browser.py", "src/tiktok_search.py", "src/metrics.py", "src/video_analysis.py", "src/vidbee_handoff.py"):
            path = self.legacy_root / name
            status = "READY"
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                valid = True
                if name == "src/pipeline.py":
                    method = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "process_hit")
                    valid = [arg.arg for arg in method.args.args] == ["self", "browser", "spec", "hit", "checkpoint"]
                    if not valid:
                        status = "ERROR"
            except (SyntaxError, StopIteration):
                valid, status = False, "ERROR"
            except OSError:
                valid = False
                status = "MISSING"
            checks.append({"name": name, "ok": valid, "status": status, "message": "旧模块源码及调用签名可读" if valid else "旧模块缺失或调用签名不匹配",
                           "error_code": None if valid else "LEGACY_MODULE_INVALID"})
        executable = self.python_path.is_file() and os.access(self.python_path, os.X_OK)
        python_check = {"name": "legacy_python", "ok": executable, "path": str(self.python_path),
                       "message": "现有 Python 可执行" if executable else "Factory 指定的旧 Python 不存在或不可执行；不会安装第二套环境",
                       "error_code": None if executable else "LEGACY_PYTHON_NOT_EXECUTABLE"}
        checks.append(python_check)
        config_path = self.legacy_root / "config.yaml"
        config = {}
        if config_path.is_file():
            for line in config_path.read_text(encoding="utf-8").splitlines():
                key, separator, value = line.partition(":")
                if separator and not key.lstrip().startswith("#"):
                    config[key.strip()] = value.split(" #", 1)[0].strip().strip("\"'")
        keys = legacy_environment(self.legacy_root)
        checks.append({"name": "legacy_config", "ok": config_path.is_file(), "path": str(config_path),
                       "message": "旧配置只读" if config_path.is_file() else "旧 config.yaml 不存在", "error_code": None if config_path.is_file() else "LEGACY_CONFIG_MISSING"})
        required = []
        if config.get("product_match_api_enabled", "false").casefold() in {"true", "yes", "on", "1"}:
            required.append(config.get("product_match_api_key_env", "TIKTOK_AUTO_PRODUCT_VISION_API_KEY"))
        if config.get("semantic_api_enabled", "false").casefold() in {"true", "yes", "on", "1"}:
            required.append(config.get("semantic_api_key_env") or config.get("product_match_api_key_env", "TIKTOK_AUTO_PRODUCT_VISION_API_KEY"))
        key_ready = all(bool(keys.get(key)) for key in required)
        checks.append({"name": "legacy_api_key_presence", "ok": key_ready,
                       "message": "所需 API 环境变量已配置；未发送请求" if key_ready else "旧系统要求的 API 环境变量未配置；仅检查存在性",
                       "error_code": None if key_ready else "LEGACY_API_KEY_MISSING",
                       "configured": {key: bool(keys.get(key)) for key in set(required) | keys.keys()}})
        if context.get("task_id"):
            images = [Path(value).resolve() for value in context.get("product_images", [])]
            images_ready = bool(images) and all(path.is_file() and (path.is_relative_to(self.storage_root)
                or path.is_relative_to(self.legacy_root / "data" / "product_references")) for path in images)
            checks.append({"name": "product_images", "ok": images_ready, "count": len(images), "missing": sum(not path.is_file() for path in images),
                           "message": "任务产品图存在且属于受控参考图目录" if images_ready else "任务产品图缺失或不属于受控参考图目录",
                           "error_code": None if images_ready else "PRODUCT_IMAGES_UNAVAILABLE"})
        import_ok, import_error = False, "legacy Python is unavailable"
        python_info = {}
        if executable:
            try:
                probe = subprocess.run([str(self.python_path), "-B", "-c", "import json,sys;print('FACTORY_PYTHON '+json.dumps({'executable':sys.executable,'version':sys.version.split()[0]}),flush=True);import src.pipeline,src.browser,src.video_analysis,src.vidbee_handoff;print('IMPORT_OK')"],
                                       cwd=self.legacy_root, env={**{key: os.environ[key] for key in BASE_ENV_KEYS if key in os.environ}, "PYTHONDONTWRITEBYTECODE": "1"},
                                       capture_output=True, text=True, timeout=10)
                python_info = next((json.loads(line.removeprefix("FACTORY_PYTHON ")) for line in probe.stdout.splitlines() if line.startswith("FACTORY_PYTHON ")), {})
                import_ok = probe.returncode == 0 and bool(python_info.get("version")) and "IMPORT_OK" in probe.stdout
                import_error = redact(probe.stderr[:600], tuple(keys.values())) or "没有返回 Python 版本与模块导入确认；请检查 Factory 指定的解释器" if not import_ok else ""
                if not python_info.get("version"):
                    python_check.update(ok=False, error_code="LEGACY_PYTHON_INVALID_EXECUTABLE", message="指定可执行文件没有返回 Python 版本信息")
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                import_error = type(exc).__name__
        python_check.update(python_info)
        checks.append({"name": "legacy_import_smoke", "ok": import_ok, "error": import_error,
                       "message": "旧模块真实导入通过；未执行采集" if import_ok else "旧模块导入失败：" + import_error,
                       "error_code": None if import_ok else "LEGACY_IMPORT_FAILED"})
        chrome_ready = CHROME_EXECUTABLE.is_file() and os.access(CHROME_EXECUTABLE, os.X_OK)
        checks.append({"name": "chrome_executable", "ok": chrome_ready, "path": str(CHROME_EXECUTABLE),
                       "message": "现有 Google Chrome 可执行" if chrome_ready else "旧 BrowserClient 使用的 Google Chrome 文件不存在或不可执行",
                       "error_code": None if chrome_ready else "CHROME_EXECUTABLE_MISSING"})
        profile = (self.legacy_root / config.get("profile_path", "chrome-profile")).resolve()
        checks.append({"name": "existing_chrome_profile", "ok": profile.is_dir(), "path": str(profile),
                       "message": "复用原 Chrome 登录 Profile" if profile.is_dir() else "原 Chrome Profile 不存在；不会新建 Profile",
                       "error_code": None if profile.is_dir() else "CHROME_PROFILE_MISSING"})
        endpoint = config.get("cdp_endpoint", "http://127.0.0.1:9222").rstrip("/")
        parsed = urlsplit(endpoint)
        cdp_ok, payload = False, {}
        if parsed.scheme == "http" and parsed.hostname == "127.0.0.1" and not parsed.username and not parsed.password:
            try:
                with urllib.request.urlopen(endpoint + "/json/version", timeout=2) as response:
                    payload = json.loads(response.read(65536))
                cdp_ok = isinstance(payload, dict) and bool(payload.get("webSocketDebuggerUrl"))
                if not isinstance(payload, dict):
                    payload = {}
            except (OSError, ValueError, urllib.error.URLError):
                pass
        checks.append({"name": "existing_cdp_get", "ok": cdp_ok, "method": "GET", "started_browser": False,
                       "endpoint": parsed._replace(query="", fragment="").geturl() if parsed.hostname == "127.0.0.1" and not parsed.username and not parsed.password else "INVALID_LOCAL_ENDPOINT",
                       "browser_version": payload.get("Browser"), "message": "原 Chrome CDP 在线；页面状态另行检查" if cdp_ok else "原 Chrome CDP 未连接；可使用采集环境恢复入口",
                       "error_code": None if cdp_ok else "CHROME_CDP_UNAVAILABLE"})
        for check in checks:
            check.setdefault("status", "READY" if check["ok"] else "MISSING")
        ready = all(item["ok"] for item in checks)
        status = "ERROR" if any(item["status"] == "ERROR" for item in checks) else "READY" if ready else "MISSING"
        return {"task_id": context.get("task_id"), "mode": "dry_run", "checks": checks, "ok": ready, "status": status,
                "checked_at": datetime.now(timezone.utc).isoformat(), "frontend_node_required": False,
                "business_executed": False, "paid_requests": 0, "single_hit_budget": 1}

    async def collect_one(self, context: Mapping, on_event: Callable[[dict], None] | None = None,
                          cancel_event: threading.Event | None = None, run_id: str | None = None, *, resume_download=False, semantic_retry=False) -> dict:
        return await asyncio.to_thread(self._collect_one, context, on_event, cancel_event, run_id, resume_download, semantic_retry)

    async def discover(self, context: Mapping, on_event: Callable[[dict], None] | None = None,
                       cancel_event: threading.Event | None = None, run_id: str | None = None) -> dict:
        return await asyncio.to_thread(self._collect_one, context, on_event, cancel_event, run_id, discover_only=True)

    def _collect_one(self, context: Mapping, on_event: Callable[[dict], None] | None,
                     cancel_event: threading.Event | None, run_id: str | None, resume_download=False, semantic_retry=False, *, discover_only=False) -> dict:
        paths = self._paths(context)
        run_id = run_id or "run-" + uuid4().hex
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", run_id):
            raise ValueError("invalid Collector run ID")
        metadata_base = Path(paths["metadata"])
        metadata_dir, log_dir = metadata_base / run_id, Path(paths["logs"]) / run_id
        metadata_dir.mkdir(parents=True, exist_ok=True)
        payload = {key: context[key] for key in CONTEXT_FIELDS if key in context}
        for key in ("task_id", "project_id", "country", "keyword", "product_id", "context_id"):
            if not payload.get(key):
                raise ValueError(f"collector context requires {key}")
        input_path = metadata_dir / "collector-context.json"
        result_path = metadata_dir / ("collector-discovery.json" if discover_only else "collector-result.json")
        state_path = metadata_base / ("collector-discovery-state.json" if discover_only else "collector-state.json")
        input_path.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
        environment = legacy_environment(self.legacy_root)
        worker = Path(__file__).with_name("collector_worker.py")
        command = [str(self.python_path), "-B", str(worker), "--legacy-root", str(self.legacy_root), "--storage-root", str(self.storage_root),
                   "--context", str(input_path), "--result", str(result_path), "--state", str(state_path)]
        if resume_download:
            command.append("--resume-download")
        if semantic_retry:
            command.append("--retry-semantic-once")
        if discover_only:
            command.append("--discover-only")
        execution = self.runner.run(
            command,
            cwd=self.legacy_root, log_dir=log_dir, env=environment, secrets=tuple(environment.values()), timeout_s=self.timeout_s,
            on_event=on_event, cancel_event=cancel_event,
        )
        result = None
        if result_path.is_file():
            result = json.loads(redact(result_path.read_text(encoding="utf-8"), tuple(environment.values())))
        if discover_only:
            if not isinstance(result, dict):
                result = {"task_id": context["task_id"], "status": "CANCELLED" if execution.cancelled else "FAILED", "hits": [],
                    "error_code": "PROCESS_CANCELLED" if execution.cancelled else "PROCESS_TIMEOUT" if execution.timed_out else "PROCESS_FAILED",
                    "error_message": "搜索子进程没有产生完整结果；没有提交下载或付费分析", "paid_requests": 0, "download_submission_count": 0}
            result["result_path"] = str(result_path)
            result["_process"] = {key: value for key, value in asdict(execution).items() if key not in {"stdout", "stderr"}}
            return result
        if not isinstance(result, dict):
            state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
            phase = self.checkpoint(payload)["phase"]
            unknown = phase == "UNKNOWN_EXTERNAL_REQUEST"
            pending = phase in {"DOWNLOAD_PENDING", "RESULT_READY", "SOURCE_VERIFICATION_READY"}
            row = state.get("row") or {}
            result = {**payload, "status": "WAITING_REVIEW" if unknown else "CANCELLED" if execution.cancelled else "FAILED",
                      "video_id": row.get("video_id"), "video_url": row.get("url"), "legacy_vidbee_task_id": row.get("vidbee_task_id"),
                      "likes": int(row["likes"]) if row.get("likes") not in (None, "") else None,
                      "views": int(row["views"]) if row.get("views") not in (None, "") else None,
                      "metric_source": row.get("metric_source") or None, "metric_evidence": row.get("metric_evidence") or None,
                      "reference_product_match": row.get("reference_product_match") or "uncertain",
                      "product_match": row.get("product_match") or "uncertain", "ai_origin": row.get("ai_origin") or "uncertain",
                      "cutout_suitability": row.get("cutout_suitability") or "uncertain",
                      "download_status": row.get("vidbee_task_status") or "not_started",
                      "decision": "manual_review", "decision_reason": "unknown_external_request" if unknown else "vidbee_download_pending" if pending else "collector_process_failed",
                      "error_code": "UNKNOWN_EXTERNAL_REQUEST" if unknown else "DOWNLOAD_PENDING" if pending else "PROCESS_TIMEOUT" if execution.timed_out else "PROCESS_CANCELLED" if execution.cancelled else "PROCESS_FAILED",
                      "error_message": "外部请求结果未知，禁止自动重试" if unknown else "仅原下载轮询中断，可显式恢复原任务" if pending else "Collector 子进程未产生可验证结果",
                      "legacy_csv_path": str(self.legacy_root / "output" / "candidates.csv"),
                      "legacy_result_reference": {"raw_result_path": str(result_path)}, "retryable": not unknown,
                      "source_asset_metadata": None, "overlay_detected": None, "watermark_detected": None}
        result["_process"] = {key: value for key, value in asdict(execution).items() if key not in {"stdout", "stderr"}}
        return result
