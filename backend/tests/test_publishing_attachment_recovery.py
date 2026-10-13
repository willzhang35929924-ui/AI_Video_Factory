"""UNIT_ONLY synthetic DOM/temp receipts. No browser, upload, attachment or publishing."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from playwright.async_api import Error as BrowserError

from app import models as m
from app.domain import now
from app.integrations.publishing.browser_adapter import ExistingHubBrowserAdapter, attachment_binding, product_receipt
from app.integrations.publishing.routes import create_publishing_router
from test_publishing_browser_steps import locator
from test_real_publishing_service import SQLitePublishingFixture


class AttachmentBrowserContracts(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='product-attachment-unit-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.adapter = ExistingHubBrowserAdapter(SimpleNamespace(output_root=self.root))
        caption = 'UNIT_ONLY_CAPTION'
        self.payload = {'publishing_task': {'id': 'unit-task', 'caption': caption,
            'selected_description': {'id': '01', 'text': caption, 'sha256': hashlib.sha256(caption.encode()).hexdigest()}},
            'account': {'id': 'unit-account', 'hubstudio_env_id': 'unit-env', 'expected_username': 'unit-user',
                'cdp_endpoint': 'http://127.0.0.1:9999', 'country': 'UNIT_COUNTRY'},
            'product': {'id': 'unit-product'}, 'video': {'path': '/unit/Composite.mp4', 'sha256': 'unit-sha'},
            'product_match': {'mode': 'runtime_match', 'expected_names': ['UNIT_PRODUCT'], 'requested_id': None},
            'checkpoint': {'UPLOAD_VIDEO': {'status': 'READY'}, 'FILL_DESCRIPTION': {'status': 'READY'},
                'product_attachment_attempt': {'run_id': 'unit-run', 'step_id': 'unit-step',
                    'started_at': (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}}}
        binding = attachment_binding(self.payload)
        self.payload['checkpoint']['product_intent'] = {key: binding[key] for key in ('product_id', 'account_id', 'environment_id',
            'username', 'video_sha256', 'country', 'mode', 'expected_names', 'requested_id')}
        self.page = Mock(url='https://www.tiktok.com/tiktokstudio/upload')
        self.page.screenshot = AsyncMock(side_effect=lambda path: Path(path).write_bytes(b'UNIT_ONLY_NOT_REAL_BROWSER_SCREENSHOT'))
        self.caption = locator(text=caption)
        self.chip = locator(text='UNIT_PRODUCT')
        self.labels = locator(text='UNIT_PRODUCT')
        self.labels.get_by_text.return_value = self.chip
        self.container = locator()
        self.container.evaluate = AsyncMock(return_value={'type': 33, 'product_id': 'UNIT_ACTUAL_CATALOG_SKU', 'keyword': 'UNIT_PRODUCT'})
        self.page.locator.side_effect = lambda selector: self.labels if selector == '[data-e2e="anchor_container"] .anchor-tag-label' else self.container if selector == '[data-e2e="anchor_container"] .anchor-tag-container' else self.caption
        self.duplicate = locator(text='UNIT_PRODUCT', count=2)
        self.duplicate.wait_for.side_effect = BrowserError('UNIT_ONLY duplicate global chip and video preview')
        self.page.get_by_text.return_value = self.duplicate
        self.products, self.link = locator(visible=False), locator(visible=False)
        self.radio = locator()
        self.radio.check = AsyncMock()
        self.radio.is_checked = AsyncMock(return_value=True)
        self.radio.is_enabled = AsyncMock(return_value=True)
        self.radio.get_attribute = AsyncMock(return_value='UNIT_PRODUCT Green')
        self.radios = Mock(first=self.radio)
        header = Mock()
        header.locator.return_value.all_text_contents = AsyncMock(return_value=['Nombre del producto', 'ID del producto', 'Precio', 'Existencias', '', 'Estado'])
        row = locator()
        row.get_by_role.return_value = self.radio
        row.locator.return_value.all_text_contents = AsyncMock(return_value=['UNIT_PRODUCT Green', 'UNIT_ACTUAL_CATALOG_SKU', '$1', '1', '', 'Activo'])
        rows = Mock(first=header)
        rows.all = AsyncMock(return_value=[row])
        self.products.locator.return_value = rows
        self.display = locator()
        self.display.input_value = AsyncMock(return_value='UNIT_PRODUCT')
        self.confirm = locator()
        self.confirm.is_enabled = AsyncMock(return_value=True)
        self.confirm.click = AsyncMock()
        self.next = locator()
        self.next.click = AsyncMock()
        self.cancel = locator()
        self.cancel.click = AsyncMock()
        self.products.get_by_role.side_effect = lambda role, name=None, **kw: self.radios if role == 'radio' else self.display if role == 'textbox' else self.confirm if name == 'Añadir' else self.cancel if name == 'Cancelar' else self.next
        self.combo = locator(text='Productos')
        self.link.get_by_role.side_effect = lambda role, **kw: self.combo if role == 'combobox' else self.next
        self.add = locator()
        self.add.click = AsyncMock()
        self.page.get_by_role.side_effect = lambda role, name=None, **kw: self.products if name == 'Añade enlaces de productos' else self.link if name == 'Añade un enlace' else self.add
        self.browser = Mock(contexts=[Mock(pages=[self.page])])
        self.browser.close = AsyncMock()
        self.runtime = Mock()
        self.runtime.chromium.connect_over_cdp = AsyncMock(return_value=self.browser)
        context = AsyncMock()
        context.__aenter__.return_value = self.runtime
        self.playwright = patch('app.integrations.publishing.browser_adapter.async_playwright', return_value=context)

    async def test_scoped_chip_ignores_duplicate_video_preview_and_receipt_precedes_confirm(self):
        async def confirm(**kw):
            saved = product_receipt(self.adapter.config, self.payload)
            self.assertIsNotNone(saved, 'durable identity-bound SKU must exist before external confirmation')
            self.assertEqual(saved['receipt']['selected_id'], 'UNIT_ACTUAL_CATALOG_SKU')
        self.confirm.click.side_effect = confirm
        result = await self.adapter.attach_product(self.page, self.payload)
        self.assertEqual(result['status'], 'READY')
        self.assertEqual(result['checkpoint']['selected_id'], 'UNIT_ACTUAL_CATALOG_SKU')
        self.assertEqual(result['evidence']['dom_evidence']['confirmation_text'], 'UNIT_PRODUCT')
        self.page.get_by_text.assert_not_called()
        self.labels.get_by_text.assert_called_once_with('UNIT_PRODUCT', exact=True)
        self.confirm.click.assert_awaited_once()
        self.cancel.click.assert_not_awaited()

    async def test_post_confirm_browser_error_preserves_receipt_and_never_replays_confirm(self):
        self.chip.wait_for.side_effect = BrowserError('UNIT_ONLY_CONNECTION_LOST_AFTER_ATTACH')
        result = await self.adapter.attach_product(self.page, self.payload)
        self.assertEqual(result['status'], 'WAITING_MANUAL')
        self.assertEqual(result['error_code'], 'PRODUCT_ATTACHMENT_OUTCOME_UNKNOWN')
        self.assertTrue(result['performed'])
        self.assertIsNotNone(product_receipt(self.adapter.config, self.payload, result['evidence']['product_receipt']))
        self.confirm.click.assert_awaited_once()
        self.cancel.click.assert_not_awaited()

    async def test_receipt_rejects_stale_attempt_changed_identity_hash_or_symlink(self):
        result = await self.adapter.attach_product(self.page, self.payload)
        reference = result['evidence']['product_receipt']
        for change in ('attempt', 'account', 'caption', 'video', 'hash'):
            with self.subTest(change=change):
                payload, candidate = deepcopy(self.payload), deepcopy(reference)
                if change == 'attempt': payload['checkpoint']['product_attachment_attempt']['step_id'] = 'UNIT_OLD_STEP'
                elif change == 'account': payload['account']['id'] = 'UNIT_OTHER_ACCOUNT'
                elif change == 'caption': payload['publishing_task']['caption'] = 'UNIT_OTHER_CAPTION'
                elif change == 'video': payload['video']['sha256'] = 'UNIT_OTHER_SHA'
                else: candidate['sha256'] = 'UNIT_BAD_HASH'
                self.assertIsNone(product_receipt(self.adapter.config, payload, candidate))
        symlink = Path(reference['path']).with_name('UNIT_SYMLINK.json')
        symlink.symlink_to(reference['path'])
        self.assertIsNone(product_receipt(self.adapter.config, self.payload, {'path': str(symlink), 'sha256': reference['sha256']}))

    async def test_readonly_reconcile_chip_without_sku_receipt_remains_unknown(self):
        self.adapter.identity = AsyncMock(return_value='unit-user')
        self.adapter.uploaded = AsyncMock(return_value=True)
        with self.playwright:
            result = await self.adapter.reconcile_product(self.payload)
        self.assertEqual(result['status'], 'WAITING_MANUAL')
        self.assertEqual(result['error_code'], 'PRODUCT_ATTACHMENT_SKU_UNVERIFIED')
        self.assertTrue(result['evidence']['read_only'])
        self.assertFalse(result['performed'])
        self.confirm.click.assert_not_awaited()
        self.page.get_by_role.assert_not_called()
        self.browser.close.assert_awaited_once()

    async def test_readonly_reconcile_uses_same_attempt_real_catalog_receipt_and_current_chip(self):
        await self.adapter.attach_product(self.page, self.payload)
        self.confirm.click.reset_mock()
        self.page.get_by_role.reset_mock()
        self.adapter.identity = AsyncMock(return_value='unit-user')
        self.adapter.uploaded = AsyncMock(return_value=True)
        with self.playwright:
            result = await self.adapter.reconcile_product(self.payload)
        self.assertEqual(result['status'], 'READY')
        self.assertEqual(result['checkpoint']['selected_id'], 'UNIT_ACTUAL_CATALOG_SKU')
        self.assertEqual(result['evidence']['reconciliation'], 'BOUND_RECEIPT_AND_CURRENT_CHIP')
        self.assertFalse(result['performed'])
        self.assertFalse(result['evidence']['publish_clicked'])
        self.confirm.click.assert_not_awaited()
        self.page.get_by_role.assert_not_called()

    async def test_changed_product_intent_never_connects_or_reads_browser(self):
        self.payload['checkpoint']['product_intent']['video_sha256'] = 'UNIT_OTHER_SHA'
        with self.playwright:
            result = await self.adapter.reconcile_product(self.payload)
        self.assertEqual(result['error_code'], 'PRODUCT_INTENT_IDENTITY_CHANGED')
        self.runtime.chromium.connect_over_cdp.assert_not_awaited()

    async def test_actual_current_metadata_and_matching_fresh_catalog_allow_readback_not_fake_preclick_receipt(self):
        self.payload['checkpoint'].pop('product_attachment_attempt')
        self.payload['checkpoint']['product_reconciliation_attempt'] = {'run_id': 'unit-old-run', 'step_id': 'unit-old-step',
            'started_at': (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()}
        self.payload['checkpoint']['product_observation'] = {'status': 'READY', 'evidence': {'unit_only': True, 'verified_at': now(),
            'candidates': [{'product_id': 'UNIT_ACTUAL_CATALOG_SKU', 'product_name': 'UNIT_PRODUCT Green', 'matches': True, 'selectable': True}]}}
        self.adapter.identity = AsyncMock(return_value='unit-user')
        self.adapter.uploaded = AsyncMock(return_value=True)
        with self.playwright:
            result = await self.adapter.reconcile_product(self.payload)
        self.assertEqual(result['status'], 'READY')
        self.assertEqual(result['evidence']['reconciliation'], 'PLATFORM_ANCHOR_AND_CURRENT_CATALOG')
        saved = product_receipt(self.adapter.config, self.payload, result['evidence']['product_receipt'])
        self.assertEqual(saved['receipt']['source'], 'OBSERVED_ATTACHED_PRODUCT_READBACK')
        self.assertEqual(saved['receipt']['selected_id'], 'UNIT_ACTUAL_CATALOG_SKU')
        self.assertEqual(saved['receipt']['selected_name'], 'UNIT_PRODUCT Green')
        script = self.container.evaluate.call_args.args[0]
        self.assertIn('parent?.alternate?.memoizedProps?.value', script, 'current nonempty alternate is a genuine observed source')
        self.assertNotIn('.return.return', script, 'do not crawl unrelated component props')
        self.confirm.click.assert_not_awaited()
        self.page.get_by_role.assert_not_called()

    async def test_metadata_missing_or_different_sku_from_visible_catalog_never_confirms(self):
        self.payload['checkpoint']['product_reconciliation_attempt'] = self.payload['checkpoint'].pop('product_attachment_attempt')
        self.payload['checkpoint']['product_observation'] = {'status': 'READY', 'evidence': {'unit_only': True, 'verified_at': now(),
            'candidates': [{'product_id': 'UNIT_EXPECTED_DIFFERENT_SKU', 'product_name': 'UNIT_PRODUCT Green', 'matches': True, 'selectable': True}]}}
        self.adapter.identity = AsyncMock(return_value='unit-user')
        self.adapter.uploaded = AsyncMock(return_value=True)
        with self.playwright:
            result = await self.adapter.reconcile_product(self.payload)
        self.assertEqual(result['error_code'], 'PRODUCT_ATTACHMENT_SKU_UNVERIFIED')
        self.assertEqual(list((self.root / 'unit-task').glob('*.json')), [])
        self.confirm.click.assert_not_awaited()

    async def test_catalog_snapshot_changed_cannot_reuse_readback_receipt(self):
        self.payload['checkpoint']['product_reconciliation_attempt'] = self.payload['checkpoint'].pop('product_attachment_attempt')
        self.payload['checkpoint']['product_observation'] = {'status': 'READY', 'evidence': {'verified_at': now(),
            'candidates': [{'product_id': 'UNIT_ACTUAL_CATALOG_SKU', 'product_name': 'UNIT_PRODUCT Green', 'matches': True, 'selectable': True}]}}
        saved = await self.adapter.save_product_readback(self.page, self.payload, 'UNIT_PRODUCT')
        reference = {'path': saved['path'], 'sha256': saved['sha256']}
        self.assertIsNotNone(product_receipt(self.adapter.config, self.payload, reference))
        original = deepcopy(self.payload['checkpoint']['product_observation'])
        self.payload['checkpoint']['product_observation']['evidence']['verified_at'] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        self.assertIsNone(product_receipt(self.adapter.config, self.payload, reference), 'pre-attempt or different catalog snapshot is not current recovery proof')
        self.payload['checkpoint']['product_observation'] = original
        self.payload['checkpoint']['product_observation']['evidence']['candidates'][0]['product_id'] = 'UNIT_CHANGED_CATALOG_SKU'
        self.assertIsNone(product_receipt(self.adapter.config, self.payload, reference))


class AttachmentServiceRecoveryContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = SQLitePublishingFixture()
        output = (self.f.root / 'unit-receipts').resolve()
        output.mkdir()
        self.f.service.config = self.f.config.model_copy(update={'stop_after_upload': False, 'output_root': output})
        self.receipt_writer = ExistingHubBrowserAdapter(self.f.service.config)
        self.task = self.f.task()
        upload = self.f.adapter.execute_step

        async def execute(name, payload):
            if name == 'UPLOAD_VIDEO':
                return await upload(name, payload)
            self.f.adapter.steps.append(name)
            screenshot = self.receipt_writer.screenshot_path(payload['publishing_task']['id'], 'product-confirmation')
            screenshot.write_bytes(b'UNIT_ONLY_NOT_REAL_SCREENSHOT')
            if name == 'FILL_DESCRIPTION':
                text = payload['publishing_task']['caption']
                digest = hashlib.sha256(text.encode()).hexdigest()
                return {'status': 'READY', 'performed': False,
                    'checkpoint': {'account_id': payload['account']['id'], 'environment_id': payload['account']['hubstudio_env_id'],
                        'video_sha256': payload['video']['sha256'], 'caption_sha256': digest,
                        'description_id': payload['publishing_task']['selected_description']['id']},
                    'evidence': {'unit_only': True, 'source': 'OBSERVED_BROWSER', 'verified_at': now(),
                        'account_id': payload['account']['id'], 'environment_id': payload['account']['hubstudio_env_id'],
                        'username': payload['account']['expected_username'], 'video_sha256': payload['video']['sha256'],
                        'publish_clicked': False, 'url': 'https://www.tiktok.com/tiktokstudio/upload', 'screenshot': str(screenshot),
                        'caption_sha256': digest, 'dom_evidence': {'caption': text, 'url': 'https://www.tiktok.com/tiktokstudio/upload'}}}
            if name == 'ATTACH_PRODUCT':
                self.receipt = self.receipt_writer.save_product_receipt(payload, {'screenshot': str(screenshot)},
                    payload['product']['name'], 'UNIT_ONLY_ACTUAL_SKU', payload['product']['name'])
                raise RuntimeError('UNIT_ONLY_AFTER_EXTERNAL_ATTACH_BEFORE_RESULT')
            return {'status': 'WAITING_MANUAL', 'performed': False, 'error_code': 'UNIT_ONLY_CONTENT_REVIEW_NOT_PASSED'}

        self.f.adapter.execute_step = AsyncMock(side_effect=execute)
        self.f.adapter.reconcile_product = AsyncMock(side_effect=self.observation)
        await self.f.service.create_batch([self.task['id']])
        await self.f.service.worker

    async def asyncTearDown(self):
        await self.f.service.shutdown()
        self.f.close()

    async def observation(self, payload):
        saved = product_receipt(self.f.service.config, payload, self.receipt)
        self.assertIsNotNone(saved)
        binding = attachment_binding(payload)
        receipt = saved['receipt']
        proof = {key: binding[key] for key in ('product_id', 'account_id', 'environment_id', 'video_sha256', 'mode')}
        proof.update(selected_name=receipt['selected_name'], selected_id=receipt['selected_id'])
        return {'status': 'READY', 'performed': False, 'checkpoint': proof,
            'evidence': {'unit_only': True, 'source': 'OBSERVED_BROWSER', 'verified_at': now(), 'read_only': True,
                'account_id': binding['account_id'], 'environment_id': binding['environment_id'], 'username': binding['username'],
                'video_sha256': binding['video_sha256'], 'caption_sha256': binding['caption_sha256'],
                'selected_name': receipt['selected_name'], 'selected_id': receipt['selected_id'],
                'attached': True, 'confirmed': True, 'publish_clicked': False,
                'reconciliation': 'PLATFORM_ANCHOR_AND_CURRENT_CATALOG' if receipt['source'] == 'OBSERVED_ATTACHED_PRODUCT_READBACK' else 'BOUND_RECEIPT_AND_CURRENT_CHIP',
                'product_receipt': self.receipt, 'screenshot': receipt['confirmation_screenshot'], 'url': 'https://www.tiktok.com/tiktokstudio/upload',
                'dom_evidence': {'selected_name': receipt['selected_name'], 'selected_id': receipt['selected_id'], 'attached': True,
                    'confirmation_text': receipt['display_name'], 'url': 'https://www.tiktok.com/tiktokstudio/upload',
                    'caption': payload['publishing_task']['caption'], 'upload_complete': True}}}

    async def test_bound_readonly_recovery_preserves_unknown_history_and_retry_skips_attachment(self):
        before = self.f.service.task(self.task['id'])
        old_step = deepcopy(before['steps'][-1])
        old_run = deepcopy(before['runs'][0])
        self.assertEqual(before['checkpoint']['inflight_step'], 'ATTACH_PRODUCT')
        result = await self.f.service.reconcile_product(self.task['id'])
        self.assertEqual(result['status'], 'READY')
        self.assertTrue(result['read_only'])
        self.assertEqual(result['public_submission'], 'NOT_PERFORMED')
        after = result['task']
        self.assertEqual(after['product_link_status'], 'PRODUCT_CONFIRMED')
        self.assertEqual(after['submission_state'], 'NOT_SUBMITTED')
        self.assertEqual(after['moderation_status'], before['moderation_status'])
        self.assertNotEqual(after['moderation_status'], 'PASSED')
        self.assertEqual(after['steps'][-2], old_step)
        self.assertEqual(after['runs'][0]['id'], old_run['id'])
        self.assertEqual(after['runs'][0]['status'], old_run['status'])
        self.assertEqual(after['steps'][-1]['name'], 'RECONCILE_PRODUCT')
        self.assertNotIn('external_outcome', after['checkpoint'])
        await self.f.service.retry(self.task['id'])
        await self.f.service.worker
        self.assertEqual(self.f.adapter.steps, ['UPLOAD_VIDEO', 'FILL_DESCRIPTION', 'ATTACH_PRODUCT', 'CONTENT_PRECHECK'])
        self.assertNotIn('SUBMIT', self.f.adapter.steps)

    async def test_missing_receipt_title_only_keeps_unknown_and_blocks_retry(self):
        Path(self.receipt['path']).unlink()
        self.f.adapter.reconcile_product = AsyncMock(return_value={'status': 'WAITING_MANUAL', 'performed': False,
            'error_code': 'PRODUCT_ATTACHMENT_SKU_UNVERIFIED', 'evidence': {'read_only': True, 'display_name': self.f.product['name']}})
        result = await self.f.service.reconcile_product(self.task['id'])
        self.assertEqual(result['status'], 'WAITING_MANUAL')
        self.assertEqual(result['task']['error'], 'PRODUCT_ATTACHMENT_SKU_UNVERIFIED')
        self.assertEqual(result['task']['checkpoint']['external_outcome'], 'UNKNOWN')
        self.assertEqual(result['task']['product_link_status'], 'NOT_CHECKED')
        with self.assertRaises(HTTPException) as blocked:
            await self.f.service.retry(self.task['id'])
        self.assertEqual(blocked.exception.detail, 'PUBLISH_RETRY_NOT_SAFE')
        self.assertEqual(self.f.adapter.steps, ['UPLOAD_VIDEO', 'FILL_DESCRIPTION', 'ATTACH_PRODUCT'])

    async def test_positive_flags_with_wrong_receipt_sku_caption_hash_or_chip_are_not_confirmation(self):
        with self.f.service.session() as session:
            payload = self.f.service.input(session, session.get(m.PublishingTask, self.task['id']))
        original = await self.observation(payload)
        for key, value in (('selected_id', 'UNIT_OTHER_SKU'), ('caption_sha256', 'UNIT_BAD_HASH'),
                ('read_only', False), ('product_receipt', None), ('reconciliation', 'TITLE_ONLY')):
            with self.subTest(key=key):
                invalid = deepcopy(original)
                invalid['evidence'][key] = value
                self.f.adapter.reconcile_product = AsyncMock(return_value=invalid)
                result = await self.f.service.reconcile_product(self.task['id'])
                self.assertEqual(result['status'], 'WAITING_MANUAL')
                self.assertEqual(result['task']['error'], 'PRODUCT_RECONCILIATION_EVIDENCE_REQUIRED')
                self.assertEqual(result['task']['checkpoint']['external_outcome'], 'UNKNOWN')
                self.assertEqual(result['task']['product_link_status'], 'NOT_CHECKED')

    async def test_changed_intent_or_active_run_blocks_external_observer(self):
        with self.f.service.session() as session:
            row = session.get(m.PublishingTask, self.task['id'])
            original = deepcopy(row.checkpoint)
            row.checkpoint = {**original, 'product_intent': {**original['product_intent'], 'video_sha256': 'UNIT_OTHER_SHA'}}
        with self.assertRaises(HTTPException) as blocked:
            await self.f.service.reconcile_product(self.task['id'])
        self.assertEqual(blocked.exception.detail, 'PRODUCT_INTENT_IDENTITY_CHANGED')
        with self.f.service.session() as session:
            row = session.get(m.PublishingTask, self.task['id'])
            row.checkpoint = original
            row.status = 'RUNNING'
        with self.assertRaises(HTTPException) as blocked:
            await self.f.service.reconcile_product(self.task['id'])
        self.assertEqual(blocked.exception.detail, 'PUBLISH_RUN_STILL_ACTIVE')
        self.f.adapter.reconcile_product.assert_not_called()

    async def test_concurrent_product_change_during_observation_cannot_clear_unknown(self):
        async def changed(payload):
            with self.f.service.session() as session:
                session.get(m.Product, self.f.product['id']).name = 'UNIT_CHANGED_PRODUCT'
            return await self.observation(payload)
        self.f.adapter.reconcile_product = AsyncMock(side_effect=changed)
        with self.assertRaises(HTTPException) as blocked:
            await self.f.service.reconcile_product(self.task['id'])
        self.assertEqual(blocked.exception.detail, 'PRODUCT_INTENT_IDENTITY_CHANGED')
        self.assertEqual(self.f.service.task(self.task['id'])['checkpoint']['external_outcome'], 'UNKNOWN')

    async def test_route_calls_readonly_recovery_not_attachment_or_publication(self):
        router = create_publishing_router(self.f.repo, self.f.service.config)
        router.service.adapter = self.f.adapter
        app = FastAPI()
        app.include_router(router)
        with TestClient(app) as client:
            response = client.post('/api/publishing/tasks/' + self.task['id'] + '/reconcile-product')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['public_submission'], 'NOT_PERFORMED')
        self.assertEqual(response.json()['task']['product_link_status'], 'PRODUCT_CONFIRMED')
        self.assertEqual(self.f.adapter.steps, ['UPLOAD_VIDEO', 'FILL_DESCRIPTION', 'ATTACH_PRODUCT'])

    async def test_legacy_unknown_reads_fresh_catalog_then_actual_anchor_without_replaying_attachment(self):
        Path(self.receipt['path']).unlink()
        with self.f.service.session() as session:
            row = session.get(m.PublishingTask, self.task['id'])
            row.checkpoint = {key: value for key, value in row.checkpoint.items() if key != 'product_attachment_attempt'}
            original_steps = deepcopy(self.f.service.task(self.task['id'])['steps'])
        screenshot = self.receipt_writer.screenshot_path(self.task['id'], 'unit-catalog')
        screenshot.write_bytes(b'UNIT_ONLY_NOT_ACTUAL_CATALOG_SCREENSHOT')
        async def catalog(payload):
            return {'status': 'READY', 'evidence': {'unit_only': True, 'source': 'OBSERVED_BROWSER', 'read_only': True,
                'verified_at': now(), 'account_id': payload['account']['id'], 'environment_id': payload['account']['hubstudio_env_id'],
                'username': payload['account']['expected_username'], 'video_sha256': payload['video']['sha256'],
                'url': 'https://www.tiktok.com/tiktokstudio/upload', 'screenshot': str(screenshot),
                'candidates': [{'product_name': payload['product']['name'], 'product_id': 'UNIT_ONLY_ACTUAL_SKU',
                    'status': 'Activo', 'availability': 'Activo', 'matches': True, 'selectable': True}], 'match_count': 1,
                'scope': 'VISIBLE_CATALOG_PAGE_ONLY', 'attached': False, 'confirmed': False, 'publish_clicked': False}}
        self.f.adapter.observe_product = AsyncMock(side_effect=catalog)
        observed = await self.f.service.observe_product(self.task['id'])
        self.assertEqual(observed['status'], 'READY')
        self.assertEqual(observed['task']['checkpoint']['external_outcome'], 'UNKNOWN')
        self.assertEqual(observed['task']['product_link_status'], 'NOT_CHECKED')
        self.assertEqual(observed['task']['steps'], original_steps)
        page = Mock(url='https://www.tiktok.com/tiktokstudio/upload')
        page.screenshot = AsyncMock(side_effect=lambda path: Path(path).write_bytes(b'UNIT_ONLY_FRESH_ANCHOR_SCREENSHOT'))
        self.receipt_writer.attached_metadata = AsyncMock(return_value={'type': 33, 'product_id': 'UNIT_ONLY_ACTUAL_SKU', 'keyword': self.f.product['name']})
        async def readback(payload):
            saved = await self.receipt_writer.save_product_readback(page, payload, self.f.product['name'])
            self.receipt = {'path': saved['path'], 'sha256': saved['sha256']}
            return await self.observation(payload)
        self.f.adapter.reconcile_product = AsyncMock(side_effect=readback)
        result = await self.f.service.reconcile_product(self.task['id'])
        self.assertEqual(result['status'], 'READY')
        self.assertEqual(result['task']['product_link_status'], 'PRODUCT_CONFIRMED')
        self.assertEqual(result['task']['steps'][:-1], original_steps)
        self.assertEqual(result['task']['checkpoint']['ATTACH_PRODUCT']['evidence']['reconciliation'], 'PLATFORM_ANCHOR_AND_CURRENT_CATALOG')
        self.assertEqual(self.f.adapter.steps, ['UPLOAD_VIDEO', 'FILL_DESCRIPTION', 'ATTACH_PRODUCT'])
        self.assertEqual(result['task']['submission_state'], 'NOT_SUBMITTED')
