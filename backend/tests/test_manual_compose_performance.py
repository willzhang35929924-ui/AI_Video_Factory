"""Verified-input reuse still hashes files; new output is decoded completely."""
import asyncio
import copy
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app.integrations.asset_registry import file_hash, probe_media
from app.integrations.manual_compose import ComposeRequest, TransformRequest
from tests import test_manual_compose as manual_tests


class VerifiedManualInputTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = manual_tests.ManualComposeTests()
        await self.fixture.asyncSetUp()
        for function, arguments, keywords in self.fixture._cleanups:
            self.addCleanup(function, *arguments, **keywords)
        self.fixture._cleanups.clear()
        self.service = self.fixture.service
        self.repository = self.fixture.repository
        self.person = await self.fixture.pair()
        self.background = await self.fixture.background()
        self.task_id = self.fixture.task_id

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    def task(self):
        return next(row for row in self.repository.state()["tasks"] if row["id"] == self.task_id)

    async def test_apply_undo_and_restore_use_current_hashes_and_streams_not_full_input_decode(self):
        before = {Path(self.person["storage_path"]): file_hash(self.person["storage_path"]),
            Path(self.person["legacy_reference"]["alpha_path"]): self.person["legacy_reference"]["alpha_sha256"]}
        with patch("app.integrations.manual_compose.probe_media", wraps=probe_media) as manual_probe, \
                patch("app.integrations.asset_registry.probe_media", wraps=probe_media) as registry_probe:
            child = (await self.service.apply_transform(self.task_id, TransformRequest(person_asset_id=self.person["id"], x=8)))["person"]
            await self.service.undo(self.task_id)
            await self.service.restore(self.task_id, child["id"])
        self.assertEqual(manual_probe.call_count, 0)
        self.assertGreaterEqual(registry_probe.call_count, 9)
        self.assertFalse(any(call.kwargs.get("count_frames") for call in registry_probe.call_args_list))
        self.assertEqual({path: file_hash(path) for path in before}, before)

    async def test_reused_input_and_new_output_have_distinct_decode_boundaries(self):
        request = ComposeRequest(task_id=self.task_id, person_asset_id=self.person["id"], background_asset_id=self.background["id"], width=64, height=96)
        with patch("app.integrations.manual_compose.probe_media", wraps=probe_media) as manual_probe, \
                patch("app.integrations.asset_registry.probe_media", wraps=probe_media) as registry_probe:
            result = await self.service.render(request)
            await self.service.approve(result["review"]["id"])
        mother_paths = {Path(self.person["storage_path"]), Path(self.person["legacy_reference"]["alpha_path"]), Path(self.person["legacy_reference"]["preview_path"])}
        for call in [*manual_probe.call_args_list, *registry_probe.call_args_list]:
            if Path(call.args[0]) in mother_paths:
                self.assertFalse(call.kwargs.get("count_frames", False))
        output = Path(result["asset"]["storage_path"])
        output_counts = [call for call in manual_probe.call_args_list if Path(call.args[0]) == output and call.kwargs.get("count_frames")]
        self.assertEqual(len(output_counts), 2)
        self.assertTrue(result["manifest"]["full_decode_verified"])
        self.assertEqual(result["manifest"]["output_media"]["frame_count"], 6)

    async def test_unknown_registered_metadata_still_requires_full_decode(self):
        self.repository.mutate(lambda state: next(row for row in state["assets"] if row["id"] == self.person["id"])["legacy_reference"].pop("alpha_metadata"))
        with patch("app.integrations.manual_compose.probe_media", wraps=probe_media) as manual_probe, \
                patch("app.integrations.asset_registry.probe_media", wraps=probe_media) as registry_probe:
            child = (await self.service.apply_transform(self.task_id, TransformRequest(person_asset_id=self.person["id"], x=8)))["person"]
        self.assertTrue(manual_probe.call_args.kwargs["count_frames"])
        self.assertGreaterEqual(sum(bool(call.kwargs.get("count_frames")) for call in registry_probe.call_args_list), 2)
        self.assertTrue(child["legacy_reference"]["full_decode_verified"])
        self.assertEqual(child["legacy_reference"]["alpha_metadata"]["frame_count"], 6)

    async def test_manifest_cached_timeline_or_preview_changes_cannot_create_versions(self):
        original = copy.deepcopy(self.person)
        preview = Path(original["legacy_reference"]["preview_path"])
        preview_bytes = preview.read_bytes()
        for changed in ("cached_frames", "cached_nan", "preview", "manifest"):
            def alter(state):
                asset = next(row for row in state["assets"] if row["id"] == self.person["id"])
                asset.update(copy.deepcopy(original))
                if changed == "cached_frames":
                    asset["probe_metadata"]["frame_count"] = 9
                if changed == "cached_nan":
                    asset["probe_metadata"]["fps"] = float("nan")
            self.repository.mutate(alter)
            if changed == "preview":
                preview.write_bytes(preview_bytes + b"CHANGED")
            manifest_path = Path(original["legacy_reference"]["manifest_path"])
            manifest_bytes = manifest_path.read_bytes()
            if changed == "manifest":
                manifest_path.write_bytes(manifest_bytes + b" ")
            before = len(self.repository.state()["assets"])
            with self.assertRaises(HTTPException) as blocked:
                await self.service.apply_transform(self.task_id, TransformRequest(person_asset_id=self.person["id"], x=8))
            self.assertEqual(blocked.exception.status_code, 409)
            self.assertEqual(len(self.repository.state()["assets"]), before)
            preview.write_bytes(preview_bytes)
            manifest_path.write_bytes(manifest_bytes)

    async def test_long_audio_container_does_not_replace_verified_video_timeline(self):
        source = self.fixture.root / "long-audio.mkv"
        self.fixture.ffmpeg("-i", str(self.fixture.rgb), "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=2",
            "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "pcm_s16le", str(source))
        person = (await self.service.upload_person(self.task_id, self.fixture.upload(source), self.fixture.upload(self.fixture.alpha)))["asset"]
        _, _, _, media = self.service.pair(self.task(), person["id"])
        self.assertEqual(media["frame_count"], 6)
        self.assertAlmostEqual(media["duration"], 1)
        self.assertGreater(media["container_duration"], 1.5)
        result = await self.service.render(ComposeRequest(task_id=self.task_id, person_asset_id=person["id"], background_asset_id=self.background["id"], width=64, height=96))
        self.assertAlmostEqual(result["manifest"]["output_media"]["duration"], 1)

    async def test_input_validation_is_running_with_phase_and_row_only_heartbeat_before_process_pid(self):
        entered, release = threading.Event(), threading.Event()
        pair = self.service.pair
        def delayed_pair(*arguments, **keywords):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test validation release timeout")
            return pair(*arguments, **keywords)
        request = ComposeRequest(task_id=self.task_id, person_asset_id=self.person["id"], background_asset_id=self.background["id"], width=64, height=96, preview=True)
        with patch.object(self.service, "pair", side_effect=delayed_pair), \
                patch("app.integrations.manual_compose.MANUAL_HEARTBEAT_SECONDS", .01), \
                patch.object(self.repository, "update_processing_run_event", wraps=self.repository.update_processing_run_event) as row_event:
            queued = await self.service.launch(request)
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                first = self.service.run(queued["run_id"])
                self.assertEqual((first["status"], first["phase"]), ("RUNNING", "VALIDATING"))
                self.assertIsNone(first["run"]["pid"])
                self.assertIsNotNone(first["run"]["started_at"])
                await asyncio.sleep(.04)
                latest = self.service.run(queued["run_id"])
                self.assertNotEqual(first["run"]["last_heartbeat"], latest["run"]["last_heartbeat"])
                self.assertTrue(any(call.args[2].keys() == {"last_heartbeat"} for call in row_event.call_args_list))
            finally:
                release.set()
            await self.service.jobs[queued["run_id"]]
        finished = self.service.run(queued["run_id"])
        self.assertEqual((finished["status"], finished["phase"]), ("READY", "READY"))
        self.assertGreater(finished["run"]["pid"], 0)

    async def test_measures_previous_full_count_against_verified_input_reuse_on_same_small_media(self):
        person, rgb = self.service.checked_asset(self.task_id, self.person["id"], "person")
        evidence = {"path": str(rgb), "legacy_reference": person["legacy_reference"]}
        with patch("app.integrations.asset_registry.probe_media", wraps=probe_media) as old_probe:
            started = time.monotonic()
            media = probe_media(rgb, count_frames=True)
            self.service.registry._person_pair(self.task(), evidence, rgb, media, person["sha256"])
            old_seconds = time.monotonic() - started
        with patch("app.integrations.asset_registry.probe_media", wraps=probe_media) as new_probe:
            started = time.monotonic()
            self.service.pair(self.task(), person["id"])
            new_seconds = time.monotonic() - started
        self.assertTrue(all(call.kwargs.get("count_frames") for call in old_probe.call_args_list))
        self.assertFalse(any(call.kwargs.get("count_frames") for call in new_probe.call_args_list))
        print(f"Temporary 6-frame input validation: old {old_seconds:.3f}s / verified {new_seconds:.3f}s; full-input-count calls {old_probe.call_count + 1} -> 0")
