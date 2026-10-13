"""Quality Run persistence contracts; isolated fixtures, not production media."""

import json
import time
import unittest
from pathlib import Path

from app.integrations.asset_registry import file_hash
from test_real_review import ReviewFixture


class QualityRunEvidenceTests(ReviewFixture, unittest.TestCase):
    def setUp(self):
        self.prepare("person")
        def begin(state):
            state["runs"].append({"id": "unit-quality-run", "task_id": "unit-task", "stage": "quality",
                "provider": "OpenCVCutoutQualityAnalyzer", "status": "running", "logs": [], "progress": 0})
            state["stages"].append({"id": "unit-quality-stage", "task_id": "unit-task", "name": "quality", "status": "running"})
        self.repository.mutate(begin)

    def report(self, *, cached=False):
        result = {"provider": "OpenCVCutoutQualityAnalyzer", "mode": "REAL", "status": "FAIL",
            "geometry_status": "GEOMETRY_FAIL", "semantic_status": "UNKNOWN", "asset_id": "unit-asset-v1",
            "task_id": "unit-task", "fingerprint": "unit-quality-fingerprint", "input_sha256": "unit-alpha-hash",
            "issue_segments": [{"issue": "HOLES", "start": .1, "end": .3}], "cache_hit": cached,
            "error_code": None, "retryable": False}
        path = self.production.settings.storage_root / "quality-report.json"
        path.write_text(json.dumps(result))
        return {**result, "report_path": str(path)}

    def record(self, result, process=None):
        self.production._record_result("unit-task", "unit-quality-run", "quality", result, process or {}, time.monotonic(), None, None)
        return self.repository.state()["runs"][0]

    def test_cache_failure_records_no_child_execution_and_exact_report_identity(self):
        result = self.report(cached=True)
        run = self.record(result)
        self.assertIsNone(run.get("pid"))
        self.assertIsNone(run.get("exit_code"))
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["error_code"], "QUALITY_GEOMETRY_FAIL")
        self.assertEqual(run["progress"], 100)
        evidence = run["resource_info"]["execution"]
        self.assertEqual(evidence["mode"], "CACHE_REUSE")
        self.assertEqual(evidence["asset_id"], result["asset_id"])
        self.assertEqual(evidence["input_sha256"], result["input_sha256"])
        self.assertEqual(evidence["fingerprint"], result["fingerprint"])
        self.assertEqual(evidence["report_sha256"], file_hash(Path(result["report_path"])))
        self.assertIn("未启动", run["logs"][-1])
        self.assertIn("HOLES", run["logs"][-1])
        self.assertEqual(json.loads(Path(result["report_path"]).read_text())["error_code"], None)

    def test_fresh_geometry_failure_is_distinct_from_child_crash(self):
        result = self.report()
        run = self.record(result, {"pid": 54321, "exit_code": 0, "resource_info": {"rss": 1234}})
        self.assertEqual(run["pid"], 54321)
        self.assertEqual(run["exit_code"], 0)
        self.assertEqual(run["error_code"], "QUALITY_GEOMETRY_FAIL")
        self.assertEqual(run["resource_info"]["execution"]["mode"], "PROCESS")
        self.assertEqual(run["resource_info"]["rss"], 1234)

    def test_missing_input_remains_precheck_without_fabricated_process(self):
        run = self.record({"status": "UNKNOWN", "provider": "OpenCVCutoutQualityAnalyzer",
            "error_code": "QUALITY_INPUT_MISSING", "cache_hit": False})
        self.assertEqual(run["error_code"], "QUALITY_INPUT_MISSING")
        self.assertIsNone(run.get("pid"))
        self.assertEqual(run["resource_info"]["execution"]["mode"], "PRECHECK_NO_EXECUTION")
        self.assertNotIn("QUALITY_GEOMETRY_FAIL", run["logs"][-1])

    def test_cached_report_identity_change_is_not_registered(self):
        result = self.report(cached=True)
        report_path = Path(result["report_path"])
        report = json.loads(report_path.read_text())
        report["input_sha256"] = "foreign-alpha"
        report_path.write_text(json.dumps(report))
        before = self.repository.state()
        with self.assertRaisesRegex(ValueError, "QUALITY_REPORT_IDENTITY_MISMATCH"):
            self.record(result)
        self.assertEqual(self.repository.state(), before)

    def test_missing_cached_report_does_not_claim_verified_geometry(self):
        result = self.report(cached=True)
        Path(result["report_path"]).unlink()
        before = self.repository.state()
        with self.assertRaisesRegex(ValueError, "QUALITY_REPORT_NOT_VERIFIED"):
            self.record(result)
        self.assertEqual(self.repository.state(), before)


if __name__ == "__main__":
    unittest.main()
