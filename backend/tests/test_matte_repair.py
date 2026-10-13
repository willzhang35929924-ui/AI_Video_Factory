import asyncio
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.integrations.matte_repair import MatteRepairService, RepairRequest, create_repair_router
from app.config import Settings
from test_real_review import MemoryRepository, UnitRegistry
from app.integrations.process_runner import BASE_ENV_KEYS
from app.integrations.asset_registry import file_hash


@unittest.skipUnless(Path('/Users/xiaoyao/ComfyUI/.venv/bin/python').is_file(), 'existing segmentation environment unavailable')
class SegmentationOpacityTests(unittest.TestCase):
    def test_probability_is_not_opacity_and_only_selected_interior_is_opaque(self):
        worker_directory = Path(__file__).resolve().parents[1] / 'app/integrations/legacy'
        script = """
import sys, numpy as np
sys.path.insert(0, sys.argv[1])
from matte_repair_worker import segmentation_alpha
p = np.zeros((16,16), dtype=np.uint8)
p[3:13,3:13] = 153
a = segmentation_alpha(p)
assert a.dtype == np.uint16 and a[8,8] == 65535
assert a[0,0] == 0 and 0 < a[3,8] < 65535
p[8,8] = 89
assert segmentation_alpha(p)[8,8] == 0
"""
        subprocess.run(['/Users/xiaoyao/ComfyUI/.venv/bin/python', '-B', '-c', script, str(worker_directory)],
            env={key: os.environ[key] for key in BASE_ENV_KEYS if key in os.environ},
            check=True, capture_output=True, timeout=60)


class RepairInputTests(unittest.TestCase):
    def value(self, **values):
        return {"person_asset_id": "person-v1", "start_frame": 10, "end_frame": 20, "idempotency_key": "unique-edit-01",
                "keyframes": [{"frame_index": 12, "object_id": "bag_1", "points": [{"x": .4, "y": .6, "label": 1}]}], **values}

    def test_half_open_interval_excludes_end_keyframe(self):
        with self.assertRaises(ValidationError):
            RepairRequest.model_validate(self.value(keyframes=[{"frame_index": 20, "object_id": "bag_1", "box": [.1,.1,.2,.2]}]))

    def test_annotations_keep_independent_objects_and_coordinates(self):
        value = self.value()
        value["keyframes"].append({"frame_index": 12, "object_id": "person_1", "box": [.2,.2,.8,.9]})
        request = RepairRequest.model_validate(value)
        self.assertEqual([item.object_id for item in request.keyframes], ["bag_1", "person_1"])
        self.assertEqual(request.keyframes[0].coordinate_space, "normalized")

    def test_traversal_object_or_outside_coordinate_rejected(self):
        for keyframe in ({"frame_index": 12, "object_id": "../bag", "box": [.1,.1,.2,.2]},
                         {"frame_index": 12, "object_id": "bag", "points": [{"x": 1.2,"y": .2,"label":1}]}):
            with self.assertRaises(ValidationError):
                RepairRequest.model_validate(self.value(keyframes=[keyframe]))

    def test_duplicate_object_keyframe_is_not_silently_overwritten(self):
        value = self.value()
        value["keyframes"].append(value["keyframes"][0])
        with self.assertRaises(ValidationError):
            RepairRequest.model_validate(value)

    def test_single_direction_validates_real_tracking_boundary_before_queuing(self):
        with self.assertRaises(ValidationError):
            RepairRequest.model_validate(self.value(direction="forward"))
        value = self.value(direction="forward")
        value["keyframes"][0]["frame_index"] = value["start_frame"]
        self.assertEqual(RepairRequest.model_validate(value).direction, "forward")
        value = self.value(direction="backward")
        value["keyframes"][0]["frame_index"] = value["end_frame"] - 1
        self.assertEqual(RepairRequest.model_validate(value).direction, "backward")

    def test_brush_only_requires_actual_positive_pixels_not_empty_stroke(self):
        frame = {"frame_index":12, "object_id":"bag_1", "strokes":[{"radius":12,"label":1,"points":[]}]}
        with self.assertRaises(ValidationError):
            RepairRequest.model_validate(self.value(keyframes=[frame], direction="keyframes_only"))
        frame["strokes"][0]["points"] = [{"x":.4,"y":.6,"label":1}]
        self.assertEqual(RepairRequest.model_validate(self.value(keyframes=[frame],direction="keyframes_only")).direction,"keyframes_only")


class RepairVersionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.task = {"id": "isolated-task", "project_id": "isolated-project", "execution_mode": "integration",
                     "context": {"production": {}}, "status": "WAITING_REVIEW", "current_stage": "review"}
        self.person = {"id": "person-v1", "task_id": "isolated-task", "type": "person", "sha256": "rgb-sha",
                       "legacy_reference": {"version": 1, "alpha_sha256": "alpha-sha", "time_mapping_path": "mapping"}}
        self.repository = MemoryRepository({"tasks": [self.task], "assets": [self.person], "runs": [], "reviews": [], "audits": []})
        async def event(*args, **kwargs):
            pass
        self.service = MatteRepairService(SimpleNamespace(repository=self.repository, settings=SimpleNamespace(storage_root=self.root), event=event, start_lock=asyncio.Lock()))
        self.service.pair = lambda *args: (self.repository.state()["tasks"][0], self.person, self.root / "RGB.mkv", self.root / "Alpha.mkv",
                                          {"media": {"width": 100, "height": 200, "frame_count": 40}, "source_sha256": "source-sha"})

    def applied_parent(self, approved=False, technical=False):
        if approved:
            self.person["status"] = "approved"
        if technical:
            self.person["legacy_reference"]["execution_scope"] = "TECHNICAL_VALIDATION"
        def setup(state):
            state["assets"][0] = json.loads(json.dumps(self.person))
            selection = "technical_validation" if technical else "production"
            state["tasks"][0]["context"].setdefault(selection, {})["current_person_asset_id"] = "person-v2"
            state["tasks"][0]["context"]["matte_repairs"] = [{"id": "repair-v2", "status": "APPLIED",
                "asset_id": "person-v2", "person_asset_id": "person-v1", "parent_rgb_sha256": self.person["sha256"],
                "parent_alpha_sha256": self.person["legacy_reference"]["alpha_sha256"],
                "execution_scope": "TECHNICAL_VALIDATION" if technical else "PRODUCTION"}]
            state["assets"].append({**self.person, "id": "person-v2"})
            if approved:
                state["reviews"].append({"id": "review-20261007190000000000-human", "task_id": "isolated-task",
                    "asset_id": "person-v1", "status": "approved"})
                state["audits"].append({"id": "isolated-human-audit", "action": "review.approve",
                    "task_id": "isolated-task", "entity_id": "review-20261007190000000000-human"})
        self.repository.mutate(setup)

    def real_pair_checks(self):
        rgb, alpha, manifest = self.root / "RGB.mkv", self.root / "Alpha.mkv", self.root / "mother.json"
        rgb.write_bytes(b"isolated immutable RGB hash fixture, not production media")
        alpha.write_bytes(b"isolated immutable Alpha hash fixture, not production media")
        manifest.write_text(json.dumps({"full_decode_verified": True, "source_sha256": "unit-source-sha",
            "media": {"width": 100, "height": 200, "fps": 30, "frame_count": 40}}))
        self.person.update(storage_path=str(rgb), sha256=file_hash(rgb), registry_status="registered")
        self.person["legacy_reference"].update(alpha_path=str(alpha), alpha_sha256=file_hash(alpha), alpha_mode="straight",
            manifest_path=str(manifest), manifest_sha256=file_hash(manifest))
        self.service.production.registry = UnitRegistry(self.repository)
        self.service.production.registry.read_path = lambda value: Path(value).resolve()
        self.service.pair = MatteRepairService.pair.__get__(self.service, MatteRepairService)
        return rgb, alpha, manifest

    async def test_preparing_is_idempotent_and_does_not_run_or_modify_parent(self):
        request = RepairRequest.model_validate(RepairInputTests().value())
        first, created = await self.service.prepare("isolated-task", request)
        second, created_again = await self.service.prepare("isolated-task", request)
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.repository.state()["runs"]), 1)
        self.assertEqual(self.repository.state()["assets"], [self.person])
        self.assertFalse(self.service.jobs)
        self.assertEqual(first["source_sha256"], "source-sha")

    async def test_same_annotations_with_new_ui_key_reuse_actual_request_fingerprint(self):
        original = RepairInputTests().value()
        first, _ = await self.service.prepare("isolated-task", RepairRequest.model_validate(original))
        duplicate = {**original, "idempotency_key": "new-browser-key-02"}
        second, created = await self.service.prepare("isolated-task", RepairRequest.model_validate(duplicate))
        self.assertFalse(created)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.repository.state()["runs"]), 1)
        with self.assertRaises(HTTPException) as error:
            await self.service.prepare("isolated-task", RepairRequest.model_validate({**original, "mode": "remove"}))
        self.assertEqual(error.exception.detail, "IDEMPOTENCY_CONFLICT")

    async def test_original_key_survives_helper_deployment_without_repeating_old_run(self):
        original = RepairInputTests().value()
        first, _ = await self.service.prepare("isolated-task", RepairRequest.model_validate(original))
        with patch("app.integrations.matte_repair.file_hash", return_value="new-helper-code-sha"):
            second, created = await self.service.prepare("isolated-task", RepairRequest.model_validate(original))
        self.assertFalse(created)
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(len(self.repository.state()["runs"]), 1)

    async def test_failed_or_interrupted_annotations_allow_explicit_new_run_keep_history(self):
        original = RepairInputTests().value()
        first, _ = await self.service.prepare("isolated-task", RepairRequest.model_validate(original))
        self.service.update("isolated-task", first["id"], status="FAILED", error_code="unit-failure")
        self.repository.mutate(lambda state: state["runs"][0].update(status="failed"))
        second, created = await self.service.prepare("isolated-task", RepairRequest.model_validate({**original, "idempotency_key": "explicit-retry-02"}))
        self.assertTrue(created)
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual(self.service.records("isolated-task")[0]["error_code"], "unit-failure")
        self.assertEqual(len(self.repository.state()["runs"]), 2)
        self.service.update("isolated-task", second["id"], status="INTERRUPTED", error_code="LOCAL_EXECUTION_INTERRUPTED")
        self.repository.mutate(lambda state: state["runs"][1].update(status="failed"))
        third, created = await self.service.prepare("isolated-task", RepairRequest.model_validate({**original, "idempotency_key": "explicit-restart-03"}))
        self.assertTrue(created)
        self.assertNotEqual(second["run_id"], third["run_id"])
        same, created = await self.service.prepare("isolated-task", RepairRequest.model_validate(original))
        self.assertFalse(created)
        self.assertEqual(same["id"], first["id"])

    async def test_factory_local_work_has_bounded_long_video_timeouts(self):
        settings = Settings(_env_file=None)
        self.assertEqual(settings.matte_repair_timeout_seconds, 7200)
        self.assertEqual(settings.legacy_video_timeout_seconds, 7200)

    async def test_apply_requires_verified_preview(self):
        record, _ = await self.service.prepare("isolated-task", RepairRequest.model_validate(RepairInputTests().value()))
        with self.assertRaises(HTTPException):
            await self.service.apply("isolated-task", record["id"])
        self.assertEqual(len(self.repository.state()["assets"]), 1)

    async def test_technical_apply_and_undo_keep_business_task_and_decision(self):
        self.person["legacy_reference"]["execution_scope"] = "TECHNICAL_VALIDATION"
        self.repository.mutate(lambda state: state["tasks"][0].update(status="CREATED", collector_result={}))
        self.service.production.registry = UnitRegistry(self.repository)
        original = self.repository.state()["tasks"][0]
        record, _ = await self.service.prepare("isolated-task", RepairRequest.model_validate(RepairInputTests().value()))
        self.service.update("isolated-task", record["id"], status="PREVIEW_READY", evidence={"asset": {"path": str(self.root / "RGB.mkv"), "sha256": "rgb-sha", "legacy_reference": {"execution_scope": "TECHNICAL_VALIDATION"}}})
        self.repository.mutate(lambda state: state["runs"][0].update(status="success"))
        applied = await self.service.apply("isolated-task", record["id"])
        self.assertEqual(applied["status"], "APPLIED")
        self.assertEqual(self.repository.state()["tasks"][0]["status"], original["status"])
        self.assertEqual(self.repository.state()["tasks"][0]["collector_result"], {})
        self.assertNotIn("current_person_asset_id", self.repository.state()["tasks"][0]["context"]["production"])
        self.assertEqual(len(self.repository.state()["reviews"]), 1)
        await self.service.apply("isolated-task", record["id"])
        self.assertEqual(len(self.repository.state()["reviews"]), 1)
        await self.service.undo("isolated-task", record["id"])
        self.assertEqual(self.repository.state()["tasks"][0]["status"], "CREATED")
        self.assertEqual(self.repository.state()["tasks"][0]["context"]["technical_validation"]["current_person_asset_id"], "person-v1")

    async def test_restart_preserves_annotations_and_releases_interrupted_run(self):
        record, _ = await self.service.prepare("isolated-task", RepairRequest.model_validate(RepairInputTests().value()))
        self.service.update("isolated-task", record["id"], status="PROPAGATING")
        await self.service.recover()
        self.assertEqual(self.service.records("isolated-task")[0]["status"], "INTERRUPTED")
        self.assertEqual(self.repository.state()["runs"][0]["status"], "failed")
        self.assertEqual(self.service.records("isolated-task")[0]["keyframes"], record["keyframes"])
        self.assertEqual(self.repository.state()["assets"], [self.person])

    async def test_queue_reservation_blocks_edit_before_process_starts(self):
        self.repository.mutate(lambda state: state.update(batches=[{"status": "QUEUED", "items": [{"task_id": "isolated-task", "status": "QUEUED"}]}]))
        with self.assertRaises(HTTPException) as error:
            await self.service.prepare("isolated-task", RepairRequest.model_validate(RepairInputTests().value()))
        self.assertEqual(error.exception.detail, "TASK_BUSY")

    async def test_undo_changes_selection_only_and_preserves_history(self):
        def setup(state):
            state["tasks"][0]["context"]["production"]["current_person_asset_id"] = "person-v2"
            state["tasks"][0]["context"]["matte_repairs"] = [{"id": "repair-v2", "status": "APPLIED", "asset_id": "person-v2", "person_asset_id": "person-v1"}]
            state["assets"].append({**self.person, "id": "person-v2"})
        self.repository.mutate(setup)
        assets = self.repository.state()["assets"]
        response = await self.service.undo("isolated-task", "repair-v2")
        self.assertEqual(self.repository.state()["assets"], assets)
        self.assertEqual(self.repository.state()["tasks"][0]["context"]["production"]["current_person_asset_id"], "person-v1")
        self.assertFalse(self.repository.state()["tasks"][0]["context"]["production"]["review_approved"])
        self.assertEqual(response["status"], "UNDONE")
        self.assertEqual(response["person_asset_id"], "person-v1")
        self.assertEqual(self.repository.state()["reviews"][-1]["issues"], ["RESTORED_VERSION_REQUIRES_REVIEW"])

    async def test_restore_version_preserves_media_and_reviews_and_is_idempotent(self):
        def setup(state):
            state["tasks"][0]["context"]["production"]["current_person_asset_id"] = "person-v2"
            state["assets"].append({**self.person,"id":"person-v2"})
            state["reviews"].append({"id":"old-review","asset_id":"person-v1","task_id":"isolated-task","status":"approved"})
        self.repository.mutate(setup)
        assets = self.repository.state()["assets"]
        restored = await self.service.restore_version("isolated-task", "person-v1")
        self.assertTrue(restored["changed"])
        self.assertEqual(self.repository.state()["assets"],assets)
        self.assertEqual(self.repository.state()["reviews"][0]["status"],"approved")
        self.assertEqual(self.repository.state()["reviews"][-1]["status"],"pending")
        self.assertEqual(self.repository.state()["tasks"][0]["status"],"WAITING_REVIEW")
        same = await self.service.restore_version("isolated-task", "person-v1")
        self.assertFalse(same["changed"])
        self.assertEqual(len(self.repository.state()["reviews"]),2)

    async def test_undo_reuses_only_exact_existing_human_approval_without_new_review(self):
        self.applied_parent(approved=True)
        before = self.repository.state()
        response = await self.service.undo("isolated-task", "repair-v2")
        state = self.repository.state()
        self.assertTrue(response["review_reused"])
        self.assertEqual(response["review_id"], before["reviews"][0]["id"])
        self.assertEqual(state["reviews"], before["reviews"])
        self.assertEqual(state["assets"], before["assets"])
        self.assertEqual(state["tasks"][0]["status"], "COLLECTED")
        self.assertTrue(state["tasks"][0]["context"]["production"]["review_approved"])
        self.assertEqual(response["status"], "UNDONE")

    async def test_restore_reuses_existing_human_approval_and_is_idempotent(self):
        self.applied_parent(approved=True)
        before = self.repository.state()
        restored = await self.service.restore_version("isolated-task", "person-v1")
        self.assertTrue(restored["review_reused"])
        self.assertEqual(restored["status"], "COLLECTED")
        self.assertEqual(self.repository.state()["reviews"], before["reviews"])
        self.assertFalse((await self.service.restore_version("isolated-task", "person-v1"))["changed"])
        self.assertEqual(self.repository.state()["reviews"], before["reviews"])

    async def test_later_pending_or_rejected_parent_review_never_inherits_old_approval(self):
        for status in ("pending", "rejected", "manual"):
            with self.subTest(status=status):
                self.repository.mutate(lambda state: state.update(assets=[self.person], reviews=[], audits=[]))
                self.applied_parent(approved=True)
                self.repository.mutate(lambda state: state["reviews"].append({"id": "rev-20261008100000000000-later",
                    "created_at": "2026-10-08T10:00:00Z", "task_id": "isolated-task", "asset_id": "person-v1", "status": status}))
                response = await self.service.undo("isolated-task", "repair-v2")
                self.assertFalse(response["review_reused"])
                state = self.repository.state()
                self.assertEqual(state["tasks"][0]["status"], "WAITING_REVIEW")
                self.assertFalse(state["tasks"][0]["context"]["production"]["review_approved"])
                self.assertEqual(len(state["reviews"]), 3 if status == "rejected" else 2)

    async def test_approval_without_real_human_audit_is_not_reused(self):
        self.applied_parent(approved=True)
        self.repository.mutate(lambda state: state.update(audits=[]))
        response = await self.service.undo("isolated-task", "repair-v2")
        self.assertFalse(response["review_reused"])
        self.assertEqual(self.repository.state()["reviews"][-1]["status"], "pending")

    async def test_restoring_parent_reactivates_only_exact_departing_lineage(self):
        self.applied_parent(approved=True)
        def compositions(state):
            for identifier, parent, superseded, technical in (
                ("exact-parent", "person-v1", "person-v2", False),
                ("different-revision", "person-v1", "person-v3", False),
                ("different-parent", "person-v0", "person-v2", False),
                ("departing-person", "person-v2", None, False),
                ("technical-composite", "person-v1", "person-v2", True)):
                reference = {"person_asset_id": parent}
                if superseded:
                    reference["superseded_by_person_asset_id"] = superseded
                if technical:
                    reference["execution_scope"] = "TECHNICAL_VALIDATION"
                state["assets"].append({"id": identifier, "task_id": "isolated-task", "type": "compose",
                    "sha256": "unit-" + identifier, "legacy_reference": reference})
        self.repository.mutate(compositions)
        before = self.repository.state()
        await self.service.restore_version("isolated-task", "person-v1")
        assets = {row["id"]: row for row in self.repository.state()["assets"]}
        self.assertNotIn("superseded_by_person_asset_id", assets["exact-parent"]["legacy_reference"])
        self.assertEqual(assets["different-revision"]["legacy_reference"]["superseded_by_person_asset_id"], "person-v3")
        self.assertEqual(assets["different-parent"]["legacy_reference"]["superseded_by_person_asset_id"], "person-v2")
        self.assertEqual(assets["departing-person"]["legacy_reference"]["superseded_by_person_asset_id"], "person-v1")
        self.assertEqual(assets["technical-composite"]["legacy_reference"]["superseded_by_person_asset_id"], "person-v2")
        self.assertEqual([row["sha256"] for row in self.repository.state()["assets"]], [row["sha256"] for row in before["assets"]])
        self.assertEqual(self.repository.state()["reviews"], before["reviews"])

    async def test_tampered_rgb_alpha_or_manifest_blocks_undo_before_selection_changes(self):
        for channel in range(3):
            with self.subTest(channel=channel):
                self.repository.mutate(lambda state: state.update(assets=[self.person], reviews=[], audits=[]))
                paths = self.real_pair_checks()
                self.applied_parent(approved=True)
                paths[channel].write_bytes(b"changed isolated immutable evidence")
                before = self.repository.state()
                with self.assertRaises(HTTPException):
                    await self.service.undo("isolated-task", "repair-v2")
                self.assertEqual(self.repository.state(), before)

    async def test_changed_repair_parent_identity_blocks_undo(self):
        self.applied_parent(approved=True)
        self.repository.mutate(lambda state: state["tasks"][0]["context"]["matte_repairs"][0].update(parent_alpha_sha256="different-parent"))
        before = self.repository.state()
        with self.assertRaises(HTTPException) as error:
            await self.service.undo("isolated-task", "repair-v2")
        self.assertEqual(error.exception.detail, "PARENT_IDENTITY_CHANGED")
        self.assertEqual(self.repository.state(), before)

    async def test_technical_approval_and_restore_never_approve_normal_production(self):
        self.applied_parent(approved=True, technical=True)
        self.repository.mutate(lambda state: state["tasks"][0].update(status="CREATED", collector_result={}))
        before = self.repository.state()["tasks"][0]
        restored = await self.service.undo("isolated-task", "repair-v2")
        task = self.repository.state()["tasks"][0]
        self.assertFalse(restored["review_reused"])
        self.assertEqual(task["status"], "CREATED")
        self.assertEqual(task["collector_result"], before["collector_result"])
        self.assertEqual(task["context"]["production"], before["context"]["production"])
        self.assertEqual(self.repository.state()["reviews"][-1]["issues"], ["TECHNICAL_VALIDATION"])

    async def test_new_alpha_apply_still_requires_new_review_after_parent_approval(self):
        self.person["status"] = "approved"
        self.repository.mutate(lambda state: state["tasks"][0]["context"]["production"].update(current_person_asset_id="person-v1"))
        self.service.production.registry = UnitRegistry(self.repository)
        record, _ = await self.service.prepare("isolated-task", RepairRequest.model_validate(RepairInputTests().value()))
        self.service.update("isolated-task", record["id"], status="PREVIEW_READY", evidence={"asset": {
            "path": str(self.root / "RGB.mkv"), "sha256": "rgb-sha", "legacy_reference": {"alpha_sha256": "new-alpha-sha"}}})
        self.repository.mutate(lambda state: state["runs"][0].update(status="success"))
        applied = await self.service.apply("isolated-task", record["id"])
        self.assertEqual(applied["status"], "APPLIED")
        self.assertEqual(self.repository.state()["reviews"][-1]["status"], "pending")
        self.assertFalse(self.repository.state()["tasks"][0]["context"]["production"]["review_approved"])

    async def test_prepare_blocks_noncurrent_parent_before_segmentation(self):
        self.repository.mutate(lambda state: state["tasks"][0]["context"]["production"].update(current_person_asset_id="person-v2"))
        with self.assertRaises(HTTPException) as error:
            await self.service.prepare("isolated-task", RepairRequest.model_validate(RepairInputTests().value()))
        self.assertTrue(error.exception.detail.startswith("PARENT_VERSION_CHANGED"))
        self.assertEqual(self.repository.state()["runs"],[])

    async def test_real_version_change_queues_quality_not_reacquisition(self):
        self.repository.mutate(lambda state: state["tasks"][0]["context"]["production"].update(current_person_asset_id="person-v1"))
        self.service.production.continue_after_repair = AsyncMock(return_value={"accepted":True,"batch_id":"quality-batch"})
        self.service.production.launch = AsyncMock()
        await self.service.continue_after_version_change("isolated-task")
        self.service.production.continue_after_repair.assert_awaited_once_with("isolated-task")
        self.service.production.launch.assert_awaited_once_with("quality-batch")

    async def test_technical_version_does_not_enter_normal_production_queue(self):
        def setup(state):
            state["tasks"][0]["context"]["production"]["current_person_asset_id"] = "person-v1"
            state["assets"][0]["legacy_reference"]["execution_scope"] = "TECHNICAL_VALIDATION"
        self.repository.mutate(setup)
        self.service.production.continue_after_repair = AsyncMock()
        await self.service.continue_after_version_change("isolated-task")
        self.service.production.continue_after_repair.assert_not_awaited()


    async def test_technical_quality_persists_issues_without_business_or_human_approval(self):
        self.person["legacy_reference"]["technical_binding"] = {"project_id": "isolated-project"}
        self.repository.mutate(lambda state: state.update(quality_assessments=[]))
        original_task = self.repository.state()["tasks"][0]
        async def analyzed(adapter, person, directory, **kwargs):
            return {"status": "FAIL", "geometry_status": "GEOMETRY_FAIL", "quality_score": 91, "metrics": {},
                "issue_segments": [{"issue": "BBOX_JUMP", "start": .2, "end": .3}], "fingerprint": "isolated-report",
                "_process": {"pid": 34567, "exit_code": 0}, "cache_hit": False}
        with patch('app.integrations.matte_repair.CutoutQualityAdapter.analyze', new=analyzed):
            await self.service.assess_technical_person('isolated-task', self.person, None)
        state = self.repository.state()
        self.assertEqual(state["tasks"][0], original_task)
        self.assertEqual(state["quality_assessments"][0]["asset_id"], 'person-v1')
        self.assertEqual(state["quality_assessments"][0]["metrics"]["semantic_status"], 'UNKNOWN')
        self.assertEqual(state["runs"][0]["pid"], 34567)
        self.assertEqual(state["runs"][0]["status"], 'success')
        self.assertFalse(state["reviews"])


class RepairRouteTests(unittest.TestCase):
    def test_apply_returns_saved_revision_and_then_queues_existing_production_continuation(self):
        previous = {"id":"unit-repair","status":"PREVIEW_READY","person_asset_id":"person-v1"}
        service = SimpleNamespace(records=lambda _: [previous],
            apply=AsyncMock(return_value={**previous,"status":"APPLIED","asset_id":"person-v2"}),
            continue_after_version_change=AsyncMock())
        app = FastAPI()
        app.include_router(create_repair_router(service))
        with TestClient(app) as client:
            response = client.post("/api/tasks/unit-task/repairs/unit-repair/apply",json={})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()["asset_id"],"person-v2")
        service.continue_after_version_change.assert_awaited_once_with("unit-task")

    def test_repeated_apply_does_not_queue_quality_again(self):
        previous = {"id":"unit-repair","status":"APPLIED","person_asset_id":"person-v1","asset_id":"person-v2"}
        service = SimpleNamespace(records=lambda _: [previous],apply=AsyncMock(return_value=previous),
            continue_after_version_change=AsyncMock())
        app = FastAPI()
        app.include_router(create_repair_router(service))
        with TestClient(app) as client:
            response = client.post("/api/tasks/unit-task/repairs/unit-repair/apply",json={})
        self.assertEqual(response.status_code,200)
        service.continue_after_version_change.assert_not_awaited()

class RepairFrameRouteTests(unittest.IsolatedAsyncioTestCase):
    def endpoint(self, service):
        return next(route.endpoint for route in create_repair_router(service).routes if route.path.endswith('/frame'))

    async def test_disconnected_frame_is_not_decoded(self):
        pair = unittest.mock.Mock()
        endpoint = self.endpoint(SimpleNamespace(pair=pair))
        response = await endpoint('unit-task', 'unit-person', SimpleNamespace(is_disconnected=AsyncMock(return_value=True)), frame=40)
        self.assertEqual(response.status_code, 499)
        pair.assert_not_called()

    async def test_frame_decode_slots_bound_concurrency_and_preserve_exact_index(self):
        endpoint = self.endpoint(SimpleNamespace(pair=lambda *args: (None, None, Path('/unit/RGB.mkv'), Path('/unit/Alpha.mkv'), {'media': {'frame_count': 100}})))
        active = maximum = 0
        calls = []
        lock = threading.Lock()
        def decode(arguments, **kwargs):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
                calls.append(arguments)
            time.sleep(.03)
            with lock:
                active -= 1
            return SimpleNamespace(stdout=b'unit-image-contract')
        with patch('app.integrations.matte_repair.subprocess.run', side_effect=decode):
            responses = await asyncio.gather(*(endpoint('unit-task', 'unit-person', SimpleNamespace(is_disconnected=AsyncMock(return_value=False)), frame=index) for index in range(8)))
        self.assertEqual(maximum, 2)
        self.assertTrue(all(response.status_code == 200 for response in responses))
        self.assertEqual({arguments[arguments.index('-vf') + 1] for arguments in calls}, {f'select=eq(n\\,{index}),scale=720:-1' for index in range(8)})
        for arguments in calls:
            self.assertNotIn('-ss', arguments)
            self.assertEqual(arguments.count('-threads'), 2)
            self.assertTrue(all(arguments[position + 1] == '2' for position, value in enumerate(arguments) if value == '-threads'))

    async def test_frame_outside_media_is_rejected_before_decode(self):
        endpoint = self.endpoint(SimpleNamespace(pair=lambda *args: (None, None, Path('/unit/RGB.mkv'), Path('/unit/Alpha.mkv'), {'media': {'frame_count': 40}})))
        with patch('app.integrations.matte_repair.subprocess.run') as decoder:
            with self.assertRaises(HTTPException) as error:
                await endpoint('unit-task', 'unit-person', SimpleNamespace(is_disconnected=AsyncMock(return_value=False)), frame=40)
        self.assertEqual(error.exception.status_code, 422)
        decoder.assert_not_called()

    async def test_verified_cfr_pair_seeks_between_frames_and_rgba_seeks_both_inputs(self):
        media = {'frame_count': 1136, 'fps': 30}
        stream = {**media, 'codec': 'ffv1', 'video_start_time': '0.000000'}
        manifest = {'media': media, 'rgb_media': stream, 'alpha_media': stream,
                    'presentation_timestamps_verified': True}
        endpoint = self.endpoint(SimpleNamespace(pair=lambda *args: (None, None, Path('/unit/RGB.mkv'), Path('/unit/Alpha.mkv'), manifest)))
        with patch('app.integrations.matte_repair.subprocess.run', return_value=SimpleNamespace(stdout=b'png')) as decoder:
            await endpoint('unit-task', 'unit-person', SimpleNamespace(is_disconnected=AsyncMock(return_value=False)), frame=911, channel='rgba')
        arguments = decoder.call_args.args[0]
        self.assertEqual([arguments[index + 1] for index, flag in enumerate(arguments) if flag == '-ss'], ['30.350000000', '30.350000000'])
        self.assertIn('select=eq(n\\,0)', arguments[arguments.index('-filter_complex') + 1])
        for changes in ({'presentation_timestamps_verified': False},
                        {'alpha_media': {**stream, 'video_start_time': '.033'}},
                        {'alpha_media': {**stream, 'frame_count': 1135}},
                        {'rgb_media': {**stream, 'codec': 'hevc'}}):
            with self.subTest(changes=changes):
                manifest.update(changes)
                with patch('app.integrations.matte_repair.subprocess.run', return_value=SimpleNamespace(stdout=b'png')) as decoder:
                    await endpoint('unit-task', 'unit-person', SimpleNamespace(is_disconnected=AsyncMock(return_value=False)), frame=911)
                self.assertNotIn('-ss', decoder.call_args.args[0])
                self.assertIn('select=eq(n\\,911)', decoder.call_args.args[0][decoder.call_args.args[0].index('-vf') + 1])
                manifest.update(presentation_timestamps_verified=True, rgb_media=stream, alpha_media=stream)

    async def test_verified_seek_is_pixel_identical_to_exact_frame_selection_at_fractional_fps(self):
        with tempfile.TemporaryDirectory(prefix='repair-exact-seek-unit-') as directory:
            rgb, alpha = Path(directory) / 'RGB.mkv', Path(directory) / 'Alpha.mkv'
            count, width, height = 12, 16, 24
            rgb_frames = b''.join(bytes((index * 19, 255 - index * 19, index * 7)) * width * height for index in range(count))
            alpha_frames = b''.join(bytes((index * 23,)) * width * height for index in range(count))
            for rate in ('30/1', '30000/1001'):
                for path, pixel_format, frames in ((rgb, 'rgb24', rgb_frames), (alpha, 'gray', alpha_frames)):
                    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-f', 'rawvideo', '-pix_fmt', pixel_format,
                        '-s', f'{width}x{height}', '-framerate', rate, '-i', '-', '-c:v', 'ffv1', str(path)],
                        input=frames, check=True, capture_output=True, timeout=20)
                fps = 30 if rate == '30/1' else 30000 / 1001
                media = {'frame_count': count, 'fps': fps}
                stream = {**media, 'codec': 'ffv1', 'video_start_time': '0.000000'}
                manifest = {'media': media, 'rgb_media': stream, 'alpha_media': stream, 'presentation_timestamps_verified': True}
                endpoint = self.endpoint(SimpleNamespace(pair=lambda *args: (None, None, rgb, alpha, manifest)))
                for index in (0, 1, 2, 7, count - 1):
                    for channel in ('rgb', 'alpha', 'rgba'):
                        with self.subTest(rate=rate, frame=index, channel=channel):
                            fast = await endpoint('unit-task', 'unit-person', SimpleNamespace(is_disconnected=AsyncMock(return_value=False)), frame=index, channel=channel)
                            manifest['presentation_timestamps_verified'] = False
                            exact = await endpoint('unit-task', 'unit-person', SimpleNamespace(is_disconnected=AsyncMock(return_value=False)), frame=index, channel=channel)
                            manifest['presentation_timestamps_verified'] = True
                            self.assertEqual(fast.body, exact.body)
                            self.assertTrue(fast.body.startswith(b'\x89PNG'))

    async def test_rgba_preview_decodes_the_same_rgb_alpha_frame_not_opaque_black(self):
        with tempfile.TemporaryDirectory(prefix="rgba-preview-unit-") as directory:
            rgb, alpha = Path(directory) / "RGB.mkv", Path(directory) / "Alpha.mkv"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=red:s=16x24:r=1",
                "-frames:v", "1", "-c:v", "ffv1", str(rgb)], check=True, capture_output=True)
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=white:s=16x24:r=1",
                "-frames:v", "1", "-vf", "drawbox=x=0:y=0:w=8:h=24:color=black:t=fill,format=gray16le",
                "-c:v", "ffv1", str(alpha)], check=True, capture_output=True)
            endpoint = self.endpoint(SimpleNamespace(pair=lambda *args: (None, None, rgb, alpha, {'media': {'frame_count': 1}})))
            response = await endpoint('unit-task', 'unit-person', SimpleNamespace(is_disconnected=AsyncMock(return_value=False)), frame=0, channel="rgba")
            decoded = subprocess.run(["ffmpeg", "-v", "error", "-f", "image2pipe", "-i", "-", "-f", "rawvideo", "-pix_fmt", "rgba", "-"],
                input=response.body, capture_output=True, check=True).stdout
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(decoded), 720 * 1080 * 4)
            self.assertEqual(decoded[(100 * 720 + 100) * 4 + 3], 0)
            self.assertEqual(decoded[(100 * 720 + 600) * 4 + 3], 255)
            self.assertGreater(decoded[(100 * 720 + 600) * 4], 200)


if __name__ == "__main__":
    unittest.main()
