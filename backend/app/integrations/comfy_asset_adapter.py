"""Factory-owned split cutout/compose bridge using the existing local interpreter."""
from __future__ import annotations

import asyncio
import json
import re
import tempfile
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from app.integrations.process_runner import ProcessRunner
from app.integrations.production_contracts import ProductionContext, ProductionResult
from app.runtime_paths import venv_python
from pydantic import BaseModel, ConfigDict, Field


class CutoutCheckpointResume(BaseModel):
    model_config = ConfigDict(extra='forbid')
    input_fingerprint: str = Field(pattern=r'^[a-f0-9]{64}$')
    checkpoint_fingerprint: str = Field(pattern=r'^[a-f0-9]{64}$')
    generation: int = Field(ge=1)


class CutoutContext(ProductionContext):
    product_images: list[str] = Field(default_factory=list)
    cutout_checkpoint_resume: CutoutCheckpointResume | None = None


class ComfyAssetAdapter:
    def __init__(self, legacy_root, storage_root=None, runner=None, python_path=None, timeout_s=3600,
                 cutout_workflow=None, compose_workflow=None, chunk_frames=8):
        root = Path(__file__).resolve().parents[3]
        self.legacy_root = Path(legacy_root).resolve()
        self.storage_root = Path(storage_root).resolve() if storage_root else None
        self.python_path = Path(python_path) if python_path else venv_python(self.legacy_root)
        self.runner = runner or ProcessRunner()
        self.timeout_s = timeout_s
        self.cutout_workflow = Path(cutout_workflow or root / 'workflows/cutout_rgb_alpha_v2.api.json').resolve()
        self.compose_workflow = Path(compose_workflow or root / 'workflows/compose_person_v2.api.json').resolve()
        self.chunk_frames = int(chunk_frames)

    async def readiness(self):
        with tempfile.TemporaryDirectory(prefix='factory-comfy-assets-') as temporary:
            return await asyncio.to_thread(self._invoke, 'readiness', {}, Path(temporary), None, None)

    def implementation_fingerprint(self, stage):
        if stage not in {'cutout', 'compose'}:
            raise ValueError('Unsupported implementation identity stage')
        with tempfile.TemporaryDirectory(prefix='factory-comfy-identity-') as temporary:
            result = self._invoke('identity', {'identity_stage': stage}, Path(temporary), None, None)
        identity = result.get('metadata', {}).get('implementation_fingerprints', {}).get(stage)
        if result['status'] != 'READY' or not isinstance(identity, dict) or not identity.get('factory_algorithm_version'):
            raise ValueError('COMFY_IMPLEMENTATION_IDENTITY_UNAVAILABLE')
        return identity

    async def execute(self, stage, context, directory, on_event=None, cancel_event=None):
        if stage not in {'cutout', 'person', 'compose'}:
            raise ValueError('Unsupported split ComfyUI stage')
        context = (CutoutContext if stage in {'cutout', 'person'} else ProductionContext).model_validate(dict(context)).model_dump()
        directory = Path(directory).resolve()
        if self.storage_root and not directory.is_relative_to(self.storage_root):
            raise ValueError('ComfyUI asset directory must be inside Factory storage')
        return await asyncio.to_thread(self._invoke, stage, context, directory, on_event, cancel_event)

    async def checkpoint(self, context, directory):
        """Inspect a partial local cutout without submitting a ComfyUI prompt."""
        context = CutoutContext.model_validate(dict(context)).model_dump()
        directory = Path(directory).resolve()
        if self.storage_root and not directory.is_relative_to(self.storage_root):
            raise ValueError('ComfyUI asset directory must be inside Factory storage')
        with tempfile.TemporaryDirectory(prefix='factory-cutout-checkpoint-') as temporary:
            return await asyncio.to_thread(self._invoke, 'cutout-checkpoint',
                {**context, 'checkpoint_directory': str(directory)}, Path(temporary), None, None)

    def _invoke(self, stage, context, directory, on_event, cancel_event):
        run_id = context.get('run_id') or 'run-' + uuid4().hex
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', run_id):
            raise ValueError('Invalid ComfyUI asset run ID')
        run = directory / 'runs' / run_id
        run.mkdir(parents=True, exist_ok=False)
        request, response = run / 'context.json', run / 'result.json'
        request.write_text(json.dumps(context, ensure_ascii=False), encoding='utf-8')
        command = [str(self.python_path), '-B', str(Path(__file__).with_name('legacy') / 'comfy_asset_worker.py'),
            '--legacy-root', str(self.legacy_root), '--stage', stage, '--context', str(request),
            '--result', str(response), '--directory', str(directory), '--cutout-workflow', str(self.cutout_workflow),
            '--compose-workflow', str(self.compose_workflow), '--chunk-frames', str(self.chunk_frames)]
        process = self.runner.run(command, cwd=self.legacy_root, log_dir=run / 'logs',
            timeout_s=30 if stage in {'readiness', 'identity'} else self.timeout_s, on_event=on_event, cancel_event=cancel_event)
        if response.is_file():
            result = ProductionResult.model_validate_json(response.read_text()).model_dump()
        else:
            result = ProductionResult(task_id=context.get('task_id'), context_id=context.get('context_id'), stage=stage,
                provider='Factory split ComfyUI', status='CANCELLED' if process.cancelled else 'FAILED',
                error_code='PROCESS_CANCELLED' if process.cancelled else 'PROCESS_TIMEOUT' if process.timed_out else 'PROCESS_FAILED',
                error_message='本地渲染未返回完整母资产或成片；已提交 ComfyUI journal 保留', retryable=True).model_dump()
        if result['task_id'] != context.get('task_id') or result['context_id'] != context.get('context_id'):
            raise ValueError('ComfyUI asset result context mismatch')
        result['_process'] = {key: value for key, value in asdict(process).items() if key not in {'stdout', 'stderr'}}
        return result
