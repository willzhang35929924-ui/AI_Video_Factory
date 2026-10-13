"""UNIT_ONLY persisted draft gates. No browser, generated caption, product attachment or publication."""
import hashlib
import unittest
from unittest.mock import AsyncMock

from app.domain import now
from test_real_publishing_service import SQLitePublishingFixture


class PersistedDraftGateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = SQLitePublishingFixture()
        output = (self.f.root / 'unit-output').resolve()
        output.mkdir()
        self.screenshot = output / 'UNIT_ONLY_NOT_REAL_CAPTURE.png'
        self.screenshot.write_bytes(b'UNIT_ONLY')
        self.f.service.config = self.f.config.model_copy(update={'stop_after_upload': False, 'output_root': output})
        self.original_upload = self.f.adapter.execute_step
        self.corrupt_caption = None
        self.corrupt_content = None
        self.content_state = 'PASSED'
        self.f.adapter.execute_step = AsyncMock(side_effect=self.execute)

    async def asyncTearDown(self):
        await self.f.service.shutdown()
        self.f.close()

    def evidence(self, payload):
        account = payload['account']
        return {'unit_only': True, 'source': 'OBSERVED_BROWSER', 'account_id': account['id'],
            'environment_id': account['hubstudio_env_id'], 'username': account['expected_username'],
            'video_sha256': payload['video']['sha256'], 'verified_at': now(), 'publish_clicked': False,
            'url': 'https://www.tiktok.com/tiktokstudio/upload', 'screenshot': str(self.screenshot)}

    async def execute(self, name, payload):
        if name == 'UPLOAD_VIDEO':
            return await self.original_upload(name, payload)
        self.f.adapter.steps.append(name)
        evidence = self.evidence(payload)
        if name == 'FILL_DESCRIPTION':
            caption = payload['publishing_task']['caption']
            digest = hashlib.sha256(caption.encode()).hexdigest()
            proof = {key: evidence[key] for key in ('account_id', 'environment_id', 'video_sha256')}
            proof.update(caption_sha256=digest, description_id=payload['publishing_task']['selected_description']['id'])
            evidence.update(caption_sha256=digest, read_only=False, dom_evidence={'caption': caption, 'url': evidence['url']})
            result = {'status': 'READY', 'checkpoint': proof, 'evidence': evidence}
            if self.corrupt_caption:
                self.corrupt_caption(result)
            return result
        if name == 'ATTACH_PRODUCT':
            intent = self.f.service.product_intent(payload)
            proof = {key: intent[key] for key in ('product_id', 'account_id', 'environment_id', 'video_sha256', 'mode')}
            proof.update(selected_name=payload['product']['name'], selected_id='UNIT_SELECTED_ID')
            evidence.update(read_only=False, attached=True, confirmed=True, selected_name=proof['selected_name'], selected_id=proof['selected_id'],
                dom_evidence={'selected_name': proof['selected_name'], 'selected_id': proof['selected_id'], 'attached': True,
                    'confirmation_text': 'UNIT_ONLY product display', 'url': evidence['url']})
            return {'status': 'READY', 'checkpoint': proof, 'evidence': evidence}
        if name == 'CONTENT_PRECHECK':
            evidence.update(read_only=True, content_review=self.content_state,
                caption_sha256=hashlib.sha256(payload['publishing_task']['caption'].encode()).hexdigest(), selected_id='UNIT_SELECTED_ID',
                dom_evidence={'panel': 'Revisión del contenido simplificada', 'enabled': True, 'visible': True,
                    'data_show': 'true', 'classes': ['status-result', 'status-success'],
                    'text': 'No se han detectado problemas. UNIT_ONLY', 'url': evidence['url']})
            result = {'status': 'READY' if self.content_state == 'PASSED' else 'WAITING_MANUAL', 'performed': False,
                'evidence': evidence, **({} if self.content_state == 'PASSED' else {'error_code': 'CONTENT_REVIEW_' + self.content_state})}
            if self.corrupt_content:
                self.corrupt_content(result)
            return result
        return {'status': 'WAITING_MANUAL', 'performed': False, 'error_code': 'UNIT_ONLY_FINAL_APPROVAL_REQUIRED'}

    async def run_task(self):
        task = self.f.task()
        await self.f.service.create_batch([task['id']])
        await self.f.service.worker
        return self.f.service.task(task['id'])

    async def test_bound_caption_product_content_persist_and_public_submission_stays_unperformed(self):
        result = await self.run_task()
        self.assertEqual(result['moderation_status'], 'PASSED')
        self.assertEqual(result['product_link_status'], 'PRODUCT_CONFIRMED')
        self.assertEqual(result['submission_state'], 'NOT_SUBMITTED')
        self.assertEqual(result['status'], 'WAITING_MANUAL')
        self.assertEqual(result['current_step'], 'FINAL_CONFIRMATION')
        self.assertEqual(result['error'], 'EXPLICIT_SINGLE_TASK_PUBLISH_CONFIRMATION_REQUIRED')
        self.assertNotIn('SUBMIT', self.f.adapter.steps)
        self.assertNotIn('VERIFY_RESULT', self.f.adapter.steps)
        self.assertNotIn('external_outcome', result['checkpoint'])

    async def test_filled_caption_requires_actual_exact_readback_before_product(self):
        self.corrupt_caption = lambda result: result['evidence']['dom_evidence'].update(caption='UNIT_ONLY mismatch')
        result = await self.run_task()
        self.assertEqual(result['error'], 'CAPTION_READBACK_EVIDENCE_REQUIRED')
        self.assertNotIn('ATTACH_PRODUCT', self.f.adapter.steps)
        self.assertEqual(result['checkpoint']['external_outcome'], 'UNKNOWN')

    async def test_caption_requires_bound_identity_and_real_capture_file_not_simple_ready_flag(self):
        self.corrupt_caption = lambda result: result['evidence'].update(screenshot='/UNIT_ONLY_MISSING.png')
        result = await self.run_task()
        self.assertEqual(result['error'], 'CAPTION_READBACK_EVIDENCE_REQUIRED')
        self.assertNotIn('ATTACH_PRODUCT', self.f.adapter.steps)

    async def test_hidden_success_is_rejected_despite_positive_provider_status(self):
        self.corrupt_content = lambda result: result['evidence']['dom_evidence'].update(data_show='false')
        result = await self.run_task()
        self.assertEqual(result['error'], 'CONTENT_REVIEW_EVIDENCE_REQUIRED')
        self.assertEqual(result['moderation_status'], 'UNKNOWN')
        self.assertNotIn('FINAL_CONFIRMATION', self.f.adapter.steps)

    async def test_music_panel_success_cannot_pass_content_review(self):
        self.corrupt_content = lambda result: result['evidence']['dom_evidence'].update(panel='Derechos de autor de la música')
        result = await self.run_task()
        self.assertEqual(result['error'], 'CONTENT_REVIEW_EVIDENCE_REQUIRED')
        self.assertEqual(result['moderation_status'], 'UNKNOWN')

    async def test_visible_error_is_manual_and_readonly_gate_can_safely_resume_without_unknown_mutation(self):
        self.content_state = 'ERROR'
        result = await self.run_task()
        self.assertEqual(result['moderation_status'], 'ERROR')
        self.assertEqual(result['error'], 'CONTENT_REVIEW_ERROR')
        self.assertNotIn('external_outcome', result['checkpoint'])
        self.assertEqual(result['checkpoint']['ATTACH_PRODUCT']['status'], 'READY')
        self.assertNotIn('FINAL_CONFIRMATION', self.f.adapter.steps)
