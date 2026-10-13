import json
import hashlib
import os
import sys
import tempfile
import threading
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import patch
from pathlib import Path

from app.integrations.process_runner import ProcessRunner, redact
from app.integrations.legacy.collector_adapter import LegacyCollectorAdapter
from app.integrations.legacy.collector_worker import collector_result, execute, source_metadata, write_json


class CollectorBridgeTests(unittest.TestCase):
    def test_runner_redacts_output_events_and_uses_explicit_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            events = []
            secret = "collector-test-key"
            execution = ProcessRunner().run(
                [sys.executable, "-c", "import os,json; print(os.environ.get('TIKTOK_AUTO_PRODUCT_VISION_API_KEY')); print('FACTORY_EVENT '+json.dumps({'type':'PROGRESS','value':os.environ.get('TIKTOK_AUTO_PRODUCT_VISION_API_KEY')}))"],
                cwd=root, log_dir=root, env={"TIKTOK_AUTO_PRODUCT_VISION_API_KEY": secret, "UNREQUESTED_KEY": "must-not-pass"}, on_event=events.append,
            )
            self.assertEqual(execution.exit_code, 0)
            self.assertNotIn(secret, execution.stdout)
            self.assertNotIn(secret, Path(execution.log_path).read_text())
            self.assertEqual(events[0]["type"], "PROCESS_STARTED")
            self.assertEqual(events[0]["pid"], execution.pid)
            self.assertIn({"type": "PROGRESS", "value": "[REDACTED]"}, events)

    def test_runner_cancellation_stops_its_own_child(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cancel = threading.Event()
            timer = threading.Timer(0.05, cancel.set)
            timer.start()
            execution = ProcessRunner().run([sys.executable, "-c", "import time;time.sleep(20)"], cwd=root, log_dir=root, cancel_event=cancel)
            timer.join()
            self.assertTrue(execution.cancelled)
            self.assertGreater(execution.pid, 0)
            self.assertNotEqual(execution.exit_code, 0)
            self.assertLess(execution.duration_s, 5)

    def test_runner_timeout_is_not_a_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            execution = ProcessRunner().run([sys.executable, "-c", "import time;time.sleep(20)"], cwd=root, log_dir=root, timeout_s=0.05)
            self.assertTrue(execution.timed_out)
            self.assertNotEqual(execution.exit_code, 0)
            self.assertTrue(Path(execution.stdout_log_path).is_file())
            self.assertTrue(Path(execution.stderr_log_path).is_file())

    def test_runner_observer_failure_retains_actual_exit_and_original_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, events = Path(temporary), []
            original = TimeoutError("isolated observer failure")
            def callback(event):
                events.append(event)
                if event["type"] == "output":
                    raise original
                if event["type"] == "process_finished":
                    raise ValueError("secondary observer failure")
            with self.assertRaises(TimeoutError) as caught:
                ProcessRunner().run([sys.executable, "-c", "import time;print('ready',flush=True);time.sleep(20)"],
                    cwd=root, log_dir=root, on_event=callback)
            self.assertIs(caught.exception, original)
            records = [json.loads(line) for line in (root / "collector-process.jsonl").read_text().splitlines()]
            terminal = records[-1]
            self.assertEqual(terminal["type"], "process_finished")
            self.assertEqual(terminal["failure_reason"], "TimeoutError")
            self.assertEqual(terminal["termination_signal"], 9)
            self.assertEqual(terminal["exit_code"], -9)
            self.assertGreaterEqual(terminal["finished_at"], terminal["started_at"])
            self.assertGreaterEqual(terminal["duration_ms"], 0)
            self.assertIn("resource_info", terminal)
            self.assertEqual(events[-1]["type"], "process_finished")
            with self.assertRaises(ProcessLookupError):
                os.kill(terminal["pid"], 0)

    def test_unknown_external_checkpoint_does_not_import_or_execute_legacy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = {"task_id": "one", "country": "墨西哥", "keyword": "test", "context_id": "one-context"}
            identity = hashlib.sha256(json.dumps(context, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            result_path = root / "collector-result.json"
            write_json(root / "collector-state.json", {"identity": identity, "status": "external_inflight", "row": {"video_id": "123"}})
            result = execute(context, root / "nonexistent-legacy", root, result_path)
            self.assertEqual(result["status"], "WAITING_REVIEW")
            self.assertEqual(result["error_code"], "UNKNOWN_EXTERNAL_REQUEST")
            self.assertFalse(result["retryable"])
            self.assertEqual(result["video_id"], "123")

    def test_ratio_is_percent_and_unknown_visual_fields_remain_unknown(self):
        result = collector_result({"task_id": "one"}, {"likes": "1234", "views": "100000", "decision": "manual_review"}, Path("/legacy"), Path("/result.json"))
        self.assertAlmostEqual(result["like_view_ratio"], 1.234)
        self.assertIsNone(result["overlay_detected"])
        self.assertIsNone(result["watermark_detected"])
        self.assertIsNone(result["source_asset_metadata"])

    def test_accepted_without_a_verified_file_cannot_complete(self):
        result = collector_result({"task_id": "one"}, {"decision": "accepted"}, Path("/legacy"), Path("/result.json"))
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["decision"], "manual_review")
        self.assertEqual(result["error_code"], "SOURCE_NOT_VERIFIED")

    def test_missing_source_cannot_make_unknown_paid_request_retryable(self):
        result = collector_result({"task_id": "one"}, {"decision": "manual_review", "download_path": "/missing-video.mp4"}, Path("/legacy"), Path("/result.json"),
                                  error_code="UNKNOWN_EXTERNAL_REQUEST", error_message="unknown")
        self.assertEqual(result["error_code"], "UNKNOWN_EXTERNAL_REQUEST")
        self.assertFalse(result["retryable"])
        self.assertEqual(result["status"], "WAITING_REVIEW")

    def test_redaction_handles_json_escaped_secret(self):
        secret = 'fake"quoted\\key'
        self.assertNotIn(secret, json.loads(redact(json.dumps(secret), [secret])))

    def source_fixture(self, root):
        path = root / "downloads" / "墨西哥" / "source.mp4"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"isolated-source-fixture")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        row = {"video_id": "123", "url": "https://www.tiktok.com/@test/video/123", "decision": "accepted",
               "download_path": str(path), "download_source_type": "vidbee_automation", "download_source_sha256": digest,
               "vidbee_task_id": "vidbee-123", "vidbee_task_status": "completed"}
        task = SimpleNamespace(task_id="vidbee-123", status="completed", file_path=str(root / "downloads" / "before-archive.mp4"), sha256=digest)
        lookup = SimpleNamespace(found=True, reason="isolated-test-lookup", task=task)
        client = SimpleNamespace(lookup_task=lambda identifier: lookup)
        return row, task, lookup, client

    def test_csv_completed_cannot_replace_live_vidbee_completed_lookup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            row, task, lookup, client = self.source_fixture(root)
            for found, status in ((False, "completed"), (True, "queued"), (True, "failed")):
                lookup.found, task.status = found, status
                with self.subTest(found=found, status=status), patch("app.integrations.legacy.collector_worker.subprocess.run") as probe:
                    result = collector_result({}, row, root, root / "collector-result.json", vidbee_client=client)
                    self.assertEqual(result["status"], "FAILED")
                    self.assertEqual(result["error_code"], "SOURCE_NOT_VERIFIED")
                    self.assertIsNone(result["source_asset_metadata"])
                    self.assertEqual(result["download_status"], "unverified")
                    probe.assert_not_called()

    def test_completed_vidbee_source_survives_country_archive_path_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            row, task, lookup, client = self.source_fixture(root)
            task.sha256 = ""
            probe_data = {"streams": [{"codec_name": "h264", "width": 1080, "height": 1920,
                                        "avg_frame_rate": "60000/1001", "r_frame_rate": "60000/1001", "nb_frames": "120", "duration": "2.002"}]}
            with patch("app.integrations.legacy.collector_worker.subprocess.run", return_value=SimpleNamespace(stdout=json.dumps(probe_data))) as probe:
                metadata = source_metadata(row, root, client)
            self.assertTrue(metadata["verified"])
            self.assertEqual(metadata["sha256"], row["download_source_sha256"])
            self.assertAlmostEqual(metadata["fps"], 60000 / 1001)
            self.assertEqual(metadata["download_status"], "completed")
            self.assertEqual(metadata["download_verification"]["association"], "sha256")
            self.assertEqual(metadata["download_verification"]["lookup_source"], lookup.reason)
            self.assertEqual(probe.call_args.args[0][-1], row["download_path"])

    def test_completed_vidbee_source_rejects_wrong_hash_or_unlinked_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            row, task, lookup, client = self.source_fixture(root)
            probe_data = {"streams": [{"codec_name": "h264", "avg_frame_rate": "60/1"}]}
            task.sha256 = "wrong-content-hash"
            with patch("app.integrations.legacy.collector_worker.subprocess.run", return_value=SimpleNamespace(stdout=json.dumps(probe_data))):
                result = collector_result({}, row, root, root / "collector-result.json", vidbee_client=client)
            self.assertEqual(result["error_code"], "SOURCE_NOT_VERIFIED")
            self.assertIsNone(result["source_asset_metadata"])
            task.sha256, row["download_source_sha256"] = "", ""
            with self.assertRaisesRegex(ValueError, "cannot be linked"):
                source_metadata(row, root, client)

    def test_legacy_none_hash_requires_completed_same_file_and_records_actual_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            row, task, lookup, client = self.source_fixture(root)
            original_digest = row["download_source_sha256"]
            row["download_source_sha256"], task.sha256 = "None", "None"
            task.file_path = row["download_path"]
            probe_data = {"streams": [{"codec_name": "h264", "width": 1080, "height": 1920,
                "avg_frame_rate": "30/1", "r_frame_rate": "30/1", "nb_frames": "60", "duration": "2"}]}
            with patch("app.integrations.legacy.collector_worker.subprocess.run", return_value=SimpleNamespace(stdout=json.dumps(probe_data))):
                metadata = source_metadata(row, root, client)
            self.assertEqual(row["download_source_sha256"], "None")
            self.assertEqual(metadata["sha256"], original_digest)
            self.assertEqual(metadata["download_verification"]["association"], "same_file")
            self.assertEqual(metadata["download_verification"]["legacy_sha256"], "missing_none_sentinel")
            self.assertFalse(metadata["download_verification"]["vidbee_sha256_provided"])
            self.assertEqual(metadata["download_verification"]["vidbee_sha256"], "missing_none_sentinel")
            task.file_path = str(root / "downloads/unrelated.mp4")
            with self.assertRaisesRegex(ValueError, "cannot be linked"):
                source_metadata(row, root, client)
            task.file_path = row["download_path"]
            task.sha256 = "nonempty-wrong-sha"
            with patch("app.integrations.legacy.collector_worker.subprocess.run", return_value=SimpleNamespace(stdout=json.dumps(probe_data))), \
                    self.assertRaisesRegex(ValueError, "verified VidBee task"):
                source_metadata(row, root, client)
            task.sha256, row["download_source_sha256"] = None, "nonempty-wrong-sha"
            with patch("app.integrations.legacy.collector_worker.subprocess.run", return_value=SimpleNamespace(stdout=json.dumps(probe_data))), \
                    self.assertRaisesRegex(ValueError, "legacy source SHA256"):
                source_metadata(row, root, client)

    def test_missing_completed_source_is_failed_asset_not_review(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            row, task, lookup, client = self.source_fixture(root)
            Path(row["download_path"]).unlink()
            result = collector_result({}, row, root, root / "collector-result.json", vidbee_client=client)
            self.assertEqual(result["status"], "FAILED")
            self.assertEqual(result["error_code"], "OUTPUT_NOT_FOUND")
            self.assertIsNone(result["source_asset_metadata"])

    def test_source_without_vidbee_task_cannot_claim_verified_download(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            row, task, lookup, client = self.source_fixture(root)
            row["download_source_type"], row["vidbee_task_id"] = "page_playback", ""
            with patch("app.integrations.legacy.collector_worker.subprocess.run") as probe:
                result = collector_result({}, row, root, root / "collector-result.json", vidbee_client=client)
            self.assertEqual(result["error_code"], "SOURCE_NOT_VERIFIED")
            self.assertIsNone(result["source_asset_metadata"])
            probe.assert_not_called()

    def test_cached_completed_result_rechecks_vidbee_without_running_pipeline(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(sys, "path", sys.path.copy()):
            root = Path(temporary).resolve()
            row, task, lookup, client = self.source_fixture(root)
            task.status = "queued"
            context = {"task_id": "task", "context_id": "context-one"}
            raw_path = root / "previous-run" / "collector-legacy-row.json"
            raw_path.parent.mkdir()
            write_json(raw_path, row)
            cached = {"status": "COMPLETED", "video_id": "123", "decision": "accepted", "download_status": "completed",
                      "legacy_result_reference": {"raw_result_path": str(raw_path)}}
            identity = hashlib.sha256(json.dumps(context, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            write_json(root / "collector-state.json", {"identity": identity, "status": "finished", "legacy_processing_entered": True, "result": cached})
            config_module, vidbee_module = ModuleType("src.config"), ModuleType("src.vidbee_handoff")
            config_module.load_config = lambda path: {"vidbee_automation_timeout_s": 1}
            config_module.resolve_paths = lambda root, config: config
            vidbee_module.VidBeeClient = lambda *_args: client
            with patch.dict(sys.modules, {"src.config": config_module, "src.vidbee_handoff": vidbee_module}):
                result = execute(context, root, root, root / "collector-result.json")
            self.assertEqual(result["status"], "FAILED")
            self.assertEqual(result["error_code"], "SOURCE_NOT_VERIFIED")
            self.assertIsNone(result["source_asset_metadata"])
            self.assertFalse((root / ".pipeline.lock").exists())

    def test_finished_unknown_external_request_with_queued_download_stays_frozen(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            context = {"task_id": "task", "context_id": "context-one"}
            cached = collector_result(context, {"video_id": "123", "vidbee_task_status": "queued"}, root, root / "collector-result.json",
                                      error_code="UNKNOWN_EXTERNAL_REQUEST", error_message="unknown")
            write_json(root / "collector-state.json", {"status": "finished", "legacy_processing_entered": True, "result": cached})
            result = execute(context, root / "nonexistent-legacy", root, root / "collector-result.json")
            self.assertEqual(result, cached)
            self.assertFalse(result["retryable"])
            self.assertFalse((root / "nonexistent-legacy").exists())

    def test_dry_run_marks_missing_required_api_key_without_business_execution(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {}, clear=True), patch("urllib.request.urlopen", side_effect=OSError):
            root = Path(temporary).resolve()
            (root / "src").mkdir()
            for name in ("pipeline", "config", "browser", "tiktok_search", "metrics", "video_analysis", "vidbee_handoff"):
                (root / "src" / f"{name}.py").write_text("def process_hit(self, browser, spec, hit, checkpoint): pass\n")
            (root / "config.yaml").write_text("product_match_api_enabled: true\nproduct_match_api_key_env: TIKTOK_AUTO_PRODUCT_VISION_API_KEY\n")
            image = root / "reference.png"
            image.write_bytes(b"test")
            result = LegacyCollectorAdapter(root, root).dry_run({"task_id": "one", "project_id": "project", "product_images": [str(image)]})
            check = next(item for item in result["checks"] if item["name"] == "legacy_api_key_presence")
            self.assertFalse(check["ok"])
            self.assertEqual(check["configured"], {"TIKTOK_AUTO_PRODUCT_VISION_API_KEY": False})
            self.assertEqual(result["status"], "MISSING")
            self.assertFalse(result["business_executed"])
            self.assertEqual(result["paid_requests"], 0)
            self.assertFalse((root / ".pipeline.lock").exists())

    def test_pre_submission_failure_retries_then_one_hit_preserves_checkpoint_and_caches_decision(self):
        calls = []

        class Pipeline:
            instance = None

            def __init__(self, *_args):
                Pipeline.instance = self
                self.config = {"search_max_results": 0, "vidbee_wait_timeout_s": 0, "vidbee_poll_interval_s": 0.2}
                self.logger = None
                self.vidbee = None
                self.old = {"checkpoint_key": "older:2", "video_id": "2", "keyword": "another", "decision": "rejected"}

            def _load_checkpoint(self, fill_authors):
                self.assert_fill_authors = fill_authors
                return {"older:2": dict(self.old)}

            def _checkpoint_key(self, keyword, video_id, context_id):
                return context_id + ":" + video_id

            def _base_row(self, spec, hit):
                return {"checkpoint_key": self._checkpoint_key(spec.keyword, hit.video_id, spec.context_id),
                        "video_id": hit.video_id, "url": hit.url, "country": spec.country, "keyword": spec.keyword, "context_id": spec.context_id}

            def process_hit(self, browser, spec, hit, checkpoint):
                calls.append(hit.video_id)
                return {**self._base_row(spec, hit), "likes": 2000, "views": 100000,
                        "metric_source": "TikTok-page-data", "metric_evidence": "video_id=1; stats",
                        "decision": "manual_review", "reason": "semantic_uncertain"}

            def _write_outputs(self):
                self.saved = list(self.rows)

            def run(self, *_args):
                raise AssertionError("full legacy pipeline is forbidden")

        class Browser:
            failures_remaining = 1

            def __init__(self, config, *_args):
                self.config = config

            def connect(self):
                if Browser.failures_remaining:
                    Browser.failures_remaining -= 1
                    raise OSError("isolated connection failure before external submission")
                return self

            def ensure_tiktok_search_login(self):
                pass

            def close(self):
                pass

        def spec(keyword, references, fingerprint, image_error, country, source_row, source_document, context_id):
            return SimpleNamespace(keyword=keyword, country=country, context_id=context_id)

        def search(browser, keyword):
            self.assertEqual(browser.config["search_max_results"], 0)
            return [SimpleNamespace(url="https://www.tiktok.com/@one/video/1", video_id="1")]

        modules = {}
        for name, values in {
            "src.browser": {"BrowserClient": Browser, "LoginRequired": type("LoginRequired", (Exception,), {}), "VerificationRequired": type("VerificationRequired", (Exception,), {})},
            "src.excel_input": {"KeywordSpec": spec}, "src.pipeline": {"Pipeline": Pipeline},
            "src.tiktok_search": {"SearchHit": lambda url, identifier: SimpleNamespace(url=url, video_id=identifier), "search_keyword": search, "video_id": lambda url: "1"},
            "src.tencent_docs": {"TencentDocsLoginRequired": type("TencentDocsLoginRequired", (Exception,), {})},
        }.items():
            module = ModuleType(name)
            module.__dict__.update(values)
            modules[name] = module
        with tempfile.TemporaryDirectory() as temporary, patch.dict(sys.modules, modules), patch.object(sys, "path", sys.path.copy()):
            root = Path(temporary).resolve()
            reference = root / "reference.png"
            reference.write_bytes(b"unit-test-only-image")
            context = {"task_id": "task", "country": "墨西哥", "keyword": "one", "context_id": "context-one", "product_images": [str(reference)]}
            result_paths = [root / name / "collector-result.json" for name in ("failed-run", "retry-run", "cached-run")]
            for path in result_paths:
                path.parent.mkdir()
            state_path = root / "collector-state.json"
            failed = execute(context, root, root, result_paths[0], state_path)
            self.assertEqual(failed["status"], "FAILED")
            self.assertEqual(failed["error_code"], "COLLECTOR_ERROR")
            self.assertEqual(calls, [])
            self.assertFalse(json.loads((root / "collector-state.json").read_text())["legacy_processing_entered"])
            result = execute(context, root, root, result_paths[1], state_path)
            self.assertEqual(calls, ["1"])
            self.assertFalse(Pipeline.instance.assert_fill_authors)
            self.assertEqual(len(Pipeline.instance.saved), 2)
            self.assertEqual(Pipeline.instance.saved[0], Pipeline.instance.old)
            self.assertEqual(result["video_id"], "1")
            cached = execute(context, root, root, result_paths[2], state_path)
            provenance_fields = {"legacy_result_reference", "metrics_provenance", "reference_provenance", "semantic_provenance"}
            self.assertEqual({key: value for key, value in cached.items() if key not in provenance_fields},
                             {key: value for key, value in result.items() if key not in provenance_fields})
            self.assertEqual(result["metrics_provenance"]["mode"], "unknown", "直接返回数值不能冒充实际 _set_metrics 读取")
            self.assertEqual(cached["metrics_provenance"]["mode"], "cached")
            self.assertIsNone(cached["metrics_provenance"]["observed_at"])
            self.assertEqual(cached["metrics_provenance"]["original_checkpoint"]["raw_result_path"], str(result_paths[1].with_name("collector-legacy-row.json")))
            self.assertTrue(result_paths[2].with_name("collector-legacy-row.json").is_file())
            self.assertEqual(calls, ["1"])
            photo_context = {**context, "task_id": "photo-task", "context_id": "photo-context", "video_id": "1",
                             "video_url": "https://www.tiktok.com/@one/photo/1"}
            photo_path = root / "photo-run/collector-result.json"
            photo_path.parent.mkdir()
            photo = execute(photo_context, root, root, photo_path, root / "photo-state.json")
            self.assertEqual(photo["decision"], "rejected")
            self.assertEqual(photo["decision_reason"], "unsupported_photo_source")
            self.assertIsNone(photo["views"])
            self.assertEqual(photo["download_status"], "not_started")
            self.assertEqual(calls, ["1"], "图片帖不能读取视频指标、调用付费分析或提交下载")


if __name__ == "__main__":
    unittest.main()
