"""UNIT_ONLY selector/guard contracts; synthetic locators are not real publishing evidence."""
from copy import deepcopy
import hashlib
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from app.integrations.publishing.browser_adapter import ExistingHubBrowserAdapter


def locator(*, text='', count=1, visible=True, classes='', data_show='true'):
    result = Mock()
    result.count = AsyncMock(return_value=count)
    result.is_visible = AsyncMock(return_value=visible)
    result.inner_text = AsyncMock(return_value=text)
    result.get_attribute = AsyncMock(side_effect=lambda name: classes if name == 'class' else data_show)
    result.wait_for = AsyncMock()
    return result


class BrowserStepContracts(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.adapter = ExistingHubBrowserAdapter(SimpleNamespace(content_review_timeout_seconds=30))
        self.adapter.evidence = AsyncMock(side_effect=lambda page, payload, label, **values: {'unit_only': True, **values})
        caption = 'UNIT_ONLY verified product presentation'
        self.payload = {'publishing_task': {'id': 'unit-only', 'caption': caption,
            'selected_description': {'id': '01', 'text': caption, 'sha256': hashlib.sha256(caption.encode()).hexdigest()}},
            'account': {'id': 'unit-account', 'hubstudio_env_id': 'unit-env', 'expected_username': 'unit'},
            'video': {'path': '/unit/Composite.mp4', 'sha256': 'unit-hash'},
            'checkpoint': {'UPLOAD_VIDEO': {'status': 'READY'}, 'FILL_DESCRIPTION': {'status': 'READY'},
                'ATTACH_PRODUCT': {'status': 'READY', 'checkpoint': {'selected_id': 'UNIT_ONLY_ID'},
                    'evidence': {'dom_evidence': {'confirmation_text': 'UNIT_ONLY attached product'}}}}}
        self.page = Mock(url='https://www.tiktok.com/tiktokstudio/upload')
        self.caption = locator(text=caption)
        self.caption.fill = AsyncMock()
        self.caption.filter.return_value = locator()
        self.page.locator.return_value = self.caption

    async def test_default_filename_caption_is_filled_then_exactly_read_back(self):
        text = self.payload['publishing_task']['caption']
        self.caption.inner_text.side_effect = ['Composite', text]
        result = await self.adapter.fill_description(self.page, self.payload)
        self.caption.fill.assert_awaited_once_with(text)
        self.assertEqual(result['status'], 'READY')
        self.assertTrue(result['performed'])
        self.assertEqual(result['evidence']['dom_evidence']['caption'], text)

    async def test_unrelated_human_caption_is_not_overwritten(self):
        self.caption.inner_text.return_value = 'UNIT_ONLY human unsent caption'
        result = await self.adapter.fill_description(self.page, self.payload)
        self.caption.fill.assert_not_awaited()
        self.assertEqual(result['error_code'], 'EXISTING_CAPTION_DO_NOT_OVERWRITE')
        self.assertFalse(result['performed'])

    async def test_exact_existing_own_caption_is_idempotent_readback_without_input(self):
        result = await self.adapter.fill_description(self.page, self.payload)
        self.assertEqual(result['status'], 'READY')
        self.assertFalse(result['performed'])
        self.caption.fill.assert_not_awaited()

    async def test_missing_or_changed_persisted_selection_does_not_touch_caption(self):
        for selection in ({}, {'id': '01', 'text': 'different', 'sha256': 'changed'}):
            with self.subTest(selection=selection):
                payload = deepcopy(self.payload)
                payload['publishing_task']['selected_description'] = selection
                result = await self.adapter.fill_description(self.page, payload)
                self.assertEqual(result['error_code'], 'PERSISTED_DESCRIPTION_REQUIRED')
                self.caption.fill.assert_not_awaited()

    async def test_caption_readback_mismatch_never_returns_ready(self):
        self.caption.inner_text.side_effect = ['Composite', 'UNIT_ONLY truncated']
        result = await self.adapter.fill_description(self.page, self.payload)
        self.assertEqual(result['status'], 'WAITING_MANUAL')
        self.assertEqual(result['error_code'], 'CAPTION_READBACK_MISMATCH')
        self.assertTrue(result['performed'])

    def content_panel(self, *, classes, text, checking_to_success=False):
        heading = locator(text='Revisión del contenido simplificada')
        panel = Mock()
        heading.locator.return_value.locator.return_value = panel
        active = locator(text=text, classes=classes)
        if checking_to_success:
            active.get_attribute.side_effect = ['status-result status-checking', 'status-result status-success', 'true']
        terminal = locator()
        panel.locator.side_effect = lambda selector: locator() if selector == '.headline-switch [aria-checked="true"]' else terminal if ':not(.status-checking)' in selector else active
        attached = locator(text='UNIT_ONLY attached product')
        labels = Mock()
        labels.get_by_text.return_value = attached
        self.page.locator.side_effect = lambda selector: labels if selector == '[data-e2e="anchor_container"] .anchor-tag-label' else self.caption
        self.page.get_by_text.side_effect = lambda name, **kw: heading if name == 'Revisión del contenido simplificada' else locator(count=2)
        return active, terminal

    async def test_hidden_success_does_not_override_visible_content_error_or_music_success(self):
        active, terminal = self.content_panel(classes='status-result status-error', text='Algo salió mal. Inténtalo de nuevo más tarde. Reintentar')
        result = await self.adapter.content_precheck(self.page, self.payload)
        self.assertEqual(result['status'], 'WAITING_MANUAL')
        self.assertEqual(result['error_code'], 'CONTENT_REVIEW_ERROR')
        self.assertEqual(result['evidence']['dom_evidence']['classes'], ['status-result', 'status-error'])
        terminal.wait_for.assert_not_awaited()

    async def test_visible_content_success_requires_exact_scoped_text(self):
        self.content_panel(classes='status-result status-success', text='No se han detectado problemas. UNIT_ONLY caveat')
        result = await self.adapter.content_precheck(self.page, self.payload)
        self.assertEqual(result['status'], 'READY')
        self.assertEqual(result['evidence']['content_review'], 'PASSED')
        self.assertTrue(result['evidence']['read_only'])
        self.assertFalse(result['performed'])
        self.page.locator.assert_any_call('[data-e2e="anchor_container"] .anchor-tag-label')
        self.assertNotIn(unittest.mock.call('UNIT_ONLY attached product', exact=True), self.page.get_by_text.call_args_list)

    async def test_success_class_with_unknown_text_is_not_pass(self):
        self.content_panel(classes='status-result status-success', text='UNIT_ONLY unrecognized DOM')
        result = await self.adapter.content_precheck(self.page, self.payload)
        self.assertEqual(result['error_code'], 'CONTENT_REVIEW_UNKNOWN')

    async def test_checking_waits_for_dom_terminal_not_fixed_sleep(self):
        active, terminal = self.content_panel(classes='status-result status-checking', text='No se han detectado problemas.', checking_to_success=True)
        result = await self.adapter.content_precheck(self.page, self.payload)
        terminal.wait_for.assert_awaited_once_with(state='visible', timeout=30000)
        self.assertEqual(result['status'], 'READY')

    async def test_warning_not_ready_is_not_content_pass(self):
        self.content_panel(classes='status-result status-warn', text='El contenido podría sufrir restricciones.')
        result = await self.adapter.content_precheck(self.page, self.payload)
        self.assertEqual(result['error_code'], 'CONTENT_REVIEW_RESTRICTED')

    async def test_product_attachment_needs_caption_readback_and_does_not_open_dialog_on_mismatch(self):
        self.caption.inner_text.return_value = 'UNIT_ONLY changed caption'
        result = await self.adapter.attach_product(self.page, self.payload)
        self.assertEqual(result['error_code'], 'VERIFIED_CAPTION_REQUIRED')
        self.page.get_by_role.assert_not_called()

    async def test_content_does_not_run_without_verified_product_or_changed_caption(self):
        payload = deepcopy(self.payload)
        payload['checkpoint']['ATTACH_PRODUCT']['status'] = 'WAITING_MANUAL'
        result = await self.adapter.content_precheck(self.page, payload)
        self.assertEqual(result['error_code'], 'VERIFIED_PRODUCT_ATTACHMENT_REQUIRED')
        self.caption.inner_text.return_value = 'UNIT_ONLY changed caption'
        result = await self.adapter.content_precheck(self.page, self.payload)
        self.assertEqual(result['error_code'], 'CAPTION_CHANGED_BEFORE_CONTENT_REVIEW')
