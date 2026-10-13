"""UNIT_ONLY adversarial browser contracts; no browser or public request is executed."""
from contextlib import asynccontextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from playwright.async_api import TimeoutError as BrowserTimeout

from app.integrations.publishing.browser_adapter import ExistingHubBrowserAdapter


class PublicBrowserContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.adapter = ExistingHubBrowserAdapter(SimpleNamespace(allow_publish=True, stop_after_upload=False,
            publication_receipt_timeout_seconds=.01))
        self.payload = {"publishing_task": {"id": "UNIT_PUBLISH", "submission_state": "SUBMITTING", "caption": "UNIT_ONLY caption"},
            "account": {"id": "UNIT_ACCOUNT", "hubstudio_env_id": "UNIT_ENV", "expected_username": "unit_username", "country": "US",
                "cdp_endpoint": "http://127.0.0.1:UNIT_ONLY"}, "video": {"path": "/UNIT_ONLY/Composite.mp4", "sha256": "UNIT_VIDEO_HASH"},
            "checkpoint": {"submission_intent": {"id": "UNIT_INTENT", "publishing_task_id": "UNIT_PUBLISH", "fingerprint": "UNIT_FINGERPRINT",
                "account_id": "UNIT_ACCOUNT", "environment_id": "UNIT_ENV", "username": "unit_username", "country": "US",
                "cdp_endpoint": "http://127.0.0.1:UNIT_ONLY", "video_sha256": "UNIT_VIDEO_HASH", "selected_product_id": "UNIT_SKU"},
                "single_task_confirmation": {"confirmed": True, "fingerprint": "UNIT_FINGERPRINT"},
                "ATTACH_PRODUCT": {"status": "READY", "checkpoint": {"selected_id": "UNIT_SKU"}}}}
        self.payload["checkpoint"]["submission_intent"]["description_sha256"] = hashlib.sha256(self.payload["publishing_task"]["caption"].encode()).hexdigest()
        self.page = Mock(url="https://www.tiktok.com/tiktokstudio/upload")
        self.control = Mock()
        self.control.count = AsyncMock(return_value=1)
        self.control.is_visible = AsyncMock(return_value=True)
        self.control.is_enabled = AsyncMock(return_value=True)
        self.control.click = AsyncMock()
        self.page.get_by_role.return_value = self.control
        self.page.screenshot = AsyncMock()
        self.browser = Mock(contexts=[SimpleNamespace(pages=[self.page])])
        self.browser.close = AsyncMock()
        self.connect = AsyncMock(return_value=self.browser)
        self.adapter.identity = AsyncMock(return_value="unit_username")
        self.adapter.uploaded = AsyncMock(return_value=True)
        self.adapter.content_precheck = AsyncMock(return_value={"status": "READY", "performed": False})
        self.adapter.evidence = AsyncMock(return_value={"unit_only": True})
        self.adapter.screenshot_path = Mock(return_value=Path("/UNIT_ONLY_SCREENSHOT.png"))

    @asynccontextmanager
    async def playwright(self):
        yield SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=self.connect))

    async def submit(self, payload=None):
        with patch("app.integrations.publishing.browser_adapter.async_playwright", self.playwright):
            return await self.adapter.submit_publication(payload or self.payload)

    async def verify(self, payload=None):
        with patch("app.integrations.publishing.browser_adapter.async_playwright", self.playwright):
            return await self.adapter.verify_publication(payload or self.payload)

    async def test_disabled_or_missing_confirmation_never_connects_or_clicks(self):
        disabled = deepcopy(self.payload)
        disabled["checkpoint"]["execution_scope"] = "UPLOAD_ONLY"
        result = await self.submit(disabled)
        self.assertEqual(result["error_code"], "PUBLIC_SUBMISSION_DISABLED")
        for field in ("submission_intent", "single_task_confirmation"):
            payload = deepcopy(self.payload)
            payload["checkpoint"].pop(field)
            result = await self.submit(payload)
            self.assertFalse(result["performed"])
        self.connect.assert_not_awaited()
        self.control.click.assert_not_awaited()

    async def test_missing_or_changed_caption_hash_cannot_connect_or_click(self):
        for digest in (None, "OTHER_DESCRIPTION_HASH"):
            with self.subTest(digest=digest):
                payload = deepcopy(self.payload)
                intent = payload["checkpoint"]["submission_intent"]
                if digest is None:
                    intent.pop("description_sha256")
                else:
                    intent["description_sha256"] = digest
                result = await self.submit(payload)
                self.assertEqual(result["status"], "WAITING_MANUAL")
                self.assertFalse(result["performed"])
        self.connect.assert_not_awaited()
        self.control.click.assert_not_awaited()

    async def test_changed_account_environment_or_video_cannot_use_old_confirmation(self):
        changes = [("account", "id", "ANOTHER_ACCOUNT"), ("account", "hubstudio_env_id", "ANOTHER_ENV"),
            ("account", "cdp_endpoint", "http://127.0.0.1:OTHER"), ("account", "expected_username", "other_username"),
            ("account", "country", "MX"), ("video", "sha256", "OTHER_VIDEO_HASH"),
            ("publishing_task", "caption", "ANOTHER caption")]
        for section, field, changed in changes:
            with self.subTest(section=section, field=field):
                payload = deepcopy(self.payload)
                payload[section][field] = changed
                result = await self.submit(payload)
                self.assertEqual(result["status"], "WAITING_MANUAL")
                self.assertFalse(result["performed"])
                self.control.click.assert_not_awaited()
        self.connect.assert_not_awaited()

    async def test_content_review_and_control_visibility_are_actual_preconditions(self):
        self.adapter.content_precheck.return_value = {"status": "WAITING_MANUAL", "performed": False, "error_code": "CONTENT_REVIEW_CHECKING"}
        result = await self.submit()
        self.assertEqual(result["error_code"], "CONTENT_REVIEW_CHECKING")
        self.control.click.assert_not_awaited()
        self.adapter.content_precheck.return_value = {"status": "READY", "performed": False}
        self.control.is_enabled.return_value = False
        result = await self.submit()
        self.assertEqual(result["error_code"], "PUBLISH_CONTROL_NOT_OBSERVED")
        self.control.click.assert_not_awaited()

    async def test_ambiguous_click_timeout_is_unknown_and_does_not_click_again(self):
        self.control.click.side_effect = BrowserTimeout("UNIT_ONLY ambiguous click")
        result = await self.submit()
        self.assertEqual(result["error_code"], "SUBMISSION_OUTCOME_UNKNOWN")
        self.assertIsNone(result["performed"])
        self.control.click.assert_awaited_once()
        result = await self.verify()
        self.assertEqual(result["error_code"], "PUBLICATION_RECEIPT_REQUIRED")
        self.control.click.assert_awaited_once()

    async def test_click_without_platform_receipt_is_not_published(self):
        result = await self.submit()
        self.assertEqual(result["status"], "READY")
        self.assertTrue(result["performed"])
        self.assertIsNone(result["checkpoint"]["publication_candidate"])
        self.assertNotIn("platform_video_id", result)
        self.assertNotIn("platform_url", result)
        self.control.click.assert_awaited_once()
        result = await self.verify()
        self.assertEqual(result["status"], "WAITING_MANUAL")
        self.assertEqual(result["error_code"], "PUBLICATION_RECEIPT_REQUIRED")

    def test_platform_candidate_requires_exact_caption_sku_username_and_unambiguous_url(self):
        intent = self.payload["checkpoint"]["submission_intent"]
        body = json.dumps({"caption": self.payload["publishing_task"]["caption"], "product": intent["selected_product_id"]})
        url = "https://www.tiktok.com/@unit_username/video/1234567890123456789"
        candidate = self.adapter.publication_candidate(body, {"url": url}, intent, self.payload["publishing_task"]["caption"])
        self.assertEqual(candidate["video_url"], url)
        for response in ({"id": "1234567890123456789"}, {"url": url.replace("unit_username", "different")},
                {"url": url.replace("www.tiktok.com", "www.tiktok.com.evil.test")}, {"url": url + "?draft=true"},
                {"urls": [url, url.replace("1234567890123456789", "2234567890123456789")]}):
            self.assertIsNone(self.adapter.publication_candidate(body, response, intent, self.payload["publishing_task"]["caption"]))
        self.assertIsNone(self.adapter.publication_candidate(body.replace("UNIT_SKU", "OTHER_SKU"), {"url": url}, intent, self.payload["publishing_task"]["caption"]))
        self.assertIsNone(self.adapter.publication_candidate(body.replace("UNIT_ONLY caption", "OTHER caption"), {"url": url}, intent, self.payload["publishing_task"]["caption"]))

    async def test_receipt_wrong_username_or_nonplatform_host_is_rejected_without_navigation(self):
        for url in ("https://www.tiktok.com/@other/video/1234567890123456789", "https://example.test/@unit_username/video/1234567890123456789"):
            payload = deepcopy(self.payload)
            payload["checkpoint"]["SUBMIT"] = {"checkpoint": {"publication_candidate": {"source": "BOUND_PLATFORM_POST_RESPONSE",
                "video_id": "1234567890123456789", "video_url": url}}}
            result = await self.verify(payload)
            self.assertEqual(result["error_code"], "PUBLICATION_RECEIPT_IDENTITY_CHANGED")
        self.connect.assert_not_awaited()

    def observed_publication(self, *, links=None, body=None, canonical=None):
        payload = deepcopy(self.payload)
        url = "https://www.tiktok.com/@unit_username/video/1234567890123456789"
        payload["checkpoint"]["SUBMIT"] = {"checkpoint": {"publication_candidate": {
            "source": "BOUND_PLATFORM_POST_RESPONSE", "video_id": "1234567890123456789", "video_url": url}}}
        public = Mock(url=url)
        public.goto = AsyncMock()
        public.close = AsyncMock()
        public.get_by_role.return_value = Mock(wait_for=AsyncMock())
        video = Mock(wait_for=AsyncMock())
        text = Mock(inner_text=AsyncMock(return_value=body or payload["publishing_task"]["caption"]))
        canonical_link = Mock(count=AsyncMock(return_value=1), get_attribute=AsyncMock(return_value=canonical or url))
        products = Mock(evaluate_all=AsyncMock(return_value=links or ["https://www.tiktok.com/view/product/UNIT_SKU"]))
        public.locator.side_effect = {"video": video, "body": text, 'link[rel="canonical"]': canonical_link,
            "a[href]": products}.__getitem__
        context = SimpleNamespace(pages=[Mock(url="https://www.tiktok.com/tiktokstudio/content")], new_page=AsyncMock(return_value=public))
        self.browser.contexts = [context]
        return payload, public

    async def test_verification_after_upload_navigation_observes_real_page_without_clicking(self):
        payload, public = self.observed_publication()
        result = await self.verify(payload)
        self.assertEqual(result["status"], "READY")
        self.assertFalse(result["performed"])
        self.assertEqual(result["platform_video_id"], "1234567890123456789")
        public.goto.assert_awaited_once()
        public.close.assert_awaited_once()
        self.adapter.uploaded.assert_not_awaited()
        self.control.click.assert_not_awaited()
        self.assertTrue(self.adapter.evidence.await_args.kwargs["published"])

    async def test_verification_requires_current_same_cdp_account_identity(self):
        payload, public = self.observed_publication()
        self.adapter.identity.return_value = "other_username"
        result = await self.verify(payload)
        self.assertEqual(result["error_code"], "PUBLICATION_ACCOUNT_CHANGED")
        public.close.assert_awaited_once()
        self.control.click.assert_not_awaited()

    async def test_public_account_navigation_waits_for_hydration_before_identity_observation(self):
        payload, public = self.observed_publication()
        seen = []
        public.get_by_role.return_value.wait_for.side_effect = lambda **kwargs: seen.append('profile-visible')
        async def identity(page):
            self.assertEqual(seen, ['profile-visible'])
            return 'unit_username'
        self.adapter.identity.side_effect = identity
        result = await self.verify(payload)
        self.assertEqual(result['status'], 'READY')
        public.get_by_role.assert_called_once_with('link', name='Perfil', exact=True)
        public.get_by_role.return_value.wait_for.assert_awaited_once_with(state='visible', timeout=30000)
        self.control.click.assert_not_awaited()

    async def test_missing_public_account_navigation_is_manual_with_actual_failure_capture(self):
        payload, public = self.observed_publication()
        public.get_by_role.return_value.wait_for.side_effect = BrowserTimeout('UNIT_ONLY missing own navigation')
        result = await self.verify(payload)
        self.assertEqual(result['status'], 'WAITING_MANUAL')
        self.assertEqual(result['error_code'], 'PUBLICATION_ACCOUNT_NAV_NOT_READY')
        self.assertFalse(result['performed'])
        self.adapter.identity.assert_not_awaited()
        self.adapter.evidence.assert_awaited_once()
        self.assertFalse(self.adapter.evidence.await_args.kwargs['authenticated_navigation_observed'])
        self.assertEqual(self.adapter.evidence.await_args.kwargs['required_navigation']['name'], 'Perfil')
        public.close.assert_awaited_once()
        self.control.click.assert_not_awaited()

    async def test_verification_rejects_missing_caption_or_wrong_canonical(self):
        for options in ({"body": "UNIT_OTHER_CAPTION"}, {"canonical": "https://www.tiktok.com/@unit_username/video/9999999999999999999"}):
            with self.subTest(options=options):
                payload, public = self.observed_publication(**options)
                result = await self.verify(payload)
                self.assertEqual(result["error_code"], "PUBLICATION_PAGE_IDENTITY_NOT_VERIFIED")
                public.close.assert_awaited_once()
        self.control.click.assert_not_awaited()

    async def test_verification_requires_exact_product_link_not_substring_or_external_host(self):
        for link in ("https://www.tiktok.com/view/product/UNIT_SKU9", "https://example.test/view/product/UNIT_SKU"):
            with self.subTest(link=link):
                payload, public = self.observed_publication(links=[link])
                result = await self.verify(payload)
                self.assertEqual(result["status"], "WAITING_MANUAL")
                self.assertEqual(result["error_code"], "PUBLICATION_PAGE_IDENTITY_NOT_VERIFIED")
                public.close.assert_awaited_once()
        self.control.click.assert_not_awaited()

    async def test_verification_input_changes_block_cdp_before_page_inspection(self):
        for section, field, value in (("account", "cdp_endpoint", "http://127.0.0.1:OTHER"),
                ("account", "id", "OTHER_ACCOUNT"), ("video", "sha256", "OTHER_MEDIA"),
                ("publishing_task", "caption", "OTHER_CAPTION")):
            with self.subTest(section=section, field=field):
                payload, _ = self.observed_publication()
                payload[section][field] = value
                result = await self.verify(payload)
                self.assertEqual(result["error_code"], "SUBMISSION_INPUT_IDENTITY_CHANGED")
        self.connect.assert_not_awaited()

    async def test_changed_or_missing_attachment_cannot_use_previous_submit_intent(self):
        for value in ("OTHER_SKU", None):
            with self.subTest(value=value):
                payload, _ = self.observed_publication()
                payload["checkpoint"]["ATTACH_PRODUCT"]["checkpoint"]["selected_id"] = value
                for action in (self.submit, self.verify):
                    result = await action(payload)
                    self.assertEqual(result["error_code"], "SUBMISSION_INPUT_IDENTITY_CHANGED")
        self.connect.assert_not_awaited()
        self.control.click.assert_not_awaited()

    async def test_verification_ambiguous_tiktok_context_cannot_prove_current_account(self):
        payload, public = self.observed_publication()
        self.browser.contexts.append(SimpleNamespace(pages=[Mock(url="https://www.tiktok.com/@other")]))
        result = await self.verify(payload)
        self.assertEqual(result["error_code"], "PUBLICATION_ACCOUNT_CHANGED")
        public.goto.assert_not_awaited()
        self.control.click.assert_not_awaited()
