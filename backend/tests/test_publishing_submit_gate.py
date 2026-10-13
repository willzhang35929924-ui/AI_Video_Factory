"""UNIT_ONLY final confirmation and unknown submission contracts; no real publishing."""
import hashlib
from copy import deepcopy
import unittest
from unittest.mock import AsyncMock

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import models as m
from app.domain import now
from app.integrations.publishing.schemas import PublishConfirmation
from app.integrations.publishing.routes import create_publishing_router
import test_publishing_draft_gate as draft_tests


class SingleTaskSubmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.gate = draft_tests.PersistedDraftGateTests()
        await self.gate.asyncSetUp()
        self.f = self.gate.f
        self.f.service.config = self.f.service.config.model_copy(update={"allow_publish": True})
        self.original_steps = self.gate.execute
        self.submits = 0
        self.verifications = 0
        self.raise_after_submit = False
        self.invalid_receipt = False
        self.unsupported = False
        self.f.adapter.execute_step = AsyncMock(side_effect=self.execute)
        self.task = await self.gate.run_task()

    async def asyncTearDown(self):
        await self.gate.asyncTearDown()

    def submit_evidence(self, payload):
        evidence = self.gate.evidence(payload)
        intent = payload["checkpoint"]["submission_intent"]
        evidence.update(publishing_task_id=payload["publishing_task"]["id"], submission_intent_id=intent["id"],
            description_sha256=hashlib.sha256(payload["publishing_task"]["caption"].encode()).hexdigest(),
            selected_id=intent["selected_product_id"])
        return evidence

    async def execute(self, name, payload):
        if name not in {"SUBMIT", "VERIFY_RESULT"}:
            return await self.original_steps(name, payload)
        evidence = self.submit_evidence(payload)
        intent = payload["checkpoint"]["submission_intent"]
        if name == "SUBMIT":
            with self.f.service.session() as session:
                row = session.get(m.PublishingTask, payload["publishing_task"]["id"])
                assert row.submission_state == "SUBMITTING"
                assert row.checkpoint["submission_intent"]["id"] == intent["id"]
            if self.unsupported:
                return {"status": "WAITING_MANUAL", "performed": False, "error_code": "UNIT_DOM_NOT_OBSERVED", "evidence": {"read_only": True}}
            self.submits += 1
            if self.raise_after_submit:
                raise RuntimeError("UNIT_ONLY connection lost after click")
            evidence.update(read_only=False, publish_clicked=True, dom_evidence={"before_url": evidence["url"],
                "publish_control_observed": True, "enabled": True, "visible": True,
                "caption": payload["publishing_task"]["caption"], "selected_id": intent["selected_product_id"]})
            return {"status": "READY", "performed": True, "evidence": evidence}
        self.verifications += 1
        identifier = "1234567890123456789"
        url = "https://www.tiktok.com/@" + intent["username"] + "/video/" + identifier
        if self.invalid_receipt:
            url = "https://www.tiktok.com/@wrong_account/video/" + identifier
        evidence.update(read_only=True, published=True, verified_at=now(), dom_evidence={"video_url": url,
            "video_id": identifier, "published": True, "username": intent["username"],
            "caption": payload["publishing_task"]["caption"], "selected_product_id": intent["selected_product_id"]})
        return {"status": "READY", "performed": False, "platform_video_id": identifier, "platform_url": url, "evidence": evidence}

    async def submit(self):
        snapshot = self.f.service.submit_confirmation(self.task["id"])
        result = await self.f.service.confirm_submit(self.task["id"], PublishConfirmation(fingerprint=snapshot["fingerprint"], confirmed=True))
        await self.f.service.worker
        return result, self.f.service.task(self.task["id"])

    async def test_confirmation_binds_all_inputs_and_only_verified_receipt_finishes(self):
        initial = self.f.service.submit_confirmation(self.task["id"])
        self.assertEqual(initial["snapshot"]["video_sha256"], self.task["input_sha256"])
        self.assertEqual(initial["snapshot"]["selected_product_id"], "UNIT_SELECTED_ID")
        self.assertEqual(initial["snapshot"]["account_id"], self.f.account["id"])
        self.assertEqual(initial["snapshot"]["description_sha256"], self.task["selected_description"]["sha256"])
        accepted, task = await self.submit()
        self.assertTrue(accepted["accepted"])
        self.assertEqual(task["status"], "PUBLISHED")
        self.assertEqual(task["submission_state"], "VERIFIED")
        self.assertEqual(self.submits, 1)
        self.assertEqual(self.verifications, 1)
        self.assertEqual(task["platform_video_id"], "1234567890123456789")
        self.assertFalse(self.f.service.queue()["locks"])
        self.assertNotIn("inflight_step", task["checkpoint"])
        with self.assertRaises(HTTPException):
            await self.f.service.retry(self.task["id"])

    async def test_confirm_false_or_changed_fingerprint_never_runs_submit(self):
        snapshot = self.f.service.submit_confirmation(self.task["id"])
        for request in (PublishConfirmation(fingerprint=snapshot["fingerprint"], confirmed=False),
                PublishConfirmation(fingerprint="0" * 64, confirmed=True)):
            with self.assertRaises(HTTPException):
                await self.f.service.confirm_submit(self.task["id"], request)
        self.assertEqual(self.submits, 0)
        self.assertEqual(self.f.service.task(self.task["id"])["submission_state"], "NOT_SUBMITTED")

    async def test_unknown_click_cannot_retry_and_readonly_receipt_reconciliation_never_clicks_again(self):
        self.raise_after_submit = True
        _, task = await self.submit()
        self.assertEqual(task["submission_state"], "UNKNOWN")
        self.assertEqual(task["status"], "WAITING_MANUAL")
        with self.assertRaises(HTTPException):
            await self.f.service.retry(task["id"])
        result = await self.f.service.reconcile_submit(task["id"])
        self.assertEqual(result["status"], "PUBLISHED")
        self.assertEqual(self.submits, 1)
        history = result["task"]["steps"]
        self.assertTrue(any(row["name"] == "SUBMIT" and row["status"] == "WAITING_MANUAL" for row in history))
        self.assertTrue(any(row["name"] == "RECONCILE_SUBMIT" and row["status"] == "READY" for row in history))
        reused = await self.f.service.reconcile_submit(task["id"])
        self.assertTrue(reused["reused"])
        self.assertEqual(self.verifications, 1)

    async def test_mismatched_receipt_cannot_mark_published_or_allow_a_second_click(self):
        self.invalid_receipt = True
        _, task = await self.submit()
        self.assertEqual(task["status"], "WAITING_MANUAL")
        self.assertEqual(task["submission_state"], "SUBMITTED")
        self.assertIsNone(task["platform_video_id"])
        with self.assertRaises(HTTPException):
            await self.f.service.confirm_submit(task["id"], PublishConfirmation(fingerprint="0" * 64, confirmed=True))
        result = await self.f.service.reconcile_submit(task["id"])
        self.assertEqual(result["status"], "WAITING_MANUAL")
        self.assertEqual(self.submits, 1)

    async def test_unobserved_publish_control_stays_not_submitted_and_never_verifies(self):
        self.unsupported = True
        _, task = await self.submit()
        self.assertEqual(task["status"], "WAITING_MANUAL")
        self.assertEqual(task["error"], "UNIT_DOM_NOT_OBSERVED")
        self.assertEqual(task["submission_state"], "NOT_SUBMITTED")
        self.assertEqual(self.submits, 0)
        self.assertEqual(self.verifications, 0)

    async def test_confirmation_invalidates_after_actual_product_or_content_evidence_changes(self):
        snapshot = self.f.service.submit_confirmation(self.task["id"])
        with self.f.service.session() as session:
            row = session.get(m.PublishingTask, self.task["id"])
            evidence = dict(row.checkpoint["CONTENT_PRECHECK"]["evidence"])
            evidence["verified_at"] = now()
            row.checkpoint = {**row.checkpoint, "CONTENT_PRECHECK": {**row.checkpoint["CONTENT_PRECHECK"], "evidence": evidence}}
        with self.assertRaises(HTTPException) as changed:
            await self.f.service.confirm_submit(self.task["id"], PublishConfirmation(fingerprint=snapshot["fingerprint"], confirmed=True))
        self.assertEqual(changed.exception.detail, "PUBLISH_CONFIRMATION_INPUT_CHANGED")
        self.assertEqual(self.submits, 0)

    async def test_confirmation_routes_bind_current_snapshot_and_only_queue_after_explicit_confirm(self):
        router = create_publishing_router(self.f.repo, self.f.service.config)
        router.service.retry = AsyncMock(return_value={"status": "QUEUED"})
        app = FastAPI()
        app.include_router(router)
        path = "/api/publishing/tasks/" + self.task["id"]
        with TestClient(app) as client:
            snapshot = client.get(path + "/submit-confirmation")
            self.assertEqual(snapshot.status_code, 200)
            fingerprint = snapshot.json()["fingerprint"]
            self.assertEqual(client.post(path + "/confirm-submit", json={"fingerprint": fingerprint}).status_code, 409)
            accepted = client.post(path + "/confirm-submit", json={"fingerprint": fingerprint, "confirmed": True})
            self.assertEqual(accepted.status_code, 202)
            self.assertTrue(accepted.json()["accepted"])
            self.assertEqual(client.post(path + "/confirm-submit", json={"fingerprint": fingerprint, "confirmed": True, "secret": "UNIT_ONLY"}).status_code, 422)
        router.service.retry.assert_awaited_once_with(self.task["id"])
        self.assertEqual(self.submits, 0)

    async def test_unknown_submission_cannot_be_replayed_after_service_recovery(self):
        self.raise_after_submit = True
        _, task = await self.submit()
        intent = deepcopy(task["checkpoint"]["submission_intent"])
        await self.f.service.recover()
        restored = self.f.service.task(task["id"])
        self.assertEqual(restored["checkpoint"]["submission_intent"], intent)
        self.assertEqual(restored["submission_state"], "UNKNOWN")
        with self.assertRaises(HTTPException):
            await self.f.service.retry(task["id"])
        result = await self.f.service.reconcile_submit(task["id"])
        self.assertEqual(result["status"], "PUBLISHED")
        self.assertEqual(self.submits, 1)
        self.assertEqual(self.verifications, 1)

    async def test_adversarial_receipt_fields_do_not_verify_wrong_account_caption_sku_or_url(self):
        self.raise_after_submit = True
        _, task = await self.submit()
        with self.f.service.session() as session:
            row = session.get(m.PublishingTask, task["id"])
            payload = self.f.service.input(session, row)
        valid = await self.execute("VERIFY_RESULT", payload)
        mutations = [("account", lambda value: value["evidence"].update(account_id="OTHER_ACCOUNT")),
            ("caption", lambda value: value["evidence"]["dom_evidence"].update(caption="OTHER caption")),
            ("sku", lambda value: value["evidence"]["dom_evidence"].update(selected_product_id="OTHER_SKU")),
            ("intent", lambda value: value["evidence"].update(submission_intent_id="OTHER_INTENT")),
            ("host", lambda value: value.update(platform_url=value["platform_url"].replace("www.tiktok.com", "example.test"))),
            ("video", lambda value: value["evidence"]["dom_evidence"].update(video_id="9876543210987654321")),
            ("timestamp", lambda value: value["evidence"].update(verified_at="2000-01-01T00:00:00Z")),
            ("screenshot", lambda value: value["evidence"].update(screenshot="/UNIT_ONLY_NO_SCREENSHOT.png"))]
        with self.f.service.session() as session:
            row = session.get(m.PublishingTask, task["id"])
            self.assertTrue(self.f.service.publication_receipt_valid(session, row, valid))
            for name, mutate in mutations:
                with self.subTest(name=name):
                    invalid = deepcopy(valid)
                    mutate(invalid)
                    self.assertFalse(self.f.service.publication_receipt_valid(session, row, invalid))
        self.assertEqual(self.submits, 1)
        self.assertEqual(self.f.service.task(task["id"])["submission_state"], "UNKNOWN")

    async def test_current_uppercase_rejected_person_blocks_submission(self):
        with self.f.service.session() as session:
            session.get(m.VideoAsset, "unit-person").status = "REJECTED"
        with self.assertRaises(HTTPException):
            self.f.service.submit_confirmation(self.task["id"])
        self.assertEqual(self.submits, 0)

    async def test_failed_expansion_does_not_block_readonly_reconciliation_of_already_submitted_approved_final(self):
        self.raise_after_submit = True
        _, old = await self.submit()
        with self.f.service.session() as session:
            session.get(m.Task, 'unit-source-task').status = 'FAILED'
            session.add(m.Review(id='UNIT_CURRENT_PERSON_REVIEW', task_id='unit-source-task', asset_id='unit-person',
                status='approved', issues=[], note='UNIT_ONLY current person approval'))
        with self.f.service.session() as session:
            row = session.get(m.PublishingTask, self.task['id'])
            with self.assertRaises(HTTPException):
                self.f.service.input(session, row)
            payload = self.f.service.input(session, row, reconcile_submitted=True)
            self.assertEqual(payload['video']['person_asset_id'], 'unit-person')
        result = await self.f.service.reconcile_submit(self.task['id'])
        self.assertEqual(result['status'], 'PUBLISHED')
        self.assertEqual(self.submits, 1)
        self.assertTrue(any(step['name'] == 'SUBMIT' and step['status'] == 'WAITING_MANUAL' for step in result['task']['steps']))
        self.assertEqual(old['checkpoint']['submission_intent'], result['task']['checkpoint']['submission_intent'])

    async def test_readonly_reconciliation_never_bypasses_rejected_person_or_terminal_source(self):
        self.raise_after_submit = True
        await self.submit()
        for status in ('REJECTED', 'CANCELLED', 'PAUSED', 'SKIPPED'):
            with self.subTest(status=status):
                with self.f.service.session() as session:
                    session.get(m.Task, 'unit-source-task').status = status
                with self.assertRaises(HTTPException):
                    await self.f.service.reconcile_submit(self.task['id'])
        with self.f.service.session() as session:
            session.get(m.Task, 'unit-source-task').status = 'FAILED'
            session.add(m.Review(id='UNIT_CURRENT_PERSON_REVIEW', task_id='unit-source-task', asset_id='unit-person',
                status='rejected', issues=[], note='UNIT_ONLY rejection'))
        with self.assertRaises(HTTPException):
            await self.f.service.reconcile_submit(self.task['id'])
        self.assertEqual(self.submits, 1)
        self.assertEqual(self.f.service.task(self.task['id'])['submission_state'], 'UNKNOWN')

    async def test_manually_selected_cross_task_final_uses_current_donor_review_lineage(self):
        with self.f.service.session() as session:
            task = session.get(m.Task, "unit-source-task")
            donor = m.Task(id="UNIT_DONOR_TASK", project_id=task.project_id, country_id=task.country_id, keyword_id=task.keyword_id,
                country=task.country, keyword=task.keyword, product_id=task.product_id, source_video_id=None, current_stage="final",
                status="COMPLETED", progress=100, thumbnail="", created_at=m.utc_now(), updated_at=m.utc_now(), execution_mode="integration",
                context={"production": {"current_person_asset_id": "unit-person"}}, collector_result={})
            session.add(donor)
            session.flush()
            session.get(m.VideoAsset, "unit-person").task_id = donor.id
            composition = session.get(m.VideoAsset, "unit-compose")
            composition.source_system = "FactoryManualCompose"
            composition.legacy_reference = {**composition.legacy_reference, "manual_compose": True, "person_task_id": donor.id}
            final = session.get(m.VideoAsset, "unit-final")
            final.legacy_reference = {**final.legacy_reference, "manual_compose": True, "person_asset_id": "unit-person", "person_task_id": donor.id}
            task.context = {"production": {"current_person_asset_id": None}, "manual_compose": {"latest_composition_asset_id": composition.id,
                "selection": {"person_asset_id": "unit-person", "person_task_id": donor.id}}}
            session.add(m.Review(id="UNIT_DONOR_REVIEW", task_id=donor.id, asset_id="unit-person", status="approved", issues=[], note="UNIT_ONLY"))
        with self.f.service.session() as session:
            payload = self.f.service.input(session, session.get(m.PublishingTask, self.task["id"]))
            self.assertEqual(payload["video"]["person_asset_id"], "unit-person")
        self.assertIn(self.task["id"], {row["id"] for row in self.f.service.tasks()})
        self.assertIn(self.task["id"], {row["id"] for row in self.f.service.queue()["tasks"]})
        with self.f.service.session() as session:
            donor = session.get(m.Task, "UNIT_DONOR_TASK")
            original = deepcopy(donor.context)
            donor.context = {"production": {"current_person_asset_id": "UNIT_NEWER_PERSON"}}
        with self.assertRaises(HTTPException):
            self.f.service.submit_confirmation(self.task["id"])
        with self.f.service.session() as session:
            session.get(m.Task, "UNIT_DONOR_TASK").context = original
            task = session.get(m.Task, "unit-source-task")
            selection = deepcopy(task.context)
            task.context = {**task.context, "manual_compose": {"selection": {"person_asset_id": "unit-person", "person_task_id": "UNIT_WRONG_OWNER"}}}
        with self.assertRaises(HTTPException):
            self.f.service.submit_confirmation(self.task["id"])
        with self.f.service.session() as session:
            session.get(m.Task, "unit-source-task").context = selection
            session.get(m.Review, "UNIT_DONOR_REVIEW").status = "rejected"
        with self.assertRaises(HTTPException):
            self.f.service.submit_confirmation(self.task["id"])
        self.assertEqual(self.submits, 0)
