from __future__ import annotations

import asyncio
import json
import re
import tempfile
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Mapping
from uuid import uuid4

from app.integrations.process_runner import ProcessRunner
from app.integrations.production_contracts import ProductionContext, ProductionResult
from app.runtime_paths import venv_python


class LegacyVideoAdapter:
    stages: tuple[str, ...] = ()
    provider = "video_auto"

    def __init__(self, legacy_root: Path, storage_root: Path | None = None, runner: ProcessRunner | None = None,
                 python_path: Path | None = None, timeout_s: float = 3600):
        self.legacy_root = Path(legacy_root).resolve()
        self.storage_root = Path(storage_root).resolve() if storage_root else None
        self.python_path = Path(python_path) if python_path else venv_python(self.legacy_root)
        self.runner = runner or ProcessRunner()
        self.timeout_s = timeout_s

    async def readiness(self, *, refresh=False) -> dict:
        return await asyncio.to_thread(self._readiness, refresh)

    @staticmethod
    def describe_contracts() -> dict:
        from app.integrations.legacy.video_worker import describe_contracts
        return describe_contracts()

    def _readiness(self, refresh=False) -> dict:
        with tempfile.TemporaryDirectory(prefix="factory-video-readiness-") as temporary:
            return self._invoke("readiness", {"_refresh_rpa_probe": True} if refresh else {}, Path(temporary), None, None)

    async def execute(self, stage: str, context: Mapping, directory: Path,
                      on_event: Callable[[dict], None] | None = None,
                      cancel_event: threading.Event | None = None) -> dict:
        if stage not in self.stages:
            raise ValueError(f"{self.provider} does not own stage {stage}")
        payload = ProductionContext.model_validate(dict(context)).model_dump()
        directory = Path(directory).resolve()
        if self.storage_root and not directory.is_relative_to(self.storage_root):
            raise ValueError("video provider directory must be inside Factory storage")
        return await asyncio.to_thread(self._invoke, stage, payload, directory, on_event, cancel_event)

    def _invoke(self, stage: str, context: dict, directory: Path, on_event, cancel_event) -> dict:
        run_id = context.get("run_id") or "run-" + uuid4().hex
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", run_id):
            raise ValueError("invalid video provider run ID")
        run_directory = directory / "runs" / run_id
        run_directory.mkdir(parents=True, exist_ok=False)
        input_path = run_directory / "context.json"
        output_path = run_directory / "result.json"
        input_path.write_text(json.dumps(context, ensure_ascii=False), encoding="utf-8")
        worker = Path(__file__).with_name("video_worker.py")
        command = [str(self.python_path), "-B", str(worker), "--legacy-root", str(self.legacy_root),
                   "--stage", stage, "--context", str(input_path), "--result", str(output_path),
                   "--directory", str(directory)]
        if self.storage_root:
            command += ["--factory-storage-root", str(self.storage_root)]
        try:
            execution = self.runner.run(command, cwd=self.legacy_root, log_dir=run_directory / "logs",
                                        timeout_s=30 if stage == "readiness" else self.timeout_s,
                                        on_event=on_event, cancel_event=cancel_event)
        except OSError as exc:
            return ProductionResult(task_id=context.get("task_id"), context_id=context.get("context_id"),
                                    stage=stage, provider=self.provider, status="FAILED", error_code="LEGACY_ENVIRONMENT_MISSING",
                                    error_message=type(exc).__name__).model_dump()
        if output_path.is_file():
            result = ProductionResult.model_validate_json(output_path.read_text(encoding="utf-8")).model_dump()
        else:
            # A killed process may have submitted a paid request. The legacy request journals
            # are preserved, and only a subsequent explicit action may inspect/recover them.
            unknown = stage in {"prompt", "background"} and bool(execution.pid)
            result = ProductionResult(task_id=context.get("task_id"), context_id=context.get("context_id"),
                                      stage=stage, provider=self.provider,
                                      status="WAITING_REVIEW" if unknown else "CANCELLED" if execution.cancelled else "FAILED",
                                      error_code="UNKNOWN_EXTERNAL_REQUEST" if unknown else "PROCESS_CANCELLED" if execution.cancelled else "PROCESS_TIMEOUT" if execution.timed_out else "PROCESS_FAILED",
                                      error_message="子进程未返回可验证结果；已有请求与资产保留", retryable=not unknown).model_dump()
        if result["task_id"] != context.get("task_id") or result["context_id"] != context.get("context_id"):
            raise ValueError("video provider result context mismatch")
        result["_process"] = {key: value for key, value in asdict(execution).items() if key not in {"stdout", "stderr"}}
        return result


class LegacyPromptAdapter(LegacyVideoAdapter):
    stages = ("prompt", "prompt_sync")
    provider = "video_auto.prompt"


class BackgroundRPAAdapter(LegacyVideoAdapter):
    stages = ("background",)
    provider = "video_auto.background_rpa"


class LegacyComfyUIAdapter(LegacyVideoAdapter):
    stages = ("cutout", "person")
    provider = "video_auto.comfyui"

    def __init__(self, *args, cutout_workflow=None, compose_workflow=None, chunk_frames=8, asset_python_path=None, **kwargs):
        from app.integrations.comfy_asset_adapter import ComfyAssetAdapter
        super().__init__(*args, **kwargs)
        asset_options = {**kwargs, 'python_path': asset_python_path or venv_python(self.legacy_root.parent / 'ComfyUI')}
        self.assets = ComfyAssetAdapter(*args, cutout_workflow=cutout_workflow,
            compose_workflow=compose_workflow, chunk_frames=chunk_frames, **asset_options)

    async def execute(self, stage, context, directory, on_event=None, cancel_event=None):
        if stage not in self.stages:
            raise ValueError(f"{self.provider} does not own stage {stage}")
        return await self.assets.execute(stage, context, directory, on_event, cancel_event)

    async def readiness(self):
        legacy = await super().readiness()
        split = await self.assets.readiness()
        checks = [row for row in legacy.get('metadata', {}).get('checks', [])
                  if row['name'] not in {'Configuration', 'ComfyUI', 'Person Asset'}]
        checks += split.get('metadata', {}).get('checks', [])
        legacy['metadata']['checks'] = checks
        legacy['status'] = 'READY' if all(row['ok'] for row in checks) else 'NOT_CONNECTED'
        return legacy


class LegacyComposeAdapter(LegacyVideoAdapter):
    stages = ("compose",)
    provider = "video_auto.compose"

    def __init__(self, *args, cutout_workflow=None, compose_workflow=None, chunk_frames=8, asset_python_path=None, **kwargs):
        from app.integrations.comfy_asset_adapter import ComfyAssetAdapter
        super().__init__(*args, **kwargs)
        asset_options = {**kwargs, 'python_path': asset_python_path or venv_python(self.legacy_root.parent / 'ComfyUI')}
        self.assets = ComfyAssetAdapter(*args, cutout_workflow=cutout_workflow,
            compose_workflow=compose_workflow, chunk_frames=chunk_frames, **asset_options)

    async def execute(self, stage, context, directory, on_event=None, cancel_event=None):
        if stage not in self.stages:
            raise ValueError(f"{self.provider} does not own stage {stage}")
        return await self.assets.execute(stage, context, directory, on_event, cancel_event)
