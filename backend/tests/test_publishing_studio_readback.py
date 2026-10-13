"""UNIT_ONLY Studio readback contracts; no browser, upload or publishing request."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock

from app.integrations.publishing.browser_adapter import studio_readback
from app.integrations.publishing.service import PublishingService
import test_publishing_browser_submit_contract as browser_tests


class StudioPostReadbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = browser_tests.PublicBrowserContractTests()
        self.fixture.setUp()
        self.adapter, self.payload = self.fixture.adapter, self.fixture.payload
        self.temporary = tempfile.TemporaryDirectory(prefix="studio-readback-unit-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.adapter.config.output_root = self.root
        directory = self.root / self.payload['publishing_task']['id']
        directory.mkdir()
        self.screenshot = directory / 'UNIT_ONLY.png'
        self.screenshot.write_bytes(b'UNIT_ONLY_SCREENSHOT_NOT_REAL_MEDIA')
        self.payload['publishing_task']['submission_state'] = 'SUBMITTED'
        self.payload['video']['duration'] = 28.444444
        self.payload['checkpoint']['submission_intent']['started_at'] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        for name in ('UPLOAD_VIDEO', 'FILL_DESCRIPTION', 'SUBMIT'):
            self.payload['checkpoint'][name] = {'status': 'READY', 'performed': name == 'SUBMIT', 'unit_only': True}
        self.item = {'item_id': '1234567890123456789', 'create_time': str(int(datetime.now(timezone.utc).timestamp()) - 20),
            'post_time': str(int(datetime.now(timezone.utc).timestamp()) - 20), 'desc': self.payload['publishing_task']['caption'],
            'duration': 28467, 'in_review': False, 'status': 102, 'visibility': 1,
            'href': 'https://www.tiktok.com/@unit_username/video/1234567890123456789', 'row_text': 'UNIT_ONLY Todo el mundo'}
        self.metadata = {'id': self.item['item_id'], 'username': 'unit_username', 'desc': self.item['desc'],
            'create_time': int(self.item['create_time']), 'private_item': False, 'restricted_visibility': False, 'friends_only': False,
            'prohibited': False, 'taken_down': 0, 'reviewing': False, 'ecommerce_video': 1, 'duration': 28,
            'products': [{'id': 'UNIT_SKU', 'keyword': 'UNIT_ONLY_PRODUCT'}]}
        self.script = Mock(count=AsyncMock(return_value=1), evaluate=AsyncMock(return_value=self.metadata))
        self.elements = Mock(evaluate_all=AsyncMock(return_value=[self.item]))
        self.studio = Mock(url='https://www.tiktok.com/tiktokstudio/content')
        self.studio.locator.return_value = self.elements
        self.public = Mock(url=self.item['href'])
        self.public.goto, self.public.close, self.public.wait_for_function = AsyncMock(), AsyncMock(), AsyncMock()
        self.public.get_by_role.return_value = Mock(wait_for=AsyncMock())
        self.videos = Mock(evaluate_all=AsyncMock(return_value=[{'duration': 28.466667, 'ready_state': 4, 'error': None,
            'visible': True, 'width': 576, 'height': 1024}, {'duration': 12.266667, 'ready_state': 4, 'error': None,
            'visible': True, 'width': 576, 'height': 1024}]))
        self.public.locator.side_effect = {'video': self.videos,
            'body': Mock(inner_text=AsyncMock(return_value=self.item['desc'])),
            'link[rel="canonical"]': Mock(count=AsyncMock(return_value=1), get_attribute=AsyncMock(return_value=self.item['href'])),
            'a[href]': Mock(evaluate_all=AsyncMock(return_value=[])),
            'script#__UNIVERSAL_DATA_FOR_REHYDRATION__': self.script}.__getitem__
        self.context = Mock(pages=[self.studio], new_page=AsyncMock(return_value=self.public))
        self.fixture.browser.contexts = [self.context]
        self.adapter.evidence = AsyncMock(side_effect=self.evidence)

    async def evidence(self, page, payload, label, **values):
        return {'source': 'OBSERVED_BROWSER', 'verified_at': datetime.now(timezone.utc).isoformat(),
            'screenshot': str(self.screenshot), 'read_only': True, 'unit_only': True, **values}

    async def test_unique_new_actual_studio_record_can_verify_public_page_without_resubmitting(self):
        result = await self.fixture.verify(self.payload)
        self.assertEqual(result['status'], 'READY')
        self.assertFalse(result['performed'])
        self.assertEqual(result['platform_video_id'], self.item['item_id'])
        receipt = studio_readback(self.adapter.config, self.payload, result['evidence']['studio_receipt'])
        self.assertEqual(receipt['receipt']['source'], 'OBSERVED_STUDIO_POST_READBACK')
        self.assertEqual(receipt['receipt']['item']['duration'], 28467)
        self.assertNotIn('publication_candidate', self.payload['checkpoint']['SUBMIT'])
        self.fixture.control.click.assert_not_awaited()
        self.adapter.uploaded.assert_not_awaited()
        self.public.goto.assert_awaited_once_with(self.item['href'], wait_until='domcontentloaded', timeout=30000)

    async def test_public_visibility_flag_survives_service_sanitizer_and_receipt_validation(self):
        result = await self.fixture.verify(self.payload)
        result['evidence']['api_key'] = 'UNIT_ONLY_MUST_BE_REDACTED'
        result['evidence']['public_metadata']['secret'] = 'UNIT_ONLY_MUST_BE_REDACTED'
        sanitized = PublishingService.result(result)
        metadata = sanitized['evidence']['public_metadata']
        self.assertIs(metadata['restricted_visibility'], False)
        self.assertNotIn('api_key', sanitized['evidence'])
        self.assertNotIn('secret', metadata)
        javascript = self.script.evaluate.await_args.args[0]
        self.assertIn('restricted_visibility: item.secret', javascript)
        self.assertNotIn('secret: item.secret', javascript)
        service = PublishingService.__new__(PublishingService)
        service.config = self.adapter.config
        service.input = Mock(return_value=self.payload)
        service.submitted_identity_valid = Mock(return_value=True)
        row = Mock(checkpoint=self.payload['checkpoint'], caption=self.payload['publishing_task']['caption'])
        self.assertTrue(service.publication_receipt_valid(None, row, sanitized))
        for value in (True, None):
            with self.subTest(restricted_visibility=value):
                altered = deepcopy(sanitized)
                if value is None:
                    altered['evidence']['public_metadata'].pop('restricted_visibility')
                else:
                    altered['evidence']['public_metadata']['restricted_visibility'] = value
                self.assertFalse(service.publication_receipt_valid(None, row, altered))
        self.fixture.control.click.assert_not_awaited()

    async def test_private_or_platform_review_record_is_observed_but_never_published(self):
        for change in ({'in_review': True}, {'visibility': 0}, {'status': 101}, {'row_text': 'UNIT_ONLY Solo yo'}):
            with self.subTest(change=change):
                self.elements.evaluate_all.return_value = [{**self.item, **change}]
                old_receipt = self.screenshot.with_suffix('.json')
                if old_receipt.exists():
                    old_receipt.unlink()
                result = await self.fixture.verify(self.payload)
                self.assertEqual(result['error_code'], 'PUBLICATION_PENDING_PLATFORM_REVIEW')
                self.assertFalse(result['performed'])
                self.assertFalse(result['evidence']['published'])
                self.assertNotIn('platform_video_id', result)
                self.assertIsNotNone(studio_readback(self.adapter.config, self.payload, result['evidence']['studio_receipt']))
        self.context.new_page.assert_not_awaited()
        self.fixture.control.click.assert_not_awaited()

    async def test_old_ambiguous_caption_duration_or_cross_account_record_cannot_create_receipt(self):
        changes = ({'create_time': '100', 'post_time': '100'}, {'post_time': '100'}, {'desc': 'OTHER caption'},
            {'duration': 31000}, {'href': 'https://www.tiktok.com/@other/video/1234567890123456789'},
            {'href': self.item['href'] + '?draft=true'})
        for change in changes:
            with self.subTest(change=change):
                self.elements.evaluate_all.return_value = [{**self.item, **change}]
                self.assertIsNone(await self.adapter.studio_candidate(self.studio, self.payload))
        self.elements.evaluate_all.return_value = [self.item, {**self.item, 'item_id': '2234567890123456789',
            'href': self.item['href'].replace('1234567890123456789', '2234567890123456789')}]
        self.assertIsNone(await self.adapter.studio_candidate(self.studio, self.payload))
        self.assertFalse(self.screenshot.with_suffix('.json').exists())

    async def test_durable_readback_binds_current_intent_and_all_existing_external_steps(self):
        candidate = await self.adapter.studio_candidate(self.studio, self.payload)
        reference = candidate['studio_receipt']
        self.assertIsNotNone(studio_readback(self.adapter.config, self.payload, reference))
        for name in ('UPLOAD_VIDEO', 'FILL_DESCRIPTION', 'ATTACH_PRODUCT', 'SUBMIT'):
            with self.subTest(name=name):
                changed = deepcopy(self.payload)
                changed['checkpoint'][name]['unit_changed'] = True
                self.assertIsNone(studio_readback(self.adapter.config, changed, reference))
        changed = deepcopy(self.payload)
        changed['checkpoint']['submission_intent']['id'] = 'OTHER_INTENT'
        self.assertIsNone(studio_readback(self.adapter.config, changed, reference))
        changed = deepcopy(self.payload)
        changed['account']['hubstudio_env_id'] = 'OTHER_ENV'
        self.assertIsNone(studio_readback(self.adapter.config, changed, reference))
        self.screenshot.write_bytes(b'UNIT_ONLY_CHANGED_SCREENSHOT')
        self.assertIsNone(studio_readback(self.adapter.config, self.payload, reference))

    async def test_readback_cannot_make_wrong_public_product_a_success(self):
        self.script.evaluate.return_value = {**self.metadata, 'products': [{'id': 'OTHER_SKU'}]}
        result = await self.fixture.verify(self.payload)
        self.assertEqual(result['status'], 'WAITING_MANUAL')
        self.assertEqual(result['error_code'], 'PUBLICATION_PAGE_IDENTITY_NOT_VERIFIED')
        self.assertEqual(result['checkpoint']['publication_observation']['source'], 'OBSERVED_STUDIO_POST_READBACK')
        self.fixture.control.click.assert_not_awaited()

    async def test_recommendation_video_is_not_selected_by_source_order_or_generic_strict_locator(self):
        result = await self.fixture.verify(self.payload)
        self.assertEqual(result['status'], 'READY')
        self.assertEqual(result['evidence']['playback']['duration'], 28.466667)
        self.videos.wait_for.assert_not_called()
        javascript = self.videos.evaluate_all.await_args.args[0]
        self.assertNotIn('src', javascript)
        self.assertNotIn('currentSrc', javascript)

    async def test_two_matching_visible_players_are_not_unique_public_playback(self):
        self.videos.evaluate_all.return_value = [self.videos.evaluate_all.return_value[0]] * 2
        result = await self.fixture.verify(self.payload)
        self.assertEqual(result['error_code'], 'PUBLICATION_PLAYBACK_NOT_UNIQUE')
        self.assertFalse(result['performed'])
        self.fixture.control.click.assert_not_awaited()

    async def test_independent_public_metadata_requires_exact_identity_and_all_visibility_flags(self):
        changes = ({'id': 'OTHER_POST'}, {'username': 'other_account'}, {'desc': 'OTHER caption'},
            {'create_time': 100}, {'create_time': int(self.item['create_time']) + 1}, {'private_item': True}, {'restricted_visibility': True}, {'friends_only': True},
            {'prohibited': True}, {'taken_down': 1}, {'reviewing': True}, {'ecommerce_video': 0},
            {'duration': 30}, {'products': [{'id': 'UNIT_SKU9'}]}, {'products': [{'id': 'UNIT_SKU'}, {'id': 'ANOTHER_SKU'}]})
        candidate = {'video_id': self.item['item_id'], 'create_time': self.item['create_time']}
        self.assertTrue(self.adapter.public_post_matches(self.metadata, candidate, self.payload))
        for change in changes:
            with self.subTest(change=change):
                self.assertFalse(self.adapter.public_post_matches({**self.metadata, **change}, candidate, self.payload))

    async def test_metadata_evaluation_is_limited_to_observed_row_and_direct_item_whitelist(self):
        await self.adapter.studio_candidate(self.studio, self.payload)
        self.studio.locator.assert_called_once_with('a[data-tt="components_PostInfoCell_a"][href]')
        javascript = self.elements.evaluate_all.await_args.args[0]
        self.assertIn("components_RowLayout_FlexRow", javascript)
        self.assertIn('i < 3', javascript)
        self.assertIn('fiber?.alternate?.memoizedProps?.item', javascript)
        self.assertIn('JSON.stringify(item) !== JSON.stringify(values[0])', javascript)
        for secret in ('cookie', 'token', 'authorization', 'localStorage', 'fetch(', 'XMLHttpRequest'):
            self.assertNotIn(secret, javascript)

