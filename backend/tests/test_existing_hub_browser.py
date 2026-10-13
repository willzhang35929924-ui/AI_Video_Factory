"""UNIT_ONLY fake DOM contracts; these are not browser or upload evidence."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app.integrations.publishing.browser_adapter import ExistingHubBrowserAdapter


UPLOAD_URL = "https://www.tiktok.com/tiktokstudio/upload"


class ExistingHubBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="existing-hub-unit-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.video = self.root / "UNIT_ONLY.mp4"
        self.video.write_bytes(b"UNIT_ONLY_NON_MEDIA")
        self.output = self.root / "output"
        self.output.mkdir()
        self.adapter = ExistingHubBrowserAdapter(SimpleNamespace(output_root=self.output,
            manual_cdp_allowed=True, network_probe_environment_ids=[], allow_publish=False, stop_after_upload=True))
        self.payload = {"publishing_task": {"id": "pub-unit-only"},
            "account": {"id": "acc-unit-only", "hubstudio_env_id": "unit-env",
                "cdp_endpoint": "http://127.0.0.1:9999", "expected_username": "unit_user", "country": "MX"},
            "video": {"path": str(self.video), "sha256": hashlib.sha256(self.video.read_bytes()).hexdigest()},
            "checkpoint": {"external_outcome": "UNKNOWN", "inflight_step": "UPLOAD_VIDEO"}}
        self.payload["checkpoint"]["upload_intent"] = {"account_id": "acc-unit-only", "environment_id": "unit-env",
            "cdp_endpoint": "http://127.0.0.1:9999", "video_sha256": self.payload["video"]["sha256"],
            "filename": self.video.name}
        self.profile = Mock(is_visible=AsyncMock(return_value=True),
            get_attribute=AsyncMock(return_value="https://www.tiktok.com/@unit_user"))
        self.avatar = Mock(count=AsyncMock(return_value=1), click=AsyncMock())
        self.control = Mock(count=AsyncMock(return_value=1), set_input_files=AsyncMock())
        self.body = Mock(inner_text=AsyncMock(return_value="Algo salió mal"))
        self.icon = Mock(count=AsyncMock(return_value=1))
        self.completion = Mock(count=AsyncMock(return_value=1), is_visible=AsyncMock(return_value=True),
            inner_text=AsyncMock(return_value="Cargado(39.16MB)"), locator=Mock(return_value=self.icon))
        self.block = Mock(count=AsyncMock(return_value=1), locator=Mock(return_value=self.completion))
        self.blocks = Mock(count=AsyncMock(return_value=0), filter=Mock(return_value=self.block))
        self.filename = Mock()
        self.page = Mock(url=UPLOAD_URL, screenshot=AsyncMock(),
            get_by_role=Mock(return_value=self.profile), get_by_text=Mock(return_value=self.filename))
        self.page.locator.side_effect = {
            'button[data-tt="Header_NewHeader_Clickable"][aria-haspopup="dialog"]': self.avatar,
            'input[type="file"][accept="video/*"]': self.control,
            "body": self.body, ".info-main": self.blocks,
        }.__getitem__
        self.browser = SimpleNamespace(contexts=[SimpleNamespace(pages=[self.page])], close=AsyncMock())
        self.connect = AsyncMock(return_value=self.browser)
        self.playwright = AsyncMock()
        self.playwright.__aenter__.return_value = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=self.connect))
        self.patch = patch("app.integrations.publishing.browser_adapter.async_playwright", return_value=self.playwright)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    async def test_identity_closes_only_the_menu_it_opened(self):
        self.assertEqual(await self.adapter.identity(self.page), "unit_user")
        self.avatar.click.assert_not_awaited()
        self.profile.is_visible.return_value = False
        self.profile.wait_for = AsyncMock()
        self.assertEqual(await self.adapter.identity(self.page), "unit_user")
        self.assertEqual(self.avatar.click.await_count, 2)
        self.profile.wait_for.assert_awaited_once_with(state="visible", timeout=5000)

    async def test_identity_rejects_untrusted_profile_href(self):
        for href in ("http://www.tiktok.com/@unit_user", "https://user@www.tiktok.com/@unit_user",
                "https://www.tiktok.com.invalid/@unit_user", "/@unit_user/video/123", "/@unit_user?other=1",
                "/@unit_user#other", "//other.test/@unit_user", "https://[/@unit_user"):
            with self.subTest(href=href):
                self.profile.get_attribute.return_value = href
                self.assertIsNone(await self.adapter.identity(self.page))

    async def test_identity_resolves_observed_relative_own_profile_on_public_page(self):
        self.page.url = "https://www.tiktok.com/@unit_user/video/1234567890123456789"
        self.profile.get_attribute.return_value = "/@unit_user"
        self.assertEqual(await self.adapter.identity(self.page), "unit_user")
        self.page.get_by_role.assert_called_once_with("link", name="Perfil", exact=True)
        self.avatar.click.assert_not_awaited()

    async def test_relative_profile_on_untrusted_page_cannot_attest_current_account(self):
        self.profile.get_attribute.return_value = "/@unit_user"
        for page_url in ("https://other.test/tiktokstudio/content", "http://www.tiktok.com/tiktokstudio/content",
                "https://user@www.tiktok.com/tiktokstudio/content"):
            with self.subTest(page_url=page_url):
                self.page.url = page_url
                self.assertIsNone(await self.adapter.identity(self.page))

    async def test_preflight_public_submission_evidence_matches_policy_without_executing(self):
        self.adapter.network = AsyncMock(return_value={"source": "OBSERVED_NETWORK", "country": "MX"})
        for allow, stop, scope, expected in (
                (False, False, "PRODUCTION", "DISABLED"),
                (True, True, "PRODUCTION", "DISABLED"),
                (True, False, "UPLOAD_ONLY", "DISABLED"),
                (True, False, "PRODUCTION", "MANUAL_CONFIRMATION_REQUIRED")):
            with self.subTest(allow_publish=allow, stop_after_upload=stop, scope=scope):
                self.adapter.config.allow_publish = allow
                self.adapter.config.stop_after_upload = stop
                payload = deepcopy(self.payload)
                payload["checkpoint"]["execution_scope"] = scope
                result = await self.adapter.preflight(payload)
                self.assertEqual(result["status"], "READY")
                self.assertEqual(result["evidence"]["public_submission"], expected)
                self.assertEqual(result["evidence"]["environment_dispatch"], "WAITING_MANUAL")
                self.assertNotIn("single_task_confirmation", payload["checkpoint"])
        self.control.set_input_files.assert_not_awaited()
        self.page.screenshot.assert_not_awaited()
        self.avatar.click.assert_not_awaited()

    async def test_preflight_submission_policy_does_not_pass_unready_observation(self):
        self.adapter.config.allow_publish = True
        self.adapter.config.stop_after_upload = False
        self.adapter.network = AsyncMock(return_value=None)
        result = await self.adapter.preflight(self.payload)
        self.assertEqual(result["status"], "WAITING_MANUAL")
        self.assertEqual(result["evidence"]["public_submission"], "MANUAL_CONFIRMATION_REQUIRED")
        self.assertEqual(next(row["status"] for row in result["checks"] if row["name"] == "vpn"), "WAITING_MANUAL")
        self.control.set_input_files.assert_not_awaited()
        self.page.screenshot.assert_not_awaited()

    async def test_reconciliation_binds_intent_before_connecting(self):
        for key in ("account_id", "environment_id", "cdp_endpoint", "video_sha256", "filename"):
            with self.subTest(key=key):
                payload = deepcopy(self.payload)
                payload["checkpoint"]["upload_intent"][key] = "UNIT_WRONG"
                result = await self.adapter.reconcile_upload(payload)
                self.assertEqual(result["status"], "WAITING_MANUAL")
                self.assertFalse(result["performed"])
        self.connect.assert_not_awaited()
        self.control.set_input_files.assert_not_awaited()

    async def test_reconciliation_is_read_only_and_does_not_pass_content_review(self):
        result = await self.adapter.reconcile_upload(self.payload)
        self.assertEqual(result["status"], "READY")
        self.assertTrue(result["evidence"]["read_only"])
        self.assertFalse(result["evidence"]["publish_clicked"])
        self.assertEqual(result["evidence"]["content_review"], "NOT_PASSED")
        self.page.get_by_text.assert_called_once_with(self.video.name, exact=True)
        self.blocks.filter.assert_called_once_with(has=self.filename)
        self.control.set_input_files.assert_not_awaited()
        self.page.get_by_role.assert_called_once_with("link", name="Perfil", exact=True)

    async def test_reconciliation_requires_exact_filename_and_success_dom(self):
        for element, attribute, value in ((self.block, "count", 0), (self.block, "count", 2),
                (self.completion, "count", 0), (self.completion, "is_visible", False),
                (self.icon, "count", 0), (self.completion, "inner_text", "Cargando(39.16MB)")):
            with self.subTest(attribute=attribute, value=value):
                method = getattr(element, attribute)
                original = method.return_value
                method.return_value = value
                result = await self.adapter.reconcile_upload(self.payload)
                self.assertEqual(result["status"], "WAITING_MANUAL")
                self.assertFalse(result["evidence"]["uploaded"])
                method.return_value = original
        self.control.set_input_files.assert_not_awaited()

    async def test_identity_mismatch_never_uploads_or_confirms(self):
        self.profile.get_attribute.return_value = "https://www.tiktok.com/@other_user"
        for operation in ("reconcile", "upload"):
            result = await self.adapter.reconcile_upload(self.payload) if operation == "reconcile" else await self.adapter.execute_step("UPLOAD_VIDEO", self.payload)
            self.assertEqual(result["status"], "WAITING_MANUAL")
            self.assertFalse(result["performed"])
        self.page.screenshot.assert_not_awaited()
        self.control.set_input_files.assert_not_awaited()

    async def test_non_upload_steps_do_not_connect_or_publish(self):
        for step in ("ATTACH_PRODUCT", "CONTENT_PRECHECK", "FINAL_CONFIRMATION", "SUBMIT", "VERIFY_RESULT"):
            result = await self.adapter.execute_step(step, self.payload)
            self.assertEqual(result["status"], "WAITING_MANUAL")
            self.assertFalse(result["performed"])
        self.connect.assert_not_awaited()

    async def test_draft_and_changed_hash_are_not_uploaded(self):
        self.body.inner_text.return_value = "no se ha guardado"
        self.assertFalse((await self.adapter.execute_step("UPLOAD_VIDEO", self.payload))["performed"])
        self.payload["video"]["sha256"] = "0" * 64
        self.assertFalse((await self.adapter.execute_step("UPLOAD_VIDEO", self.payload))["performed"])
        self.control.set_input_files.assert_not_awaited()

    def test_upload_page_requires_one_trusted_https_page(self):
        for url in ("http://www.tiktok.com/tiktokstudio/upload", "https://user@www.tiktok.com/tiktokstudio/upload",
                "https://www.tiktok.com.invalid/tiktokstudio/upload", "https://www.tiktok.com/@unit_user"):
            with self.subTest(url=url):
                self.page.url = url
                self.assertIsNone(self.adapter.upload_page(self.browser))
        self.page.url = UPLOAD_URL
        self.assertIs(self.adapter.upload_page(self.browser), self.page)
        self.browser.contexts[0].pages.append(self.page)
        self.assertIsNone(self.adapter.upload_page(self.browser))

    async def test_output_symlinks_are_rejected_before_browser_writes(self):
        fixed = datetime(2026, 10, 8, 12, 0, 0, tzinfo=timezone.utc)
        clock = patch("app.integrations.publishing.browser_adapter.datetime")
        date = clock.start()
        date.now.return_value = fixed
        self.addCleanup(clock.stop)
        for target_kind in ("directory", "upload-screenshot", "reconcile-screenshot"):
            with self.subTest(target_kind=target_kind):
                directory = self.output / target_kind
                outside = self.root / (target_kind + "-outside")
                outside.mkdir()
                payload = deepcopy(self.payload)
                payload["publishing_task"]["id"] = target_kind
                if target_kind == "directory":
                    directory.symlink_to(outside, target_is_directory=True)
                else:
                    directory.mkdir()
                    label = "before-upload" if target_kind == "upload-screenshot" else "upload-verified"
                    name = label + "-" + fixed.strftime('%Y%m%dT%H%M%S%f') + ".png"
                    (directory / name).symlink_to(outside / name)
                try:
                    if target_kind == "reconcile-screenshot":
                        await self.adapter.reconcile_upload(payload)
                    else:
                        await self.adapter.execute_step("UPLOAD_VIDEO", payload)
                except (OSError, ValueError):
                    pass
                self.page.screenshot.assert_not_awaited()
                self.control.set_input_files.assert_not_awaited()

    def test_replaced_output_root_cannot_redirect_screenshots(self):
        self.output.rename(self.root / "original-output")
        outside = self.root / "UNIT_OLD_FACTORY"
        outside.mkdir()
        self.output.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "PUBLISH_OUTPUT_PATH_NOT_ALLOWED"):
            self.adapter.screenshot_path("pub-unit-only", "before-upload")
        self.assertEqual(list(outside.iterdir()), [])

    def product_picker(self, *, products_visible=True, link_visible=False):
        self.adapter.config.product_selection_mode = "runtime_match"
        self.payload["product"] = {"name": "UNIT PRODUCT", "platform_product_id": "UNIT_PLATFORM_ID"}
        self.products_visible, self.link_visible = products_visible, link_visible
        self.product_headers = Mock(all_text_contents=AsyncMock(return_value=[
            "Nombre del producto", "ID del producto", "Precio", "Existencias", "", "Estado"]))
        self.product_radio = Mock(count=AsyncMock(return_value=1), is_visible=AsyncMock(return_value=True),
            is_enabled=AsyncMock(return_value=True), get_attribute=AsyncMock(return_value="UNIT PRODUCT Green"),
            wait_for=AsyncMock(), check=AsyncMock(), click=AsyncMock())
        self.product_cells = Mock(all_text_contents=AsyncMock(return_value=[
            "UNIT PRODUCT Green\nTu tienda", "UNIT_PLATFORM_ID", "$49.9", "15", "", "Activo"]))
        self.product_row = Mock(is_visible=AsyncMock(return_value=True),
            get_by_role=Mock(return_value=self.product_radio), locator=Mock(return_value=self.product_cells))
        header = Mock(locator=Mock(return_value=self.product_headers),
            get_by_role=Mock(return_value=Mock(count=AsyncMock(return_value=0))))
        self.product_table = Mock(first=header, all=AsyncMock(return_value=[header, self.product_row]))
        radios = Mock(first=self.product_radio, check=AsyncMock(), click=AsyncMock())

        def show_link(**_):
            self.link_visible = True

        def show_products(**_):
            self.products_visible, self.link_visible = True, False

        def cancel_products(**_):
            self.products_visible, self.link_visible = False, True

        def cancel_link(**_):
            self.link_visible = False

        self.add_link = Mock(click=AsyncMock(side_effect=show_link))
        self.next_products = Mock(click=AsyncMock(side_effect=show_products))
        self.cancel_products = Mock(click=AsyncMock(side_effect=cancel_products))
        self.cancel_link = Mock(click=AsyncMock(side_effect=cancel_link))
        self.confirm_product = Mock(click=AsyncMock())
        self.publish = Mock(click=AsyncMock())
        self.link_type = Mock(inner_text=AsyncMock(return_value="Productos"))
        self.products = Mock(is_visible=AsyncMock(side_effect=lambda: self.products_visible),
            locator=Mock(side_effect=lambda selector: self.product_table if selector == "tr" else self.fail(selector)))
        self.products.get_by_role.side_effect = lambda role, **kw: {
            ("radio", None): radios, ("button", "Cancelar"): self.cancel_products,
            ("button", "Confirmar"): self.confirm_product,
        }[(role, kw.get("name"))]
        self.link = Mock(is_visible=AsyncMock(side_effect=lambda: self.link_visible))
        self.link.get_by_role.side_effect = lambda role, **kw: {
            ("combobox", None): self.link_type, ("button", "Siguiente"): self.next_products,
            ("button", "Cancelar"): self.cancel_link,
        }[(role, kw.get("name"))]
        self.page.get_by_role.side_effect = lambda role, **kw: {
            ("link", "Perfil"): self.profile, ("dialog", "Añade enlaces de productos"): self.products,
            ("dialog", "Añade un enlace"): self.link, ("button", "Añadir"): self.add_link,
            ("button", "Publicar"): self.publish,
        }[(role, kw.get("name"))]

    def assert_product_was_not_mutated(self):
        self.control.set_input_files.assert_not_awaited()
        self.product_radio.check.assert_not_awaited()
        self.product_radio.click.assert_not_awaited()
        self.confirm_product.click.assert_not_awaited()
        self.publish.click.assert_not_awaited()

    async def test_product_observation_wrong_account_does_not_open_picker(self):
        self.product_picker(products_visible=False)
        self.profile.get_attribute.return_value = "https://www.tiktok.com/@other_user"
        result = await self.adapter.observe_product(self.payload)
        self.assertEqual(result["error_code"], "PRODUCT_ACCOUNT_CHANGED")
        self.add_link.click.assert_not_awaited()
        self.next_products.click.assert_not_awaited()
        self.cancel_products.click.assert_not_awaited()
        self.cancel_link.click.assert_not_awaited()
        self.page.screenshot.assert_not_awaited()
        self.assert_product_was_not_mutated()

    async def test_product_header_changes_never_create_candidates(self):
        self.product_picker()
        original = self.product_headers.all_text_contents.return_value
        for column in range(6):
            with self.subTest(column=column):
                headers = list(original)
                headers[column] = "UNIT_CHANGED_HEADER"
                self.product_headers.all_text_contents.return_value = headers
                result = await self.adapter.observe_product(self.payload)
                self.assertEqual(result["error_code"], "PRODUCT_TABLE_SCHEMA_CHANGED")
                self.assertNotIn("candidates", result["evidence"])
        self.page.screenshot.assert_not_awaited()
        self.cancel_products.click.assert_not_awaited()
        self.assert_product_was_not_mutated()

    async def test_matched_product_is_only_a_visible_candidate_not_attachment(self):
        self.product_picker()
        result = await self.adapter.observe_product(self.payload)
        self.assertEqual(result["status"], "READY")
        evidence = result["evidence"]
        self.assertEqual(evidence["candidates"], [{"product_name": "UNIT PRODUCT Green", "product_id": "UNIT_PLATFORM_ID",
            "availability": "Activo", "status": "Activo", "selectable": True, "matches": True}])
        self.assertEqual(evidence["match_count"], 1)
        self.assertEqual(evidence["scope"], "VISIBLE_CATALOG_PAGE_ONLY")
        self.assertTrue(evidence["read_only"])
        for field in ("attached", "confirmed", "publish_clicked"):
            self.assertFalse(evidence[field])
        self.product_radio.get_attribute.assert_awaited_once_with("value")
        self.assert_product_was_not_mutated()

    async def test_product_match_aliases_use_the_service_supplied_name_contract(self):
        self.product_picker()
        self.payload["product"]["name"] = "UNIT OTHER CATALOG NAME"
        self.payload["product_match"] = {"mode": "runtime_match", "expected_names": [" unit   PRODUCT "],
            "requested_id": "UNIT_UNUSED_ID"}
        result = await self.adapter.observe_product(self.payload)
        self.assertEqual(result["evidence"]["match_count"], 1)
        self.payload["product_match"]["expected_names"] = ["UNIT PRODUCTS"]
        result = await self.adapter.observe_product(self.payload)
        self.assertEqual(result["evidence"]["match_count"], 0)
        self.assert_product_was_not_mutated()

    async def test_exact_product_id_uses_second_cell_not_price_or_title(self):
        self.product_picker()
        self.payload["checkpoint"]["product_selection_mode"] = "exact_id"
        self.payload["product"]["name"] = "UNIT DIFFERENT NAME"
        result = await self.adapter.observe_product(self.payload)
        self.assertEqual(result["evidence"]["match_count"], 1)
        self.payload["product"]["platform_product_id"] = "$49.9"
        result = await self.adapter.observe_product(self.payload)
        self.assertEqual(result["evidence"]["match_count"], 0)
        self.assert_product_was_not_mutated()

    async def test_hidden_disabled_and_inactive_product_rows_cannot_match(self):
        self.product_picker()
        for element, method in ((self.product_row, "is_visible"), (self.product_radio, "is_visible"),
                (self.product_radio, "is_enabled")):
            with self.subTest(method=method, element=element):
                getattr(element, method).return_value = False
                result = await self.adapter.observe_product(self.payload)
                self.assertEqual(result["evidence"]["match_count"], 0)
                if method == "is_visible":
                    self.assertEqual(result["evidence"]["candidates"], [])
                getattr(element, method).return_value = True
        self.product_cells.all_text_contents.return_value[-1] = "Inactivo"
        result = await self.adapter.observe_product(self.payload)
        self.assertEqual(result["evidence"]["match_count"], 0)
        self.assert_product_was_not_mutated()

    async def test_product_observation_closes_only_dialogs_it_opened(self):
        for products_visible, link_visible in ((False, False), (False, True), (True, False)):
            with self.subTest(products_visible=products_visible, link_visible=link_visible):
                self.product_picker(products_visible=products_visible, link_visible=link_visible)
                self.assertEqual((await self.adapter.observe_product(self.payload))["status"], "READY")
                self.assertEqual(self.cancel_products.click.await_count, int(not products_visible))
                self.assertEqual(self.cancel_link.click.await_count, int(not products_visible and not link_visible))
                if products_visible:
                    self.assertTrue(self.products_visible)
                    self.add_link.click.assert_not_awaited()
                    self.next_products.click.assert_not_awaited()
                elif link_visible:
                    self.assertTrue(self.link_visible)
                    self.add_link.click.assert_not_awaited()
                self.assert_product_was_not_mutated()

    async def test_non_product_link_type_does_not_open_or_confirm_products(self):
        self.product_picker(products_visible=False)
        self.link_type.inner_text.return_value = "UNIT_OTHER_LINK_TYPE"
        result = await self.adapter.observe_product(self.payload)
        self.assertEqual(result["error_code"], "PRODUCT_LINK_TYPE_NOT_SELECTED")
        self.next_products.click.assert_not_awaited()
        self.cancel_link.click.assert_awaited_once()
        self.cancel_products.click.assert_not_awaited()
        self.assert_product_was_not_mutated()


if __name__ == "__main__":
    unittest.main()
