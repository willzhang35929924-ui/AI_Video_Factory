import threading
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app.integrations.asset_registry import AssetRegistry
import test_real_review as review_fixtures


class ProductionIntegrityTests(review_fixtures.ReviewFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.prepare()

    async def stage(self, invoke):
        queued = await self.production.start(["unit-task"], retry=True, stage="prompt")
        self.production.cancels = {queued["batch_id"]: threading.Event()}
        self.production._checkpoint = AsyncMock()
        return await self.production._stage(queued["batch_id"], "unit-task", "prompt", "LegacyPromptAdapter", invoke)

    async def test_paid_provider_exception_holds_retry_and_preserves_run(self):
        async def invoke(*args):
            raise TimeoutError("request may have reached the paid provider")
        outcome = await self.stage(invoke)
        self.assertEqual(outcome["error_code"], "UNKNOWN_EXTERNAL_REQUEST")
        self.assertEqual(self.repository.state()["tasks"][0]["status"], "WAITING_REVIEW")
        self.repository.mutate(lambda state: state["batches"][0].update(status="COMPLETED_WITH_ERRORS"))
        with self.assertRaises(HTTPException) as caught:
            await self.production.start(["unit-task"], retry=True)
        self.assertIn("UNKNOWN_EXTERNAL_REQUEST", caught.exception.detail)
        self.assertEqual(len(self.repository.state()["runs"]), 1)

    async def test_invalid_partial_result_cannot_erase_unknown_external_request(self):
        async def invoke(*args):
            return {"status": "WAITING_REVIEW", "error_code": "UNKNOWN_EXTERNAL_REQUEST", "assets": [{"asset_type": "INVALID"}]}
        outcome = await self.stage(invoke)
        self.assertEqual(outcome["error_code"], "UNKNOWN_EXTERNAL_REQUEST")
        self.assertFalse(outcome["retryable"])

    async def test_last_available_review_does_not_complete_missing_backgrounds(self):
        self.repository.mutate(lambda state: state["prompts"].append({"id": "unit-missing-prompt", "task_id": "unit-task", "status": "READY"}))
        await self.review.action("approve", "unit-review-v1", {})
        state = self.repository.state()
        self.assertEqual(state["reviews"][0]["status"], "approved")
        self.assertEqual(state["tasks"][0]["status"], "WAITING_REVIEW")
        self.assertEqual(state["tasks"][0]["context"]["production"]["incomplete_prompt_ids"], ["unit-missing-prompt"])

    async def test_previous_generation_approval_does_not_complete_new_generation(self):
        def previous(state):
            state["prompts"].append({"id": "unit-old-prompt", "task_id": "unit-task", "status": "READY"})
            state["assets"].append({"id": "unit-old-compose", "task_id": "unit-task", "type": "compose",
                "legacy_reference": {"prompt_id": "unit-old-prompt", "factory_fingerprint": "old-generation"}})
            state["reviews"].append({"id": "unit-old-review", "task_id": "unit-task", "asset_id": "unit-old-compose", "status": "approved"})
        self.repository.mutate(previous)
        await self.review.action("approve", "unit-review-v1", {})
        self.assertEqual(self.repository.state()["tasks"][0]["status"], "WAITING_REVIEW")

    async def test_all_current_prompts_approved_can_complete(self):
        await self.review.action("approve", "unit-review-v1", {})
        self.assertEqual(self.repository.state()["tasks"][0]["status"], "COMPLETED")

    async def test_changed_alpha_evidence_is_not_registered(self):
        root = self.production.settings.storage_root
        alpha = root / "alpha.mkv"
        alpha.write_bytes(b"changed alpha fixture")
        self.production.settings.legacy_tiktok_auto_root = root
        self.production.settings.readonly_asset_roots = []
        registry = AssetRegistry(self.repository, self.production.settings)
        media = {"width": 1080, "height": 1920, "fps": 30, "duration": 10}
        with patch("app.integrations.asset_registry.probe_media", return_value=media):
            with self.assertRaisesRegex(ValueError, "PERSON_ALPHA_HASH_MISMATCH"):
                registry.register(self.repository.state()["tasks"][0], {
                    "path": self.original_asset["storage_path"],
                    "legacy_reference": {"alpha_path": str(alpha), "alpha_sha256": "expected-original-alpha"},
                }, "person", "unit-run")
        self.assertEqual(len(self.repository.state()["assets"]), 1)


class UploadRaceTests(review_fixtures.ReviewFixture, unittest.TestCase):
    setUp = review_fixtures.ManualBackgroundTests.setUp
    upload = review_fixtures.ManualBackgroundTests.upload

    def test_production_started_during_upload_is_rechecked_before_asset_write(self):
        async def received(*args):
            self.repository.mutate(lambda state: state["runs"].append({"id": "unit-production-run", "task_id": "unit-task", "status": "running"}))
            return b"\x89PNG\r\n\x1a\nfixture", "background.png"
        with patch("app.integrations.production_routes.upload_body", side_effect=received):
            response = self.upload()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(len(self.repository.state()["runs"]), 1)
        self.assertEqual(self.production.registry.registered, [])
        self.assertFalse((self.production.settings.storage_root / "projects").exists())


if __name__ == "__main__":
    unittest.main()
