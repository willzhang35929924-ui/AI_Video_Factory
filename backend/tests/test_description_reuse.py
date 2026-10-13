"""UNIT_ONLY isolated SQLite/image bytes; no model, cloud document, or production writes."""
from copy import deepcopy
import json
import unittest
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import models as m
from app.integrations.publishing.description_generation import ContentReviewRequest, DescriptionGenerationService
from app.integrations.publishing.description_selector import description_pool_hash, description_review_ready
from app.integrations.publishing.routes import create_publishing_router
from app.integrations.publishing.schemas import DescriptionReuse, ProductInput
from test_real_publishing_service import SQLitePublishingFixture


class DescriptionReuseTests(unittest.TestCase):
    def setUp(self):
        self.f = SQLitePublishingFixture()
        self.addCleanup(self.f.close)
        self.image = self.f.root / "UNIT_ONLY_REFERENCE.jpg"
        self.other_image = self.f.root / "UNIT_ONLY_REFERENCE_COPY.jpg"
        self.image.write_bytes(b"UNIT_ONLY_IMAGE_BYTES_NOT_REAL_ACCEPTANCE")
        self.other_image.write_bytes(self.image.read_bytes())
        self.texts = [{"id": str(i).zfill(2), "text": "UNIT_ONLY saved text " + str(i), "weight": 1, "enabled": True} for i in range(1, 21)]
        self.target = self.f.service.save_product(ProductInput(name="UNIT_PRODUCT", country="MX", image=str(self.image),
            keywords=["UNIT_EXACT_KEYWORD"], publishing_context={"source_document": "UNIT_TARGET_DOC", "source_row": 19, "context_id": "UNIT_TARGET_CONTEXT", "custom": "KEEP"}))
        self.source = self.f.service.save_product(ProductInput(name="UNIT_PRODUCT", country="MX", image=str(self.other_image),
            keywords=["UNIT_EXACT_KEYWORD"], descriptions=self.texts, platform_product_id="UNIT_WRONG_BINDING_DO_NOT_COPY",
            binding_reference="UNIT_WRONG_BINDING_DO_NOT_COPY", publishing_context={"source_document": "UNIT_SOURCE_DOC", "source_row": 3,
                "context_id": "UNIT_SOURCE_CONTEXT", "keyword": "UNIT_EXACT_KEYWORD"}))
        self.texts = deepcopy(self.source["descriptions"])
        with self.f.service.session() as session:
            row = session.get(m.Product, self.source["id"])
            row.publishing_context = {**row.publishing_context, "description_review_required": True}
        self.review = DescriptionGenerationService(self.f.service, SimpleNamespace(), SimpleNamespace())

    def request(self):
        options = self.f.service.description_reuse_options(self.target["id"])
        source = next(row for row in options["sources"] if row["product_id"] == self.source["id"])
        return DescriptionReuse(source_product_id=source["product_id"], source_pool_sha256=source["pool_sha256"],
            source_input_sha256=source["input_sha256"], target_input_sha256=options["target"]["input_sha256"], confirmed=True)

    def target_product(self):
        return next(row for row in self.f.service.products() if row["id"] == self.target["id"])

    def assert_rejected(self, request, code):
        with self.assertRaises(HTTPException) as rejected:
            self.f.service.reuse_descriptions(self.target["id"], request)
        self.assertEqual(rejected.exception.status_code, 409)
        self.assertIn(code, rejected.exception.detail)
        self.assertFalse(self.target_product()["descriptions"])

    def test_options_are_readonly_and_match_actual_image_hash_not_path_or_keyword_guess(self):
        before = self.f.repo.state()
        options = self.f.service.description_reuse_options(self.target["id"])
        self.assertEqual([row["product_id"] for row in options["sources"]], [self.source["id"]])
        self.assertEqual(options["target"]["product_image_sha256"], options["sources"][0]["product_image_sha256"])
        self.assertNotEqual(self.image, self.other_image)
        self.assertEqual(options["sources"][0]["content_review"], "REQUIRED")
        self.assertEqual(self.f.repo.state(), before)

    def test_explicit_reuse_preserves_target_context_and_binding_does_not_inherit_fact_review(self):
        request = self.request()
        result = self.f.service.reuse_descriptions(self.target["id"], request)
        self.assertFalse(result["reused"])
        self.assertFalse(result["model_called"])
        self.assertFalse(result["cloud_document_modified"])
        saved = self.target_product()
        self.assertEqual(saved["descriptions"], self.texts)
        self.assertIsNone(saved["platform_product_id"])
        self.assertEqual(saved["binding_reference"], "")
        context = saved["publishing_context"]
        self.assertEqual(context["source_document"], "UNIT_TARGET_DOC")
        self.assertEqual(context["source_row"], 19)
        self.assertEqual(context["context_id"], "UNIT_TARGET_CONTEXT")
        self.assertEqual(context["custom"], "KEEP")
        self.assertTrue(context["description_review_required"])
        self.assertFalse(description_review_ready(saved))
        self.assertEqual(context["description_reuse"]["source_context"]["source_document"], "UNIT_SOURCE_DOC")
        self.assertEqual(context["description_reuse"]["source_pool_sha256"], description_pool_hash(self.texts))
        source = next(row for row in self.f.service.products() if row["id"] == self.source["id"])
        self.assertEqual(source["platform_product_id"], "UNIT_WRONG_BINDING_DO_NOT_COPY")
        with self.f.service.session() as session:
            self.assertTrue(session.scalar(select(m.AuditLog.id).where(m.AuditLog.action == "publishing.description_pool_reused", m.AuditLog.entity_id == saved["id"])))

    def test_confirm_is_required_and_schema_cannot_accept_true_as_string(self):
        request = self.request().model_copy(update={"confirmed": False})
        with self.assertRaises(HTTPException) as rejected:
            self.f.service.reuse_descriptions(self.target["id"], request)
        self.assertEqual(rejected.exception.status_code, 422)
        with self.assertRaises(ValueError):
            DescriptionReuse.model_validate({**request.model_dump(), "confirmed": "true"})
        self.assertFalse(self.target_product()["descriptions"])

    def test_repeat_of_same_confirmed_reuse_is_readonly_idempotent(self):
        request = self.request()
        first = self.f.service.reuse_descriptions(self.target["id"], request)
        before = self.f.repo.state()
        second = self.f.service.reuse_descriptions(self.target["id"], request)
        self.assertTrue(second["reused"])
        self.assertEqual(first["lineage"], second["lineage"])
        self.assertEqual(self.f.repo.state(), before)

    def test_source_text_or_source_context_change_invalidates_preview(self):
        for field in ("text", "context"):
            with self.subTest(field=field):
                request = self.request()
                with self.f.service.session() as session:
                    source = session.get(m.Product, self.source["id"])
                    original = deepcopy(source.descriptions if field == "text" else source.publishing_context)
                    if field == "text":
                        rows = deepcopy(source.descriptions)
                        rows[0]["text"] = "UNIT_CHANGED_TEXT"
                        source.descriptions = rows
                    else:
                        source.publishing_context = {**source.publishing_context, "source_row": 4}
                self.assert_rejected(request, "DESCRIPTION_REUSE_SOURCE_CHANGED")
                with self.f.service.session() as session:
                    source = session.get(m.Product, self.source["id"])
                    if field == "text":
                        source.descriptions = original
                    else:
                        source.publishing_context = original

    def test_target_edit_after_preview_is_never_overwritten(self):
        request = self.request()
        with self.f.service.session() as session:
            target = session.get(m.Product, self.target["id"])
            target.publishing_context = {**target.publishing_context, "custom": "USER_EDIT_DURING_PREVIEW"}
        self.assert_rejected(request, "DESCRIPTION_REUSE_TARGET_CHANGED")
        self.assertEqual(self.target_product()["publishing_context"]["custom"], "USER_EDIT_DURING_PREVIEW")

    def test_changed_reference_image_is_rejected_even_when_filename_hash_claims_match(self):
        request = self.request()
        self.other_image.write_bytes(b"UNIT_DIFFERENT_ACTUAL_IMAGE")
        self.assert_rejected(request, "DESCRIPTION_REUSE_PRODUCT_IDENTITY_MISMATCH")
        self.assertFalse(self.f.service.description_reuse_options(self.target["id"])["sources"])

    def test_same_country_but_different_exact_keyword_cannot_reuse(self):
        request = self.request()
        with self.f.service.session() as session:
            source = session.get(m.Product, self.source["id"])
            source.keywords = ["unit_exact_keyword"]
            source.publishing_context = {**source.publishing_context, "keyword": "unit_exact_keyword"}
        self.assert_rejected(request, "DESCRIPTION_REUSE_PRODUCT_IDENTITY_MISMATCH")
        self.assertFalse(self.f.service.description_reuse_options(self.target["id"])["sources"])

    def test_missing_or_untrusted_reference_image_is_not_a_reusable_identity(self):
        for path in ("", "relative.jpg", str(self.f.root / "MISSING.jpg"), str(self.f.root.parent / "OUTSIDE.jpg")):
            with self.subTest(path=path):
                with self.f.service.session() as session:
                    session.get(m.Product, self.target["id"]).image = path
                with self.assertRaises(HTTPException):
                    self.f.service.description_reuse_options(self.target["id"])

    def test_partial_pool_is_not_an_option_or_a_valid_direct_request(self):
        request = self.request()
        with self.f.service.session() as session:
            session.get(m.Product, self.source["id"]).descriptions = self.texts[:19]
        self.assertFalse(self.f.service.description_reuse_options(self.target["id"])["sources"])
        self.assert_rejected(request, "DESCRIPTION_REUSE_SOURCE_CHANGED")

    def test_existing_target_text_is_protected_instead_of_overwritten(self):
        with self.f.service.session() as session:
            session.get(m.Product, self.target["id"]).descriptions = [{"id": "01", "text": "UNIT_TARGET_MANUAL_TEXT", "weight": 1, "enabled": True}]
        request = self.request()
        with self.assertRaises(HTTPException) as rejected:
            self.f.service.reuse_descriptions(self.target["id"], request)
        self.assertIn("DESCRIPTION_REUSE_TARGET_NOT_EMPTY", rejected.exception.detail)
        self.assertEqual(self.target_product()["descriptions"][0]["text"], "UNIT_TARGET_MANUAL_TEXT")

    def test_ready_generation_readback_evidence_is_preserved_not_reexecuted(self):
        with self.f.service.session() as session:
            session.add(m.DescriptionGeneration(id="UNIT_SAVED_GEN", product_id=self.source["id"], country="MX", keyword="UNIT_EXACT_KEYWORD",
                model="UNIT_ALREADY_USED_MODEL", prompt_version="UNIT_V1", input_fingerprint="f" * 64, status="READY", request={},
                descriptions=self.texts, evidence={"readback": {"readback_sha256": "e" * 64}, "model_called": True}))
            session.add(m.AuditLog(id="UNIT_SOURCE_IMPORT", action="publishing.description_source_imported", entity_id=self.source["id"],
                message=json.dumps({"sha256": description_pool_hash(self.texts), "source_context": "UNIT_SOURCE_CONTEXT", "content_review": "REQUIRED"}), created_at=m.utc_now()))
        result = self.f.service.reuse_descriptions(self.target["id"], self.request())
        self.assertEqual(result["lineage"]["source_generation_evidence"][0]["evidence"]["readback"]["readback_sha256"], "e" * 64)
        self.assertEqual(result["lineage"]["source_catalog_evidence"][0]["id"], "UNIT_SOURCE_IMPORT")
        self.assertFalse(result["model_called"])
        self.assertEqual(self.review.product_review(self.target["id"])["status"], "REQUIRED")

    def test_source_approved_facts_do_not_approve_target_but_target_can_be_reviewed_normally(self):
        digest = description_pool_hash(self.texts)
        self.review.review_product_content(self.source["id"], ContentReviewRequest(confirm_facts=True, sha256=digest))
        self.f.service.reuse_descriptions(self.target["id"], self.request())
        self.assertFalse(description_review_ready(self.target_product()))
        reviewed = self.review.review_product_content(self.target["id"], ContentReviewRequest(confirm_facts=True, sha256=digest))
        self.assertEqual(reviewed["status"], "APPROVED")
        self.assertTrue(description_review_ready(self.target_product()))

    def test_active_generation_and_unresolved_publish_preserve_inputs(self):
        request = self.request()
        with self.f.service.session() as session:
            session.add(m.DescriptionGeneration(id="UNIT_ACTIVE_GEN", product_id=self.target["id"], country="MX", keyword="UNIT_EXACT_KEYWORD",
                model="UNIT_MODEL", prompt_version="UNIT_V1", input_fingerprint="a" * 64, status="SUBMITTED", request={}, descriptions=[], evidence={}))
        self.assert_rejected(request, "DESCRIPTION_GENERATION_RUNNING")
        with self.f.service.session() as session:
            generation = session.get(m.DescriptionGeneration, "UNIT_ACTIVE_GEN")
            generation.status, generation.error_code = "WAITING_MANUAL", "UNKNOWN_EXTERNAL_REQUEST"
        self.assert_rejected(request, "DESCRIPTION_GENERATION_RUNNING")
        with self.f.service.session() as session:
            session.get(m.DescriptionGeneration, "UNIT_ACTIVE_GEN").error_code = None
            session.add(m.PublishingTask(id="UNIT_UNKNOWN_PUBLIC", task_id="unit-source-task", video_id="unit-final", account_id=self.f.account["id"],
                country="MX", product_id=self.target["id"], caption="UNIT_ALREADY_USED_CAPTION", product_binding="UNIT", status="WAITING_MANUAL", submission_state="UNKNOWN"))
        self.assert_rejected(request, "PRODUCT_IN_ACTIVE_OR_UNRESOLVED_PUBLISH")

    def test_country_mismatch_and_same_product_are_rejected(self):
        request = self.request()
        with self.f.service.session() as session:
            session.get(m.Product, self.source["id"]).country = "US"
        self.assert_rejected(request, "DESCRIPTION_REUSE_PRODUCT_IDENTITY_MISMATCH")
        self.assertFalse(self.f.service.description_reuse_options(self.target["id"])["sources"])
        self.assert_rejected(request.model_copy(update={"source_product_id": self.target["id"]}), "DESCRIPTION_REUSE_ACTIVE_DISTINCT_PRODUCTS_REQUIRED")

    def test_prior_target_review_is_preserved_but_cannot_approve_new_reused_pool(self):
        old_review = {"source": "USER_CONFIRMED", "sha256": description_pool_hash(self.texts), "confirmed_at": "UNIT_OLD_REVIEW"}
        with self.f.service.session() as session:
            target = session.get(m.Product, self.target["id"])
            target.publishing_context = {**target.publishing_context, "description_review": old_review}
        result = self.f.service.reuse_descriptions(self.target["id"], self.request())
        self.assertEqual(result["lineage"]["previous_target_review"], old_review)
        self.assertFalse(description_review_ready(self.target_product()))

    def test_regular_product_save_cannot_forge_or_remove_reuse_lineage(self):
        result = self.f.service.reuse_descriptions(self.target["id"], self.request())
        product = result["product"]
        edited = self.f.service.save_product(ProductInput(name=product["name"], country=product["country"], image=product["image"],
            keywords=product["keywords"], descriptions=self.texts, publishing_context={"description_reuse": {"source_product_id": "UNIT_FORGED"},
                "description_review_required": False}), product["id"])
        self.assertEqual(edited["publishing_context"]["description_reuse"], result["lineage"])
        self.assertTrue(edited["publishing_context"]["description_review_required"])
        self.assertFalse(description_review_ready(edited))

    def test_router_reads_options_and_requires_actual_preview_fingerprints(self):
        app = FastAPI()
        app.include_router(create_publishing_router(self.f.repo, self.f.service.config))
        base = "/api/publishing/products/" + self.target["id"] + "/descriptions"
        request = self.request().model_dump()
        with TestClient(app) as client:
            self.assertEqual(client.get(base + "/reuse-options").status_code, 200)
            self.assertEqual(client.post(base + "/reuse", json={**request, "confirmed": False}).status_code, 422)
            self.assertEqual(client.post(base + "/reuse", json={**request, "source_pool_sha256": "0" * 64}).status_code, 409)
            self.assertEqual(client.post(base + "/reuse", json={**request, "secret": "UNIT_FORGED"}).status_code, 422)
            response = client.post(base + "/reuse", json=request)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(len(response.json()["product"]["descriptions"]), 20)
            self.assertFalse(response.json()["model_called"])
