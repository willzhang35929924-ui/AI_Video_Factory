"""External snapshot media stay unchanged; local output uses isolated storage only."""
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.config import Settings
from app.integrations.asset_registry import file_hash, probe_media
from app.integrations.manual_compose import ComposeRequest, TransformRequest
from app.integrations.matte_repair import MatteRepairService, RepairRequest, create_repair_router
from app.storage import StoragePaths
from tests import test_manual_compose as manual_tests


WORKER = Path(__file__).parents[1] / "app/integrations/legacy/alpha_preview_worker.py"


class ReadonlyAssetRootTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = manual_tests.ManualComposeTests()
        await self.fixture.asyncSetUp()
        for function, arguments, keywords in self.fixture._cleanups:
            self.addCleanup(function, *arguments, **keywords)
        self.fixture._cleanups.clear()
        self.service = self.fixture.service
        self.registry = self.service.registry
        self.repository = self.fixture.repository
        self.settings = self.fixture.settings
        self.person = await self.fixture.pair()
        self.background = await self.fixture.background()
        self.external = self.settings.storage_root
        self.original = self.files()
        self.settings.storage_root = self.fixture.root / "staging-storage"
        self.settings.storage_root.mkdir()
        self.settings.readonly_asset_roots = [self.external]
        self.settings.legacy_tiktok_auto_root = self.fixture.root / "legacy-collector"
        self.settings.legacy_video_auto_root = self.fixture.root / "legacy-video"
        self.settings.quality_python = Settings(_env_file=None).quality_python
        self.repository.app_mode = "integration"
        self.task_id = self.fixture.task_id

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()
        self.assertEqual(self.files(), self.original)

    def files(self):
        return {str(path.relative_to(self.external)): file_hash(path) for path in self.external.rglob("*") if path.is_file()}

    def task(self):
        return next(row for row in self.repository.state()["tasks"] if row["id"] == self.task_id)

    async def test_external_registered_media_readable_but_registration_and_delete_stay_denied(self):
        self.assertEqual(Settings(_env_file=None).readonly_asset_roots, [])
        path, _ = self.registry.content(self.person["id"])
        self.assertEqual(path, Path(self.person["storage_path"]))
        self.assertTrue(self.registry.media_status(self.person)["preview_available"])
        preview, mime = self.registry.preview(self.person["id"])
        self.assertTrue(preview.is_relative_to(self.external))
        self.assertEqual(mime, "video/mp4")
        self.assertEqual(probe_media(preview, count_frames=True)["frame_count"], 6)
        self.assertEqual(self.service.pair(self.task(), self.person["id"])[-1]["frame_count"], 6)
        before = self.repository.state()
        with self.assertRaises(ValueError):
            self.registry.allowed_path(path)
        with self.assertRaises(ValueError):
            self.registry.register(self.task(), {"path": self.background["storage_path"]}, "background", "blocked-external-output")
        plan = self.registry.deletion_status(self.background["id"])
        self.assertFalse(plan["can_delete"])
        self.assertIn("EXTERNAL_LEGACY_FILE", plan["reasons"])
        with patch("app.integrations.asset_registry.os.replace") as move, self.assertRaises(HTTPException) as blocked:
            self.registry.delete_asset(self.background["id"], plan["fingerprint"], confirmed=True)
        self.assertEqual(blocked.exception.status_code, 409)
        self.assertEqual(blocked.exception.detail["code"], "EXTERNAL_LEGACY_FILE")
        move.assert_not_called()
        self.assertFalse((self.settings.storage_root / ".asset-delete").exists())
        after = self.repository.state()
        self.assertEqual({key: value for key, value in after.items() if key != "audits"},
            {key: value for key, value in before.items() if key != "audits"})
        self.assertEqual(after["audits"][:-1], before["audits"])
        audit = after["audits"][-1]
        self.assertEqual(audit["action"], "asset.delete_failed")
        self.assertEqual(audit["entity_id"], self.background["id"])
        self.assertEqual(json.loads(audit["message"])["error"]["code"], "EXTERNAL_LEGACY_FILE")

    async def test_readonly_rgb_reuse_rejects_unbound_unverified_cross_task_and_technical_parent(self):
        evidence = {"path": self.person["storage_path"], "sha256": self.person["sha256"],
            "legacy_reference": {"parent_person_asset_id": self.person["id"], "execution_scope": "PRODUCTION",
                **{key: str(self.settings.storage_root / key) for key in ("alpha_path", "manifest_path", "preview_path")}}}
        original = json.loads(json.dumps(self.person))
        cases = [
            {"registry_status": "unverified"}, {"type": "compose"},
            {"size": self.person["size"] + 1}, {"sha256": "UNIT_DIFFERENT_RGB_HASH"},
            {"legacy_reference": {**self.person["legacy_reference"], "full_decode_verified": False}},
            {"legacy_reference": {**self.person["legacy_reference"], "execution_scope": "TECHNICAL_VALIDATION"}},
        ]
        for changed in cases:
            with self.subTest(changed=changed):
                self.repository.mutate(lambda state: next(row for row in state["assets"] if row["id"] == self.person["id"]).update(changed))
                before = self.repository.state()
                with self.assertRaises(ValueError):
                    self.registry.register(self.task(), evidence, "person", "UNIT_READONLY_REUSE_GUARD")
                self.assertEqual(self.repository.state(), before)
                self.repository.mutate(lambda state: next(row for row in state["assets"] if row["id"] == self.person["id"]).update(original))
        for parent_id, scope in ((None, "PRODUCTION"), (self.person["id"], "TECHNICAL_VALIDATION")):
            bad = {**evidence, "legacy_reference": {**evidence["legacy_reference"], "parent_person_asset_id": parent_id, "execution_scope": scope}}
            before = self.repository.state()
            with self.assertRaises(ValueError):
                self.registry.register(self.task(), bad, "person", "UNIT_READONLY_REUSE_GUARD")
            self.assertEqual(self.repository.state(), before)
        before = self.repository.state()
        with self.assertRaises(ValueError):
            self.registry.register({**self.task(), "id": "UNIT_OTHER_TASK"}, evidence, "person", "UNIT_CROSS_TASK_GUARD")
        self.assertEqual(self.repository.state(), before)
        self.assertEqual(self.files(), self.original)

    async def test_transform_preview_and_full_composition_write_only_staging(self):
        child = (await self.service.apply_transform(self.task_id,
            TransformRequest(person_asset_id=self.person["id"], x=8, y=-4, scale=.75)))["person"]
        self.assertEqual(child["storage_path"], self.person["storage_path"])
        self.assertEqual(child["legacy_reference"]["alpha_path"], self.person["legacy_reference"]["alpha_path"])
        request = ComposeRequest(task_id=self.task_id, person_asset_id=child["id"], background_asset_id=self.background["id"],
            width=64, height=96, preview=True)
        queued = await self.service.launch(request)
        await self.service.jobs[queued["run_id"]]
        preview = self.service.run(queued["run_id"])
        self.assertEqual(preview["status"], "READY")
        self.assertTrue(self.service.preview(queued["run_id"]).is_relative_to(self.settings.storage_root))
        self.assertIsNone(preview["result"]["asset"])
        rendered = await self.service.render(request.model_copy(update={"preview": False}))
        final = (await self.service.approve(rendered["review"]["id"]))["final"]
        self.assertTrue(Path(final["storage_path"]).is_relative_to(self.settings.storage_root))
        self.assertEqual(final["legacy_reference"]["person_asset_id"], child["id"])
        self.assertEqual(self.files(), self.original)

    async def test_matte_info_and_rgb_alpha_frames_read_external_transform_pair_without_widening_write_root(self):
        child = (await self.service.apply_transform(self.task_id,
            TransformRequest(person_asset_id=self.person['id'], x=8, y=-4, scale=.75)))['person']
        repair = MatteRepairService(self.service.production)
        self.addAsyncCleanup(repair.shutdown)
        _, actual, rgb, alpha, manifest = repair.pair(self.task_id, child['id'])
        self.assertEqual(actual['legacy_reference']['transform'], {'x': 8.0, 'y': -4.0, 'scale': .75})
        self.assertTrue(rgb.is_relative_to(self.external))
        self.assertTrue(alpha.is_relative_to(self.external))
        app = FastAPI()
        app.include_router(create_repair_router(repair))
        with TestClient(app) as client:
            info = client.get(f'/api/tasks/{self.task_id}/person/{child["id"]}/info')
            self.assertEqual(info.status_code, 200)
            self.assertEqual(info.json()['media']['frame_count'], 6)
            for channel in ('rgb', 'alpha'):
                response = client.get(f'/api/tasks/{self.task_id}/person/{child["id"]}/frame?frame=0&channel={channel}')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers['content-type'], 'image/png')
                self.assertTrue(response.content.startswith(b'\x89PNG'))
        with self.assertRaises(ValueError):
            self.registry.allowed_path(alpha)
        self.assertEqual(self.files(), self.original)

    async def test_matte_readonly_inputs_cannot_escape_configured_roots(self):
        repair = MatteRepairService(self.service.production)
        self.addAsyncCleanup(repair.shutdown)
        before = self.person['legacy_reference']['alpha_path']
        outside = self.external.with_name(self.external.name + '-matte-outside')
        outside.mkdir()
        alpha = outside / 'outside-alpha.mkv'
        alpha.write_bytes(b'UNIT_ONLY_UNAUTHORIZED')
        self.repository.mutate(lambda state: next(asset for asset in state['assets'] if asset['id'] == self.person['id'])['legacy_reference'].update(alpha_path=str(alpha)))
        with self.assertRaises(HTTPException) as blocked:
            repair.pair(self.task_id, self.person['id'], False)
        self.assertEqual(blocked.exception.status_code, 409)
        self.assertEqual(blocked.exception.detail, 'MOTHER_INPUT_PATH_NOT_AUTHORIZED')
        self.repository.mutate(lambda state: next(asset for asset in state['assets'] if asset['id'] == self.person['id'])['legacy_reference'].update(alpha_path=before))
        self.assertEqual(self.files(), self.original)

    async def test_historical_matte_preview_reads_configured_readonly_root_without_widening_outputs(self):
        repair = MatteRepairService(self.service.production)
        self.addAsyncCleanup(repair.shutdown)
        preview = Path(self.person['legacy_reference']['preview_path'])
        self.assertTrue(preview.is_relative_to(self.external))
        record = {'id': 'UNIT_HISTORICAL_REPAIR_PREVIEW', 'status': 'APPLIED',
            'evidence': {'preview_path': str(preview), 'preview_sha256': file_hash(preview)}}
        self.repository.mutate(lambda state: next(row for row in state['tasks'] if row['id'] == self.task_id)['context'].update(matte_repairs=[record]))
        app = FastAPI()
        app.include_router(create_repair_router(repair))
        with TestClient(app) as client:
            response = client.get(f'/api/tasks/{self.task_id}/repairs/{record["id"]}/preview')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers['content-type'], 'video/mp4')
            self.assertEqual(response.content, preview.read_bytes())
            outside = self.fixture.root / 'unregistered-preview.mp4'
            outside.write_bytes(b'UNIT_ONLY_UNAUTHORIZED_PREVIEW')
            self.repository.mutate(lambda state: next(row for row in state['tasks'] if row['id'] == self.task_id)['context']['matte_repairs'][0]['evidence'].update(preview_path=str(outside)))
            response = client.get(f'/api/tasks/{self.task_id}/repairs/{record["id"]}/preview')
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()['detail'], 'PREVIEW_PATH_NOT_AUTHORIZED')
        with self.assertRaises(ValueError):
            self.registry.allowed_path(preview)
        self.assertEqual(self.files(), self.original)

    async def test_matte_brush_preview_from_external_transformed_pair_only_creates_isolated_output(self):
        child = (await self.service.apply_transform(self.task_id,
            TransformRequest(person_asset_id=self.person['id'], x=8, y=-4, scale=.75)))['person']
        self.service.production.consume_deferred_continue = AsyncMock()
        repair = MatteRepairService(self.service.production)
        self.addAsyncCleanup(repair.shutdown)
        request = RepairRequest(person_asset_id=child['id'], start_frame=0, end_frame=1, mode='remove',
            direction='keyframes_only', idempotency_key='UNIT_ONLY_EXTERNAL_MASK_PREVIEW',
            keyframes=[{'frame_index': 0, 'object_id': 'cloth_1',
                'strokes': [{'label': 1, 'radius': 8, 'points': [{'x': .75, 'y': .5}]}]}])
        queued, created = await repair.prepare(self.task_id, request)
        self.assertTrue(created)
        self.assertTrue(Path(queued['directory']).is_relative_to(self.settings.storage_root))
        await repair.launch(self.task_id, queued['id'])
        await repair.jobs[queued['id']]
        preview = next(item for item in repair.records(self.task_id) if item['id'] == queued['id'])
        stderr = Path(queued['directory']) / 'logs' / 'collector-stderr.log'
        detail = stderr.read_text()[-3000:] if stderr.is_file() else preview.get('error_code')
        self.assertEqual(preview['status'], 'PREVIEW_READY', detail)
        self.assertEqual(preview['evidence']['changed_frame_count'], 1)
        output = Path(preview['evidence']['preview_path'])
        self.assertTrue(output.is_relative_to(self.settings.storage_root))
        self.assertEqual(probe_media(output, count_frames=True)['frame_count'], 6)
        result = json.loads((Path(queued['directory']) / 'result.json').read_text())
        self.assertEqual(result['asset']['legacy_reference']['transform'], {'x': 8.0, 'y': -4.0, 'scale': .75})
        self.assertEqual(result['asset']['path'], child['storage_path'])
        self.assertEqual(self.task()['context']['production']['current_person_asset_id'], child['id'])
        self.assertEqual(self.files(), self.original)

    async def test_real_alpha_preview_cache_uses_staging_and_preserves_snapshot_and_database(self):
        before = self.repository.state()
        path, mime = self.registry.preview(self.person["id"], "alpha")
        self.assertTrue(path.is_relative_to(self.settings.storage_root / "diagnostic_previews"))
        self.assertEqual(mime, "video/mp4")
        self.assertEqual(probe_media(path, count_frames=True)["frame_count"], 6)
        self.assertEqual(self.registry.preview(self.person["id"], "alpha")[0], path)
        self.assertEqual(self.repository.state(), before)
        self.assertEqual(self.files(), self.original)

    async def test_local_alpha_revision_registers_only_known_readonly_parent_rgb_and_undo_preserves_history(self):
        child = (await self.service.apply_transform(self.task_id,
            TransformRequest(person_asset_id=self.person['id'], x=8, y=-4, scale=.75)))['person']
        self.service.production.consume_deferred_continue = AsyncMock()
        repair = MatteRepairService(self.service.production)
        self.addAsyncCleanup(repair.shutdown)
        request = RepairRequest(person_asset_id=child['id'], start_frame=0, end_frame=1, mode='remove',
            direction='keyframes_only', idempotency_key='UNIT_ONLY_EXTERNAL_MASK_APPLY_UNDO',
            keyframes=[{'frame_index': 0, 'object_id': 'cloth_1',
                'strokes': [{'label': 1, 'radius': 8, 'points': [{'x': .75, 'y': .5}]}]}])
        queued, _ = await repair.prepare(self.task_id, request)
        await repair.launch(self.task_id, queued['id'])
        await repair.jobs[queued['id']]
        preview = next(item for item in repair.records(self.task_id) if item['id'] == queued['id'])
        stderr = Path(queued['directory']) / 'logs' / 'collector-stderr.log'
        self.assertEqual(preview['status'], 'PREVIEW_READY', stderr.read_text()[-3000:] if stderr.is_file() else preview.get('error_code'))
        bad = json.loads(json.dumps(preview['evidence']['asset']))
        bad['legacy_reference']['preview_path'] = self.person['legacy_reference']['preview_path']
        with self.assertRaises(ValueError):
            self.registry.register(self.task(), bad, 'person', queued['run_id'])
        applied = await repair.apply(self.task_id, queued['id'])
        revised = next(asset for asset in self.repository.state()['assets'] if asset['id'] == applied['asset_id'])
        self.assertEqual(revised['storage_path'], child['storage_path'])
        self.assertEqual(revised['sha256'], child['sha256'])
        self.assertEqual(revised['legacy_reference']['transform'], {'x': 8.0, 'y': -4.0, 'scale': .75})
        self.assertEqual(revised['legacy_reference']['parent_person_asset_id'], child['id'])
        for key in ('alpha_path', 'manifest_path', 'preview_path'):
            self.assertTrue(Path(revised['legacy_reference'][key]).is_relative_to(self.settings.storage_root))
        self.assertNotEqual(revised['legacy_reference']['alpha_sha256'], child['legacy_reference']['alpha_sha256'])
        self.assertEqual(self.task()['context']['production']['current_person_asset_id'], revised['id'])
        undone = await repair.undo(self.task_id, queued['id'])
        self.assertEqual(undone['status'], 'UNDONE')
        self.assertEqual(self.task()['context']['production']['current_person_asset_id'], child['id'])
        self.assertIn(revised['id'], [asset['id'] for asset in self.repository.state()['assets']])
        self.assertTrue(Path(revised['legacy_reference']['alpha_path']).is_file())
        self.assertFalse(self.task()['context']['production']['review_approved'])
        with self.assertRaises(ValueError):
            self.registry.allowed_path(revised['storage_path'])
        self.assertEqual(self.files(), self.original)

    async def test_readonly_resolution_rejects_siblings_and_symlink_escape_without_widening_write_paths(self):
        external_rgb = Path(self.person["storage_path"])
        self.settings.readonly_asset_roots = []
        with self.assertRaises(ValueError):
            self.registry.read_path(external_rgb)
        self.settings.readonly_asset_roots = [self.external]
        sibling = self.external.with_name(self.external.name + "-sibling")
        sibling.mkdir()
        outside = sibling / "outside.mp4"
        outside.write_bytes(b"UNIT_OUTSIDE_READ_ROOT")
        with self.assertRaises(ValueError):
            self.registry.read_path(outside)
        link = self.settings.storage_root / "external-rgb.mkv"
        link.symlink_to(external_rgb)
        self.assertEqual(self.registry.read_path(link), external_rgb)
        with self.assertRaises(ValueError):
            self.registry.allowed_path(link)
        escape = self.settings.storage_root / "escape.mp4"
        escape.symlink_to(outside)
        with self.assertRaises(ValueError):
            self.registry.read_path(escape)

    async def test_preview_cache_and_manual_run_directory_cannot_escape_to_existing_legacy_root(self):
        legacy = self.settings.legacy_tiktok_auto_root / "downloads"
        legacy.mkdir(parents=True)
        (self.settings.storage_root / "diagnostic_previews").symlink_to(legacy, target_is_directory=True)
        with self.assertRaises(HTTPException):
            self.registry.preview(self.person["id"], "alpha")
        compose = Path(StoragePaths(self.settings.storage_root).task_paths(self.task()["project_id"], self.task_id)["compose"])
        compose.parent.mkdir(parents=True)
        compose.symlink_to(legacy, target_is_directory=True)
        before = self.repository.state()
        with self.assertRaises(ValueError):
            self.service.begin(self.task(), "compose", "FactoryManualCompose")
        self.assertEqual(list(legacy.iterdir()), [])
        self.assertEqual(self.repository.state(), before)

    async def test_alpha_worker_refuses_output_and_result_in_readonly_root(self):
        attempt = self.settings.storage_root / "previews/runs/blocked-attempt"
        attempt.mkdir(parents=True)
        request_path = attempt / "request.json"
        external_output, external_result = self.external / "forbidden-preview.mp4", self.external / "forbidden-result.json"
        for output, result in ((external_output, attempt.parent.parent / "preview.json"), (attempt / "Alpha.mp4", external_result)):
            request_path.write_text(json.dumps({"inputs": {"alpha_path": self.person["legacy_reference"]["alpha_path"],
                "manifest_path": self.person["legacy_reference"]["manifest_path"]}, "storage_root": str(self.settings.storage_root),
                "readonly_asset_roots": [str(self.external)], "output_path": str(output)}))
            process = subprocess.run([str(self.settings.quality_python), "-B", str(WORKER), "--request", str(request_path), "--result", str(result)],
                capture_output=True, text=True, timeout=30)
            self.assertNotEqual(process.returncode, 0)
            self.assertIn("ALPHA_PREVIEW_PATH_OUTSIDE_FACTORY", process.stderr)
            self.assertFalse(output.exists())
            self.assertFalse(result.exists())
        self.assertEqual(self.files(), self.original)

    async def test_inner_preview_cache_symlinks_cannot_write_or_move_external_files(self):
        allowed_path = self.registry.allowed_path
        for filename in ("runs", "preview.json"):
            links = []
            def cache_link(value):
                path = allowed_path(value)
                if path.parent == self.settings.storage_root / "diagnostic_previews":
                    path.mkdir(parents=True, exist_ok=True)
                    link = path / filename
                    link.symlink_to(self.external if filename == "runs" else Path(self.person["legacy_reference"]["manifest_path"]))
                    links.append(link)
                return path
            with patch.object(self.registry, "allowed_path", side_effect=cache_link), self.assertRaises(HTTPException):
                self.registry.preview(self.person["id"], "alpha")
            for link in links:
                link.unlink()
            self.assertEqual(self.files(), self.original)
