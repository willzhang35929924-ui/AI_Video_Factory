import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.integrations.legacy.browser_prepare_worker import prepare
from app.integrations.process_runner import ProcessResult
from app.integrations.production_readiness import ProductionReadiness
from app.integrations.production_routes import create_production_router
from test_real_review import MemoryRepository


class BrowserPreparationWorkerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / "chrome-profile").mkdir()
        self.calls, self.login, self.captcha = [], False, False
        self.page_url, self.page_title = "https://www.tiktok.com/", "TikTok - Make Your Day"
        self.body = "TikTok For You Following Explore LIVE Upload Search Discover videos and creators"
        self.navigation = {"navigation_routes": ["/foryou", "/following"], "visible_video": False}
        owner = self
        class Page:
            @property
            def url(page):
                return owner.page_url
            def title(page):
                return owner.page_title
            def locator(page, selector):
                return SimpleNamespace(inner_text=lambda **_: owner.body)
            def evaluate(page, expression):
                return owner.navigation
            def goto(page, url, **_):
                owner.calls.append(("goto", url))
                owner.page_url = url
            def wait_for_timeout(page, _):
                pass
        class VerificationRequired(RuntimeError):
            pass
        class Browser:
            def __init__(browser, config, root, logger):
                owner.calls.append(("config", config))
            def connect(browser):
                owner.calls.append(("connect",))
            def open(browser, url):
                owner.calls.append(("open", url))
                if owner.captcha:
                    owner.calls.append(("preserve",))
                    raise VerificationRequired("unit-secret should not be returned")
                return Page()
            def _needs_tiktok_login(browser, page):
                return owner.login
            def preserve_manual_page(browser, page):
                owner.calls.append(("preserve",))
            def check_for_verification(browser, page):
                pass
            def close(browser):
                owner.calls.append(("disconnect",))
        config = ModuleType("src.config")
        config.load_config = lambda _: {"cdp_endpoint": "http://127.0.0.1:9222", "profile_path": self.root / "chrome-profile", "api_key": "unit-secret"}
        browser = ModuleType("src.browser")
        browser.BrowserClient = Browser
        browser.LoginRequired = type("LoginRequired", (RuntimeError,), {})
        browser.VerificationRequired = VerificationRequired
        self.modules = {"src.config": config, "src.browser": browser}

    def run_prepare(self):
        with patch.dict(sys.modules, self.modules), patch.object(sys, "path", sys.path.copy()):
            return prepare(self.root)

    def test_ready_preserves_existing_profile_and_home_without_search(self):
        result = self.run_prepare()
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["profile_path"], str(self.root / "chrome-profile"))
        self.assertFalse(result["search_executed"])
        self.assertEqual(result["paid_requests"], 0)
        self.assertEqual([row[1] for row in self.calls if row[0] == "open"], ["https://www.tiktok.com/"])
        self.assertTrue(self.calls[0][1]["keep_chrome_running"])
        self.assertNotIn("unit-secret", json.dumps(result))
        self.assertTrue(result["positive_tiktok_page"])
        self.assertEqual(result["page_url"], self.page_url)

    def test_error_empty_or_redirect_page_cannot_be_ready_without_login_button(self):
        for url, title, body in (("chrome-error://chromewebdata/", "www.tiktok.com", "This site can’t be reached ERR_CONNECTION_CLOSED"),
                                 ("about:blank", "", ""), ("https://other.example/", "TikTok", self.body),
                                 ("https://not-tiktok.com/", "TikTok", self.body)):
            with self.subTest(url=url):
                self.page_url, self.page_title, self.body = url, title, body
                result = self.run_prepare()
                self.assertEqual(result["status"], "WAITING_MANUAL")
                self.assertEqual(result["error_code"], "TIKTOK_PAGE_NOT_CONFIRMED")
                self.assertFalse(result["positive_tiktok_page"])
                self.assertEqual(result["page_url"], url)

    def test_brand_title_without_positive_readable_navigation_is_not_ready(self):
        self.navigation = {"navigation_routes": [], "visible_video": False}
        result = self.run_prepare()
        self.assertEqual(result["status"], "WAITING_MANUAL")
        self.assertFalse(result["positive_tiktok_page"])

    def test_actual_page_diagnostics_exclude_query_and_body(self):
        self.page_url = "https://www.tiktok.com/?access_token=unit-secret#unit-secret"
        self.body += " unit-body-not-returned"
        result = self.run_prepare()
        self.assertEqual(result["page_url"], "https://www.tiktok.com/")
        self.assertNotIn("unit-secret", json.dumps(result))
        self.assertNotIn("unit-body-not-returned", json.dumps(result))

    def test_redirect_to_actual_login_url_is_waiting_even_without_login_selector(self):
        self.page_url = "https://www.tiktok.com/login/phone-or-email"
        result = self.run_prepare()
        self.assertEqual(result["status"], "WAITING_MANUAL")
        self.assertEqual(result["error_code"], "LOGIN_REQUIRED")

    def test_login_required_preserves_login_page_without_login_automation(self):
        self.login = True
        result = self.run_prepare()
        self.assertEqual(result["status"], "WAITING_MANUAL")
        self.assertEqual(result["error_code"], "LOGIN_REQUIRED")
        self.assertIn(("goto", "https://www.tiktok.com/login"), self.calls)
        self.assertIn(("preserve",), self.calls)

    def test_captcha_is_not_bypassed_and_raw_exception_is_not_logged(self):
        self.captcha = True
        result = self.run_prepare()
        self.assertEqual(result["error_code"], "CAPTCHA_REQUIRED")
        self.assertEqual(result["status"], "WAITING_MANUAL")
        self.assertNotIn("unit-secret", json.dumps(result))
        self.assertFalse(any(row[0] == "goto" for row in self.calls))

    def test_remote_endpoint_cannot_open_any_browser(self):
        self.modules["src.config"].load_config = lambda _: {"cdp_endpoint": "https://remote.example:9222", "profile_path": self.root / "chrome-profile"}
        with self.assertRaisesRegex(ValueError, "EXISTING_LOCAL_CDP_REQUIRED"):
            self.run_prepare()
        self.assertEqual(self.calls, [])


class BrowserPreparationServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        settings = SimpleNamespace(storage_root=self.root, legacy_tiktok_auto_root=self.root / "existing-legacy",
                                   legacy_tiktok_auto_python=self.root / "existing-legacy/.venv/bin/python", integration_dry_run=False, app_mode="integration")
        self.service = ProductionReadiness(settings, SimpleNamespace(rules=lambda: {"sha256": "isolated-rule"}, dry_run=lambda task_id=None: {"status": "READY", "checks": []}),
            SimpleNamespace(readiness=AsyncMock(return_value={"status": "READY"})),
            SimpleNamespace(assets=SimpleNamespace(readiness=AsyncMock(return_value={"metadata": {"checks": []}}))))
        self.service._tiktok = Mock(return_value={"status": "READY"})
        self.service._quality = Mock(return_value={"status": "READY"})

    async def test_readiness_get_never_starts_preparation_or_creates_request(self):
        with patch("app.integrations.production_readiness.ProcessRunner") as runner:
            await self.service.check()
        runner.assert_not_called()
        self.assertFalse((self.root / "browser_preparation").exists())

    async def test_queue_reuses_operation_and_only_background_action_invokes_worker(self):
        first, created = await self.service.queue_tiktok_preparation()
        again, created_again = await self.service.queue_tiktok_preparation()
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first["operation_id"], again["operation_id"])
        commands = []
        def run(command, **options):
            commands.append((command, options))
            Path(command[command.index("--result") + 1]).write_text(json.dumps({"action": "prepare_tiktok", "status": "WAITING_MANUAL",
                "error_code": "LOGIN_REQUIRED", "message": "isolated manual login", "search_executed": False, "paid_requests": 0, "api_key": "unit-secret"}))
            return ProcessResult(0, 0, "", "", "", 0)
        with patch("app.integrations.production_readiness.ProcessRunner.run", side_effect=run):
            await self.service.run_tiktok_preparation(first["operation_id"])
        result = self.service.tiktok_preparation(first["operation_id"])
        self.assertEqual(result["status"], "WAITING_MANUAL")
        self.assertIn("started_at", result)
        self.assertIn("finished_at", result)
        self.assertNotIn("unit-secret", json.dumps(result))
        self.assertTrue(commands[0][0][2].endswith("browser_prepare_worker.py"))
        self.assertEqual(commands[0][1]["cwd"], self.root / "existing-legacy")
        self.assertNotIn("env", commands[0][1])
        self.assertFalse(self.service.preparation_id)
        self.assertTrue((self.root / "browser_preparation" / first["operation_id"] / "request.json").is_file())

    async def test_operation_lookup_cannot_escape_factory(self):
        with self.assertRaises(ValueError):
            self.service.tiktok_preparation("browser-../../config.yaml")


class BrowserPreparationRoutesTests(unittest.TestCase):
    def app(self, active=False, batch=None):
        repository = MemoryRepository({"runs": [{"provider": "LegacyCollectorAdapter", "status": "running"}] if active else [], "batches": [batch] if batch else []})
        preparation = {"operation_id": "browser-isolated-01", "status": "QUEUED", "paid_requests": 0, "search_executed": False}
        readiness = SimpleNamespace(queue_tiktok_preparation=AsyncMock(return_value=(preparation, True)), run_tiktok_preparation=AsyncMock(),
                                    tiktok_preparation=Mock(return_value=preparation))
        production = SimpleNamespace(repository=repository, readiness=readiness, start_lock=asyncio.Lock())
        app = FastAPI()
        app.include_router(create_production_router(production))
        return app, readiness

    def test_post_is_202_and_separate_from_readiness_and_business_tasks(self):
        app, readiness = self.app()
        with TestClient(app) as client:
            response = client.post("/api/production/prepare-tiktok")
            self.assertEqual(response.status_code, 202)
            operation = response.json()["operation_id"]
            self.assertEqual(client.get("/api/production/browser-preparations/" + operation).status_code, 200)
        readiness.run_tiktok_preparation.assert_awaited_once_with("browser-isolated-01")

    def test_active_collection_blocks_browser_preparation(self):
        app, readiness = self.app(active=True)
        with TestClient(app) as client:
            self.assertEqual(client.post("/api/production/prepare-tiktok").status_code, 409)
        readiness.queue_tiktok_preparation.assert_not_awaited()

    def test_idle_paused_batch_allows_browser_recovery_without_resuming_tasks(self):
        app, readiness = self.app(batch={"status": "PAUSED", "active_task_id": None,
                                        "items": [{"status": "QUEUED"}]})
        with TestClient(app) as client:
            self.assertEqual(client.post("/api/production/prepare-tiktok").status_code, 202)
        readiness.queue_tiktok_preparation.assert_awaited_once()

    def test_paused_batch_with_unfinished_step_still_blocks_browser_preparation(self):
        for batch in ({"status": "PAUSED", "active_task_id": "task-running", "items": []},
                      {"status": "PAUSED", "active_task_id": None, "items": [{"status": "RUNNING"}]},
                      {"status": "RUNNING", "active_task_id": None, "items": []},
                      {"status": "QUEUED", "active_task_id": None, "items": []}):
            with self.subTest(batch=batch):
                app, readiness = self.app(batch=batch)
                with TestClient(app) as client:
                    self.assertEqual(client.post("/api/production/prepare-tiktok").status_code, 409)
                readiness.queue_tiktok_preparation.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
