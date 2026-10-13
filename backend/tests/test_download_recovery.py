import fcntl
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from app.config import Settings
from app.domain import find, now
from app.integrations.collector_service import CollectorService
from app.integrations.asset_registry import file_hash
from app.integrations.contracts import CollectorResult
from app.integrations.legacy.collector_adapter import LegacyCollectorAdapter
from app.integrations.legacy.collector_worker import SEMANTIC_ENUM_GUIDANCE, execute, verify_complete_download, write_json
from app.integrations.production_service import ProductionService
from app.integrations.production_readiness import STAGE_REQUIREMENTS
from app.integrations.process_runner import ProcessResult
from app.repository import StateRepository


class DownloadRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.storage = self.root / "factory"
        self.storage.mkdir()
        self.source = self.root / "downloads/source.mp4"
        self.source.parent.mkdir()
        self.source.write_bytes(b"isolated-unit-source")
        self.reference = self.storage / "reference.png"
        self.reference.write_bytes(b"isolated-unit-reference")
        self.config = self.root / "config.yaml"
        self.config.write_text("isolated_test_rules: true\n")
        self.context = {"task_id": "unit-task", "project_id": "unit-project", "product_id": "unit-product",
            "country": "MX", "keyword": "unit-product", "product_images": [str(self.reference)],
            "source_document": "isolated-unit", "source_row": 2, "context_id": "unit-context"}
        self.state_path = self.storage / "collector-state.json"
        self.result_path = self.storage / "new-run/collector-result.json"
        self.result_path.parent.mkdir()
        self.calls = []
        self.task = SimpleNamespace(task_id="original-vidbee", status="queued", file_path=str(self.source),
                                    sha256=hashlib.sha256(self.source.read_bytes()).hexdigest(), error="")
        self.metadata = {"path": str(self.source), "verified": True, "download_status": "completed", "duration": 1,
            "width": 640, "height": 360, "fps": 30, "codec": "h264", "sha256": self.task.sha256,
            "file_size": self.source.stat().st_size, "full_decode_verified": True}
        fingerprint = hashlib.sha256(hashlib.sha256(self.reference.read_bytes()).hexdigest().encode()).hexdigest()[:16]
        row = {"country": "MX", "keyword": "unit-product", "context_id": "unit-context", "source_document": "isolated-unit",
            "source_row": 2, "video_id": "original-video", "url": "https://www.tiktok.com/@unit/video/original-video",
            "checkpoint_key": "unit-context:original-video", "likes": 2000, "views": 100000,
            "reference_product_match": "pass", "reference_image_fingerprint": fingerprint,
            "screening_config_fingerprint": "unit-rules", "vidbee_task_id": self.task.task_id,
            "vidbee_task_status": "queued", "decision": "manual_review", "reason": "vidbee_download_pending"}
        self.state = {"identity": hashlib.sha256(json.dumps(self.context, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
            "status": "download_wait", "legacy_processing_entered": True, "row": row,
            "selected_hit": {"url": row["url"], "video_id": row["video_id"]},
            "recovery_evidence": {**{key: row[key] for key in ("country", "keyword", "context_id", "source_document", "source_row", "video_id", "vidbee_task_id", "reference_image_fingerprint", "screening_config_fingerprint")},
                "video_url": row["url"], "config_sha256": hashlib.sha256(self.config.read_bytes()).hexdigest(),
                "completed_stages": ["metrics", "reference_product_match"], "rules_snapshot": {"min_views": 100000}}}
        write_json(self.state_path, self.state)
        owner = self

        class Pipeline:
            def __init__(self, *_):
                self.config = {"vidbee_wait_timeout_s": 0, "vidbee_poll_interval_s": .2, "download_min_file_bytes": 1}
                self._screening_config_fingerprint = "unit-rules"
                self.vidbee = SimpleNamespace(lookup_task=self.lookup)

            def lookup(self, identifier):
                owner.calls.append(("lookup", identifier))
                return SimpleNamespace(found=True, task=owner.task, reason="unit-only")

            def _load_checkpoint(self, **_):
                owner.calls.append(("csv", "missing"))
                return {}

            def process_hit(self, *_):
                raise AssertionError("recovery must never call process_hit")

            def _complete_vidbee_task(self, row, task):
                owner.calls.append(("semantic", task.task_id))
                module = sys.modules["src.pipeline"]
                module.write_download_metadata(owner.source, keyword=row["keyword"])
                module.save_analysis(owner.source.with_suffix(".analysis.json"), {"analysis_status": "completed"})
                return {**row, "download_path": task.file_path, "download_source_type": "vidbee_automation",
                        "download_source_sha256": task.sha256, "analysis_status": "completed",
                        "decision": "accepted", "reason": "all_gates_passed"}

        def forbidden(*_args, **_kwargs):
            raise AssertionError("recovery must never search, open a browser or queue a download")

        def metadata(path, **_):
            if path.is_relative_to(owner.source.parent):
                raise AssertionError("legacy sidecar may not be changed")
            path.with_suffix(".json").write_text(json.dumps({"path": str(path)}))

        modules = {
            "src.browser": {"BrowserClient": forbidden, "LoginRequired": type("LoginRequired", (Exception,), {}),
                            "VerificationRequired": type("VerificationRequired", (Exception,), {})},
            "src.excel_input": {"KeywordSpec": lambda keyword, references, fingerprint, error, country, row, document, context: SimpleNamespace(reference_fingerprint=fingerprint)},
            "src.pipeline": {"Pipeline": Pipeline, "write_download_metadata": metadata,
                             "save_analysis": lambda path, value: path.write_text(json.dumps(value)),
                             "match_product_references": forbidden},
            "src.tiktok_search": {"SearchHit": forbidden, "search_keyword": forbidden, "video_id": forbidden},
            "src.tencent_docs": {"TencentDocsLoginRequired": type("TencentDocsLoginRequired", (Exception,), {})},
        }
        self.modules = {}
        for name, values in modules.items():
            module = ModuleType(name)
            module.__dict__.update(values)
            self.modules[name] = module

    def tearDown(self):
        self.temporary.cleanup()

    def execute(self, *, semantic_retry=False):
        with patch.dict(sys.modules, self.modules), patch.object(sys, "path", sys.path.copy()), \
                patch("app.integrations.legacy.collector_worker.source_metadata", return_value=self.metadata), \
                patch("app.integrations.legacy.collector_worker.verify_complete_download", return_value=self.metadata):
            return execute(self.context, self.root, self.storage, self.result_path, self.state_path, resume_only=not semantic_retry, semantic_retry=semantic_retry)

    def semantic_origin(self, outcome="completed"):
        data = self.state_path.parent / "legacy-work/data"
        report_path = data / "semantic_analysis/original-semantic/analysis.json"
        (report_path.parent / "attempt-001").mkdir(parents=True, exist_ok=True)
        sheet = data / "samples/original-source/contact_sheet.jpg"
        sheet.parent.mkdir(parents=True)
        sheet.write_bytes(b"isolated-contact-sheet")
        frame = sheet.parent / "frame-001.jpg"
        frame.write_bytes(b"isolated-frame")
        write_json(sheet.parent / "sampling.json", {"identity": {"source_sha256": self.metadata["sha256"]},
            "contact_sheet": str(sheet), "frames": [str(frame)]})
        row = {**self.state["row"], "download_path": str(self.source), "download_source_sha256": self.metadata["sha256"],
            "vidbee_task_status": "completed", "analysis_status": "invalid_response", "reason": "analysis_error",
            "analysis_report": str(report_path), "analysis_identity": "original-semantic", "contact_sheet": str(sheet),
            "reference_images": str(self.reference), "product_match": "uncertain", "ai_origin": "uncertain", "cutout_suitability": "uncertain"}
        report = {"analysis_status": "invalid_response", "analysis_identity": row["analysis_identity"], "analysis_attempt": 1}
        write_json(report_path, report)
        write_json(report_path.parent / "state.json", {"status": "invalid_response", "identity": row["analysis_identity"], "attempt": 1, "result": report})
        write_json(report_path.parent / "attempt-001/request.json", {"model": "isolated-model", "endpoint": "https://unit.invalid/chat/completions",
            "metadata": {key: row[key] for key in ("country", "keyword", "context_id", "reference_product_match")},
            "contact_sheet": {"name": sheet.name, "sha256": hashlib.sha256(sheet.read_bytes()).hexdigest()},
            "references": [{"name": self.reference.name, "sha256": hashlib.sha256(self.reference.read_bytes()).hexdigest()}]})
        write_json(report_path.parent / "attempt-001/response.json", {"status": "invalid_response", "error": "semantic response has invalid product_match verdict"})
        self.state.update(status="finished", row=row, result={"status": "WAITING_REVIEW", "decision": "manual_review", "error_code": None},
            download_verification=self.metadata, source_revalidation={"semantic_submission_count": 0})
        write_json(self.state_path, self.state)
        self.task.status = "completed"
        module = ModuleType("src.video_analysis")
        module._api_configuration = lambda config: None
        module._semantic_open = lambda *_args: self.fail("isolated recovery must not submit an HTTP request")
        module._semantic_configuration = lambda config: ("https://unit.invalid/chat/completions", "isolated-model", "unit-key-not-exported")
        module._semantic_reference_paths = lambda metadata: [Path(value) for value in metadata["reference_images"].split("|")]
        module._semantic_identity = lambda sheet, references, row, config, endpoint, model: "new-protocol-semantic" if SEMANTIC_ENUM_GUIDANCE in config.get("semantic_guidance", "") else "original-semantic"
        self.modules[module.__name__] = module
        owner = self
        def analyze(pipeline, received):
            owner.calls.append(("explicit-semantic", received["video_id"]))
            owner.semantic_runtime_config = pipeline.config.copy()
            owner.assertEqual(json.loads(owner.state_path.read_text())["status"], "external_inflight")
            if outcome != "not_submitted":
                directory = data / "semantic_analysis/new-protocol-semantic/attempt-001"
                directory.mkdir(parents=True)
                write_json(directory / "request.json", {"isolated_unit_request": True})
                if outcome not in {"exception", "response_unknown"}:
                    write_json(directory / "response.json", {"status": outcome})
            if outcome == "exception":
                raise RuntimeError("isolated-paid-interruption")
            return {**received, "analysis_status": outcome, "analysis_identity": "new-protocol-semantic",
                "analysis_report": str(data / "semantic_analysis/new-protocol-semantic/analysis.json"),
                "decision": "accepted" if outcome == "completed" else "manual_review",
                "reason": "all_gates_passed" if outcome == "completed" else "analysis_error"}
        self.modules["src.pipeline"].Pipeline._analyze_download = analyze
        return report_path

    def test_explicit_enum_retry_reuses_original_source_once_and_preserves_received_error(self):
        report = self.semantic_origin()
        origin = {str(path): path.read_bytes() for path in report.parent.rglob("*.json")}
        result = self.execute(semantic_retry=True)
        self.assertEqual(result["decision"], "accepted")
        self.assertIsNone(result["error_code"])
        self.assertEqual(self.calls, [("lookup", "original-vidbee"), ("explicit-semantic", "original-video")])
        proof = result["legacy_result_reference"]["semantic_retry"]
        self.assertEqual(proof["semantic_submission_count"], 1)
        self.assertEqual(proof["semantic_submission_limit"], 1)
        self.assertEqual(proof["reference_match_submission_count"], 0)
        self.assertTrue(proof["origin_request_may_have_been_billed"])
        self.assertEqual(proof["verified_source_sha256"], self.metadata["sha256"])
        self.assertFalse(self.semantic_runtime_config["retry_semantic"])
        self.assertIn("likely_real", self.semantic_runtime_config["semantic_guidance"])
        self.assertEqual(self.semantic_runtime_config["download_min_file_bytes"], 1)
        self.assertEqual(origin, {str(path): path.read_bytes() for path in report.parent.rglob("*.json")})
        self.assertNotIn("unit-key-not-exported", json.dumps(result))
        self.assertEqual(self.execute(semantic_retry=True)["error_code"], "RECOVERY_CONFLICT")
        self.assertEqual(self.calls.count(("explicit-semantic", "original-video")), 1)

    def test_semantic_retry_never_runs_from_normal_download_resume(self):
        self.semantic_origin()
        result = self.execute()
        self.assertEqual(result["error_code"], "RECOVERY_CONFLICT")
        self.assertEqual(self.calls, [])

    def test_semantic_retry_rejects_changed_source_enum_evidence_context_and_images_before_paid_submit(self):
        report = self.semantic_origin()
        original = {str(path): path.read_bytes() for path in report.parent.rglob("*.json")}
        for change in ("source", "response", "context", "image", "rules", "sheet", "decode"):
            with self.subTest(change=change):
                self.calls.clear()
                write_json(self.state_path, self.state)
                for path, value in original.items():
                    Path(path).write_bytes(value)
                self.config.write_text("isolated_test_rules: true\n")
                self.reference.write_bytes(b"isolated-unit-reference")
                verification = self.metadata.copy()
                if change == "source":
                    verification["sha256"] = "changed-actual-source"
                elif change == "decode":
                    verification["full_decode_verified"] = False
                elif change == "response":
                    write_json(report.parent / "attempt-001/response.json", {"status": "response_unknown"})
                elif change == "context":
                    write_json(report.parent / "attempt-001/request.json", {"metadata": {"country": "FR"}})
                elif change == "image":
                    self.reference.write_bytes(b"changed-reference")
                elif change == "rules":
                    self.config.write_text("changed rules\n")
                elif change == "sheet":
                    request_path = report.parent / "attempt-001/request.json"
                    request = json.loads(request_path.read_text())
                    request["contact_sheet"]["sha256"] = "changed-sheet"
                    write_json(request_path, request)
                with patch("app.integrations.legacy.collector_worker.verify_complete_download", return_value=verification):
                    # Bypass the fixture's unchanged-source verification patch.
                    from app.integrations.legacy.collector_worker import execute
                    with patch.dict(sys.modules, self.modules), patch.object(sys, "path", sys.path.copy()), \
                            patch("app.integrations.legacy.collector_worker.source_metadata", return_value=self.metadata):
                        result = execute(self.context, self.root, self.storage, self.result_path, self.state_path, semantic_retry=True)
                self.assertEqual(result["error_code"], "RECOVERY_CONFLICT")
                self.assertFalse(any(call[0] == "explicit-semantic" for call in self.calls))

    def test_semantic_retry_unknown_or_interruption_freezes_the_new_protocol_without_losing_origin(self):
        for outcome in ("response_unknown", "exception"):
            with self.subTest(outcome=outcome):
                self.calls.clear()
                # Restore a separate immutable origin for each isolated failure.
                if self.state_path.parent.joinpath("legacy-work").exists():
                    shutil.rmtree(self.state_path.parent / "legacy-work")
                self.semantic_origin(outcome)
                result = self.execute(semantic_retry=True)
                self.assertEqual(result["error_code"], "UNKNOWN_EXTERNAL_REQUEST")
                saved = json.loads(self.state_path.read_text())
                self.assertEqual(saved["row"]["video_id"], "original-video")
                self.assertEqual(saved["semantic_retry"]["origin_identity"], "original-semantic")
                self.assertEqual(self.execute(semantic_retry=True)["error_code"], "UNKNOWN_EXTERNAL_REQUEST")
                self.assertEqual(self.calls.count(("explicit-semantic", "original-video")), 1)

    def test_semantic_retry_invalid_new_response_remains_uncertain_and_cannot_submit_again(self):
        self.semantic_origin("invalid_response")
        result = self.execute(semantic_retry=True)
        self.assertEqual(result["decision"], "manual_review")
        self.assertEqual(result["product_match"], "uncertain")
        self.assertEqual(self.execute(semantic_retry=True)["error_code"], "RECOVERY_CONFLICT")
        self.assertEqual(self.calls.count(("explicit-semantic", "original-video")), 1)

    def test_semantic_retry_accepts_only_content_identical_authorized_legacy_reference_alias(self):
        report = self.semantic_origin()
        alias = self.root / "data/product_references" / self.reference.name
        alias.parent.mkdir(parents=True)
        alias.write_bytes(self.reference.read_bytes())
        self.state["row"]["reference_images"] = str(alias)
        write_json(self.state_path, self.state)
        result = self.execute(semantic_retry=True)
        self.assertIsNone(result["error_code"])
        binding = result["legacy_result_reference"]["semantic_retry"]["reference_path_bindings"][0]
        self.assertEqual(binding["origin_path"], str(alias))
        self.assertEqual(binding["context_path"], str(self.reference))
        self.assertEqual(binding["sha256"], hashlib.sha256(self.reference.read_bytes()).hexdigest())
        self.assertEqual(self.state["row"]["analysis_report"], str(report))

    def test_semantic_retry_rejects_same_hash_image_path_outside_authorized_reference_storage(self):
        self.semantic_origin()
        alias = self.root / self.reference.name
        alias.write_bytes(self.reference.read_bytes())
        self.state["row"]["reference_images"] = str(alias)
        write_json(self.state_path, self.state)
        self.assertEqual(self.execute(semantic_retry=True)["error_code"], "RECOVERY_CONFLICT")
        self.assertFalse(any(call[0] == "explicit-semantic" for call in self.calls))

    def test_new_protocol_directory_prevents_paid_replay_even_without_top_level_retry_marker(self):
        self.semantic_origin()
        (self.state_path.parent / "legacy-work/data/semantic_analysis/new-protocol-semantic").mkdir()
        self.assertEqual(self.execute(semantic_retry=True)["error_code"], "RECOVERY_CONFLICT")
        self.assertFalse(any(call[0] == "explicit-semantic" for call in self.calls))

    def test_unsubmitted_semantic_configuration_does_not_claim_a_paid_request(self):
        self.semantic_origin("not_submitted")
        result = self.execute(semantic_retry=True)
        proof = result["legacy_result_reference"]["semantic_retry"]
        self.assertEqual(proof["semantic_submission_count"], 0)
        self.assertFalse(proof["response_received"])
        self.assertEqual(result["decision"], "manual_review")
        self.assertEqual(self.execute(semantic_retry=True)["error_code"], "RECOVERY_CONFLICT")

    def test_adapter_explicit_semantic_flag_does_not_use_batch_cli_or_download_resume(self):
        commands = []
        class Runner:
            def run(self, command, **options):
                commands.append(command)
                result = Path(command[command.index("--result") + 1])
                write_json(result, {"decision": "manual_review", "decision_reason": "isolated-adapter-result"})
                return ProcessResult(pid=12345, exit_code=0, stdout="", stderr="", log_path="isolated.log", duration_s=0)
        adapter = LegacyCollectorAdapter(self.root, self.storage, runner=Runner())
        result = asyncio.run(adapter.collect_one(self.context, run_id="isolated-semantic-run", semantic_retry=True))
        self.assertIn("--retry-semantic-once", commands[0])
        self.assertNotIn("--resume-download", commands[0])
        self.assertFalse(any(Path(value).name == "run.py" for value in commands[0]))
        self.assertEqual(result["decision"], "manual_review")

    def test_csv_missing_queued_then_completed_keeps_original_ids_and_one_analysis(self):
        first = self.execute()
        self.assertEqual(first["error_code"], "DOWNLOAD_PENDING")
        self.assertEqual(first["legacy_vidbee_task_id"], "original-vidbee")
        self.task.status = "completed"
        second = self.execute()
        self.assertEqual(second["decision"], "accepted")
        self.assertIsNone(second["error_code"])
        self.assertEqual(second["legacy_result_reference"]["download_recovery"]["search_count"], 0)
        self.assertEqual(second["legacy_result_reference"]["download_recovery"]["download_submission_count"], 0)
        self.assertEqual(self.calls.count(("semantic", "original-vidbee")), 1)
        self.assertFalse(self.source.with_suffix(".json").exists())
        self.assertFalse(self.source.with_suffix(".analysis.json").exists())
        self.assertEqual(json.loads((self.result_path.parent / "source.json").read_text())["path"], str(self.source))
        # A duplicate recovery callback cannot execute a second semantic analysis.
        duplicate = self.execute()
        self.assertEqual(duplicate["error_code"], "RECOVERY_CONFLICT")
        self.assertEqual(self.calls.count(("semantic", "original-vidbee")), 1)

    def test_explicit_missing_sha_revalidation_never_repeats_received_semantic_request(self):
        self.task.status = "completed"
        row = {**self.state["row"], "download_path": str(self.source), "download_source_sha256": "None",
            "analysis_status": "invalid_response", "reason": "analysis_error", "product_match": "uncertain", "ai_origin": "uncertain",
            "cutout_suitability": "uncertain"}
        self.state.update(status="finished", row=row, result={"status": "FAILED", "decision": "manual_review",
            "error_code": "COLLECTOR_ERROR", "error_message": "ValueError: legacy source SHA256 does not match the completed download",
            "legacy_result_reference": {"raw_result_path": "origin-failed-row.json"}})
        write_json(self.state_path, self.state)
        result = self.execute()
        self.assertIsNone(result["error_code"])
        self.assertEqual(result["decision"], "manual_review")
        self.assertEqual(result["decision_reason"], "analysis_error")
        self.assertEqual(result["status"], "WAITING_REVIEW")
        self.assertTrue(result["source_asset_metadata"]["full_decode_verified"])
        self.assertEqual(self.calls, [("lookup", "original-vidbee")])
        self.assertEqual(result["legacy_result_reference"]["source_revalidation"]["semantic_submission_count"], 0)
        self.assertEqual(json.loads(self.state_path.read_text())["row"]["download_source_sha256"], "None")
        self.assertFalse(self.source.with_suffix(".analysis.json").exists())

    def test_missing_sha_revalidation_still_requires_original_context_rules_and_images(self):
        self.task.status = "completed"
        self.state.update(status="finished", row={**self.state["row"], "download_path": str(self.source),
            "download_source_sha256": "None", "analysis_status": "invalid_response", "reason": "analysis_error"},
            result={"error_code": "COLLECTOR_ERROR", "error_message": "ValueError: legacy source SHA256 does not match the completed download"})
        self.state["recovery_evidence"]["config_sha256"] = "different-rules"
        write_json(self.state_path, self.state)
        result = self.execute()
        self.assertEqual(result["error_code"], "RECOVERY_CONFLICT")
        self.assertEqual(self.calls, [])

    def test_changed_rules_image_context_or_external_id_never_fall_back_to_search(self):
        for change in ("rules", "image", "context", "task_id", "evidence"):
            with self.subTest(change=change):
                write_json(self.state_path, self.state)
                self.config.write_text("isolated_test_rules: true\n")
                self.reference.write_bytes(b"isolated-unit-reference")
                context = dict(self.context)
                if change == "rules":
                    self.config.write_text("isolated_test_rules: changed\n")
                elif change == "image":
                    self.reference.write_bytes(b"changed-unit-reference")
                elif change == "context":
                    state = json.loads(self.state_path.read_text())
                    state["row"]["country"] = "FR"
                    write_json(self.state_path, state)
                elif change == "task_id":
                    state = json.loads(self.state_path.read_text())
                    state["row"]["vidbee_task_id"] = "replacement-not-allowed"
                    write_json(self.state_path, state)
                else:
                    state = json.loads(self.state_path.read_text())
                    state["recovery_evidence"] = {}
                    write_json(self.state_path, state)
                result = self.execute()
                self.assertEqual(result["error_code"], "RECOVERY_CONFLICT")
                self.assertEqual(self.calls, [])

    def test_unknown_semantic_request_is_frozen_before_legacy_import(self):
        self.state.update(status="external_inflight", operation="semantic_analysis")
        write_json(self.state_path, self.state)
        result = self.execute()
        self.assertEqual(result["error_code"], "UNKNOWN_EXTERNAL_REQUEST")
        self.assertFalse(result["retryable"])
        self.assertEqual(self.calls, [])

    def test_task_lease_excludes_second_worker(self):
        with self.state_path.with_suffix(".lease").open("a") as lease:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.execute()
        self.assertEqual(result["error_code"], "RECOVERY_BUSY")
        self.assertEqual(self.calls, [])

    def test_checkpoint_distinguishes_download_wait_from_paid_unknown(self):
        adapter = LegacyCollectorAdapter(self.root, self.storage)
        paths = adapter._paths(self.context)
        checkpoint = Path(paths["metadata"]) / "collector-state.json"
        checkpoint.parent.mkdir(parents=True)
        write_json(checkpoint, self.state)
        self.assertEqual(adapter.checkpoint(self.context)["phase"], "DOWNLOAD_PENDING")
        self.state.update(status="external_inflight", operation="semantic_analysis")
        write_json(checkpoint, self.state)
        self.assertEqual(adapter.checkpoint(self.context)["phase"], "UNKNOWN_EXTERNAL_REQUEST")

    def test_checkpoint_offers_source_revalidation_only_for_known_missing_sha_failure(self):
        adapter = LegacyCollectorAdapter(self.root, self.storage)
        checkpoint = Path(adapter._paths(self.context)["metadata"]) / "collector-state.json"
        checkpoint.parent.mkdir(parents=True)
        self.state.update(status="finished", row={**self.state["row"], "download_path": str(self.source),
            "download_source_sha256": "None", "analysis_status": "invalid_response", "reason": "analysis_error"},
            result={"error_code": "COLLECTOR_ERROR", "error_message": "ValueError: legacy source SHA256 does not match the completed download"})
        write_json(checkpoint, self.state)
        self.assertEqual(adapter.checkpoint(self.context)["phase"], "SOURCE_VERIFICATION_READY")
        self.state["row"]["analysis_status"] = "response_unknown"
        write_json(checkpoint, self.state)
        self.assertEqual(adapter.checkpoint(self.context)["phase"], "CONFLICT")
        self.state["result"]["error_code"] = "UNKNOWN_EXTERNAL_REQUEST"
        write_json(checkpoint, self.state)
        self.assertEqual(adapter.checkpoint(self.context)["phase"], "UNKNOWN_EXTERNAL_REQUEST")

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "local media tools unavailable")
    def test_complete_file_requires_full_real_decode_and_correct_hash(self):
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=size=64x64:rate=5:duration=1",
                        "-c:v", "libx264", str(self.source)], check=True, capture_output=True)
        self.task.status, self.task.sha256 = "completed", hashlib.sha256(self.source.read_bytes()).hexdigest()
        row = {**self.state["row"], "download_path": str(self.source), "download_source_sha256": self.task.sha256}
        pipeline = SimpleNamespace(config={"download_min_file_bytes": 1}, vidbee=SimpleNamespace(
            lookup_task=lambda _: SimpleNamespace(found=True, task=self.task, reason="unit-only")))
        metadata = verify_complete_download(pipeline, row, self.task, self.root)
        self.assertTrue(metadata["full_decode_verified"])
        self.assertEqual(metadata["frame_count"], 5)
        self.assertEqual(metadata["sha256"], self.task.sha256)
        row["download_source_sha256"] = "wrong-hash"
        with self.assertRaises(ValueError):
            verify_complete_download(pipeline, row, self.task, self.root)


class DownloadRecoveryServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.settings = Settings(storage_root=self.root / "storage", legacy_tiktok_auto_root=self.root,
                                 database_url="sqlite:///" + str(self.root / "unit.db"), integration_dry_run=False)
        self.repository = StateRepository(self.settings.database_url, Path(__file__).parents[1] / "app/seed.json")
        self.rules_patch = patch("app.integrations.collector_service.read_production_rules", return_value={"sha256": "unit-rules", "rules": {
            "min_views": 100000, "min_like_view_ratio": .012, "product_match_min_confidence": .85}})
        self.rules_patch.start()
        self.calls = []
        owner = self

        class Adapter(LegacyCollectorAdapter):
            async def collect_one(self, context, **options):
                owner.calls.append(options.get("resume_download"))
                if not options.get("resume_download"):
                    raise AssertionError("formal recovery must use the download-only worker")
                return {**context, "video_id": "903", "video_url": "https://www.tiktok.com/@unit/video/903",
                        "views": 100000, "likes": 2000, "decision": "rejected", "decision_reason": "unit-original-semantic-rejected",
                        "legacy_vidbee_task_id": "original-vidbee", "legacy_csv_path": "/legacy/output/candidates.csv", "legacy_result_reference": {}}

        self.adapter = Adapter(self.root, self.settings.storage_root)
        async def broadcast(_event):
            pass
        self.hub = SimpleNamespace(broadcast=broadcast)
        self.collector = CollectorService(self.repository, self.hub, self.settings, self.adapter)
        created = await self.collector.create({"country": "MX", "keyword": "isolated-unit", "product_images": [str(self.root / "unit.png")],
            "video_id": "903", "video_url": "https://www.tiktok.com/@unit/video/903"})
        self.task_id = created["task_id"]
        context = self.collector.context(self.task_id)
        path = Path(self.adapter._paths(context)["metadata"]) / "collector-state.json"
        path.parent.mkdir(parents=True)
        state = {"identity": hashlib.sha256(json.dumps(context, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
                 "status": "download_wait", "legacy_processing_entered": True,
                 "row": {"context_id": context["context_id"], "video_id": "903", "vidbee_task_id": "original-vidbee", "reason": "vidbee_download_pending"},
                 "recovery_evidence": {"vidbee_task_id": "original-vidbee", "config_sha256": "unit-config-sha", "reference_image_fingerprint": "unit-ref", "screening_config_fingerprint": "unit-rules"}}
        write_json(path, state)
        self.checkpoint_path = path
        self.repository.mutate(lambda current: find(current, "tasks", self.task_id).update(status="WAITING_REVIEW", collector_result={
            "decision": "manual_review", "decision_reason": "vidbee_download_pending", "error_code": "DOWNLOAD_PENDING", "legacy_vidbee_task_id": "original-vidbee"}))

    async def asyncTearDown(self):
        await self.collector.shutdown()
        self.rules_patch.stop()
        self.repository.engine.dispose()
        self.temporary.cleanup()

    async def test_explicit_resume_creates_new_run_preserves_binding_and_rejects_duplicate(self):
        with self.assertRaises(HTTPException):
            await self.collector.queue(self.task_id, retry=True)
        queued = await self.collector.queue(self.task_id, resume_download=True)
        with self.assertRaises(HTTPException):
            await self.collector.queue(self.task_id, resume_download=True)
        await self.collector.run(self.task_id, queued["run_id"])
        state = self.repository.state()
        self.assertEqual(self.calls, [True])
        self.assertEqual(find(state, "tasks", self.task_id)["collector_result"]["decision"], "rejected")
        self.assertEqual(next(row for row in state["legacy_bindings"] if row["task_id"] == self.task_id)["download_task_id"], "original-vidbee")
        self.assertTrue(find(state, "runs", queued["run_id"])["idempotency_key"].startswith(self.task_id + ":download-resume:"))
        with self.assertRaises(HTTPException):
            await self.collector.queue(self.task_id, resume_download=True)
        self.assertEqual(len([run for run in self.repository.state()["runs"] if run["task_id"] == self.task_id]), 1)

    async def test_restart_download_wait_remains_explicitly_resumable_without_auto_start(self):
        self.repository.mutate(lambda state: state["runs"].append({"id": "interrupted-unit", "task_id": self.task_id,
            "provider": "LegacyCollectorAdapter", "stage": "collector", "status": "running", "progress": 60, "logs": ["original-log"], "created_at": now()}))
        await self.collector.recover()
        run = find(self.repository.state(), "runs", "interrupted-unit")
        self.assertEqual(run["error_code"], "DOWNLOAD_PENDING")
        self.assertIn("original-log", run["logs"])
        self.assertTrue(self.collector.can_resume_download(self.task_id))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.collector.jobs, {})

    async def test_formal_production_resume_keeps_the_original_batch(self):
        self.settings.collector_unattended_enabled = False
        class Ready:
            async def check(self, **_):
                return {"ready": True, "checks": [{"name": name, "status": "READY"} for name in set().union(*STAGE_REQUIREMENTS.values())]}

        production = ProductionService(self.repository, self.hub, self.settings, self.collector)
        production.readiness = Ready()
        batch = await production.start([self.task_id])
        batch_id = batch["batch_id"]
        def interrupted(state):
            current = find(state, "batches", batch_id)
            current.update(status="COMPLETED_WITH_ERRORS")
            current["items"][0]["status"] = "WAITING_REVIEW"
            find(state, "tasks", self.task_id).update(status="WAITING_REVIEW")
        self.repository.mutate(interrupted)
        response = await production.control("resume")
        self.assertEqual(response["batch_id"], batch_id)
        await asyncio.wait_for(production.jobs[batch_id], 3)
        self.assertEqual(len(self.repository.state()["batches"]), 1)
        self.assertEqual(self.calls, [True])
        self.assertEqual(find(self.repository.state(), "tasks", self.task_id)["status"], "REJECTED")
        await production.shutdown()

    async def test_http_resume_action_returns_202_and_uses_original_context(self):
        from app.main import create_app
        with TestClient(create_app(self.settings, collector_adapter=self.adapter)) as client:
            response = client.post("/api/actions", json={"action": "resume_collection", "entity_id": self.task_id})
            self.assertEqual(response.status_code, 202, response.text)
            self.assertEqual(response.json()["task_id"], self.task_id)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                task = client.get("/api/tasks/" + self.task_id).json()["task"]
                if task["status"] != "COLLECTING":
                    break
                time.sleep(.01)
            self.assertEqual(task["collector_result"]["legacy_vidbee_task_id"], "original-vidbee")
            self.assertEqual(self.calls, [True])
            self.assertEqual(client.post("/api/actions", json={"action": "resume_collection", "entity_id": self.task_id}).status_code, 409)

    def received_invalid_semantic(self):
        state = json.loads(self.checkpoint_path.read_text())
        state.update(status="finished", result={"error_code": None},
            download_verification={"full_decode_verified": True, "sha256": "isolated-source-sha"})
        state["row"].update(reference_product_match="pass", analysis_status="invalid_response", reason="analysis_error",
            vidbee_task_status="completed", analysis_report="isolated-invalid-report", contact_sheet="isolated-sheet")
        write_json(self.checkpoint_path, state)
        self.repository.mutate(lambda current: find(current, "tasks", self.task_id).update(status="WAITING_REVIEW", collector_result={
            "decision": "manual_review", "decision_reason": "analysis_error", "error_code": None, "legacy_vidbee_task_id": "original-vidbee"}))
        owner = self
        async def collect(context, **options):
            owner.calls.append(options)
            owner.assertTrue(options.get("semantic_retry"))
            owner.assertFalse(options.get("resume_download"))
            current = json.loads(owner.checkpoint_path.read_text())
            current["semantic_retry"] = {"protocol": "factory-semantic-enums-v1", "status": "completed"}
            current["row"]["analysis_status"] = "completed"
            write_json(owner.checkpoint_path, current)
            return {**context, "video_id": "903", "video_url": "https://www.tiktok.com/@unit/video/903",
                "views": 100000, "likes": 2000, "reference_product_match": "pass", "product_match": "pass", "ai_origin": "likely_real",
                "cutout_suitability": "pass", "decision": "manual_review", "decision_reason": "watermark_unverified",
                "legacy_vidbee_task_id": "original-vidbee", "legacy_csv_path": "/legacy/output/candidates.csv", "legacy_result_reference": {}}
        self.adapter.collect_one = collect

    async def test_explicit_semantic_retry_creates_fresh_run_and_never_bypasses_watermark_review(self):
        self.settings.collector_unattended_enabled = False
        self.received_invalid_semantic()
        self.repository.mutate(lambda state: state["runs"].append({"id": "origin-invalid-response-run", "task_id": self.task_id,
            "provider": "LegacyCollectorAdapter", "stage": "collector", "status": "failed", "progress": 80,
            "logs": ["origin-invalid-response-preserved"], "created_at": now(), "error_code": "COLLECTOR_ERROR"}))
        self.assertTrue(self.collector.can_retry_semantic(self.task_id))
        self.assertFalse(self.collector.can_resume_download(self.task_id))
        with self.assertRaises(HTTPException):
            await self.collector.queue(self.task_id, retry=True)
        queued = await self.collector.queue(self.task_id, semantic_retry=True)
        with self.assertRaises(HTTPException):
            await self.collector.queue(self.task_id, semantic_retry=True)
        await self.collector.run(self.task_id, queued["run_id"])
        current = self.repository.state()
        self.assertEqual(find(current, "tasks", self.task_id)["status"], "WAITING_REVIEW")
        self.assertEqual(find(current, "tasks", self.task_id)["collector_result"]["decision_reason"], "watermark_unverified")
        self.assertEqual(find(current, "runs", "origin-invalid-response-run")["logs"], ["origin-invalid-response-preserved"])
        self.assertEqual(find(current, "runs", "origin-invalid-response-run")["status"], "failed")
        self.assertIn(":semantic-protocol-retry:", find(current, "runs", queued["run_id"])["idempotency_key"])
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(self.collector.can_retry_semantic(self.task_id))

    async def test_semantic_retry_recovery_does_not_auto_submit_and_prior_paid_unknown_blocks(self):
        self.received_invalid_semantic()
        queued = await self.collector.queue(self.task_id, semantic_retry=True)
        await self.collector.recover()
        self.assertEqual(self.calls, [])
        self.assertEqual(find(self.repository.state(), "runs", queued["run_id"])["error_code"], "PROCESS_INTERRUPTED")
        state = json.loads(self.checkpoint_path.read_text())
        state.update(status="external_inflight", operation="explicit_semantic_protocol_retry")
        write_json(self.checkpoint_path, state)
        self.repository.mutate(lambda current: find(current, "runs", queued["run_id"]).update(status="queued"))
        await self.collector.recover()
        self.assertEqual(find(self.repository.state(), "runs", queued["run_id"])["error_code"], "UNKNOWN_EXTERNAL_REQUEST")
        self.assertFalse(self.collector.can_retry_semantic(self.task_id))
        with self.assertRaises(HTTPException):
            await self.collector.queue(self.task_id, semantic_retry=True)
        self.assertEqual(self.calls, [])

    async def test_normal_production_retry_uses_existing_task_and_the_semantic_flag_only(self):
        self.received_invalid_semantic()
        production = ProductionService(self.repository, self.hub, self.settings, self.collector)
        async def readiness(**_):
            return {"ready": True, "checks": [{"name": name, "status": "READY"} for name in set().union(*STAGE_REQUIREMENTS.values())]}
        production.readiness.check = readiness
        with self.assertRaises(HTTPException):
            await production.start([self.task_id])
        queued = await production.start([self.task_id], retry=True)
        await production.launch(queued["batch_id"])
        await asyncio.wait_for(production.jobs[queued["batch_id"]], 3)
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.calls[0]["semantic_retry"])
        self.assertEqual(find(self.repository.state(), "tasks", self.task_id)["collector_result"]["decision_reason"], "watermark_unverified")
        self.assertEqual(len([task for task in self.repository.state()["tasks"] if task["id"] == self.task_id]), 1)
        await production.shutdown()

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "local media tools unavailable")
    async def test_semantic_retry_reuses_actual_source_asset_and_source_video_identity(self):
        source = self.root / "downloads/source.mp4"
        source.parent.mkdir()
        await asyncio.to_thread(subprocess.run, ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=size=64x64:rate=5:duration=1",
            "-c:v", "libx264", str(source)], check=True, capture_output=True)
        legacy_task = SimpleNamespace(task_id="original-vidbee", status="completed", file_path=str(source),
            sha256=hashlib.sha256(source.read_bytes()).hexdigest())
        fake_pipeline = SimpleNamespace(config={"download_min_file_bytes": 1}, vidbee=SimpleNamespace(
            lookup_task=lambda _: SimpleNamespace(found=True, task=legacy_task, reason="isolated-completed-evidence")))
        metadata = await asyncio.to_thread(verify_complete_download, fake_pipeline,
            {"download_path": str(source), "download_source_type": "vidbee_automation", "vidbee_task_id": legacy_task.task_id}, legacy_task, self.root)
        result = CollectorResult.model_validate({**self.collector.context(self.task_id), "video_id": "903",
            "video_url": "https://www.tiktok.com/@unit/video/903", "views": 100000, "likes": 2000,
            "decision": "manual_review", "decision_reason": "analysis_error", "legacy_vidbee_task_id": legacy_task.task_id,
            "download_status": "completed", "source_asset_metadata": metadata,
            "legacy_csv_path": str(self.root / "output/candidates.csv"), "legacy_result_reference": {}}).model_dump(mode="json")
        original = await self.collector.queue(self.task_id, resume_download=True)
        self.assertTrue(self.collector._finish(self.task_id, original["run_id"], result, {"pid": 12345, "exit_code": 0}, time.monotonic()))
        before = self.repository.state()
        source_id = find(before, "tasks", self.task_id)["source_video_id"]
        asset = next(item for item in before["assets"] if item["task_id"] == self.task_id and item["type"] == "source")
        original_download = next(item for item in before["stages"] if item["task_id"] == self.task_id and item["name"] == "download")
        self.assertEqual(original_download["status"], "success")
        self.assertIsNone(original_download["start_time"])
        self.assertEqual(original_download["duration"], 0)
        original_checkpoint = find(before, "tasks", self.task_id)["context"]["download_checkpoint"]
        self.assertEqual(original_checkpoint["verification_run_id"], original["run_id"])
        self.assertEqual(original_checkpoint["new_download_submission_count"], 0)
        self.assertIsNone(original_checkpoint["download_duration_ms"])
        self.assertEqual(original_checkpoint["sha256"], legacy_task.sha256)
        self.received_invalid_semantic()
        queued = await self.collector.queue(self.task_id, semantic_retry=True)
        new_result = {**result, "decision_reason": "watermark_unverified", "legacy_result_reference": {"semantic_retry": {"protocol": "unit"}}}
        self.assertTrue(self.collector._finish(self.task_id, queued["run_id"], new_result, {}, time.monotonic()))
        after = self.repository.state()
        self.assertEqual(find(after, "tasks", self.task_id)["source_video_id"], source_id)
        self.assertEqual([item for item in after["assets"] if item["task_id"] == self.task_id and item["type"] == "source"], [asset])
        self.assertEqual(find(after, "tasks", self.task_id)["collector_result"]["legacy_result_reference"]["reused_source_asset_id"], asset["id"])
        self.assertEqual(find(after, "runs", original["run_id"])["status"], "success")
        checkpoints = find(after, "tasks", self.task_id)["context"]["download_checkpoints"]
        self.assertEqual(len(checkpoints), 2)
        self.assertEqual(checkpoints[0], original_checkpoint)
        self.assertEqual(checkpoints[1]["verification_run_id"], queued["run_id"])
        self.assertEqual(checkpoints[1]["source_asset_id"], asset["id"])
        self.assertEqual(next(item for item in after["stages"] if item["task_id"] == self.task_id and item["name"] == "download"), original_download)
        context = self.collector.context(self.task_id)
        image = Path(context["product_images"][0])
        image.write_bytes(b"isolated-reference-for-reconciliation")
        fingerprint = hashlib.sha256(file_hash(image).encode()).hexdigest()[:16]
        snapshot = json.loads(self.checkpoint_path.read_text())
        snapshot["row"].update(reference_image_fingerprint=fingerprint)
        snapshot["recovery_evidence"].update({**{key: context[key] for key in ("country", "keyword", "context_id", "source_document", "source_row")},
            "video_id": result["video_id"], "video_url": result["video_url"], "reference_image_fingerprint": fingerprint,
            "config_sha256": "unit-rules"})
        write_json(self.checkpoint_path, snapshot)
        run_result = Path(self.adapter._paths(context)["metadata"]) / original["run_id"] / "collector-result.json"
        run_result.parent.mkdir(parents=True)
        write_json(run_result, result)
        def legacy_stage(current):
            row = next(item for item in current["stages"] if item["task_id"] == self.task_id and item["name"] == "download")
            row.update(status="not_connected", end_time=None)
        self.repository.mutate(legacy_stage)
        origin_runs = [row for row in self.repository.state()["runs"] if row["task_id"] == self.task_id]
        verification_calls = []
        async def verify_completed(adapter, received_context, received_row, directory):
            verification_calls.append(received_row.copy())
            self.assertEqual(received_context, context)
            directory.mkdir(parents=True)
            return {"status": "READY", "asset": metadata, "_process": {"pid": 54321, "exit_code": 0, "duration_ms": 10,
                "started_at": 1791390000, "finished_at": 1791390000.01, "command": ["isolated-unit-verifier"], "cwd": str(self.root)}}
        with patch("app.integrations.collector_service.VidbeeDownloaderAdapter.verify_completed_once", new=verify_completed):
            reconciled = await self.collector.reconcile_verified_source(self.task_id)
            self.assertEqual(reconciled["status"], "READY")
            self.assertFalse(reconciled["business_run_created"])
            self.assertEqual(reconciled["paid_requests"], 0)
            checkpoint = reconciled["checkpoint"]
            self.assertEqual(checkpoint["verification_run_id"], original["run_id"])
            self.assertEqual(checkpoint["source_asset_id"], asset["id"])
            self.assertIsNone(checkpoint["download_duration_ms"])
            proof_path = Path(checkpoint["reconciliation"]["path"])
            self.assertEqual(file_hash(proof_path), checkpoint["reconciliation"]["sha256"])
            self.assertEqual(json.loads(proof_path.read_text())["download_submission_count"], 0)
            current_runs = [row for row in self.repository.state()["runs"] if row["task_id"] == self.task_id]
            self.assertEqual(current_runs[:-1], origin_runs)
            self.assertEqual(current_runs[-1]["provider"], "VidbeeDownloaderAdapter")
            self.assertEqual(current_runs[-1]["stage"], "download")
            self.assertEqual(current_runs[-1]["pid"], 54321)
            self.assertEqual(current_runs[-1]["started_at"].replace('+00:00', 'Z'), time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(1791390000)))
            self.assertIn("verify_existing_source", current_runs[-1]["logs"][0])
            self.assertIn(origin_runs[0]["id"], current_runs[-1]["logs"][1])
            self.assertEqual(find(self.repository.state(), "tasks", self.task_id)["collector_result"], new_result)
            self.assertEqual(len(verification_calls), 1)
            original_image = image.read_bytes()
            for failure in ("source_hash", "image", "rules", "origin_result", "busy"):
                with self.subTest(failure=failure):
                    saved_result = run_result.read_bytes()
                    saved_source = source.read_bytes()
                    if failure == "source_hash":
                        source.write_bytes(b"changed-completed-source")
                    elif failure == "image":
                        image.write_bytes(b"changed-reference")
                    elif failure == "rules":
                        self.repository.mutate(lambda state: find(state, "tasks", self.task_id)["context"]["production_rules"].update(sha256="changed-rules"))
                    elif failure == "origin_result":
                        invalid = result.copy()
                        invalid["context_id"] = "different-context"
                        write_json(run_result, invalid)
                    else:
                        self.repository.mutate(lambda state: find(state, "runs", original["run_id"]).update(status="running"))
                    with self.assertRaises(HTTPException):
                        await self.collector.reconcile_verified_source(self.task_id)
                    self.assertEqual(len(verification_calls), 1)
                    run_result.write_bytes(saved_result)
                    image.write_bytes(original_image)
                    if source.read_bytes() != saved_source:
                        source.write_bytes(saved_source)
                        os.utime(source, ns=(metadata["mtime_ns"], metadata["mtime_ns"]))
                    self.repository.mutate(lambda state: (find(state, "tasks", self.task_id)["context"]["production_rules"].update(sha256="unit-rules"),
                        find(state, "runs", original["run_id"]).update(status="success")))

    async def test_candidate_result_cannot_mark_download_stage_ready(self):
        queued = await self.collector.queue(self.task_id, resume_download=True)
        await self.collector.run(self.task_id, queued["run_id"])
        current = self.repository.state()
        task = find(current, "tasks", self.task_id)
        download = next(item for item in current["stages"] if item["task_id"] == self.task_id and item["name"] == "download")
        self.assertEqual(download["status"], "not_connected")
        self.assertNotIn("download_checkpoint", task["context"])


if __name__ == "__main__":
    unittest.main()
