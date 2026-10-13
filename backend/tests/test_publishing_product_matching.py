"""Temp SQLite/synthetic observers only; no real product attachment or publishing."""
from copy import deepcopy
from datetime import timedelta
import hashlib
from unittest.mock import AsyncMock
import unittest

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import models as m
from app.domain import now
from app.integrations.publishing.config import PublishingConfig
from app.integrations.publishing.routes import create_publishing_router
from app.integrations.publishing.schemas import AccountInput, ProductInput, TaskInput
from test_real_publishing_service import SQLitePublishingFixture


class ProductMatchingPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = SQLitePublishingFixture()

    async def asyncTearDown(self):
        await self.f.service.shutdown()
        self.f.close()

    def no_platform_id(self):
        with self.f.service.session() as session:
            product = session.get(m.Product, self.f.product["id"])
            product.platform_product_id, product.binding_reference = None, ""

    async def test_runtime_match_default_allows_named_product_without_id_but_not_confirmed(self):
        self.assertEqual(PublishingConfig().product_selection_mode, "runtime_match")
        self.no_platform_id()
        task = self.f.task()
        result = await self.f.service.preflight(task["id"])
        self.assertEqual(result["status"], "READY")
        link = next(check for check in result["checks"] if check["name"] == "product_link")
        self.assertEqual(link["status"], "NOT_CHECKED")
        self.assertEqual(link["reason"], "MATCH_PRODUCT_DURING_SELECTION_NOT_CONFIRMED")
        self.assertEqual(self.f.service.task(task["id"])["product_link_status"], "NOT_CHECKED")
        self.assertEqual(task["checkpoint"]["product_selection_mode"], "runtime_match")

    async def test_exact_id_remains_strict_and_policy_is_bound_to_task(self):
        self.no_platform_id()
        self.f.service.config = self.f.config.model_copy(update={"product_selection_mode": "exact_id"})
        task = self.f.task()
        self.f.service.config = self.f.config
        result = await self.f.service.preflight(task["id"])
        self.assertEqual(result["status"], "WAITING_MANUAL")
        self.assertEqual(result["error_code"], "PUBLISHING_DATA_INCOMPLETE")
        self.assertEqual(next(check for check in result["checks"] if check["name"] == "product_link")["reason"], "REAL_PRODUCT_LINK_ID_REQUIRED")

    async def test_runtime_match_does_not_bypass_missing_caption_or_product_names(self):
        self.no_platform_id()
        task = self.f.task()
        with self.f.service.session() as session:
            product = session.get(m.Product, self.f.product["id"])
            product.descriptions = []
        result = await self.f.service.preflight(task["id"])
        self.assertEqual(result["status"], "WAITING_MANUAL")
        self.assertEqual(result["error_code"], "PUBLISHING_DATA_INCOMPLETE")
        with self.f.service.session() as session:
            product = session.get(m.Product, self.f.product["id"])
            product.name, product.keywords = "", []
        result = await self.f.service.preflight(task["id"])
        self.assertEqual(next(check for check in result["checks"] if check["name"] == "product_link")["reason"], "REAL_PRODUCT_NAME_OR_KEYWORD_REQUIRED")

    async def test_runtime_match_keeps_country_source_task_product_and_account_bindings_strict(self):
        other = self.f.service.save_product(ProductInput(name="UNIT_OTHER_PRODUCT", country="US"))
        account = self.f.service.save_account(AccountInput(name="UNIT_OTHER_ACCOUNT", country="US", product_ids=[other["id"]]))
        with self.assertRaises(HTTPException) as blocked:
            self.f.service.create_task(TaskInput(task_id="unit-source-task", video_id="unit-final", account_id=account["id"], product_id=other["id"]))
        self.assertEqual(blocked.exception.detail, "PUBLISH_COUNTRY_PRODUCT_MISMATCH")
        self.assertEqual(self.f.service.tasks(), [])

    async def confirmation_run(self, *, selected_name=None, selected_id="UNIT_OBSERVED_ID", read_only=False):
        output = self.f.root / "unit-caption-evidence"
        output.mkdir()
        screenshot = output / "UNIT_ONLY.png"
        screenshot.write_bytes(b"UNIT_ONLY_NOT_BROWSER_EVIDENCE")
        self.f.service.config = self.f.service.config.model_copy(update={"stop_after_upload": False, "output_root": output.resolve()})
        task = self.f.task()
        upload = self.f.adapter.execute_step
        async def execute(name, payload):
            if name == "UPLOAD_VIDEO":
                return await upload(name, payload)
            self.f.adapter.steps.append(name)
            if name == "FILL_DESCRIPTION":
                text = payload["publishing_task"]["caption"]
                digest = hashlib.sha256(text.encode()).hexdigest()
                return {"status": "READY", "checkpoint": {"account_id": payload["account"]["id"], "environment_id": payload["account"]["hubstudio_env_id"],
                    "video_sha256": payload["video"]["sha256"], "caption_sha256": digest, "description_id": payload["publishing_task"]["selected_description"]["id"]},
                    "evidence": {"unit_only": True, "source": "OBSERVED_BROWSER", "verified_at": now(), "account_id": payload["account"]["id"],
                        "environment_id": payload["account"]["hubstudio_env_id"], "username": payload["account"]["expected_username"], "video_sha256": payload["video"]["sha256"],
                        "publish_clicked": False, "url": "https://www.tiktok.com/tiktokstudio/upload", "screenshot": str(screenshot), "caption_sha256": digest,
                        "dom_evidence": {"caption": text, "url": "https://www.tiktok.com/tiktokstudio/upload"}}}
            if name != "ATTACH_PRODUCT":
                return {"status": "WAITING_MANUAL", "performed": False, "error_code": "UNIT_ONLY_CONTENT_REVIEW_NOT_PASSED", "evidence": {"unit_only": True}}
            intent = self.f.service.product_intent(payload)
            title = selected_name or payload["product"]["name"]
            proof = {key: intent[key] for key in ("product_id", "account_id", "environment_id", "video_sha256", "mode")}
            proof.update(selected_name=title, selected_id=selected_id)
            return {"status": "READY", "checkpoint": proof, "evidence": {"unit_only": True, "source": "OBSERVED_BROWSER", "verified_at": now(),
                "account_id": intent["account_id"], "environment_id": intent["environment_id"], "username": intent["username"], "video_sha256": intent["video_sha256"],
                "selected_name": title, "selected_id": selected_id, "read_only": read_only, "attached": True, "confirmed": True, "publish_clicked": False,
                "dom_evidence": {"selected_name": title, "selected_id": selected_id, "attached": True, "confirmation_text": "UNIT_ONLY_ATTACHMENT_OBSERVATION", "url": "https://www.tiktok.com/tiktokstudio/upload"}}}
        self.f.adapter.execute_step = AsyncMock(side_effect=execute)
        await self.f.service.create_batch([task["id"]])
        await self.f.service.worker
        return self.f.service.task(task["id"])

    async def test_confirmation_requires_actual_selected_name_id_and_attachment_not_readonly_candidate(self):
        result = await self.confirmation_run(read_only=True)
        self.assertEqual(result["status"], "WAITING_MANUAL")
        self.assertEqual(result["error"], "PRODUCT_CONFIRMATION_EVIDENCE_REQUIRED")
        self.assertEqual(result["product_link_status"], "NOT_CHECKED")
        self.assertEqual(self.f.adapter.steps, ["UPLOAD_VIDEO", "FILL_DESCRIPTION", "ATTACH_PRODUCT"])
        self.assertEqual(result["checkpoint"]["external_outcome"], "UNKNOWN")

    async def test_wrong_product_name_cannot_confirm_different_product_even_with_positive_flags(self):
        result = await self.confirmation_run(selected_name="UNIT_DIFFERENT_BODY_PRODUCT")
        self.assertEqual(result["error"], "PRODUCT_CONFIRMATION_EVIDENCE_REQUIRED")
        self.assertEqual(result["product_link_status"], "NOT_CHECKED")

    async def test_exact_id_wrong_observed_id_cannot_confirm_even_matching_name(self):
        self.f.service.config = self.f.config.model_copy(update={"product_selection_mode": "exact_id"})
        result = await self.confirmation_run(selected_id="UNIT_WRONG_PLATFORM_ID")
        self.assertEqual(result["error"], "PRODUCT_CONFIRMATION_EVIDENCE_REQUIRED")
        self.assertEqual(result["product_link_status"], "NOT_CHECKED")

    async def test_bound_nonreadonly_unit_confirmation_only_sets_product_not_content_or_publish(self):
        result = await self.confirmation_run()
        self.assertEqual(result["product_link_status"], "PRODUCT_CONFIRMED")
        self.assertEqual(result["moderation_status"], "UNKNOWN")
        self.assertEqual(result["submission_state"], "NOT_SUBMITTED")
        self.assertEqual(result["error"], "UNIT_ONLY_CONTENT_REVIEW_NOT_PASSED")
        self.assertNotIn("SUBMIT", self.f.adapter.steps)
        self.assertEqual(self.f.repo.state()["publishing"][0]["product_link_status"], "PRODUCT_CONFIRMED")


class ProductObservationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = SQLitePublishingFixture()
        self.f.service.config = self.f.config.model_copy(update={"output_root": (self.f.root / "unit-outputs").resolve()})
        self.f.service.config.output_root.mkdir()
        self.screenshot = self.f.service.config.output_root / "UNIT_ONLY_NOT_REAL_SCREENSHOT.png"
        self.screenshot.write_bytes(b"UNIT_ONLY_SCREENSHOT")
        self.task = self.f.task(upload_only=True)
        await self.f.service.create_batch([self.task["id"]])
        await self.f.service.worker
        self.f.adapter.observe_product = AsyncMock(side_effect=self.observation)

    async def asyncTearDown(self):
        await self.f.service.shutdown()
        self.f.close()

    async def observation(self, payload):
        return {"status": "READY", "evidence": {"unit_only": True, "source": "OBSERVED_BROWSER", "read_only": True,
            "account_id": payload["account"]["id"], "environment_id": payload["account"]["hubstudio_env_id"],
            "username": payload["account"]["expected_username"], "video_sha256": payload["video"]["sha256"], "verified_at": now(),
            "url": "https://www.tiktok.com/tiktokstudio/upload", "screenshot": str(self.screenshot),
            "candidates": [{"product_name": payload["product"]["name"] + " UNIT_VARIANT", "product_id": "UNIT_VISIBLE_ID", "availability": "Activo", "status": "Activo", "selectable": True, "matches": True}],
            "match_count": 1, "scope": "VISIBLE_CATALOG_PAGE_ONLY", "attached": False, "confirmed": False, "publish_clicked": False}}

    async def test_read_only_candidates_persist_checkpoint_audit_not_confirmation_or_id(self):
        before = self.f.service.task(self.task["id"])
        result = await self.f.service.observe_product(self.task["id"])
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["match_status"], "MATCHED_CANDIDATE")
        after = result["task"]
        self.assertEqual(after["status"], before["status"])
        self.assertEqual(after["steps"], before["steps"])
        self.assertEqual(after["runs"], before["runs"])
        self.assertEqual(after["checkpoint"]["upload_intent"], before["checkpoint"]["upload_intent"])
        self.assertEqual(after["product_link_status"], "NOT_CHECKED")
        self.assertEqual(after["submission_state"], "NOT_SUBMITTED")
        self.assertEqual(self.f.adapter.steps, ["UPLOAD_VIDEO"])
        with self.f.service.session() as session:
            self.assertEqual(session.get(m.Product, self.f.product["id"]).platform_product_id, "unit-platform-id")
            self.assertEqual(session.scalar(select(func.count()).select_from(m.AuditLog).where(m.AuditLog.action == "publishing.product_observed")), 1)

    async def test_observer_rejects_wrong_intent_identity_time_url_screenshot_and_confirmed_claims(self):
        with self.f.service.session() as session:
            payload = self.f.service.input(session, session.get(m.PublishingTask, self.task["id"]))
        proof = await self.observation(payload)
        for field, value in (("account_id", "UNIT_WRONG"), ("environment_id", "UNIT_WRONG"), ("username", "UNIT_WRONG"), ("video_sha256", "UNIT_WRONG"),
                ("read_only", False), ("attached", True), ("confirmed", True), ("publish_clicked", True), ("verified_at", (m.utc_now() + timedelta(hours=1)).isoformat()),
                ("url", "https://example.invalid/upload"), ("url", None), ("screenshot", str(self.f.root / "outside.png")), ("screenshot", None),
                ("match_count", True), ("match_count", 2), ("scope", "ALL_PRODUCTS")):
            with self.subTest(field=field, value=value):
                invalid = deepcopy(proof)
                invalid["evidence"][field] = value
                self.f.adapter.observe_product = AsyncMock(return_value=invalid)
                result = await self.f.service.observe_product(self.task["id"])
                self.assertEqual(result["status"], "WAITING_MANUAL")
                self.assertEqual(result["observed"]["error_code"], "PRODUCT_OBSERVATION_EVIDENCE_REQUIRED")
                self.assertEqual(result["task"]["product_link_status"], "NOT_CHECKED")

    async def test_missing_verified_upload_or_unknown_checkpoint_blocks_observer_before_external_call(self):
        with self.f.service.session() as session:
            row = session.get(m.PublishingTask, self.task["id"])
            original = deepcopy(row.checkpoint)
            row.checkpoint = {**original, "UPLOAD_VIDEO": {"status": "READY"}}
        with self.assertRaises(HTTPException) as blocked:
            await self.f.service.observe_product(self.task["id"])
        self.assertEqual(blocked.exception.detail, "VERIFIED_UPLOADED_VIDEO_REQUIRED")
        with self.f.service.session() as session:
            session.get(m.PublishingTask, self.task["id"]).checkpoint = {**original, "external_outcome": "UNKNOWN"}
        with self.assertRaises(HTTPException) as blocked:
            await self.f.service.observe_product(self.task["id"])
        self.assertEqual(blocked.exception.detail, "PRODUCT_OBSERVATION_SAFE_CHECKPOINT_REQUIRED")
        self.f.adapter.observe_product.assert_not_called()

    async def test_product_or_control_change_during_await_cannot_persist_old_candidates(self):
        async def changed(payload):
            with self.f.service.session() as session:
                session.get(m.Product, self.f.product["id"]).name = "UNIT_CHANGED_DURING_OBSERVATION"
            return await self.observation(payload)
        self.f.adapter.observe_product = AsyncMock(side_effect=changed)
        with self.assertRaises(HTTPException) as blocked:
            await self.f.service.observe_product(self.task["id"])
        self.assertEqual(blocked.exception.detail, "PRODUCT_OBSERVATION_INPUT_CHANGED")
        self.assertNotIn("product_observation", self.f.service.task(self.task["id"])["checkpoint"])

    async def test_zero_or_multiple_candidates_are_not_global_uniqueness_or_confirmation(self):
        with self.f.service.session() as session:
            payload = self.f.service.input(session, session.get(m.PublishingTask, self.task["id"]))
        proof = await self.observation(payload)
        for count in (0, 2):
            with self.subTest(count=count):
                observation = deepcopy(proof)
                observation["evidence"]["candidates"] = [deepcopy(proof["evidence"]["candidates"][0]) for _ in range(count)]
                observation["evidence"]["match_count"] = count
                self.f.adapter.observe_product = AsyncMock(return_value=observation)
                result = await self.f.service.observe_product(self.task["id"])
                self.assertEqual(result["status"], "READY")
                self.assertEqual(result["scope"], "VISIBLE_CATALOG_PAGE_ONLY")
                self.assertEqual(result["match_status"], "NO_MATCH_VISIBLE" if count == 0 else "MULTIPLE_VISIBLE_CANDIDATES")
                self.assertEqual(result["task"]["product_link_status"], "NOT_CHECKED")

    async def test_actual_router_response_is_readonly_and_candidate_fields_are_explicit(self):
        router = create_publishing_router(self.f.repo, self.f.service.config)
        router.service.adapter = self.f.adapter
        app = FastAPI()
        app.include_router(router)
        with TestClient(app) as client:
            response = client.post("/api/publishing/tasks/" + self.task["id"] + "/observe-product")
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["match_count"], 1)
        self.assertEqual(result["candidates"][0]["product_id"], "UNIT_VISIBLE_ID")
        self.assertEqual(result["public_submission"], "NOT_PERFORMED")
        self.assertEqual(result["task"]["product_link_status"], "NOT_CHECKED")

    async def test_disabled_or_inactive_name_match_is_valid_observation_not_selectable_candidate(self):
        with self.f.service.session() as session:
            payload = self.f.service.input(session, session.get(m.PublishingTask, self.task["id"]))
        proof = await self.observation(payload)
        for status in ("Activo", "Inactivo"):
            with self.subTest(status=status):
                observation = deepcopy(proof)
                candidate = observation["evidence"]["candidates"][0]
                candidate.update(status=status, availability=status, selectable=False, matches=False)
                observation["evidence"]["match_count"] = 0
                self.f.adapter.observe_product = AsyncMock(return_value=observation)
                result = await self.f.service.observe_product(self.task["id"])
                self.assertEqual(result["status"], "READY")
                self.assertEqual(result["match_count"], 0)
                self.assertEqual(result["match_status"], "NO_MATCH_VISIBLE")

    async def test_upload_only_cannot_enter_attach_step_even_with_verified_upload(self):
        before = self.f.service.task(self.task["id"])
        with self.assertRaises(HTTPException) as blocked:
            self.f.service.step_start(self.task["id"], before["runs"][0]["id"], "ATTACH_PRODUCT")
        self.assertEqual(blocked.exception.detail, "UPLOAD_ONLY_PRODUCT_ATTACHMENT_FORBIDDEN")
        self.assertEqual(self.f.service.task(self.task["id"]), before)


if __name__ == "__main__":
    unittest.main()
