import asyncio
import hashlib
import json
import struct
import subprocess
import threading
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.domain import find, now
from app.integrations.asset_registry import file_hash
from app.integrations.matte_repair import MatteRepairService, TechnicalRepairImportRequest, create_repair_router
import test_asset_registry as registry_tests


class TechnicalRepairImportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixture = registry_tests.AssetRegistryTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.root, self.registry, self.repository = fixture.root, fixture.registry, fixture.repository
        path, manifest, _ = fixture.technical_fixture()
        self.task = fixture.task
        self.task.update(status="CREATED", collector_result={})
        self.repository.mutate(lambda state: find(state, "tasks", self.task["id"]).update(status="CREATED", collector_result={}))
        self.before = [hashlib.sha256(f"unit-alpha-{index}".encode()).hexdigest() for index in range(12)]
        self.after = list(self.before)
        self.after[5] = hashlib.sha256(b"unit-new-alpha-5").hexdigest()
        self.rgb_hashes = [hashlib.sha256(f"unit-rgb-{index}".encode()).hexdigest() for index in range(12)]
        (path.parent / "frame_hashes.json").write_text(json.dumps({"rgb": self.rgb_hashes, "alpha": self.before}))
        (path.parent / "time_mapping.json").write_text(json.dumps({"frames": 12}))
        self.manifest = manifest
        self.directory = fixture.settings.storage_root / "technical_validation" / "unit-repair"
        self.directory.mkdir()
        self.alpha = self.directory / "Alpha.v2.mkv"
        self.alpha.write_bytes(b"unit-new-alpha")
        def probe(value, **kwargs):
            if Path(value).name.startswith("Alpha"):
                return manifest["alpha_media"]
            if Path(value).suffix == ".mp4":
                return {**manifest["media"], "codec": "h264", "pix_fmt": "yuv420p"}
            return manifest["media"]
        self.probe = probe
        with patch("app.integrations.asset_registry.probe_media", side_effect=probe):
            self.parent = self.registry.register_technical_pair(self.task, path, "unit-parent-registration")
        self.repository.mutate(lambda state: find(state, "tasks", self.task["id"])["context"].update(
            technical_validation={"current_person_asset_id": self.parent["id"]}))
        parent_ref = self.parent["legacy_reference"]
        self.original = {"id": "original-technical-repair", "run_id": "original-inference-run", "task_id": manifest["task_id"],
            "execution_scope": manifest["execution_scope"], "person_asset_id": "original-unregistered-mother",
            "rgb_path": self.parent["storage_path"], "alpha_path": parent_ref["alpha_path"],
            "parent_rgb_sha256": self.parent["sha256"], "parent_alpha_sha256": parent_ref["alpha_sha256"],
            "source_width": 64, "source_height": 96, "parent_manifest": manifest, "parent_reference": parent_ref,
            "start_frame": 3, "end_frame": 9, "mode": "retain", "direction": "both",
            "keyframes": [{"frame_index": 4, "object_id": "bag_1", "coordinate_space": "source_pixels",
                           "points": [{"x": 32, "y": 48, "label": 1}], "box": [16, 24, 48, 72]}]}
        self.write("request", self.original)
        self.frame_evidence = {"interval": "half_open", "start_frame": 3, "end_frame": 9, "outside_frame_count": 6,
            "outside_alpha_hashes_unchanged": True, "rgb_reused_unchanged": True, "changed_frames": [5],
            "alpha_before": self.before, "alpha_after": self.after, "tracking_provider": "SAM2.1", "automatic_semantic_pass": False}
        self.write("frame_evidence", self.frame_evidence)
        self.write("frame_hashes", {"rgb": self.rgb_hashes, "alpha": self.after})
        previews = []
        for channel in ("black", "white", "bright", "source", "alpha_overlay"):
            preview = self.directory / f"Preview_{channel}.mp4"
            preview.write_bytes(b"unit-preview-only")
            previews.append({"background": channel, "filename": preview.name, "sha256": file_hash(preview), "full_decode_verified": True})
        repaired = {**manifest, "schema": "factory.foreground.repair.v1", "parent_rgb_path": self.parent["storage_path"],
            "alpha_sha256": file_hash(self.alpha), "parent_alpha_sha256": parent_ref["alpha_sha256"],
            "repair_id": self.original["id"], "processing_run_id": self.original["run_id"], "repair": self.frame_evidence,
            "semantic_review": "PENDING", "previews": previews}
        self.write("manifest", repaired)
        mask = self.directory / "mask.png"
        def chunk(kind, value):
            return struct.pack(">I", len(value)) + kind + value + struct.pack(">I", zlib.crc32(kind + value))
        mask.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 64, 96, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress((b"\x00" + b"\xff"*64)*96)) + chunk(b"IEND", b""))
        self.fingerprint = {"model_sha256": "1"*64, "code_commit": "2"*40, "tracking_worker_sha256": "3"*64}
        self.tracking = {"status": "READY", "provider": "SAM2.1", "task_id": self.original["task_id"],
            "source_rgb": self.parent["storage_path"], "source_sha256": self.parent["sha256"], **self.fingerprint,
            "start_frame": 3, "end_frame": 9, "direction": "both", "interval": "half_open", "width": 64, "height": 96,
            "frame_count": 6, "object_ids": ["bag_1"], "keyframes": self.original["keyframes"],
            "frames": [{"frame_index": index, "objects": {"bag_1": {"mask_path": str(mask), "sha256": file_hash(mask)}}} for index in range(3, 9)]}
        self.write("tracking_summary", self.tracking)
        ref = {**parent_ref, "alpha_path": str(self.alpha), "alpha_sha256": file_hash(self.alpha),
            "manifest_path": str(self.directory / "manifest.json"), "parent_person_asset_id": self.original["person_asset_id"],
            "repair_id": self.original["id"], "frame_hashes_path": str(self.directory / "frame_hashes.json"),
            "preview_path": str(self.directory / "Preview_black.mp4"), "preview_sha256": previews[0]["sha256"],
            "factory_fingerprint": "unit-repaired-pair"}
        self.result = {"status": "PREVIEW_READY", "asset": {"path": self.parent["storage_path"], "sha256": self.parent["sha256"], "legacy_reference": ref},
            "preview_path": ref["preview_path"], "preview_sha256": ref["preview_sha256"], "frame_evidence_path": str(self.directory / "frame_evidence.json"),
            "changed_frame_count": 1, "tracking_summary_path": str(self.directory / "tracking_summary.json"),
            "tracking_summary_sha256": file_hash(self.directory / "tracking_summary.json")}
        self.write("result", self.result)
        production = SimpleNamespace(repository=self.repository, registry=self.registry, settings=fixture.settings,
            start_lock=asyncio.Lock(), event=AsyncMock())
        self.service = MatteRepairService(production)
        self.request = TechnicalRepairImportRequest(person_asset_id=self.parent["id"], directory=str(self.directory), idempotency_key="import-unit-01")

    def write(self, name, value):
        (self.directory / f"{name}.json").write_text(json.dumps(value))

    def decoded(self, path, pixel_format, cancel):
        if Path(path) == self.alpha:
            return self.after
        if Path(path).name == "Alpha.mkv":
            return self.before
        return self.rgb_hashes

    def proof_patches(self):
        return [patch("app.integrations.asset_registry.probe_media", side_effect=self.probe),
                patch("app.integrations.matte_repair.probe_media", side_effect=self.probe),
                patch.object(self.service, "tracking_fingerprint", return_value=self.fingerprint),
                patch.object(self.service, "decoded_hashes", side_effect=self.decoded)]

    async def prepared(self):
        return await self.service.prepare_import(self.task["id"], self.request)

    async def test_reservation_is_independent_idempotent_and_normalizes_original_annotations(self):
        before = self.repository.state()
        first, created = await self.prepared()
        second, again = await self.prepared()
        self.assertTrue(created)
        self.assertFalse(again)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["keyframes"][0]["points"][0]["x"], .5)
        self.assertEqual(first["keyframes"][0]["box"], [.25, .25, .75, .75])
        self.assertEqual(first["origin_keyframes"], self.original["keyframes"])
        self.assertEqual(first["origin_run_id"], "original-inference-run")
        run = find(self.repository.state(), "runs", first["run_id"])
        self.assertEqual(run["provider"], "FactoryTechnicalRepairRegistration")
        self.assertIsNone(run.get("pid"))
        self.assertEqual(self.repository.state()["assets"], before["assets"])
        self.assertEqual(find(self.repository.state(), "tasks", self.task["id"])["status"], "CREATED")
        self.assertFalse(self.service.jobs)

    async def test_same_key_changed_artifact_conflicts_without_overwriting_history(self):
        await self.prepared()
        self.write("result", {**self.result, "altered": True})
        with self.assertRaises(HTTPException) as error:
            await self.prepared()
        self.assertEqual(error.exception.detail, "IDEMPOTENCY_CONFLICT")
        self.assertEqual(len([row for row in self.repository.state()["runs"] if row["task_id"] == self.task["id"]]), 1)

    async def test_incomplete_result_busy_task_and_stale_parent_are_not_reserved(self):
        original = (self.directory / "result.json").read_bytes()
        self.write("result", {"status": "FAILED"})
        with self.assertRaises(HTTPException):
            await self.prepared()
        (self.directory / "result.json").write_bytes(original)
        self.repository.mutate(lambda state: state["runs"].append({"id": "active-unit-run", "task_id": self.task["id"], "stage": "person", "provider": "unit",
            "status": "running", "progress": 0, "logs": [], "created_at": now()}))
        with self.assertRaises(HTTPException) as error:
            await self.prepared()
        self.assertEqual(error.exception.detail, "TASK_BUSY")
        self.repository.mutate(lambda state: find(state, "runs", "active-unit-run").update(status="success"))
        self.repository.mutate(lambda state: find(state, "tasks", self.task["id"])["context"]["technical_validation"].update(current_person_asset_id="different-version"))
        with self.assertRaises(HTTPException) as error:
            await self.prepared()
        self.assertEqual(error.exception.detail, "PARENT_VERSION_CHANGED")

    async def test_verified_import_apply_and_undo_preserve_original_media_and_business_state(self):
        originals = {path: path.read_bytes() for path in self.directory.iterdir() if path.is_file()}
        record, _ = await self.prepared()
        asset_count = len(self.repository.state()["assets"])
        patches = self.proof_patches()
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.service.cancels[record["id"]] = threading.Event()
        await self.service.run_import(self.task["id"], record["id"])
        actual = self.service.records(self.task["id"])[0]
        self.assertEqual(actual["status"], "PREVIEW_READY")
        self.assertFalse(actual["evidence"]["automatic_semantic_pass"])
        self.assertEqual(actual["evidence"]["original_execution"]["run_id"], "original-inference-run")
        self.assertEqual(len(self.repository.state()["assets"]), asset_count)
        applied = await self.service.apply(self.task["id"], record["id"])
        repaired = find(self.repository.state(), "assets", applied["asset_id"])
        self.assertEqual(repaired["storage_path"], self.parent["storage_path"])
        self.assertEqual(repaired["legacy_reference"]["execution_scope"], "TECHNICAL_VALIDATION")
        self.assertEqual(repaired["legacy_reference"]["origin_repair_run_id"], "original-inference-run")
        await self.service.undo(self.task["id"], record["id"])
        task = find(self.repository.state(), "tasks", self.task["id"])
        self.assertEqual(task["status"], "CREATED")
        self.assertEqual(task["collector_result"], {})
        self.assertEqual(task["context"]["technical_validation"]["current_person_asset_id"], self.parent["id"])
        for path, data in originals.items():
            self.assertEqual(path.read_bytes(), data)

    async def test_changed_tracking_mask_or_decoded_alpha_does_not_create_asset(self):
        record, _ = await self.prepared()
        patches = self.proof_patches()
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        mask = self.directory / "mask.png"
        original = mask.read_bytes()
        mask.write_bytes(original + b"changed")
        with self.assertRaisesRegex(ValueError, "TRACKING_MASK_CHANGED"):
            self.service.verify_import(self.task["id"], record, threading.Event())
        mask.write_bytes(original)
        self.after[0] = "f"*64
        with self.assertRaisesRegex(ValueError, "READBACK_MISMATCH"):
            self.service.verify_import(self.task["id"], record, threading.Event())
        self.assertEqual(len([asset for asset in self.repository.state()["assets"] if asset["task_id"] == self.task["id"]]), 1)

    async def test_model_or_annotation_change_is_rejected_and_failed_registration_is_retained(self):
        record, _ = await self.prepared()
        patches = self.proof_patches()
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.fingerprint["model_sha256"] = "4"*64
        self.service.cancels[record["id"]] = threading.Event()
        await self.service.run_import(self.task["id"], record["id"])
        self.assertEqual(self.service.records(self.task["id"])[0]["status"], "FAILED")
        run = find(self.repository.state(), "runs", record["run_id"])
        self.assertEqual(run["error_code"], "TECHNICAL_TRACKING_IDENTITY_MISMATCH")
        self.assertIsNone(run.get("pid"))
        self.assertEqual(find(self.repository.state(), "tasks", self.task["id"])["status"], "CREATED")
        self.assertTrue((self.directory / "result.json").exists())

    async def test_missing_original_tracking_task_uses_bound_request_without_rewriting_evidence(self):
        self.tracking["task_id"] = None
        self.write("tracking_summary", self.tracking)
        self.result["tracking_summary_sha256"] = file_hash(self.directory / "tracking_summary.json")
        self.write("result", self.result)
        original = {path: path.read_bytes() for path in self.directory.iterdir() if path.is_file()}
        record, _ = await self.prepared()
        patches = self.proof_patches()
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.service.cancels[record["id"]] = threading.Event()
        await self.service.run_import(self.task["id"], record["id"])
        actual = self.service.records(self.task["id"])[0]
        self.assertEqual(actual["status"], "PREVIEW_READY")
        self.assertEqual(actual["provenance"]["task_binding_source"], "origin_request")
        self.assertIsNone(actual["provenance"]["tracking_task_id"])
        self.assertEqual(actual["provenance"]["origin_request_task_id"], self.original["task_id"])
        self.assertEqual(actual["provenance"]["origin_request_sha256"], file_hash(self.directory / "request.json"))
        self.assertEqual(actual["provenance"]["tracking_summary_sha256"], self.result["tracking_summary_sha256"])
        self.assertEqual(actual["evidence"]["asset"]["legacy_reference"]["origin_task_id"], self.original["task_id"])
        for path, data in original.items():
            self.assertEqual(path.read_bytes(), data)

    async def test_nonempty_wrong_tracking_task_still_fails_and_production_never_uses_missing_id_fallback(self):
        self.tracking["task_id"] = "other-task"
        self.write("tracking_summary", self.tracking)
        self.result["tracking_summary_sha256"] = file_hash(self.directory / "tracking_summary.json")
        self.write("result", self.result)
        record, _ = await self.prepared()
        patches = self.proof_patches()
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        with self.assertRaisesRegex(ValueError, "TECHNICAL_TRACKING_IDENTITY_MISMATCH"):
            self.service.verify_import(self.task["id"], record, threading.Event())
        self.tracking["task_id"] = None
        self.write("tracking_summary", self.tracking)
        self.result["tracking_summary_sha256"] = file_hash(self.directory / "tracking_summary.json")
        self.write("result", self.result)
        record = {**record, "origin_result_sha256": file_hash(self.directory / "result.json")}
        original_pair = self.service.pair(self.task["id"], self.parent["id"])
        production_parent = json.loads(json.dumps(original_pair[1]))
        production_parent["legacy_reference"]["execution_scope"] = "PRODUCTION"
        with patch.object(self.service, "pair", return_value=(original_pair[0], production_parent, *original_pair[2:])):
            with self.assertRaisesRegex(ValueError, "TECHNICAL_TRACKING_IDENTITY_MISMATCH"):
                self.service.verify_import(self.task["id"], record, threading.Event())

    async def test_restart_keeps_original_evidence_and_releases_verification_reservation(self):
        record, _ = await self.prepared()
        self.service.update(self.task["id"], record["id"], status="VERIFYING")
        await self.service.recover()
        self.assertEqual(self.service.records(self.task["id"])[0]["status"], "INTERRUPTED")
        self.assertEqual(find(self.repository.state(), "runs", record["run_id"])["error_code"], "LOCAL_EXECUTION_INTERRUPTED")
        self.assertEqual(self.service.records(self.task["id"])[0]["origin_request_sha256"], file_hash(self.directory / "request.json"))

    async def test_artifact_tamper_after_verified_preview_blocks_apply(self):
        record, _ = await self.prepared()
        patches = self.proof_patches()
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.service.cancels[record["id"]] = threading.Event()
        await self.service.run_import(self.task["id"], record["id"])
        self.write("manifest", {"tampered": True})
        with self.assertRaises(HTTPException) as error:
            await self.service.apply(self.task["id"], record["id"])
        self.assertEqual(error.exception.detail, "TECHNICAL_REPAIR_ARTIFACT_CHANGED")

    async def test_readback_uses_actual_lossless_ffmpeg_frames_without_model_execution(self):
        path = self.directory / "decode-unit.mkv"
        frames = [bytes([1, 0])*64*96, bytes([2, 0])*64*96]
        subprocess.run(["ffmpeg", "-n", "-v", "error", "-f", "rawvideo", "-pix_fmt", "gray16le", "-s", "64x96", "-framerate", "6",
                        "-i", "-", "-an", "-c:v", "ffv1", "-level", "3", "-pix_fmt", "gray16le", str(path)],
                       input=b"".join(frames), capture_output=True, check=True, timeout=20)
        self.assertEqual(self.service.decoded_hashes(path, "gray16le", threading.Event()), [hashlib.sha256(frame).hexdigest() for frame in frames])

    async def test_http_import_returns_202_and_preserves_queued_registration_without_inference(self):
        self.service.launch_import = AsyncMock()
        app = FastAPI()
        app.include_router(create_repair_router(self.service))
        with TestClient(app) as client:
            route = f"/api/tasks/{self.task['id']}/technical-validation/repairs/import"
            first = client.post(route, json=self.request.model_dump())
            self.assertEqual(first.status_code, 202)
            self.assertEqual(first.json()["status"], "QUEUED")
            second = client.post(route, json=self.request.model_dump())
            self.assertEqual(second.status_code, 202)
            self.assertEqual(first.json()["run_id"], second.json()["run_id"])
        self.service.launch_import.assert_awaited_once()
        run = find(self.repository.state(), "runs", first.json()["run_id"])
        self.assertEqual(run["status"], "queued")
        self.assertIsNone(run["pid"])


if __name__ == "__main__":
    unittest.main()
