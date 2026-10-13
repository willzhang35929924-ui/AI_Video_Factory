import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.domain import find, now
from app.integrations.collector_review import verified_video_url
from app.integrations.collector_service import CollectorService
from app.integrations.input_routes import create_input_router
from app.repository import StateRepository


class CollectorCandidateReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = Settings(app_mode="integration", integration_dry_run=True,
            database_url="sqlite:///" + str(self.root / "test.db"), storage_root=self.root / "storage")
        self.repository = StateRepository(self.settings.database_url, Path(__file__).parents[1] / "app/seed.json", app_mode="integration")
        self.adapter = SimpleNamespace(collect_one=AsyncMock(), dry_run=lambda _: {"ok": True})
        self.hub = SimpleNamespace(broadcast=AsyncMock())
        self.collector = CollectorService(self.repository, self.hub, self.settings, self.adapter)
        self.rules = {"source": "/ISOLATED_TEST/config.yaml", "sha256": "unit-only", "rules": {
            "min_views": 100, "min_like_view_ratio": .001, "product_match_min_confidence": .85}}
        self.ids = []
        for ordinal in range(3):
            with patch.object(self.collector, "rules", return_value=self.rules):
                result = asyncio.run(self.collector.create({"country": "US", "keyword": "ISOLATED_TEST lipstick",
                    "product_images": [str(self.root / "unit-reference.png")], "source_row": ordinal + 1}))
            task_id = result["task_id"]
            self.ids.append(task_id)
            def candidate(state, task_id=task_id, ordinal=ordinal):
                task = find(state, "tasks", task_id)
                video_id = str(1234567890123456780 + ordinal)
                task.update(status="WAITING_REVIEW", collector_result={"video_id": video_id,
                    "video_url": f"https://www.tiktok.com/@unit/video/{video_id}", "views": 10000, "likes": 500,
                    "like_view_ratio": 5, "decision": "manual_review", "decision_reason": "ISOLATED_TEST",
                    "error_code": "EXTERNAL_BUDGET_EXHAUSTED", "reference_product_match": "uncertain", "ai_origin": "uncertain"})
                state["runs"].append({"id": "unit-run-" + str(ordinal), "task_id": task_id, "stage": "collector",
                    "status": "failed", "provider": "LegacyCollectorAdapter", "progress": 0, "logs": ["ISOLATED_TEST_ONLY"],
                    "created_at": now(), "error_code": "EXTERNAL_BUDGET_EXHAUSTED"})
            self.repository.mutate(candidate)
        self.app = FastAPI()
        self.app.include_router(create_input_router(self.collector, self.settings))
        self.client = TestClient(self.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.repository.engine.dispose()
        self.temp.cleanup()

    def detail(self, index=0):
        response = self.client.get("/api/collector/candidates/" + self.ids[index])
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def decide(self, index, action, detail=None):
        detail = detail or self.detail(index)
        return self.client.post("/api/collector/candidates/" + self.ids[index] + "/decision", json={
            "action": action, "reason": "ISOLATED_TEST user choice", "fingerprint": detail["fingerprint"], "confirmed": True})

    def test_budget_consent_is_persistent_without_ai_pass_or_execution(self):
        before = self.repository.state()
        self.assertEqual(self.detail()["state"], "WAITING_EXTERNAL_BUDGET")
        response = self.decide(0, "accept")
        self.assertEqual(response.status_code, 200, response.text)
        detail = self.detail()
        self.assertEqual((detail["candidate_decision"], detail["state"]), ("ACCEPTED", "WAITING_EXTERNAL_BUDGET"))
        self.assertFalse(detail["quality_approved"])
        after = self.repository.state()
        self.assertEqual(after["runs"], before["runs"])
        self.assertEqual(after["reviews"], before["reviews"])
        self.assertEqual(after["tasks"][0]["collector_result"], before["tasks"][0]["collector_result"])
        self.adapter.collect_one.assert_not_called()
        reopened = StateRepository(self.settings.database_url, self.repository.seed_path, app_mode="integration")
        try:
            history = find(reopened.state(), "tasks", self.ids[0])["context"]["collector_candidate_reviews"]
            self.assertEqual(history[-1]["operator"], "local_user")
            self.assertEqual(history[-1]["video_id"], detail["video_id"])
            self.assertEqual(history[-1]["paid_requests_submitted"], 0)
        finally:
            reopened.engine.dispose()

    def test_reject_preserves_history_and_other_candidates_independent(self):
        before = self.repository.state()
        for index, action in enumerate(("accept", "reject", "accept")):
            self.assertEqual(self.decide(index, action).status_code, 200)
        after = self.repository.state()
        self.assertEqual([find(after, "tasks", identifier)["status"] for identifier in self.ids], ["WAITING_REVIEW", "REJECTED", "WAITING_REVIEW"])
        self.assertEqual(after["runs"], before["runs"])
        self.assertEqual([row["collector_result"] for row in after["tasks"]], [row["collector_result"] for row in before["tasks"]])
        with self.assertRaises(Exception) as failure:
            asyncio.run(self.collector.queue(self.ids[1]))
        self.assertEqual(failure.exception.status_code, 409)
        self.adapter.collect_one.assert_not_called()

    def test_batch_one_conflict_does_not_roll_back_other_saved_rows(self):
        details = [self.detail(index) for index in range(3)]
        response = self.client.post("/api/collector/candidates/decisions", json={"action": "accept", "reason": "ISOLATED_TEST batch",
            "confirmed": True, "candidates": {self.ids[0]: details[0]["fingerprint"], self.ids[1]: "0" * 64, self.ids[2]: details[2]["fingerprint"]}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["saved"], 2)
        self.assertEqual([row["status"] for row in response.json()["results"]], ["SAVED", "FAILED", "SAVED"])
        self.assertEqual(self.detail(1)["candidate_decision"], "PENDING")

    def test_exact_submission_is_idempotent_and_changed_evidence_is_rejected(self):
        detail = self.detail()
        self.assertEqual(self.decide(0, "accept", detail).status_code, 200)
        response = self.decide(0, "accept", detail)
        self.assertTrue(response.json()["idempotent"])
        self.assertEqual(len(self.detail()["history"]), 1)
        stale = self.detail(1)
        self.repository.mutate(lambda state: find(state, "tasks", self.ids[1])["collector_result"].update(views=42))
        self.assertEqual(self.decide(1, "accept", stale).status_code, 409)

    def test_active_run_downstream_and_watermark_review_cannot_be_overridden(self):
        for issue in ("active", "watermark"):
            task_id = self.ids[0 if issue == "active" else 1]
            def change(state):
                if issue == "active":
                    find(state, "runs", "unit-run-0").update(status="running")
                else:
                    find(state, "tasks", task_id)["collector_result"].update(decision_reason="watermark_unverified")
            self.repository.mutate(change)
            self.assertFalse(self.detail(0 if issue == "active" else 1)["reviewable"])
            self.assertEqual(self.decide(0 if issue == "active" else 1, "accept").status_code, 409)

    def test_confirmation_and_untrusted_status_fields_are_not_accepted(self):
        payload = {"action": "accept", "reason": "unit", "fingerprint": self.detail()["fingerprint"]}
        endpoint = "/api/collector/candidates/" + self.ids[0] + "/decision"
        self.assertEqual(self.client.post(endpoint, json=payload).status_code, 422)
        self.assertEqual(self.client.post(endpoint, json={**payload, "confirmed": True, "quality_approved": True}).status_code, 422)

    def test_original_url_must_match_verified_host_path_and_video_id(self):
        valid = "https://www.tiktok.com/@unit/video/123"
        self.assertEqual(verified_video_url(valid, "123"), valid)
        for invalid in ("javascript:alert(1)", "https://www.tiktok.com.evil.test/@unit/video/123", "https://user:secret@www.tiktok.com/@unit/video/123",
                "https://www.tiktok.com:9222/@unit/video/123", "https://www.tiktok.com/@unit/video/124", valid + "?token=secret", valid + "\n"):
            self.assertIsNone(verified_video_url(invalid, "123"))
