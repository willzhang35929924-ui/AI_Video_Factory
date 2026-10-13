"""Contract tests with isolated SQLite and transport mocks, not production acceptance."""
import asyncio
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app import models as m
from app.integrations.publishing.config import DescriptionAIConfig
from app.integrations.publishing.description_generation import ContentReviewRequest, DescriptionGenerationService, GenerationRequest, validate_captions
from app.integrations.publishing.description_selector import description_pool_hash, description_review_ready
from app.integrations.publishing.schemas import ProductInput
from test_real_publishing_service import SQLitePublishingFixture


def response_data():
    return {"descriptions": [{"ordinal": i, "text": f"Neutral presentation {i}: view the supplied product.", "fact_ids": []} for i in range(1, 21)]}


class DescriptionValidationTests(unittest.TestCase):
    def test_twenty_unique_ordinals_and_no_claims(self):
        rows = validate_captions(response_data(), DescriptionAIConfig(), [])
        self.assertEqual([row["id"] for row in rows], [str(i).zfill(2) for i in range(1, 21)])

    def test_duplicate_ordinals_texts_claims_and_invalid_facts_are_rejected(self):
        for kind in ("ordinal", "duplicate", "claim", "facts"):
            with self.subTest(kind=kind):
                data = response_data()
                if kind == "ordinal":
                    data["descriptions"][0]["ordinal"] = 20
                if kind == "duplicate":
                    data["descriptions"][0]["text"] = data["descriptions"][1]["text"]
                if kind == "claim":
                    data["descriptions"][0]["text"] = "Guaranteed cure for pain"
                if kind == "facts":
                    data["descriptions"][0]["fact_ids"] = [1]
                with self.assertRaises(ValueError):
                    validate_captions(data, DescriptionAIConfig(), [])

    def test_secret_value_and_untrusted_endpoint_not_accepted_by_config(self):
        for value in ({"api_key": "UNIT_KEY_NOT_REAL"}, {"base_url": "https://user:password@example.test/v1"}, {"api_key_env": "unit key"}):
            with self.assertRaises(ValueError):
                DescriptionAIConfig.model_validate(value)


class DescriptionServiceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = SQLitePublishingFixture()
        self.addCleanup(self.fixture.close)
        self.service = self.fixture.service
        self.service.config.output_root = self.fixture.root / "descriptions-output"
        self.product = self.service.save_product(ProductInput(name="UNIT_DESCRIPTION_PRODUCT", country="MX", keywords=["UNIT_KEYWORD"]))
        self.service.config.description_ai = DescriptionAIConfig(enabled=True, model="UNIT_MODEL", base_url="https://unit.invalid/v1", sync_to_tencent=False)
        self.descriptions = DescriptionGenerationService(self.service, SimpleNamespace(), SimpleNamespace())
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"VIDEO_PUBLISH_DESCRIPTION_API_KEY": "UNIT_NOT_REAL_SECRET"}).start()

    def generate(self, reply):
        async def run():
            with patch("app.integrations.publishing.description_generation.httpx.AsyncClient.post", new=reply):
                row = await self.descriptions.create(self.product["id"], GenerationRequest(confirm_external_request=True))
                await self.descriptions.jobs[row["id"]]
                return self.descriptions.get(row["id"])
        return asyncio.run(run())

    def test_real_service_persists_validated_local_output_without_secret(self):
        reply = AsyncMock(return_value=httpx.Response(200, json={"id": "unit-request", "choices": [{"message": {"content": json.dumps(response_data())}}]}))
        row = self.generate(reply)
        self.assertEqual(row["status"], "READY")
        self.assertEqual(len(row["descriptions"]), 20)
        self.assertNotIn("UNIT_NOT_REAL_SECRET", json.dumps(row, default=str))
        saved = next(product for product in self.service.products() if product["id"] == self.product["id"])
        self.assertEqual(len(saved["descriptions"]), 20)
        self.assertEqual(saved["publishing_context"]["description_source"], "AI_LOCAL")
        self.assertEqual(reply.await_count, 1)

    def test_generated_text_needs_explicit_fact_review_and_changes_invalidate_it(self):
        row = self.generate(AsyncMock(return_value=httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(response_data())}}]})))
        saved = next(product for product in self.service.products() if product["id"] == self.product["id"])
        self.assertFalse(description_review_ready(saved))
        digest = description_pool_hash(saved["descriptions"])
        with self.assertRaises(Exception) as caught:
            self.descriptions.content_review(row["id"], ContentReviewRequest(sha256=digest))
        self.assertEqual(caught.exception.status_code, 422)
        reviewed = self.descriptions.content_review(row["id"], ContentReviewRequest(confirm_facts=True, sha256=digest))
        self.assertEqual(reviewed["evidence"]["content_review"]["status"], "APPROVED")
        saved = next(product for product in self.service.products() if product["id"] == self.product["id"])
        self.assertTrue(description_review_ready(saved))
        saved["descriptions"][0]["text"] = "Changed identity text."
        self.assertFalse(description_review_ready(saved))

    def test_catalog_edit_cannot_forge_or_remove_ai_content_review(self):
        row = self.generate(AsyncMock(return_value=httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(response_data())}}]})))
        saved = next(product for product in self.service.products() if product["id"] == self.product["id"])
        digest = description_pool_hash(saved["descriptions"])
        edited = self.service.save_product(ProductInput(name=saved["name"], country=saved["country"], keywords=saved["keywords"],
            descriptions=[{key: value for key, value in item.items() if key in {"id", "text", "weight", "enabled"}} for item in saved["descriptions"]],
            publishing_context={"description_review": {"sha256": digest}}), self.product["id"])
        self.assertEqual(edited["publishing_context"]["description_generation_id"], row["id"])
        self.assertFalse(description_review_ready(edited))

    def test_current_saved_text_snapshot_never_overwrites_generation_history(self):
        row = self.generate(AsyncMock(return_value=httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(response_data())}}]})))
        original = row["descriptions"]
        saved = next(product for product in self.service.products() if product["id"] == self.product["id"])
        texts = [{key: value for key, value in item.items() if key in {"id", "text", "weight", "enabled"}} for item in saved["descriptions"]]
        texts[0]["text"] = "UNIT_CURRENT_SAVED_TEXT_NOT_OLD_GENERATION"
        edited = self.service.save_product(ProductInput(name=saved["name"], country=saved["country"], keywords=saved["keywords"],
            descriptions=texts, publishing_context={"description_review_required": False}), self.product["id"])
        current = self.descriptions.get(row["id"])
        self.assertEqual(current["descriptions"], original)
        self.assertEqual(current["current_product"]["descriptions"], edited["descriptions"])
        self.assertEqual(current["current_product"]["descriptions"][0]["text"], texts[0]["text"])
        self.assertEqual(current["current_product"]["sha256"], description_pool_hash(edited["descriptions"]))
        self.assertEqual(current["current_product"]["status"], "REQUIRED")
        with self.assertRaises(Exception) as caught:
            self.descriptions.content_review(row["id"], ContentReviewRequest(confirm_facts=True, sha256=description_pool_hash(original)))
        self.assertEqual(caught.exception.status_code, 409)
        reviewed = self.descriptions.review_product_content(self.product["id"], ContentReviewRequest(confirm_facts=True, sha256=current["current_product"]["sha256"]))
        self.assertEqual(reviewed["status"], "APPROVED")
        with self.service.session() as session:
            self.assertEqual(session.get(m.DescriptionGeneration, row["id"]).descriptions, original)
        self.assertEqual(self.descriptions.get(row["id"])["descriptions"], original)

    def test_manual_product_pool_is_not_forced_into_generated_source_review(self):
        saved = self.service.save_product(ProductInput(name="UNIT_HAND_WRITTEN_POOL", country="MX", descriptions=[{"id": "01", "text": "UNIT_USER_SUPPLIED_CAPTION"}],
            publishing_context={"description_review_required": True, "description_review": {"source": "USER_CONFIRMED", "sha256": "0" * 64}}))
        self.assertNotIn("description_review_required", saved["publishing_context"])
        self.assertNotIn("description_review", saved["publishing_context"])
        self.assertTrue(description_review_ready(saved))
        self.assertEqual(self.descriptions.product_review(saved["id"])["status"], "NOT_REQUIRED")
        with self.assertRaises(Exception) as caught:
            self.descriptions.review_product_content(self.product["id"], ContentReviewRequest(confirm_facts=True, sha256=description_pool_hash([])))
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(caught.exception.detail, "DESCRIPTION_POOL_EMPTY")

    def test_provider_metadata_cannot_persist_secret_or_arbitrary_usage(self):
        body = {"id": "unit-UNIT_NOT_REAL_SECRET-request", "usage": {"prompt_tokens": True, "completion_tokens": 17, "total_tokens": -1,
            "secret": "UNIT_NOT_REAL_SECRET", "billing": {"api_key": "UNIT_NOT_REAL_SECRET"}},
            "choices": [{"finish_reason": "UNIT_NOT_REAL_SECRET", "message": {"content": json.dumps(response_data())}}]}
        row = self.generate(AsyncMock(return_value=httpx.Response(200, json=body)))
        self.assertEqual(row["status"], "READY")
        self.assertEqual(row["evidence"]["usage"], {"completion_tokens": 17})
        self.assertEqual(row["evidence"]["request_id"], "unit-[REDACTED]-request")
        self.assertIsNone(row["evidence"]["finish_reason"])
        self.assertNotIn("UNIT_NOT_REAL_SECRET", json.dumps(row, default=str))
        for name in ("response.json", "validated-response.json"):
            self.assertNotIn("UNIT_NOT_REAL_SECRET", (self.descriptions.root / row["id"] / name).read_text())

    def test_provider_token_integer_allowlist_and_simple_finish_reason_are_retained(self):
        body = {"id": "unit-request", "usage": {"prompt_tokens": 0, "completion_tokens": 20, "total_tokens": 20, "extra": 100},
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(response_data())}}]}
        row = self.generate(AsyncMock(return_value=httpx.Response(200, json=body)))
        self.assertEqual(row["evidence"]["usage"], {"prompt_tokens": 0, "completion_tokens": 20, "total_tokens": 20})
        self.assertEqual(row["evidence"]["finish_reason"], "stop")

    def test_unreviewed_changed_pool_cannot_reuse_cached_selection_as_manual_caption(self):
        task = self.fixture.task()
        selected = self.service.select_description(task["id"])
        self.assertEqual(selected["status"], "READY")
        with self.service.session() as session:
            product = session.get(m.Product, self.fixture.product["id"])
            product.publishing_context = {**product.publishing_context, "description_review_required": True}
        result = self.service.select_description(task["id"])
        self.assertEqual(result["status"], "WAITING_MANUAL")
        self.assertEqual(result["error_code"], "DESCRIPTION_CONTENT_REVIEW_REQUIRED")
        self.assertEqual(self.service.task(task["id"])["selected_description"], selected["checkpoint"])
        with self.service.session() as session:
            row = session.get(m.PublishingTask, task["id"])
            for validate in (self.service.input, self.service.ready_to_queue):
                with self.subTest(validate=validate.__name__), self.assertRaises(Exception) as blocked:
                    validate(session, row)
                self.assertEqual(blocked.exception.status_code, 409)
                self.assertEqual(blocked.exception.detail, "DESCRIPTION_CONTENT_REVIEW_REQUIRED")

    def test_timeout_is_unknown_and_same_input_never_resubmits(self):
        reply = AsyncMock(side_effect=httpx.ReadTimeout("UNIT_TIMEOUT"))
        row = self.generate(reply)
        self.assertEqual(row["error_code"], "UNKNOWN_EXTERNAL_REQUEST")
        again = asyncio.run(self.descriptions.create(self.product["id"], GenerationRequest(confirm_external_request=True)))
        self.assertEqual(again["id"], row["id"])
        self.assertEqual(reply.await_count, 1)

    def test_bad_response_is_not_saved_as_product_description(self):
        reply = AsyncMock(return_value=httpx.Response(200, json={"choices": [{"message": {"content": "not-json"}}]}))
        row = self.generate(reply)
        self.assertEqual(row["status"], "WAITING_MANUAL")
        self.assertEqual(row["descriptions"], [])
        response_path = self.descriptions.root / row["id"] / "response.json"
        self.assertEqual(json.loads(response_path.read_text())["content"], "not-json")

    def test_invalid_structure_reports_fields_without_response_secrets(self):
        value = {"descriptions": [{"ordinal": 1, "text": "UNIT_NOT_REAL_SECRET"}]}
        row = self.generate(AsyncMock(return_value=httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(value)}}]})))
        self.assertEqual(row["status"], "WAITING_MANUAL")
        self.assertNotIn("UNIT_NOT_REAL_SECRET", json.dumps(row, default=str))
        self.assertNotIn("UNIT_NOT_REAL_SECRET", (self.descriptions.root / row["id"] / "response.json").read_text())

    def test_explicit_retry_creates_new_history_but_unknown_cannot_retry(self):
        row = self.generate(AsyncMock(return_value=httpx.Response(200, json={"choices": [{"message": {"content": "not-json"}}]})))
        async def retry():
            with patch("app.integrations.publishing.description_generation.httpx.AsyncClient.post", new=AsyncMock(return_value=httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(response_data())}}]}))) as reply:
                new = await self.descriptions.create(self.product["id"], GenerationRequest(confirm_external_request=True, retry_generation_id=row["id"]))
                await self.descriptions.jobs[new["id"]]
                self.assertEqual(reply.await_count, 1)
                self.assertEqual(self.descriptions.get(new["id"])["status"], "READY")
                self.assertNotEqual(row["id"], new["id"])
                self.assertEqual(self.descriptions.get(row["id"])["status"], "WAITING_MANUAL")
        asyncio.run(retry())
        self.descriptions.update(row["id"], error_code="UNKNOWN_EXTERNAL_REQUEST")
        with self.assertRaises(Exception) as caught:
            asyncio.run(self.descriptions.create(self.product["id"], GenerationRequest(confirm_external_request=True, retry_generation_id=row["id"])))
        self.assertEqual(caught.exception.status_code, 409)

    def test_http_error_does_not_log_body_or_retry(self):
        reply = AsyncMock(return_value=httpx.Response(401, text="UNIT_NOT_REAL_SECRET"))
        row = self.generate(reply)
        self.assertEqual(row["status"], "FAILED")
        self.assertEqual(row["error_code"], "DESCRIPTION_API_HTTP_401")
        self.assertNotIn("UNIT_NOT_REAL_SECRET", json.dumps(row, default=str))
        self.assertEqual(reply.await_count, 1)

    def test_confirmation_required_and_restart_preserves_unknown(self):
        with self.assertRaises(Exception) as caught:
            asyncio.run(self.descriptions.create(self.product["id"], GenerationRequest()))
        self.assertEqual(caught.exception.status_code, 422)
        row = self.generate(AsyncMock(side_effect=httpx.ReadTimeout("UNIT_TIMEOUT")))
        self.descriptions.update(row["id"], status="SUBMITTED")
        asyncio.run(self.descriptions.recover())
        self.assertEqual(self.descriptions.get(row["id"])["error_code"], "UNKNOWN_EXTERNAL_REQUEST")
