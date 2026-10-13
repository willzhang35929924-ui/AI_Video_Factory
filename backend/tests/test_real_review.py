import asyncio
import copy
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.integrations.production_routes import create_production_router
from app.integrations.production_service import ProductionService
from app.integrations.production_readiness import STAGE_REQUIREMENTS
from app.integrations.asset_registry import file_hash
from app.integrations.review_service import RealReviewService


class MemoryRepository:
    def __init__(self, state):
        self.value = copy.deepcopy(state)

    def state(self):
        return copy.deepcopy(self.value)

    def read_latest_batch(self, active_statuses):
        batches = sorted(self.state()["batches"], key=lambda row: (row.get("created_at", ""), row["id"]), reverse=True)
        return next((row for row in batches if row["status"] in active_statuses), batches[0] if batches else None)

    def mutate(self, callback):
        value = copy.deepcopy(self.value)
        result = callback(value)
        self.value = value
        return result


class UnitRegistry:
    def __init__(self, repository):
        self.repository = repository
        self.failure = None
        self.registered = []

    def content(self, identifier):
        asset = next(row for row in self.repository.state()["assets"] if row["id"] == identifier)
        return Path(asset["storage_path"]), "video/mp4"

    def register(self, task, evidence, kind, run_id, **values):
        if self.failure:
            raise self.failure
        item = {"id": "new-" + kind, "task_id": task["id"], "type": kind, "storage_path": evidence["path"],
                "legacy_reference": {**evidence.get("legacy_reference", {}), "processing_run_id": run_id},
                "status": "READY", "registry_status": "registered", "sha256": evidence.get("sha256", "unit-hash")}
        self.registered.append((kind, run_id, copy.deepcopy(evidence), values))
        self.repository.mutate(lambda state: state["assets"].append(item))
        return item


class ReviewFixture:
    def prepare(self, asset_kind="compose"):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name).resolve()
        path = root / "original.mp4"
        path.write_bytes(b"isolated review fixture; never production media")
        asset = {"id": "unit-asset-v1", "task_id": "unit-task", "type": asset_kind,
                 "status": "READY", "registry_status": "registered", "storage_path": str(path), "sha256": "unit-hash",
                 "legacy_reference": {"version": 1, "prompt_id": "unit-prompt"}}
        task = {"id": "unit-task", "project_id": "unit-project", "execution_mode": "integration",
                "status": "WAITING_REVIEW", "current_stage": "review", "country": "MX", "keyword": "body",
                "context": {"production": {}, "task_context": {"context_id": "unit-context"}},
                "collector_result": {"decision": "accepted"}}
        state = {"tasks": [task], "assets": [asset], "runs": [], "batches": [], "audits": [], "errors": [],
                 "stages": [{"id": "stage-" + name, "task_id": task["id"], "name": name, "status": "not_connected" if name == "publish" else "pending"}
                            for name in ("review", "final", "publish")],
                 "reviews": [{"id": "unit-review-v1", "task_id": task["id"], "asset_id": asset["id"], "status": "pending", "note": ""}],
                 "prompts": [{"id": "unit-prompt", "task_id": task["id"], "status": "READY", "fingerprint": "prompt-fingerprint"}],
                 "publishing": [], "quality_assessments": [], "settings": {"production_policy": {"auto_advance": False}}}
        self.repository = MemoryRepository(state)
        production = ProductionService.__new__(ProductionService)
        production.repository = self.repository
        production.settings = SimpleNamespace(app_mode="integration", storage_root=root, legacy_video_auto_root=root,
            factory_comfy_cutout_workflow=root / "cutout.json", factory_comfy_compose_workflow=root / "compose.json")
        production.start_lock = asyncio.Lock()
        production.manual_compose = None
        production.jobs = {}
        production.cancels = {}
        production.closing = False
        production.registry = UnitRegistry(self.repository)
        identity = SimpleNamespace(implementation_fingerprint=lambda stage: {"factory_algorithm_version": "isolated-" + stage})
        production.providers = {"cutout": SimpleNamespace(assets=identity), "compose": SimpleNamespace(assets=identity)}
        production.readiness = SimpleNamespace(cached={"checks": [{"workflow_sha256": "workflow-fixture"}]})
        async def ready(refresh=False, stage=None, task_id=None):
            names = STAGE_REQUIREMENTS.get(stage, set().union(*STAGE_REQUIREMENTS.values()))
            return {"ready": True, "checks": [{"name": name, "status": "READY"} for name in names]}
        async def event(*args, **kwargs):
            pass
        production.readiness.check = ready
        production.event = event
        self.production = production
        self.review = RealReviewService(production)
        self.original_asset = copy.deepcopy(asset)

    def verified_person(self):
        root = self.production.settings.storage_root
        source, alpha, manifest = root / "source.mp4", root / "Alpha.mkv", root / "manifest.json"
        source.write_bytes(b"isolated source evidence fixture")
        alpha.write_bytes(b"isolated alpha evidence fixture")
        self.production.settings.factory_comfy_cutout_workflow.write_text('{"unit":"cutout-workflow"}')
        rgb = Path(self.original_asset["storage_path"])
        implementation = self.production.providers["cutout"].assets.implementation_fingerprint("cutout")
        manifest.write_text(json.dumps({"task_id": "unit-task", "full_decode_verified": True,
            "rgb_sha256": file_hash(rgb), "alpha_sha256": file_hash(alpha), "source_path": str(source),
            "source_sha256": file_hash(source), "workflow_sha256": file_hash(self.production.settings.factory_comfy_cutout_workflow),
            "implementation_hashes": implementation}))
        self.production.registry.allowed_path = lambda value: Path(value).resolve()
        def setup(state):
            task = state["tasks"][0]
            task["context"]["production"]["current_person_asset_id"] = "unit-asset-v1"
            task["collector_result"]["source_asset_metadata"] = {"sha256": file_hash(source)}
            asset = state["assets"][0]
            asset.update(type="person", sha256=file_hash(rgb))
            asset["legacy_reference"].update(alpha_path=str(alpha), alpha_sha256=file_hash(alpha), manifest_path=str(manifest),
                manifest_sha256=file_hash(manifest), source_sha256=file_hash(source), workflow_sha256=file_hash(self.production.settings.factory_comfy_cutout_workflow))
            fingerprint = self.production._fingerprint(task, "person", implementation=implementation)
            asset["legacy_reference"]["factory_fingerprint"] = fingerprint
            state["assets"].append({"id": "unit-source", "task_id": "unit-task", "type": "source", "status": "READY",
                "registry_status": "registered", "storage_path": str(source), "sha256": file_hash(source),
                "legacy_reference": {}, "legacy_video_id": "unit-video"})
        self.repository.mutate(setup)
        return self.repository.state()["assets"][0]


class RealReviewTests(ReviewFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.prepare()

    async def test_technical_review_cannot_create_final_or_change_business_task(self):
        self.repository.mutate(lambda state: state["assets"][0]["legacy_reference"].update(execution_scope="TECHNICAL_VALIDATION"))
        task = self.repository.state()["tasks"][0]
        await self.review.action("approve", "unit-review-v1", {})
        self.assertEqual(self.repository.state()["tasks"][0], task)
        self.assertEqual(self.repository.state()["reviews"][0]["status"], "approved")
        self.assertFalse(self.production.registry.registered)
        with self.assertRaises(HTTPException):
            await self.review.action("retry", "unit-review-v1", {})

    async def test_local_resume_does_not_resume_unknown_paid_request(self):
        self.repository.mutate(lambda state: state["runs"].append({"id": "local", "task_id": "unit-task", "provider": "LegacyComposeAdapter", "error_code": "LOCAL_EXECUTION_INTERRUPTED"}))
        self.assertTrue(self.production._local_resume("unit-task"))
        self.repository.mutate(lambda state: state["runs"].insert(0, {"id": "paid", "task_id": "unit-task", "provider": "BackgroundRPAAdapter", "error_code": "UNKNOWN_EXTERNAL_REQUEST"}))
        self.assertFalse(self.production._local_resume("unit-task"))

    async def test_technical_assets_do_not_change_production_totals(self):
        def setup(state):
            state["quality_assessments"] = []
            state["assets"][0]["legacy_reference"].update(execution_scope="TECHNICAL_VALIDATION")
        self.repository.mutate(setup)
        summary = self.production.summary(["unit-task"])
        self.assertEqual(summary["compose_count"], 0)
        self.assertEqual(summary["review_count"], 0)

    async def test_approval_keeps_original_asset_and_does_not_publish(self):
        await self.review.action("approve", "unit-review-v1", {})
        state = self.repository.state()
        original = next(row for row in state["assets"] if row["id"] == "unit-asset-v1")
        self.assertEqual(original["storage_path"], self.original_asset["storage_path"])
        self.assertEqual(original["sha256"], self.original_asset["sha256"])
        self.assertEqual(original["legacy_reference"]["version"], 1)
        self.assertEqual(state["publishing"], [])
        self.assertEqual(next(row for row in state["stages"] if row["name"] == "publish")["status"], "not_connected")
        final = next(row for row in state["assets"] if row["type"] == "final")
        review_run = next(row for row in state["runs"] if row["id"] == final["legacy_reference"]["processing_run_id"])
        self.assertEqual(review_run["provider"], "HumanReview")
        self.assertEqual(review_run["status"], "success")
        with self.assertRaises(HTTPException):
            await self.review.action("reject", "unit-review-v1", {})

    async def test_person_approval_can_continue_without_marking_entire_task_complete(self):
        self.verified_person()
        await self.review.action("approve", "unit-review-v1", {})
        self.assertNotEqual(self.repository.state()["tasks"][0]["status"], "COMPLETED")
        outcome = await self.production.start(["unit-task"], retry=True, stage="background", prompt_id="unit-prompt")
        self.assertTrue(outcome["accepted"])

    async def test_rejected_review_retries_from_saved_assets(self):
        await self.review.action("reject", "unit-review-v1", {})
        outcome = await self.review.action("retry", "unit-review-v1", {})
        self.assertTrue(outcome["accepted"])
        state = self.repository.state()
        self.assertEqual(state["reviews"][0]["status"], "rejected")
        self.assertEqual(state["assets"][0]["storage_path"], self.original_asset["storage_path"])

    async def test_pending_review_retry_is_allowed_without_forcing_approval(self):
        outcome = await self.review.action("reprocess", "unit-review-v1", {})
        self.assertTrue(outcome["accepted"])
        self.assertEqual(self.repository.state()["reviews"][0]["status"], "pending")

    async def test_failed_final_registration_cannot_commit_successful_review(self):
        self.production.registry.failure = ValueError("unit hash mismatch")
        with self.assertRaises((ValueError, HTTPException)):
            await self.review.action("approve", "unit-review-v1", {})
        state = self.repository.state()
        self.assertEqual(state["reviews"][0]["status"], "pending")
        self.assertNotEqual(state["tasks"][0]["status"], "COMPLETED")
        self.assertEqual(len(state["assets"]), 1)

    async def test_ffprobe_failure_during_approval_preserves_pending_review_and_failed_run(self):
        self.production.registry.failure = subprocess.CalledProcessError(1, ["ffprobe"])
        with self.assertRaises((subprocess.SubprocessError, HTTPException)):
            await self.review.action("approve", "unit-review-v1", {})
        state = self.repository.state()
        self.assertEqual(state["reviews"][0]["status"], "pending")
        self.assertEqual(state["runs"][0]["status"], "failed")

    async def test_render_retry_keeps_paid_prompt_cache_directory(self):
        outcome = await self.production.start(["unit-task"], retry=True, stage="compose")
        batch_id = outcome["batch_id"]
        self.production.cancels = {batch_id: threading.Event()}
        self.production.closing = False
        self.production._checkpoint = AsyncMock()
        self.production._record_result = lambda *args, **kwargs: None
        directories = []
        async def invoke(run_id, directory, callback):
            directories.append(directory)
            return {"status": "READY"}
        await self.production._stage(batch_id, "unit-task", "prompt", "LegacyPromptAdapter", invoke)
        self.repository.mutate(lambda state: state["tasks"][0]["context"]["production"].update(generation=3, cutout_generation=3, compose_generation=3))
        await self.production._stage(batch_id, "unit-task", "prompt", "LegacyPromptAdapter", invoke)
        self.assertEqual(directories[0], directories[1], "render versions must not silently reset paid prompt request journals")

    async def test_saved_composition_does_not_require_tiktok_or_rpa(self):
        async def readiness(refresh=False, stage=None, task_id=None):
            return {"ready": False, "checks": [{"name": name, "status": "READY"} for name in STAGE_REQUIREMENTS["compose"]] + [
                {"name": "TikTok", "status": "MISSING"}, {"name": "RPA", "status": "WAITING_MANUAL"}]}
        self.production.readiness.check = readiness
        result = await self.production.start(["unit-task"], retry=True, stage="compose")
        self.assertTrue(result["accepted"])
        self.assertEqual(self.repository.state()["runs"], [])

    async def test_compose_dispatch_never_enters_collector_or_paid_stages(self):
        self.production._recompose_saved = AsyncMock()
        queued = await self.production.start(["unit-task"], retry=True, stage="compose")
        self.production.cancels[queued["batch_id"]] = threading.Event()
        await self.production._task(queued["batch_id"], "unit-task")
        self.production._recompose_saved.assert_awaited_once()
        self.assertEqual(self.repository.state()["runs"], [])

    async def test_parent_fingerprint_string_without_applied_repair_is_not_reused(self):
        root = self.production.settings.storage_root
        self.production.settings.factory_comfy_cutout_workflow.write_text('{"unit":"workflow"}')
        source, alpha, manifest = root / "source.mkv", root / "alpha.mkv", root / "manifest.json"
        source.write_bytes(b"isolated source")
        alpha.write_bytes(b"isolated alpha")
        original = self.repository.state()["assets"][0]
        manifest.write_text(json.dumps({"task_id": "unit-task", "full_decode_verified": True,
            "rgb_sha256": file_hash(original["storage_path"]), "alpha_sha256": file_hash(alpha), "source_path": str(source),
            "source_sha256": file_hash(source), "workflow_sha256": file_hash(self.production.settings.factory_comfy_cutout_workflow),
            "implementation_hashes": {"factory_algorithm_version": "isolated-cutout"}}))
        self.production.registry.allowed_path = lambda value: Path(value).resolve()
        fingerprint = self.production._fingerprint(self.repository.state()["tasks"][0], "person")
        def setup(state):
            state["tasks"][0]["context"]["production"].update(current_person_asset_id="unit-asset-v1")
            asset = state["assets"][0]
            asset.update(type="person", sha256=file_hash(asset["storage_path"]))
            asset["legacy_reference"].update(parent_factory_fingerprint=fingerprint, factory_fingerprint="local-alpha-edit")
            asset["legacy_reference"].update(alpha_path=str(alpha), alpha_sha256=file_hash(alpha), manifest_path=str(manifest),
                manifest_sha256=file_hash(manifest), source_sha256=file_hash(source), workflow_sha256=file_hash(self.production.settings.factory_comfy_cutout_workflow))
        self.repository.mutate(setup)
        self.assertIsNone(self.production._asset("unit-task", "person"))

    async def test_old_composition_cannot_be_approved_after_parent_repair(self):
        self.repository.mutate(lambda state: state["assets"][0]["legacy_reference"].update(superseded_by_person_asset_id="person-v2"))
        with self.assertRaises(HTTPException) as error:
            await self.review.action("approve", "unit-review-v1", {})
        self.assertIn("SUPERSEDED_VERSION", error.exception.detail)
        self.assertEqual(self.repository.state()["reviews"][0]["status"], "pending")
        self.assertFalse(self.production.registry.registered)


class ManualBackgroundTests(ReviewFixture, unittest.TestCase):
    def setUp(self):
        self.prepare()
        app = FastAPI()
        app.include_router(create_production_router(self.production))
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def upload(self, filename="background.png"):
        return self.client.post("/api/background/manual?task_id=unit-task&prompt_id=unit-prompt",
                                content=b"\x89PNG\r\n\x1a\nfixture", headers={"x-filename": filename})

    def test_upload_path_traversal_is_rejected_without_run(self):
        response = self.upload("%2e%2e%2foutside.png")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.repository.state()["runs"], [])

    def test_manual_background_registers_fingerprint_used_by_composition_cache(self):
        response = self.upload()
        self.assertEqual(response.status_code, 201)
        kind, run_id, evidence, _values = self.production.registry.registered[0]
        self.assertEqual(kind, "background")
        self.assertTrue(evidence.get("legacy_reference", {}).get("factory_fingerprint"))
        self.assertEqual(self.repository.state()["runs"][0]["status"], "success")
        self.assertEqual(self.repository.state()["runs"][0]["id"], run_id)

    def test_ffprobe_failure_records_failed_run(self):
        self.production.registry.failure = subprocess.CalledProcessError(1, ["ffprobe"])
        response = self.upload()
        self.assertEqual(response.status_code, 422)
        run = self.repository.state()["runs"][0]
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["error_code"], "INVALID_BACKGROUND")


if __name__ == "__main__":
    unittest.main()
