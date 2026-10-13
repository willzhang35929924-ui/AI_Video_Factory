import asyncio
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from pydantic import ValidationError

from app.action_schemas import ReviewNote
from app.config import Settings
from app.domain import find, now
from app.integrations.asset_registry import file_hash
from app.integrations.collector_service import CollectorService
from app.integrations.contracts import CollectorResult
from app.integrations.legacy.collector_worker import verify_complete_download, write_json
from app.integrations.production_service import ProductionService
from app.integrations.production_readiness import STAGE_REQUIREMENTS
from app.integrations.review_service import RealReviewService
from app.integrations.source_review import WATERMARK_UNVERIFIED_MESSAGE
from app.repository import StateRepository


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "local media tools unavailable")
class SourceReviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.legacy = self.root / "legacy"
        self.storage = self.root / "factory"
        self.source = self.legacy / "downloads/source.mp4"
        self.source.parent.mkdir(parents=True)
        await asyncio.to_thread(subprocess.run, ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=size=64x64:rate=5:duration=1", "-c:v", "libx264", str(self.source)], check=True, capture_output=True)
        self.image = self.storage / "input.png"
        self.image.parent.mkdir()
        self.image.write_bytes(b"isolated-reference-not-production")
        self.settings = Settings(app_mode="integration", integration_dry_run=False, storage_root=self.storage,
            legacy_tiktok_auto_root=self.legacy, legacy_tiktok_auto_python=Path(sys.executable), database_url="sqlite:///" + str(self.root / "test.db"))
        self.repository = StateRepository(self.settings.database_url, Path(__file__).parents[1] / "app/seed.json")
        self.addCleanup(self.repository.engine.dispose)
        self.hub = SimpleNamespace(broadcast=AsyncMock())
        self.collector = CollectorService(self.repository, self.hub, self.settings)
        self.rules = {"source": str(self.legacy / "config.yaml"), "sha256": "unit-config", "read_only": True,
            "rules": {"min_views": 100, "min_likes": 0, "min_like_view_ratio": .01, "product_match_min_confidence": .85,
                "reference_product_match_required": "pass", "accepted_product_match": "pass", "accepted_ai_origin": "likely_real", "accepted_cutout_suitability": "pass",
                "accepted_require_cutout_suitability": True, "manual_review_on_uncertain": True, "vidbee_require_watermark_review": True}}
        self.collector.rules = lambda: copy.deepcopy(self.rules)
        created = await self.collector.create({"country": "MX", "keyword": "unit-product", "product_images": [str(self.image)], "source_document": "isolated-sheet", "source_row": 2,
            "video_id": "904", "video_url": "https://www.tiktok.com/@unit/video/904"})
        self.task_id = created["task_id"]
        self.context = self.collector.context(self.task_id)
        external = SimpleNamespace(task_id="unit-vidbee-original", status="completed", file_path=str(self.source), sha256=file_hash(self.source))
        pipeline = SimpleNamespace(config={"download_min_file_bytes": 1}, vidbee=SimpleNamespace(lookup_task=lambda _: SimpleNamespace(found=True, task=external, reason="isolated-same-file")))
        self.metadata = await asyncio.to_thread(verify_complete_download, pipeline,
            {"download_path": str(self.source), "download_source_type": "vidbee_automation", "vidbee_task_id": external.task_id}, external, self.legacy)
        self.result = CollectorResult.model_validate({**self.context, "video_id": "904", "video_url": "https://www.tiktok.com/@unit/video/904",
            "views": 100000, "likes": 2000, "reference_product_match": "pass", "product_match": "pass", "product_match_confidence": .95,
            "ai_origin": "likely_real", "cutout_suitability": "pass", "decision": "manual_review", "decision_reason": "watermark_unverified",
            "legacy_vidbee_task_id": external.task_id, "download_status": "completed", "source_asset_metadata": self.metadata,
            "legacy_csv_path": str(self.legacy / "output/candidates.csv"), "legacy_result_reference": {}}).model_dump(mode="json")
        self.run_id = "unit-original-collector-run"
        self.repository.mutate(lambda state: state["runs"].append({"id": self.run_id, "task_id": self.task_id, "stage": "collector", "status": "running", "provider": "LegacyCollectorAdapter", "progress": 0, "logs": [], "created_at": now(), "idempotency_key": self.task_id + ":collector:0"}))
        self.collector._finish(self.task_id, self.run_id, self.result, {"pid": 12345, "exit_code": 0}, time.monotonic())
        self.asset = next(item for item in self.repository.state()["assets"] if item["task_id"] == self.task_id and item["type"] == "source")
        self.directory = self.storage / "projects" / self.context["project_id"] / "tasks" / self.task_id / "metadata"
        (self.directory / self.run_id).mkdir(parents=True)
        write_json(self.directory / self.run_id / "collector-result.json", self.result)
        report_path = self.directory / "legacy-work/data/semantic_analysis/unit-semantic/analysis.json"
        (report_path.parent / "attempt-001").mkdir(parents=True)
        report = {"analysis_status": "completed", "analysis_identity": "unit-semantic", "product_match": "pass", "ai_origin": "likely_real", "cutout_suitability": "pass"}
        write_json(report_path, report)
        write_json(report_path.parent / "attempt-001/response.json", {"status": "completed", "result": report})
        write_json(report_path.parent / "state.json", {"status": "completed", "identity": "unit-semantic", "result": report})
        image_fingerprint = hashlib.sha256(file_hash(self.image).encode()).hexdigest()[:16]
        row = {**self.context, "video_id": self.result["video_id"], "url": self.result["video_url"], "reference_image_fingerprint": image_fingerprint,
            "vidbee_task_id": external.task_id, "reference_product_match": "pass", "reference_product_confidence": .95, "analysis_status": "completed", "analysis_identity": "unit-semantic",
            "analysis_report": str(report_path), "product_match": "pass", "ai_origin": "likely_real", "cutout_suitability": "pass", "decision": "manual_review", "reason": "watermark_unverified", "error": WATERMARK_UNVERIFIED_MESSAGE}
        write_json(self.directory / "collector-state.json", {"status": "finished", "legacy_processing_entered": True,
            "identity": hashlib.sha256(json.dumps(self.context, ensure_ascii=False, sort_keys=True).encode()).hexdigest(), "row": row,
            "recovery_evidence": {**self.context, "config_sha256": "unit-config", "reference_image_fingerprint": image_fingerprint,
                "video_id": self.result["video_id"], "video_url": self.result["video_url"], "vidbee_task_id": external.task_id}})
        self.production = ProductionService(self.repository, self.hub, self.settings, self.collector)
        self.repository.mutate(lambda state: state["settings"].update(production_policy={"auto_advance": False}))
        self.review = RealReviewService(self.production)
        self.verify_calls = []
        async def verify(context, row, directory):
            self.verify_calls.append(copy.deepcopy(row))
            directory.mkdir(parents=True)
            return {"status": "READY", "asset": copy.deepcopy(self.metadata), "_process": {"pid": 54321, "exit_code": 0, "duration_ms": 10, "started_at": 1791390000, "finished_at": 1791390000.01}}
        self.production.vidbee.verify_completed_once = verify
        self.review_id = self.production.source_review.register_pending(self.task_id)

    async def test_pending_registration_is_idempotent_and_never_a_final_asset(self):
        first = self.repository.state()
        self.assertEqual(self.production.source_review.register_pending(self.task_id), self.review_id)
        state = self.repository.state()
        self.assertEqual(state, first)
        self.assertEqual(find(state, "reviews", self.review_id)["issues"], ["WATERMARK_UNVERIFIED"])
        self.assertEqual(find(state, "tasks", self.task_id)["status"], "WAITING_REVIEW")
        self.assertEqual(len([asset for asset in state["assets"] if asset["task_id"] == self.task_id]), 1)
        self.assertEqual(len([run for run in state["runs"] if run["task_id"] == self.task_id]), 1)
        self.assertFalse(self.verify_calls)

    async def test_human_confirmation_required_and_strict_boolean(self):
        for payload in ({}, {"confirmed_full_video_watermark_free": False}, {"confirmed_full_video_watermark_free": "true"}, {"confirmed_full_video_watermark_free": 1}):
            with self.subTest(payload=payload), self.assertRaises(HTTPException):
                await self.review.action("approve", self.review_id, payload)
        self.assertFalse(self.verify_calls)
        with self.assertRaises(ValidationError):
            ReviewNote.model_validate({"confirmed_full_video_watermark_free": "true"})

    async def test_approval_is_source_only_persistent_and_never_changes_ai_decision(self):
        original = copy.deepcopy(find(self.repository.state(), "tasks", self.task_id)["collector_result"])
        response = await self.review.action("approve", self.review_id, {"confirmed_full_video_watermark_free": True, "note": "isolated human confirmation"})
        self.assertEqual(response["scope"], "SOURCE_WATERMARK_ONLY")
        state = self.repository.state()
        task = find(state, "tasks", self.task_id)
        self.assertEqual(task["status"], "COLLECTED")
        self.assertEqual(task["current_stage"], "download")
        self.assertEqual(task["collector_result"], original)
        self.assertFalse(task["context"].get("production", {}).get("review_approved"))
        self.assertFalse(any(asset["task_id"] == self.task_id and asset["type"] == "final" for asset in state["assets"]))
        self.assertEqual(self.production.summary([self.task_id])["accepted_count"], 0)
        self.assertEqual(self.production.summary([self.task_id])["approved_source_count"], 1)
        self.assertEqual(len(self.verify_calls), 1)
        verifier = next(run for run in state["runs"] if run["task_id"] == self.task_id and run["provider"] == "VidbeeDownloaderAdapter")
        self.assertEqual(verifier["pid"], 54321)
        restarted = StateRepository(self.settings.database_url, Path(__file__).parents[1] / "app/seed.json")
        try:
            self.assertEqual(find(restarted.state(), "tasks", self.task_id)["context"]["source_watermark_reviews"], task["context"]["source_watermark_reviews"])
        finally:
            restarted.engine.dispose()
        with self.assertRaises(HTTPException):
            await self.review.action("approve", self.review_id, {"confirmed_full_video_watermark_free": True})

    async def test_source_retry_or_cutout_fix_is_not_available_and_rejection_keeps_media(self):
        for action in ("retry", "reprocess", "fix"):
            with self.subTest(action=action), self.assertRaises(HTTPException):
                await self.review.action(action, self.review_id, {})
        await self.review.action("reject", self.review_id, {"note": "watermark visible"})
        self.assertEqual(find(self.repository.state(), "tasks", self.task_id)["status"], "REJECTED")
        self.assertTrue(self.source.is_file())
        self.assertEqual(file_hash(self.source), self.asset["sha256"])
        self.assertEqual(self.production.summary([self.task_id])["accepted_count"], 0)
        self.assertFalse(self.verify_calls)

    async def test_unknown_external_or_non_watermark_manual_cannot_be_approved(self):
        def unknown(state):
            state["runs"].append({"id": "unit-unknown-request", "task_id": self.task_id, "stage": "background", "provider": "BackgroundRPAAdapter", "status": "failed", "progress": 0, "logs": [], "created_at": now(), "error_code": "UNKNOWN_EXTERNAL_REQUEST"})
        self.repository.mutate(unknown)
        with self.assertRaises(HTTPException):
            await self.review.action("approve", self.review_id, {"confirmed_full_video_watermark_free": True})
        self.assertFalse(self.verify_calls)
        self.repository.mutate(lambda state: (state["runs"].pop(), find(state, "tasks", self.task_id)["collector_result"].update(decision_reason="semantic_uncertain")))
        with self.assertRaises(HTTPException):
            await self.review.action("approve", self.review_id, {"confirmed_full_video_watermark_free": True})

    async def test_changed_source_images_rules_context_or_analysis_invalidate_approval(self):
        await self.review.action("approve", self.review_id, {"confirmed_full_video_watermark_free": True})
        report = self.directory / "legacy-work/data/semantic_analysis/unit-semantic/analysis.json"
        for changed in ("source", "image", "rules", "context", "analysis"):
            source_bytes, image_bytes, report_bytes = self.source.read_bytes(), self.image.read_bytes(), report.read_bytes()
            with self.subTest(changed=changed):
                if changed == "source":
                    self.source.write_bytes(b"changed-source")
                elif changed == "image":
                    self.image.write_bytes(b"changed-image")
                elif changed == "rules":
                    self.rules["sha256"] = "changed-rule-file"
                elif changed == "context":
                    self.repository.mutate(lambda state: find(state, "tasks", self.task_id)["context"]["task_context"].update(source_row=9))
                else:
                    report.write_text("{}")
                with self.assertRaises((HTTPException, ValueError, KeyError)):
                    await self.production.source_review.validate_approval(self.task_id)
                self.assertEqual(len(self.verify_calls), 1, "changed evidence must fail before another external check")
            self.source.write_bytes(source_bytes)
            os.utime(self.source, ns=(self.metadata["mtime_ns"], self.metadata["mtime_ns"]))
            self.image.write_bytes(image_bytes)
            report.write_bytes(report_bytes)
            self.rules["sha256"] = "unit-config"
            self.repository.mutate(lambda state: find(state, "tasks", self.task_id)["context"]["task_context"].update(source_row=2))

    async def test_production_resume_uses_approved_source_without_collector_or_paid_requests(self):
        await self.review.action("approve", self.review_id, {"confirmed_full_video_watermark_free": True})
        self.production.readiness.check = AsyncMock(return_value={"ready": True, "checks": [{"name": name, "status": "READY"} for name in set().union(*STAGE_REQUIREMENTS.values())]})
        queued = await self.production.start([self.task_id])
        self.production.cancels[queued["batch_id"]] = threading.Event()
        self.production._checkpoint = AsyncMock()
        self.production._stage = AsyncMock(return_value={"status": "WAITING"})
        self.collector.queue = AsyncMock(side_effect=AssertionError("must not search or re-analyze"))
        self.production._video_stage = AsyncMock(side_effect=AssertionError("must not execute paid or cutout in this test"))
        await self.production._task(queued["batch_id"], self.task_id)
        self.collector.queue.assert_not_awaited()
        self.production._video_stage.assert_not_awaited()
        self.assertEqual(self.production._stage.await_args.args[2], "download")
        self.assertEqual(len(self.verify_calls), 2)
        self.assertEqual(find(self.repository.state(), "tasks", self.task_id)["collector_result"]["decision"], "manual_review")

    async def test_changed_vidbee_association_fails_approval_and_preserves_verification_run(self):
        async def mismatch(context, row, directory):
            directory.mkdir(parents=True)
            metadata = copy.deepcopy(self.metadata)
            metadata["download_verification"]["task_id"] = "different-external-task"
            return {"status": "READY", "asset": metadata, "_process": {"pid": 54321, "exit_code": 0, "duration_ms": 10}}
        self.production.vidbee.verify_completed_once = mismatch
        with self.assertRaises(HTTPException):
            await self.review.action("approve", self.review_id, {"confirmed_full_video_watermark_free": True})
        state = self.repository.state()
        self.assertEqual(find(state, "reviews", self.review_id)["status"], "pending")
        self.assertEqual(find(state, "tasks", self.task_id)["status"], "WAITING_REVIEW")
        self.assertEqual(find(state, "runs", self.run_id)["status"], "success")
        verification = next(run for run in state["runs"] if run["task_id"] == self.task_id and run["provider"] == "VidbeeDownloaderAdapter")
        self.assertEqual(verification["status"], "failed")
        self.assertEqual(verification["error_code"], "SOURCE_REVIEW_DOWNLOAD_NOT_VERIFIED")

    async def test_approval_expired_before_production_never_runs_a_paid_stage(self):
        await self.review.action("approve", self.review_id, {"confirmed_full_video_watermark_free": True})
        self.production.readiness.check = AsyncMock(return_value={"ready": True, "checks": [{"name": name, "status": "READY"} for name in set().union(*STAGE_REQUIREMENTS.values())]})
        queued = await self.production.start([self.task_id])
        self.production.cancels[queued["batch_id"]] = threading.Event()
        self.image.write_bytes(b"different-reference-after-human-approval")
        self.production._stage = AsyncMock(side_effect=AssertionError("expired source cannot enter another stage"))
        self.collector.queue = AsyncMock(side_effect=AssertionError("expired source cannot be rescreened automatically"))
        await self.production._task(queued["batch_id"], self.task_id)
        self.production._stage.assert_not_awaited()
        self.collector.queue.assert_not_awaited()
        self.assertEqual(find(self.repository.state(), "tasks", self.task_id)["status"], "WAITING_REVIEW")
        self.assertEqual(len(self.verify_calls), 1)


if __name__ == "__main__":
    unittest.main()
