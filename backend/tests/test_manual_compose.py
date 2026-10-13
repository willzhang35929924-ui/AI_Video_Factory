"""Small temporary media exercise the local implementation, not production acceptance."""
import asyncio
import copy
import io
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.config import Settings
from app.domain import now
from app.integrations.asset_registry import AssetRegistry, file_hash, probe_media
from app.integrations.manual_compose import ComposeRequest, ComposeSelectionRequest, ManualComposeService, TransformRequest
from app.integrations.manual_compose_routes import create_manual_compose_router
from app.integrations.production_service import PRODUCTION_STAGES, ProductionService
from app.repository import StateRepository


class ManualComposeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="manual-compose-isolated-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.settings = Settings(storage_root=self.root / "storage", database_url="sqlite:///" + str(self.root / "unit.db"))
        self.settings.storage_root.mkdir()
        self.repository = StateRepository(self.settings.database_url, Path(__file__).parents[1] / "app/seed.json")
        self.addCleanup(self.repository.engine.dispose)
        task = copy.deepcopy(self.repository.seed()["tasks"][0])
        task.update(id="manual-unit", source_video_id=None, execution_mode="integration", status="CREATED", current_stage="person", progress=0,
            context={"task_context": {"context_id": "manual-unit-context"}, "production": {}}, collector_result={}, created_at=now(), updated_at=now())
        def seed(state):
            state["tasks"].append(task)
            state["stages"].extend({"id": task["id"] + "-" + stage, "task_id": task["id"], "name": stage, "status": "pending",
                "start_time": None, "end_time": None, "duration": 0, "retry_count": 0, "error": None} for stage in PRODUCTION_STAGES)
        self.repository.mutate(seed)
        production = ProductionService.__new__(ProductionService)
        production.repository, production.settings = self.repository, self.settings
        production.registry = AssetRegistry(self.repository, self.settings)
        production.start_lock = asyncio.Lock()
        production.event = AsyncMock()
        self.service = ManualComposeService(production)
        self.task_id = task["id"]
        self.rgb, self.alpha, self.image = self.root / "original.mkv", self.root / "mask.mkv", self.root / "blue.png"
        self.ffmpeg("-f", "lavfi", "-i", "color=red:s=32x48:r=6", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
            "-frames:v", "6", "-t", "1", "-vf", "format=bgr0", "-c:v", "ffv1", "-c:a", "pcm_s16le", str(self.rgb))
        self.ffmpeg("-f", "lavfi", "-i", "color=white:s=32x48:r=6", "-frames:v", "6", "-vf",
            "drawbox=x=0:y=0:w=16:h=48:color=black:t=fill,format=gray16le", "-c:v", "ffv1", "-pix_fmt", "gray16le", str(self.alpha))
        self.ffmpeg("-f", "lavfi", "-i", "color=blue:s=64x96", "-frames:v", "1", str(self.image))

    async def asyncTearDown(self):
        await self.service.shutdown()

    def ffmpeg(self, *arguments):
        return subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-filter_threads", "1", "-threads", "1", *arguments],
            check=True, capture_output=True, timeout=30)

    def upload(self, path, name=None):
        return UploadFile(filename=name or path.name, file=io.BytesIO(path.read_bytes()))

    async def pair(self):
        return (await self.service.upload_person(self.task_id, self.upload(self.rgb), self.upload(self.alpha)))["asset"]

    async def background(self):
        return (await self.service.upload_background(self.task_id, self.upload(self.image)))["asset"]

    async def test_duplicate_background_upload_removes_only_new_unregistered_copy_and_retains_runs(self):
        original = await self.background()
        original_path = Path(original["storage_path"])
        before = self.repository.state()
        original_run = next(row for row in before["runs"] if row["id"] == original["legacy_reference"]["processing_run_id"])
        response = await self.service.upload_background(self.task_id, self.upload(self.image))
        self.assertEqual(response["asset"], next(row for row in before["assets"] if row["id"] == original["id"]))
        self.assertEqual(file_hash(original_path), original["sha256"])
        cleanup = response["cleanup"]
        self.assertEqual(cleanup["status"], "DELETED")
        self.assertEqual(cleanup["retained_path"], str(original_path))
        self.assertEqual(cleanup["deleted_bytes"], self.image.stat().st_size)
        self.assertFalse(Path(cleanup["uploaded_path"]).exists())
        after = self.repository.state()
        self.assertEqual(after["assets"], before["assets"])
        self.assertEqual(next(row for row in after["runs"] if row["id"] == original_run["id"]), original_run)
        new_run = after["runs"][-1]
        self.assertNotEqual(new_run["id"], original_run["id"])
        self.assertEqual(new_run["status"], "success")
        self.assertEqual(new_run["resource_info"]["background_upload"], cleanup)
        self.assertTrue(Path(cleanup["uploaded_path"]).parent.is_dir())
        self.assertEqual(json.loads(after["audits"][-1]["message"]), cleanup)

    async def test_duplicate_background_cleanup_failure_preserves_original_and_reports_new_copy(self):
        original = await self.background()
        with patch.object(Path, "unlink", side_effect=PermissionError("UNIT_CLEANUP_DENIED")), self.assertRaises(HTTPException) as failure:
            await self.service.upload_background(self.task_id, self.upload(self.image))
        self.assertEqual(failure.exception.status_code, 409)
        self.assertEqual(failure.exception.detail["code"], "BACKGROUND_DUPLICATE_CLEANUP_FAILED")
        state = self.repository.state()
        run = state["runs"][-1]
        cleanup = run["resource_info"]["background_upload"]
        self.assertEqual(run["status"], "failed")
        self.assertEqual(cleanup["status"], "FAILED")
        self.assertIn("UNIT_CLEANUP_DENIED", cleanup["error"])
        self.assertTrue(cleanup["uploaded_file_exists"])
        self.assertTrue(Path(cleanup["uploaded_path"]).is_file())
        self.assertEqual(file_hash(original["storage_path"]), original["sha256"])
        self.assertEqual(len([row for row in state["assets"] if row["task_id"] == self.task_id]), 1)
        self.assertEqual(json.loads(state["audits"][-1]["message"])["status"], "FAILED")

    async def test_duplicate_background_cleanup_never_removes_a_new_file_registered_by_another_asset(self):
        original = await self.background()
        register = self.service.registry.register

        def register_shared(task, evidence, kind, run_id):
            result = register(task, evidence, kind, run_id)
            shared = copy.deepcopy(result)
            shared.update(id="unit-shared-new-background", storage_path=evidence["path"], external_path=evidence["path"])
            self.repository.mutate(lambda state: state["assets"].append(shared))
            return result

        with patch.object(self.service.registry, "register", side_effect=register_shared), self.assertRaises(HTTPException) as failure:
            await self.service.upload_background(self.task_id, self.upload(self.image))
        self.assertEqual(failure.exception.detail["code"], "BACKGROUND_DUPLICATE_CLEANUP_FAILED")
        run = self.repository.state()["runs"][-1]
        cleanup = run["resource_info"]["background_upload"]
        self.assertEqual(cleanup["error"], "BACKGROUND_DUPLICATE_CLEANUP_NOT_EXCLUSIVE")
        self.assertTrue(Path(cleanup["uploaded_path"]).is_file())
        self.assertEqual(file_hash(original["storage_path"]), original["sha256"])

    async def test_background_upload_route_returns_registered_identity_and_safe_duplicate_cleanup(self):
        app = FastAPI()
        app.include_router(create_manual_compose_router(self.service))
        url = f"/api/manual-compose/tasks/{self.task_id}/upload-background"
        with TestClient(app) as client:
            first = client.post(url, files={"image": ("first.png", self.image.read_bytes(), "image/png")})
            second = client.post(url, files={"image": ("second.png", self.image.read_bytes(), "image/png")})
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(second.status_code, 201, second.text)
        self.assertEqual(first.json()["asset"]["id"], second.json()["asset"]["id"])
        self.assertEqual(second.json()["cleanup"]["status"], "DELETED")
        self.assertTrue(Path(first.json()["asset"]["storage_path"]).is_file())
        self.assertFalse(Path(second.json()["cleanup"]["uploaded_path"]).exists())

    async def test_plain_rgb_cannot_be_registered_as_person(self):
        before = len(self.repository.state()["assets"])
        with self.assertRaises(HTTPException) as caught:
            await self.service.upload_person(self.task_id, self.upload(self.rgb))
        self.assertEqual(caught.exception.status_code, 422)
        self.assertIn("PERSON_ALPHA_REQUIRED", caught.exception.detail)
        self.assertEqual(len(self.repository.state()["assets"]), before)
        self.assertEqual(self.repository.state()["runs"][-1]["status"], "failed")

    async def test_selection_persists_without_runs_transform_or_review_approval(self):
        person, first = await self.pair(), await self.background()
        another = self.root / "green.png"
        self.ffmpeg("-f", "lavfi", "-i", "color=green:s=64x96", "-frames:v", "1", str(another))
        second = (await self.service.upload_background(self.task_id, self.upload(another)))["asset"]
        before = self.repository.state()
        before_task = next(row for row in before["tasks"] if row["id"] == self.task_id)
        await self.service.select_asset(self.task_id, ComposeSelectionRequest(kind="person", asset_id=person["id"]))
        result = await self.service.select_asset(self.task_id, ComposeSelectionRequest(kind="background", asset_id=first["id"]))
        self.assertEqual(result["selection"], {"person_asset_id": person["id"], "background_asset_id": first["id"],
            "person_task_id": self.task_id, "background_task_id": self.task_id})
        reopened = StateRepository(self.settings.database_url, Path(__file__).parents[1] / "app/seed.json")
        self.addCleanup(reopened.engine.dispose)
        saved = next(row for row in reopened.state()["tasks"] if row["id"] == self.task_id)
        self.assertEqual(saved["context"]["manual_compose"]["selection"], result["selection"])
        self.assertEqual(saved["status"], before_task["status"])
        self.assertEqual(saved["context"]["production"], before_task["context"]["production"])
        self.assertEqual(self.repository.state()["runs"], before["runs"])
        self.assertEqual(self.repository.state()["reviews"], before["reviews"])
        self.assertNotEqual(first["id"], second["id"])

    async def test_selection_rejects_historical_person_and_wrong_asset_kind(self):
        person, background = await self.pair(), await self.background()
        child = (await self.service.apply_transform(self.task_id, TransformRequest(person_asset_id=person["id"], x=8)))["person"]
        for request in (ComposeSelectionRequest(kind="person", asset_id=person["id"]),
                ComposeSelectionRequest(kind="person", asset_id=background["id"]),
                ComposeSelectionRequest(kind="background", asset_id=child["id"])):
            with self.assertRaises(HTTPException):
                await self.service.select_asset(self.task_id, request)
        self.assertEqual(self.service.versions(self.task_id)["selection"], {})
        self.assertEqual(self.service.versions(self.task_id)["current_person_asset_id"], child["id"])

    async def test_selection_route_retains_old_transforms_contract_and_checks_media(self):
        person, background = await self.pair(), await self.background()
        app = FastAPI()
        app.include_router(create_manual_compose_router(self.service))
        with TestClient(app) as client:
            response = client.put(f"/api/manual-compose/tasks/{self.task_id}/selection", json={"kind": "background", "asset_id": background["id"]})
            self.assertEqual(response.status_code, 200)
            versions = client.get(f"/api/manual-compose/tasks/{self.task_id}/transforms").json()
            self.assertEqual(versions["current_person_asset_id"], person["id"])
            self.assertEqual(versions["selection"]["background_asset_id"], background["id"])
            self.assertEqual(client.put(f"/api/manual-compose/tasks/{self.task_id}/selection", json={"kind": "final", "asset_id": person["id"]}).status_code, 422)
            Path(background["storage_path"]).write_bytes(b"changed")
            invalid = client.put(f"/api/manual-compose/tasks/{self.task_id}/selection", json={"kind": "background", "asset_id": background["id"]})
            self.assertIn(invalid.status_code, (404, 409))

    def library_task(self):
        task = copy.deepcopy(self.repository.read_task(self.task_id))
        task.update(id="manual-donor", status="CREATED", context={"task_context": {"context_id": "manual-donor-context"}, "production": {}})
        def add(state):
            state["tasks"].append(task)
            state["stages"].extend({"id": task["id"] + "-" + stage, "task_id": task["id"], "name": stage, "status": "pending",
                "start_time": None, "end_time": None, "duration": 0, "retry_count": 0, "error": None} for stage in PRODUCTION_STAGES)
        self.repository.mutate(add)
        return task["id"]

    async def test_library_selection_reuses_owner_evidence_without_changing_person_versions(self):
        current = await self.pair()
        donor_id = self.library_task()
        person = (await self.service.upload_person(donor_id, self.upload(self.rgb), self.upload(self.alpha)))["asset"]
        background = (await self.service.upload_background(donor_id, self.upload(self.image)))["asset"]
        donor_before = self.repository.read_task(donor_id)
        await self.service.select_asset(self.task_id, ComposeSelectionRequest(kind="person", asset_id=person["id"]))
        await self.service.select_asset(self.task_id, ComposeSelectionRequest(kind="background", asset_id=background["id"]))
        target = self.repository.read_task(self.task_id)
        self.assertEqual(target["context"]["production"]["current_person_asset_id"], current["id"])
        self.assertEqual(target["context"]["manual_compose"]["selection"], {"person_asset_id": person["id"], "person_task_id": donor_id,
            "background_asset_id": background["id"], "background_task_id": donor_id})
        self.assertEqual(self.repository.read_task(donor_id), donor_before)
        result = await self.service.render(ComposeRequest(task_id=self.task_id, person_asset_id=person["id"], background_asset_id=background["id"], width=64, height=96))
        self.assertEqual(result["manifest"]["person_asset_id"], person["id"])
        self.assertEqual(result["manifest"]["background_asset_id"], background["id"])
        self.assertEqual(result["manifest"]["person_task_id"], donor_id)
        self.assertEqual(result["manifest"]["background_task_id"], donor_id)
        self.assertEqual(result["manifest"]["cutout_inference_executions"], 0)
        self.assertEqual(result["manifest"]["output_media"]["frame_count"], 6)
        self.assertIsNone(self.service.versions(self.task_id)["saved_backgrounds"])
        self.assertEqual(self.repository.read_task(donor_id), donor_before)
        final = (await self.service.approve(result["review"]["id"]))["final"]
        self.assertEqual(final["legacy_reference"]["person_asset_id"], person["id"])
        self.assertEqual(self.repository.read_task(donor_id), donor_before)

    async def test_library_person_current_rejection_and_manifest_owner_are_checked(self):
        await self.pair()
        donor_id = self.library_task()
        person = (await self.service.upload_person(donor_id, self.upload(self.rgb), self.upload(self.alpha)))["asset"]
        child = (await self.service.apply_transform(donor_id, TransformRequest(person_asset_id=person["id"], x=8)))["person"]
        with self.assertRaises(HTTPException) as historical:
            await self.service.select_asset(self.task_id, ComposeSelectionRequest(kind="person", asset_id=person["id"]))
        self.assertIn("CURRENT_PERSON_CHANGED", historical.exception.detail)
        await self.service.select_asset(self.task_id, ComposeSelectionRequest(kind="person", asset_id=child["id"]))
        self.repository.mutate(lambda state: next(row for row in state["assets"] if row["id"] == child["id"]).update(status="REJECTED"))
        with self.assertRaises(HTTPException):
            await self.service.select_asset(self.task_id, ComposeSelectionRequest(kind="person", asset_id=child["id"]))
        self.assertIn(donor_id, self.service.library()["blocked_task_ids"])
        app = FastAPI()
        app.include_router(create_manual_compose_router(self.service))
        with TestClient(app) as client:
            library = client.get("/api/manual-compose/library")
            self.assertEqual(library.status_code, 200)
            self.assertEqual(library.json()["current_person_asset_ids"][donor_id], child["id"])

    async def test_library_background_type_is_verified_from_media_not_just_asset_label(self):
        person = await self.pair()
        task = self.repository.read_task(self.task_id)
        video = self.service.registry.register(task, {"path": person["storage_path"], "sha256": person["sha256"]}, "background", "unit-background-label")
        with self.assertRaises(HTTPException) as caught:
            await self.service.select_asset(self.task_id, ComposeSelectionRequest(kind="background", asset_id=video["id"]))
        self.assertEqual(caught.exception.status_code, 422)
        self.assertIn("BACKGROUND_IMAGE_REQUIRED", caught.exception.detail)
        self.assertEqual(self.service.versions(self.task_id)["selection"], {})

    async def test_supplied_pair_and_embedded_alpha_are_verified(self):
        person = await self.pair()
        self.assertEqual(person["probe_metadata"]["frame_count"], 6)
        self.assertTrue(person["legacy_reference"]["full_decode_verified"])
        self.assertEqual(probe_media(person["legacy_reference"]["alpha_path"])["pix_fmt"], "gray16le")
        self.assertTrue(person["probe_metadata"]["audio"])
        embedded = self.root / "transparent.mov"
        self.ffmpeg("-f", "lavfi", "-i", "color=red:s=32x48:r=6,format=rgba,colorchannelmixer=aa=0.5",
            "-frames:v", "6", "-c:v", "qtrle", str(embedded))
        uploaded = await self.service.upload_person(self.task_id, self.upload(embedded))
        self.assertNotEqual(uploaded["asset"]["id"], person["id"])

    async def test_import_frame_hashes_and_timeline_are_decoded_evidence_for_repair(self):
        person = await self.pair()
        reference = person["legacy_reference"]
        manifest = json.loads(Path(reference["manifest_path"]).read_text())
        hashes = json.loads(Path(reference["frame_hashes_path"]).read_text())
        mapping = json.loads(Path(reference["time_mapping_path"]).read_text())
        self.assertEqual(manifest["media"]["fps_rational"], "6/1")
        self.assertTrue(manifest["presentation_timestamps_verified"])
        for kind, path, pixel_format in (("rgb", person["storage_path"], "rgb48le"),
                ("alpha", reference["alpha_path"], "gray16le")):
            decoded = self.ffmpeg("-xerror", "-i", path, "-map", "0:v:0", "-an", "-pix_fmt", pixel_format,
                "-fps_mode", "passthrough", "-f", "framehash", "-hash", "sha256", "-").stdout.decode()
            actual = [line.split(",")[-1].strip() for line in decoded.splitlines() if line and not line.startswith("#")]
            self.assertEqual(hashes[kind], actual)
            self.assertEqual(len(actual), 6)
        self.assertEqual(mapping["normalized_to_source_frame"], list(range(6)))
        self.assertEqual(mapping["normalized_frame_timestamps_s"], mapping["alpha_frame_timestamps_s"])
        self.assertEqual(mapping["original_frame_timestamps_s"], mapping["normalized_frame_timestamps_s"])
        self.assertEqual(mapping["source_sha256"], file_hash(self.rgb))
        for field in ("frame_hashes", "time_mapping"):
            self.assertEqual(reference[field + "_sha256"], file_hash(reference[field + "_path"]))
            self.assertEqual(manifest[field + "_sha256"], reference[field + "_sha256"])

    async def test_transform_undo_restore_references_original_pair_without_recutout(self):
        person = await self.pair()
        before = copy.deepcopy(next(row for row in self.repository.state()["assets"] if row["id"] == person["id"]))
        original_files = set(self.settings.storage_root.rglob("*"))
        result = await self.service.apply_transform(self.task_id, TransformRequest(person_asset_id=person["id"], x=8, y=-4, scale=.75))
        child = result["person"]
        self.assertEqual(child["storage_path"], person["storage_path"])
        self.assertEqual(child["legacy_reference"]["alpha_path"], person["legacy_reference"]["alpha_path"])
        self.assertEqual(set(self.settings.storage_root.rglob("*")), original_files)
        self.assertEqual(next(row for row in self.repository.state()["assets"] if row["id"] == person["id"]), before)
        await self.service.undo(self.task_id)
        self.assertEqual(self.service.versions(self.task_id)["current_person_asset_id"], person["id"])
        await self.service.restore(self.task_id, child["id"])
        self.assertEqual(self.service.versions(self.task_id)["current_person_asset_id"], child["id"])
        self.assertEqual(file_hash(child["storage_path"]), before["sha256"])
        self.assertEqual(len(self.service.versions(self.task_id)["versions"]), 1)

    async def test_real_ffmpeg_render_registers_composition_review_and_retains_audio(self):
        person, background = await self.pair(), await self.background()
        outcome = await self.service.render(ComposeRequest(task_id=self.task_id, person_asset_id=person["id"], background_asset_id=background["id"], width=64, height=96))
        output = self.service.preview(outcome["run_id"])
        media = probe_media(output, count_frames=True)
        self.assertEqual(media["frame_count"], 6)
        self.assertEqual((media["width"], media["height"]), (64, 96))
        self.assertTrue(media["audio"])
        self.assertEqual(outcome["manifest"]["cutout_inference_executions"], 0)
        self.assertEqual(outcome["asset"]["sha256"], file_hash(output))
        run = next(row for row in self.repository.state()["runs"] if row["id"] == outcome["run_id"])
        self.assertEqual(run["exit_code"], 0)
        self.assertGreater(run["pid"], 0)
        self.assertTrue(Path(run["stdout_log_path"]).is_file())
        with Session(self.repository.engine) as session:
            composition = session.scalar(select(models.CompositionAsset).where(models.CompositionAsset.asset_id == outcome["asset"]["id"]))
            self.assertEqual(composition.person_asset_id, person["id"])
        pixels = self.ffmpeg("-i", str(output), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1").stdout
        left, right = pixels[(40 * 64 + 8) * 3:(40 * 64 + 8) * 3 + 3], pixels[(40 * 64 + 48) * 3:(40 * 64 + 48) * 3 + 3]
        self.assertGreater(left[2], left[0] + 150)
        self.assertGreater(right[0], right[2] + 150)
        approved = await self.service.approve(outcome["review"]["id"])
        self.assertEqual(approved["final"]["type"], "final")
        human_run = next(row for row in self.repository.state()["runs"] if row["id"] == approved["final"]["legacy_reference"]["processing_run_id"])
        self.assertEqual((human_run["provider"], human_run["stage"], human_run["status"]), ("HumanReview", "final", "success"))
        self.assertEqual(self.repository.state()["tasks"][-1]["context"]["production_strategy"], "manual_compose")

    async def test_applied_xy_scale_changes_real_pixels_without_changing_mother(self):
        person, background = await self.pair(), await self.background()
        mother_hash = file_hash(person["storage_path"])
        applied = await self.service.apply_transform(self.task_id, TransformRequest(person_asset_id=person["id"], x=8, y=8, scale=.5))
        child = applied["person"]
        outcome = await self.service.render(ComposeRequest(task_id=self.task_id, person_asset_id=child["id"], background_asset_id=background["id"], width=64, height=96))
        pixels = self.ffmpeg("-i", str(self.service.preview(outcome["run_id"])), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1").stdout
        def pixel(x, y):
            return pixels[(y * 64 + x) * 3:(y * 64 + x) * 3 + 3]
        self.assertGreater(pixel(48, 50)[0], pixel(48, 50)[2] + 150)
        for point in ((48, 10), (25, 50), (60, 50)):
            color = pixel(*point)
            self.assertGreater(color[2], color[0] + 150)
        self.assertEqual(file_hash(person["storage_path"]), mother_hash)
        self.assertEqual(outcome["manifest"]["transform"], {"x": 8., "y": 8., "scale": .5})

    async def test_changed_composition_manifest_cannot_be_approved(self):
        person, background = await self.pair(), await self.background()
        result = await self.service.render(ComposeRequest(task_id=self.task_id, person_asset_id=person["id"], background_asset_id=background["id"], width=64, height=96))
        manifest = Path(result["asset"]["legacy_reference"]["manifest_path"])
        manifest.write_bytes(manifest.read_bytes() + b" ")
        with self.assertRaises(HTTPException) as caught:
            await self.service.approve(result["review"]["id"])
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(next(row for row in self.repository.state()["reviews"] if row["id"] == result["review"]["id"])["status"], "pending")
        self.assertFalse(any(row["task_id"] == self.task_id and row["type"] == "final" for row in self.repository.state()["assets"]))

    async def test_frame_mismatch_pause_and_extreme_scale_cannot_advance(self):
        shorter = self.root / "short-mask.mkv"
        self.ffmpeg("-i", str(self.alpha), "-frames:v", "5", "-c:v", "ffv1", "-pix_fmt", "gray16le", str(shorter))
        with self.assertRaises(HTTPException) as mismatch:
            await self.service.upload_person(self.task_id, self.upload(self.rgb), self.upload(shorter))
        self.assertIn("ALIGNMENT_MISMATCH", mismatch.exception.detail)
        person, background = await self.pair(), await self.background()
        child = (await self.service.apply_transform(self.task_id, TransformRequest(person_asset_id=person["id"], scale=1e308)))["person"]
        with self.assertRaises(HTTPException) as extreme:
            await self.service.render(ComposeRequest(task_id=self.task_id, person_asset_id=child["id"], background_asset_id=background["id"]))
        self.assertEqual(extreme.exception.status_code, 422)
        task = next(row for row in self.repository.state()["tasks"] if row["id"] == self.task_id)
        self.assertIsNone(task["context"]["production"]["active_run_id"])
        self.assertEqual(self.repository.state()["runs"][-1]["status"], "failed")
        before = len(self.repository.state()["runs"])
        self.repository.mutate(lambda state: next(row for row in state["tasks"] if row["id"] == self.task_id).update(status="PAUSED"))
        with self.assertRaises(HTTPException) as paused:
            await self.service.render(ComposeRequest(task_id=self.task_id, person_asset_id=person["id"], background_asset_id=background["id"]))
        self.assertEqual(paused.exception.status_code, 409)
        self.assertEqual(len(self.repository.state()["runs"]), before)

    async def test_preview_does_not_create_composition_review_or_final(self):
        person, background = await self.pair(), await self.background()
        before = self.repository.state()
        result = await self.service.render(ComposeRequest(task_id=self.task_id, person_asset_id=person["id"], background_asset_id=background["id"], width=64, height=96, preview=True))
        self.assertTrue(result["preview"])
        self.assertIsNone(result["asset"])
        self.assertIsNone(result["review"])
        after = self.repository.state()
        self.assertEqual(after["assets"], before["assets"])
        self.assertEqual(after["reviews"], before["reviews"])

    async def test_final_video_cannot_be_selected_as_alpha_person(self):
        person, background = await self.pair(), await self.background()
        result = await self.service.render(ComposeRequest(task_id=self.task_id, person_asset_id=person["id"],
            background_asset_id=background["id"], width=64, height=96))
        final = (await self.service.approve(result["review"]["id"]))["final"]
        before = self.repository.state()
        with self.assertRaises(HTTPException) as caught:
            self.service.pair(self.service.task(self.task_id), final["id"])
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(caught.exception.detail, "MANUAL_COMPOSE_ASSET_NOT_ELIGIBLE")
        self.assertEqual(self.repository.state(), before)

    async def test_launch_returns_before_validation_and_blocks_duplicate_run(self):
        person, background = await self.pair(), await self.background()
        request = ComposeRequest(task_id=self.task_id, person_asset_id=person["id"], background_asset_id=background["id"], width=64, height=96)
        with patch.object(self.service, "pair", wraps=self.service.pair) as validation:
            launched = await self.service.launch(request)
            self.assertEqual(validation.call_count, 0)
            self.assertEqual(self.service.run(launched["run_id"])["status"], "QUEUED")
            with self.assertRaises(HTTPException) as duplicate:
                await self.service.launch(request)
            self.assertEqual(duplicate.exception.status_code, 409)
            await self.service.jobs[launched["run_id"]]
        result = self.service.run(launched["run_id"])
        self.assertEqual(result["status"], "READY")
        self.assertGreater(result["run"]["pid"], 0)
        self.assertEqual(result["result"]["manifest"]["output_media"]["frame_count"], 6)

    async def test_background_verification_failure_closes_run(self):
        person, background = await self.pair(), await self.background()
        launched = await self.service.launch(ComposeRequest(task_id=self.task_id, person_asset_id=person["id"], background_asset_id=background["id"], width=64, height=96))
        alpha = Path(person["legacy_reference"]["alpha_path"])
        alpha.write_bytes(alpha.read_bytes() + b"unit_changed_before_background_validation")
        await self.service.jobs[launched["run_id"]]
        failed = self.service.run(launched["run_id"])
        self.assertEqual(failed["status"], "FAILED")
        self.assertIsNone(failed["result"])
        self.assertEqual(self.service.versions(self.task_id)["job"]["status"], "FAILED")

    async def test_manual_reject_is_persisted_without_advancing_and_historical_is_scoped(self):
        person = await self.pair()
        old_review = next(row for row in self.repository.state()["reviews"] if row["asset_id"] == person["id"])
        child = (await self.service.apply_transform(self.task_id, TransformRequest(person_asset_id=person["id"], x=8)))["person"]
        before = self.repository.state()
        await self.service.reject(old_review["id"], "historical unit rejection")
        after = self.repository.state()
        self.assertEqual(after["tasks"], before["tasks"])
        self.assertEqual(after["runs"], before["runs"])
        self.assertEqual(after["stages"], before["stages"])
        current_review = next(row for row in after["reviews"] if row["asset_id"] == child["id"])
        await self.service.reject(current_review["id"], "current unit rejection")
        rejected = self.repository.state()
        self.assertEqual(next(row for row in rejected["tasks"] if row["id"] == self.task_id)["status"], "REJECTED")
        self.assertEqual(next(row for row in rejected["reviews"] if row["id"] == current_review["id"])["note"], "current unit rejection")
        self.assertEqual(rejected["runs"], before["runs"])
        self.assertFalse(any(row["task_id"] == self.task_id and row["type"] == "final" for row in rejected["assets"]))

    async def test_changed_mask_or_symlink_outside_storage_is_blocked(self):
        person = await self.pair()
        alpha = Path(person["legacy_reference"]["alpha_path"])
        alpha.write_bytes(alpha.read_bytes() + b"changed")
        with self.assertRaises(HTTPException):
            await self.service.apply_transform(self.task_id, TransformRequest(person_asset_id=person["id"]))
        link = self.settings.storage_root / "outside.mkv"
        link.symlink_to(self.rgb)
        self.repository.mutate(lambda state: next(row for row in state["assets"] if row["id"] == person["id"]).update(storage_path=str(link), external_path=str(link)))
        with self.assertRaises(HTTPException):
            self.service.checked_asset(self.task_id, person["id"], "person")

    async def test_active_execution_foreign_assets_and_oversized_upload_are_blocked(self):
        person, background = await self.pair(), await self.background()
        self.repository.mutate(lambda state: state["runs"].append({"id": "running-local", "task_id": self.task_id, "stage": "compose",
            "provider": "FactoryManualCompose", "status": "running", "progress": 0, "logs": [], "created_at": now()}))
        with self.assertRaises(HTTPException):
            await self.service.apply_transform(self.task_id, TransformRequest(person_asset_id=person["id"]))
        self.repository.mutate(lambda state: next(row for row in state["runs"] if row["id"] == "running-local").update(status="failed"))
        with patch.object(self.settings, "manual_import_max_bytes", 4), self.assertRaises(HTTPException) as too_large:
            await self.service.upload_background(self.task_id, self.upload(self.image))
        self.assertEqual(too_large.exception.status_code, 413)
        self.repository.mutate(lambda state: next(row for row in state["assets"] if row["id"] == background["id"]).update(task_id=state["tasks"][0]["id"]))
        with self.assertRaises(HTTPException):
            await self.service.render(ComposeRequest(task_id=self.task_id, person_asset_id=person["id"], background_asset_id=background["id"]))

    def test_config_returns_actual_positive_import_limit(self):
        self.assertEqual(Settings(_env_file=None).manual_import_max_bytes, 2 * 1024 ** 3)
        for invalid in (0, -1):
            with self.assertRaises(ValueError):
                Settings(_env_file=None, manual_import_max_bytes=invalid)
        self.settings.manual_import_max_bytes = 12345
        app = FastAPI()
        app.include_router(create_manual_compose_router(self.service))
        with TestClient(app) as client:
            response = client.get("/api/manual-compose/config")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"manual_import_max_bytes": 12345})

    async def test_import_limit_is_inclusive_and_applies_to_each_file(self):
        self.settings.manual_import_max_bytes = max(self.rgb.stat().st_size, self.alpha.stat().st_size)
        self.assertGreater(self.rgb.stat().st_size + self.alpha.stat().st_size, self.settings.manual_import_max_bytes)
        person = await self.pair()
        self.assertEqual(person["registry_status"], "registered")
        self.settings.manual_import_max_bytes = self.image.stat().st_size
        await self.background()
        before = len(self.repository.state()["assets"])
        self.settings.manual_import_max_bytes -= 1
        with self.assertRaises(HTTPException) as blocked:
            await self.service.upload_background(self.task_id, self.upload(self.image))
        self.assertEqual(blocked.exception.status_code, 413)
        self.assertIn(str(self.settings.manual_import_max_bytes), blocked.exception.detail)
        self.assertEqual(len(self.repository.state()["assets"]), before)
        run = self.repository.state()["runs"][-1]
        self.assertEqual(run["status"], "failed")
        partial = next(self.settings.storage_root.rglob(run["id"])) / "Background.png"
        self.assertLessEqual(partial.stat().st_size, self.settings.manual_import_max_bytes)

    async def test_routes_reject_bad_transform_and_upload_content(self):
        app = FastAPI()
        app.include_router(create_manual_compose_router(self.service))
        with TestClient(app) as client:
            bad = client.post(f"/api/manual-compose/tasks/{self.task_id}/transforms", json={"person_asset_id": "any", "scale": 0})
            self.assertEqual(bad.status_code, 422)
            image = client.post(f"/api/manual-compose/tasks/{self.task_id}/upload-background", files={"image": ("bad.png", b"invalid image", "image/png")})
            self.assertEqual(image.status_code, 422)
        self.assertEqual(self.repository.state()["runs"][-1]["status"], "failed")

    async def test_render_route_forbids_coordinate_inputs_without_starting_a_run(self):
        app = FastAPI()
        app.include_router(create_manual_compose_router(self.service))
        request = {"task_id": self.task_id, "person_asset_id": "unit-person", "background_asset_id": "unit-background"}
        before = self.repository.state()
        with TestClient(app) as client:
            for field, value in (("x", 8), ("y", -4), ("scale", .75)):
                with self.subTest(field=field):
                    response = client.post("/api/manual-compose/render", json={**request, field: value})
                    self.assertEqual(response.status_code, 422, response.text)
                    self.assertTrue(any(item["type"] == "extra_forbidden" and item["loc"] == ["body", field]
                        for item in response.json()["detail"]))
        self.assertEqual(self.repository.state(), before)

    async def test_router_upload_render_and_approve_use_real_media(self):
        app = FastAPI()
        app.include_router(create_manual_compose_router(self.service))
        with TestClient(app) as client:
            person = client.post(f"/api/manual-compose/tasks/{self.task_id}/upload-person", files={
                "rgb_file": ("rgb.mkv", self.rgb.read_bytes(), "video/x-matroska"), "alpha_file": ("alpha.mkv", self.alpha.read_bytes(), "video/x-matroska")})
            self.assertEqual(person.status_code, 201, person.text)
            background = client.post(f"/api/manual-compose/tasks/{self.task_id}/upload-background", files={"image": ("../../blue.png", self.image.read_bytes(), "image/png")})
            self.assertEqual(background.status_code, 201, background.text)
            request = {"task_id": self.task_id, "person_asset_id": person.json()["asset"]["id"], "background_asset_id": background.json()["asset"]["id"], "width": 64, "height": 96}
            rendered = client.post("/api/manual-compose/render", json=request)
            self.assertEqual(rendered.status_code, 202, rendered.text)
            run_url = rendered.json()["run_url"]
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                job = client.get(run_url).json()
                if job["status"] in {"READY", "FAILED"}:
                    break
                time.sleep(.03)
            self.assertEqual(job["status"], "READY", job)
            result = job["result"]
            preview = client.get(result["preview_url"])
            self.assertEqual(preview.status_code, 200)
            self.assertTrue(preview.headers["content-type"].startswith("video/mp4"))
            self.assertGreater(len(preview.content), 1000)
            approved = client.post("/api/manual-compose/reviews/" + result["review"]["id"] + "/approve", json={"note": "unit full composition approved"})
            self.assertEqual(approved.status_code, 200, approved.text)
            self.assertEqual(approved.json()["final"]["type"], "final")
            self.assertEqual(next(row for row in self.repository.state()["reviews"] if row["id"] == result["review"]["id"])["note"], "unit full composition approved")
            person_review = next(row for row in self.repository.state()["reviews"] if row["asset_id"] == person.json()["asset"]["id"])
            rejected = client.post("/api/manual-compose/reviews/" + person_review["id"] + "/reject", json={"note": "unit rejects current person"})
            self.assertEqual(rejected.status_code, 200, rejected.text)
            self.assertEqual(next(row for row in self.repository.state()["tasks"] if row["id"] == self.task_id)["status"], "REJECTED")


if __name__ == "__main__":
    unittest.main()
