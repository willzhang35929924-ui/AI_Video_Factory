import asyncio
import copy
import hashlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.config import Settings
from app.domain import find
from app.integrations.collector_service import CollectorService, record_collector_environment
from app.integrations.legacy.collector_adapter import LegacyCollectorAdapter
from app.integrations.production_readiness import ProductionReadiness, STAGE_REQUIREMENTS
from app.integrations.production_routes import create_production_router
from app.integrations.production_service import ProductionService
from app.repository import StateRepository
from tests.test_real_collector import FakeCollector, RULES


class CollectorEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "src").mkdir()
        for name in ("pipeline", "config", "browser", "tiktok_search", "metrics", "video_analysis", "vidbee_handoff"):
            (self.root / "src" / f"{name}.py").write_text("def process_hit(self, browser, spec, hit, checkpoint): pass\n")
        (self.root / "config.yaml").write_text("profile_path: chrome-profile\ncdp_endpoint: http://127.0.0.1:9222\nproduct_match_api_enabled: true\nproduct_match_api_key_env: TIKTOK_AUTO_PRODUCT_VISION_API_KEY\n")
        (self.root / ".env").write_text("TIKTOK_AUTO_PRODUCT_VISION_API_KEY=isolated-preflight-secret\n")
        (self.root / "chrome-profile").mkdir()
        self.adapter = LegacyCollectorAdapter(self.root, self.root, python_path=Path(sys.executable))

    def check(self, adapter=None, context=None):
        with patch("app.integrations.legacy.collector_adapter.CHROME_EXECUTABLE", Path(sys.executable)), \
                patch("app.integrations.legacy.collector_adapter.urllib.request.urlopen", side_effect=lambda *_args, **_kwargs: io.BytesIO(json.dumps({"Browser": "Chrome/unit", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/unit"}).encode())), \
                patch.dict(os.environ, {}, clear=True):
            return (adapter or self.adapter).dry_run(context)

    def test_no_task_checks_real_python_modules_and_browser_without_requiring_product_images(self):
        before = {name: hashlib.sha256((self.root / name).read_bytes()).hexdigest() for name in ("config.yaml", ".env")}
        value = self.check()
        self.assertTrue(value["ok"], value)
        python = next(row for row in value["checks"] if row["name"] == "legacy_python")
        self.assertEqual(python["version"], sys.version.split()[0])
        self.assertEqual(python["executable"], sys.executable)
        self.assertFalse(any(row["name"] == "product_images" for row in value["checks"]))
        self.assertFalse(value["frontend_node_required"])
        self.assertFalse(value["business_executed"])
        self.assertEqual(value["paid_requests"], 0)
        self.assertNotIn("isolated-preflight-secret", json.dumps(value))
        self.assertEqual(before, {name: hashlib.sha256((self.root / name).read_bytes()).hexdigest() for name in before})
        self.assertFalse((self.root / "src/__pycache__").exists())

    def test_settings_with_missing_python_reports_actual_path_not_ready(self):
        settings = Settings(legacy_tiktok_auto_root=self.root, legacy_tiktok_auto_python=self.root / "missing-python")
        value = self.check(LegacyCollectorAdapter(settings.legacy_tiktok_auto_root, self.root, python_path=settings.legacy_tiktok_auto_python))
        python = next(row for row in value["checks"] if row["name"] == "legacy_python")
        self.assertFalse(value["ok"])
        self.assertEqual(python["error_code"], "LEGACY_PYTHON_NOT_EXECUTABLE")
        self.assertEqual(python["path"], str(self.root / "missing-python"))

    def test_nonexecutable_python_file_cannot_pass_presence_check(self):
        path = self.root / "not-executable"
        path.write_text("not a Python executable")
        path.chmod(0o600)
        adapter = LegacyCollectorAdapter(self.root, self.root, python_path=path)
        with patch("app.integrations.legacy.collector_adapter.subprocess.run") as spawn:
            value = self.check(adapter)
        spawn.assert_not_called()
        self.assertFalse(next(row["ok"] for row in value["checks"] if row["name"] == "legacy_python"))

    def test_executable_that_is_not_python_cannot_pass_settings_preflight(self):
        value = self.check(LegacyCollectorAdapter(self.root, self.root, python_path=Path("/usr/bin/true")))
        python = next(row for row in value["checks"] if row["name"] == "legacy_python")
        self.assertFalse(value["ok"])
        self.assertFalse(python["ok"])
        self.assertEqual(python["error_code"], "LEGACY_PYTHON_INVALID_EXECUTABLE")

    def test_non_python_environment_recovers_with_existing_interpreter_without_business(self):
        before = {name: (self.root / name).read_bytes() for name in ("config.yaml", ".env")}
        broken = self.check(LegacyCollectorAdapter(self.root, self.root, python_path=Path("/usr/bin/true")))
        restored = self.check(self.adapter)
        self.assertEqual((broken["status"], restored["status"]), ("MISSING", "READY"))
        missing = next(row for row in broken["checks"] if row["name"] == "legacy_python")
        ready = next(row for row in restored["checks"] if row["name"] == "legacy_python")
        self.assertEqual(missing["error_code"], "LEGACY_PYTHON_INVALID_EXECUTABLE")
        self.assertEqual(ready["version"], sys.version.split()[0])
        self.assertEqual(ready["executable"], sys.executable)
        self.assertTrue(ready["ok"])
        self.assertEqual(before, {name: (self.root / name).read_bytes() for name in before})
        for result in (broken, restored):
            self.assertFalse(result["business_executed"])
            self.assertEqual(result["paid_requests"], 0)
        self.assertFalse((self.root / ".pipeline.lock").exists())

    def test_import_failure_returns_specific_redacted_traceback(self):
        (self.root / "src/video_analysis.py").write_text("raise RuntimeError('isolated-preflight-secret')\n")
        value = self.check()
        failure = next(row for row in value["checks"] if row["name"] == "legacy_import_smoke")
        self.assertFalse(value["ok"])
        self.assertEqual(failure["error_code"], "LEGACY_IMPORT_FAILED")
        self.assertIn("RuntimeError", failure["error"])
        self.assertIn("[REDACTED]", failure["error"])
        self.assertNotIn("isolated-preflight-secret", json.dumps(value))

    def test_task_requires_its_actual_reference_files(self):
        value = self.check(context={"task_id": "unit-task", "project_id": "unit-project", "product_images": [str(self.root / "missing.png")]})
        failure = next(row for row in value["checks"] if row["name"] == "product_images")
        self.assertFalse(value["ok"])
        self.assertEqual(failure["count"], 1)
        self.assertEqual(failure["missing"], 1)
        self.assertEqual(failure["error_code"], "PRODUCT_IMAGES_UNAVAILABLE")

    def test_actual_readiness_worker_returns_page_missing_without_constructing_browser(self):
        modules = {}
        browser = Mock(side_effect=AssertionError("empty pages must not connect or start Chrome"))
        for name, fields in {"browser_prepare_worker": {"inspect_tiktok_page": Mock(side_effect=AssertionError("no page to inspect"))},
            "src.config": {"load_config": lambda _path: {"cdp_endpoint": "http://127.0.0.1:9222"}},
            "src.browser": {"BrowserClient": browser, "VerificationRequired": type("VerificationRequired", (Exception,), {})},
            "src.cdp": {"CDPPage": browser}}.items():
            module = ModuleType(name)
            module.__dict__.update(fields)
            modules[name] = module
        path = Path(__file__).parents[1] / "app/integrations/legacy/readiness_worker.py"
        spec = importlib.util.spec_from_file_location("isolated_readiness_worker", path)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, modules), patch.object(sys, "argv", [str(path), str(self.root)]), \
                patch.object(sys, "path", sys.path.copy()), patch("urllib.request.urlopen", return_value=io.BytesIO(b"[]")):
            spec.loader.exec_module(module)
            result = module.main()
        self.assertEqual(result["status"], "WAITING_MANUAL")
        self.assertEqual(result["error_code"], "TIKTOK_PAGE_MISSING")
        self.assertEqual(result["page_count"], 0)
        browser.assert_not_called()


class CollectorPreflightApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(storage_root=Path(self.temp.name), integration_dry_run=False)
        self.collector = SimpleNamespace(rules=Mock(return_value={"sha256": "unit-only"}),
            dry_run=Mock(return_value={"status": "READY", "ok": True, "checks": [{"name": "legacy_python", "status": "READY", "ok": True, "version": "3.14.7", "path": "/isolated/.venv/bin/python"}]}))
        self.vidbee = SimpleNamespace(readiness=AsyncMock(return_value={"status": "READY"}))
        self.video = SimpleNamespace(assets=SimpleNamespace(readiness=AsyncMock(side_effect=AssertionError("ComfyUI must not block Collector"))))
        self.prompt = SimpleNamespace(readiness=AsyncMock(side_effect=AssertionError("RPA must not block Collector")))
        self.service = ProductionReadiness(self.settings, self.collector, self.vidbee, self.video, self.prompt)
        self.service._tiktok = Mock(return_value={"status": "WAITING_MANUAL", "error_code": "TIKTOK_PAGE_MISSING", "page_count": 0, "message": "原 Chrome 没有 TikTok 页面"})

    async def test_empty_chrome_page_is_manual_wait_not_missing_python(self):
        value = await self.service.check(True, stage="collector")
        self.assertFalse(value["ready"])
        self.assertEqual(value["status"], "WAITING_MANUAL")
        missing = [row for row in value["checks"] if row["required"] and row["status"] != "READY"]
        self.assertEqual([row["name"] for row in missing], ["TikTok"])
        self.assertEqual(missing[0]["error_code"], "TIKTOK_PAGE_MISSING")
        self.assertEqual({row["name"] for row in value["checks"] if row["required"]}, STAGE_REQUIREMENTS["collector"])
        self.collector.dry_run.assert_called_once_with(None)
        self.video.assets.readiness.assert_not_awaited()
        self.prompt.readiness.assert_not_awaited()

    async def test_rules_read_failure_retains_python_environment_evidence(self):
        self.collector.dry_run.return_value = {"status": "MISSING", "checks": [{"name": "legacy_python", "status": "MISSING", "ok": False, "error_code": "LEGACY_PYTHON_NOT_EXECUTABLE", "path": "/isolated/missing-python"}]}
        self.collector.rules.side_effect = OSError("not exposed credential")
        value = await self.service.check(True, stage="collector")
        row = next(row for row in value["checks"] if row["name"] == "Collector")
        self.assertEqual(row["error_code"], "LEGACY_RULES_UNAVAILABLE")
        self.assertEqual(row["input_checks"][0]["error_code"], "LEGACY_PYTHON_NOT_EXECUTABLE")
        self.assertNotIn("not exposed credential", json.dumps(value))

    async def test_preflight_get_is_read_only_and_scoped_to_collector(self):
        app = FastAPI()
        app.include_router(create_production_router(SimpleNamespace(readiness=self.service)))
        with patch("app.integrations.production_readiness.ProcessRunner") as runner, TestClient(app) as client:
            response = client.get("/api/collector/preflight?refresh=true")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["stage"], "collector")
        self.assertEqual(response.json()["paid_requests"], 0)
        self.assertFalse(response.json()["business_executed"])
        self.assertFalse((self.settings.storage_root / "browser_preparation").exists())
        runner.assert_not_called()


class CollectorEnvironmentPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = Settings(app_mode="integration", integration_dry_run=False, storage_root=self.root / "storage", database_url="sqlite:///" + str(self.root / "unit.db"))
        self.repo = StateRepository(self.settings.database_url, Path(__file__).parents[1] / "app/seed.json", app_mode="integration")
        self.adapter = FakeCollector()
        self.service = CollectorService(self.repo, SimpleNamespace(broadcast=AsyncMock()), self.settings, self.adapter)
        with patch("app.integrations.collector_service.read_production_rules", return_value=RULES):
            self.task_id = (await self.service.create({"country": "墨西哥", "keyword": "body", "product_images": [str(self.root / "product.png")]}))["task_id"]

    async def asyncTearDown(self):
        self.repo.engine.dispose()
        self.temp.cleanup()

    async def test_failed_preflight_preserves_task_and_creates_no_fake_run(self):
        before = copy.deepcopy(find(self.repo.state(), "tasks", self.task_id))
        self.adapter.dry_run = lambda context: {"ok": False, "status": "MISSING", "checks": [{"name": "legacy_python", "ok": False, "status": "MISSING", "message": "Python 不存在", "error_code": "LEGACY_PYTHON_NOT_EXECUTABLE"}]}
        with self.assertRaises(HTTPException) as raised:
            await self.service.queue(self.task_id)
        self.assertEqual(raised.exception.detail["checks"][0]["error_code"], "LEGACY_PYTHON_NOT_EXECUTABLE")
        task = find(self.repo.state(), "tasks", self.task_id)
        self.assertEqual(task["status"], before["status"])
        self.assertEqual(task["current_stage"], before["current_stage"])
        self.assertEqual(task["context"]["collector_environment"]["status"], "MISSING")
        self.assertEqual(task["context"]["task_context"], before["context"]["task_context"])
        self.assertFalse(self.repo.state()["runs"])
        self.assertEqual(self.adapter.calls, 0)

    async def test_true_manual_wait_is_recorded_without_changing_review_or_queue(self):
        value = record_collector_environment(self.repo, self.task_id, {"ready": False, "checks": [{"name": "TikTok", "required": True, "status": "WAITING_MANUAL", "message": "请打开原页面", "error_code": "TIKTOK_PAGE_MISSING"}]})
        self.assertEqual(value["status"], "WAITING_MANUAL")
        self.assertEqual(value["reasons"][0]["error_code"], "TIKTOK_PAGE_MISSING")
        self.assertEqual(find(self.repo.state(), "tasks", self.task_id)["status"], "CREATED")
        self.assertFalse(self.repo.state()["runs"])
        self.assertFalse(self.repo.state()["batches"])

    async def test_run_keeps_environment_evidence_when_resource_telemetry_arrives(self):
        with patch("app.integrations.collector_service.read_production_rules", return_value=RULES):
            queued = await self.service.queue(self.task_id)
        await self.service._event(self.task_id, queued["run_id"], {"type": "process_finished", "resource_info": {"cpu_seconds": .3}})
        run = find(self.repo.state(), "runs", queued["run_id"])
        self.service._run_end(run, {"resource_info": {"cpu_seconds": .4}, "exit_code": 0}, None)
        self.assertEqual(run["resource_info"]["cpu_seconds"], .4)
        self.assertEqual(run["resource_info"]["collector_environment"]["status"], "READY")
        self.assertEqual(run["resource_info"]["collector_environment"]["paid_requests"], 0)
        self.assertTrue(any(line.startswith("[ENV]") for line in run["logs"]))

    async def test_production_start_records_manual_environment_gate_before_any_batch_or_run(self):
        service = ProductionService(self.repo, SimpleNamespace(broadcast=AsyncMock()), self.settings, self.service)
        service.readiness = SimpleNamespace(check=AsyncMock(return_value={"ready": False, "stage": "collector", "checks": [
            {"name": name, "status": "WAITING_MANUAL" if name == "TikTok" else "READY", "required": True,
             "message": "原 Chrome 没有 TikTok 页面" if name == "TikTok" else "已检查", "error_code": "TIKTOK_PAGE_MISSING" if name == "TikTok" else None}
            for name in STAGE_REQUIREMENTS["collector"]]}))
        with self.assertRaises(HTTPException) as raised:
            await service.start([self.task_id])
        self.assertEqual(raised.exception.detail["code"], "PRODUCTION_NOT_READY")
        task = find(self.repo.state(), "tasks", self.task_id)
        self.assertEqual(task["status"], "CREATED")
        self.assertEqual(task["context"]["collector_environment"]["status"], "WAITING_MANUAL")
        self.assertFalse(self.repo.state()["runs"])
        self.assertFalse(self.repo.state()["batches"])


if __name__ == "__main__":
    unittest.main()
