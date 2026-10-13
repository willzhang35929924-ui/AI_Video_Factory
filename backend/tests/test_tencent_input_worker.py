import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import URLError

from app.integrations.legacy import browser_client, input_worker


class TencentInputWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.legacy = self.directory / "legacy"
        self.profile = self.legacy / "chrome-profile"
        self.profile.mkdir(parents=True)
        self.job = self.directory / "job"
        self.job.mkdir()
        self.document = "https://docs.qq.com/sheet/unit?tab=BB08J2"
        (self.job / "request.json").write_text(json.dumps({"source_document": self.document}))
        image = self.job / "reference.png"
        image.write_bytes(b"product image fixture")
        self.spec = SimpleNamespace(
            country="墨西哥", keyword="Sujetador", image_error="", reference_images=[image],
            source_document=self.document, source_row=2, context_id="legacy-context",
        )
        self.config = {
            "profile_path": self.profile,
            "cdp_endpoint": "http://127.0.0.1:9222",
            "keep_chrome_running": False,
            "tencent_docs_url": "https://docs.qq.com/sheet/old",
        }
        self.instances = []
        self.connect_error = None
        self.cdp_available = True
        owner = self

        class BrowserClient:
            def __init__(self, config, root, logger):
                self.config, self.root, self.logger = config, root, logger
                self.connect = Mock(side_effect=self._connect)
                self.close = Mock()
                owner.instances.append(self)

            def _connect(self):
                if owner.connect_error:
                    raise owner.connect_error
                self.started_existing_profile = not owner.cdp_available
                return self

        self.BrowserClient = BrowserClient
        self.tencent_reader = Mock(return_value=[self.spec])
        self.excel_reader = Mock(return_value=[self.spec])
        self.load_config = Mock(side_effect=lambda _path: self.config.copy())
        self.direct_cdp = Mock(side_effect=AssertionError("Do not bypass BrowserClient.connect"))
        self.modules = {}
        for name, values in {
            "src.excel_input": {"read_keyword_specs": self.excel_reader},
            "src.browser": {"BrowserClient": BrowserClient},
            "src.config": {"load_config": self.load_config},
            "src.tencent_docs": {"read_tencent_keyword_specs": self.tencent_reader},
            "src.cdp": {"CDPBrowser": self.direct_cdp},
        }.items():
            module = ModuleType(name)
            module.__dict__.update(values)
            self.modules[name] = module
        self.native_browser_class = Mock(side_effect=lambda legacy: legacy)

    def run_worker(self, kind="tencent"):
        arguments = ["input_worker.py", "--legacy-root", str(self.legacy),
                     "--job-dir", str(self.job), "--kind", kind]
        with patch.dict(sys.modules, self.modules), patch.object(sys, "argv", arguments), \
             patch.object(sys, "path", sys.path.copy()), \
             patch.object(browser_client, "native_browser_class", self.native_browser_class), \
             patch.object(input_worker, "native_browser_class", self.native_browser_class, create=True):
            return_code = input_worker.main()
        return return_code, json.loads((self.job / "result.json").read_text())

    def test_existing_cdp_uses_native_browser_connect_and_keeps_login_profile(self):
        code, result = self.run_worker()
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["worksheet_tab"], "BB08J2")
        self.assertEqual(result["rows"][0]["context_id"], "legacy-context")
        self.native_browser_class.assert_called_once_with(self.BrowserClient)
        self.direct_cdp.assert_not_called()
        client = self.instances[0]
        client.connect.assert_called_once_with()
        client.close.assert_called_once_with()
        self.assertTrue(client.config["keep_chrome_running"])
        self.assertEqual(client.config["profile_path"], self.profile)
        self.assertEqual(client.config["tencent_docs_url"], self.document)
        self.assertEqual(client.config["tencent_docs_cache_path"], self.job / "source.xlsx")
        self.tencent_reader.assert_called_once_with(client, client.config, client.logger)
        self.load_config.assert_called_once_with(self.legacy / "config.yaml")
        self.assertFalse(self.config["keep_chrome_running"])

    def test_missing_cdp_uses_connect_to_start_only_the_existing_profile(self):
        self.cdp_available = False
        code, result = self.run_worker()
        self.assertEqual((code, result["status"]), (0, "READY"))
        self.assertTrue(self.instances[0].started_existing_profile)
        self.instances[0].connect.assert_called_once_with()
        self.assertEqual(self.instances[0].config["profile_path"], self.profile)
        self.direct_cdp.assert_not_called()

    def test_missing_profile_waits_without_creating_another_profile(self):
        self.profile.rmdir()
        code, result = self.run_worker()
        self.assertEqual((code, result["status"]), (2, "WAITING_MANUAL"))
        self.assertEqual(result["error_code"], "EXISTING_CHROME_PROFILE_REQUIRED")
        self.assertFalse(self.profile.exists())
        self.assertEqual(self.instances, [])
        self.tencent_reader.assert_not_called()
        self.direct_cdp.assert_not_called()

    def test_different_profile_is_rejected_even_if_it_exists(self):
        other_profile = self.directory / "other-profile"
        other_profile.mkdir()
        self.config["profile_path"] = other_profile
        code, result = self.run_worker()
        self.assertEqual((code, result["status"]), (2, "WAITING_MANUAL"))
        self.assertEqual(result["error_code"], "EXISTING_CHROME_PROFILE_REQUIRED")
        self.assertEqual(self.instances, [])
        self.tencent_reader.assert_not_called()

    def test_connect_failure_is_explicit_manual_cdp_error_and_preserves_profile(self):
        self.connect_error = URLError("Connection refused")
        code, result = self.run_worker()
        self.assertEqual((code, result["status"]), (2, "WAITING_MANUAL"))
        self.assertEqual(result["error_code"], "CHROME_CDP_UNAVAILABLE")
        self.assertTrue(self.profile.is_dir())
        self.assertTrue(self.instances[0].config["keep_chrome_running"])
        self.instances[0].close.assert_called_once_with()
        self.tencent_reader.assert_not_called()
        self.assertNotIn("URLError · URLError", result["message"])

    def test_tencent_login_requirement_keeps_chrome_running_and_closes_only_connection(self):
        login_required = type("TencentDocsLoginRequired", (Exception,), {})
        self.tencent_reader.side_effect = login_required("Complete login in Chrome")
        code, result = self.run_worker()
        self.assertEqual((code, result["status"]), (2, "WAITING_MANUAL"))
        self.assertEqual(result["error_code"], "TencentDocsLoginRequired")
        client = self.instances[0]
        self.assertTrue(client.config["keep_chrome_running"])
        client.connect.assert_called_once_with()
        client.close.assert_called_once_with()
        self.assertTrue(self.profile.is_dir())
        self.assertEqual(result["rows"], [])

    def test_reader_failure_after_connection_is_not_mislabeled_as_cdp_failure(self):
        self.tencent_reader.side_effect = URLError("Tencent export connection failed")
        code, result = self.run_worker()
        self.assertEqual((code, result["status"]), (2, "FAILED"))
        self.assertEqual(result["error_code"], "URLError")
        self.instances[0].close.assert_called_once_with()

    def test_excel_does_not_connect_or_launch_chrome(self):
        with patch("app.integrations.excel_mapping.mapped_workbook", return_value={"worksheet": "产品"}):
            code, result = self.run_worker("excel")
        self.assertEqual((code, result["status"]), (0, "READY"))
        self.assertEqual(result["worksheet"], "产品")
        self.excel_reader.assert_called_once_with(
            self.job / "reader.xlsx", self.job / "product_references", source_document=self.document,
        )
        self.load_config.assert_not_called()
        self.native_browser_class.assert_not_called()
        self.direct_cdp.assert_not_called()
        self.tencent_reader.assert_not_called()
        self.assertEqual(self.instances, [])


if __name__ == "__main__":
    unittest.main()

