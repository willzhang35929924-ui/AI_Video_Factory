import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from app.config import Settings
from app.domain import find, now
from app.integrations.collector_service import CollectorService
from app.integrations.legacy.collector_adapter import LegacyCollectorAdapter
from app.integrations.input_service import CollectorInputService
from app.integrations.production_service import ProductionService
from app.repository import StateRepository
from tests.test_real_collector import RULES
from tests.test_production_queue import ReadyDependencies, RecordingProvider


class IdentityTestAdapter:
    def __init__(self):
        self.hits = [101, 102, 103]
        self.collect_calls = 0
        self.preflight_calls = 0
        self.processed_ids = []
        self.failures = set()

    def dry_run(self, context):
        self.preflight_calls += 1
        return {"ok": True, "status": "READY", "checks": []}

    async def discover(self, context, **options):
        return {"status": "READY", "hits": [{"video_id": str(identifier),
            "url": f"https://www.tiktok.com/@identity/video/{identifier}"} for identifier in self.hits],
            "paid_requests": 0, "search_config": {}, "_process": {"exit_code": 0}}

    async def collect_one(self, context, **options):
        self.collect_calls += 1
        self.processed_ids.append(context.get("video_id"))
        if context.get("video_id") in self.failures:
            raise OSError("isolated failure before any external request")
        return {**context, "decision": "rejected", "decision_reason": "isolated_contract_result",
            "legacy_csv_path": "/isolated/candidates.csv", "legacy_result_reference": {}, "views": 100, "likes": 1}


class CollectorHistoryDedupeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.settings = Settings(app_mode="integration", integration_dry_run=False,
            storage_root=root / "storage", database_url="sqlite:///" + str(root / "isolated.db"))
        self.repository = StateRepository(self.settings.database_url,
            Path(__file__).parents[1] / "app/seed.json", app_mode="integration")
        self.adapter = IdentityTestAdapter()
        self.events = []
        async def broadcast(event):
            self.events.append(event)
        self.service = CollectorService(self.repository, SimpleNamespace(broadcast=broadcast), self.settings, self.adapter)
        self.rules = patch("app.integrations.collector_service.read_production_rules", return_value=RULES)
        self.rules.start()

    async def asyncTearDown(self):
        self.rules.stop()
        self.repository.engine.dispose()
        self.temp.cleanup()

    async def create(self, country="墨西哥", keyword="body"):
        return (await self.service.create({"country": country, "keyword": keyword,
            "product_images": [str(self.settings.storage_root / "product.png")]}))["task_id"]

    def bind(self, task_id, identifier, url=None, *, status=None):
        def change(state):
            task = find(state, "tasks", task_id)
            task["context"]["task_context"].update(video_id=str(identifier),
                video_url=url or f"https://www.tiktok.com/@identity/video/{identifier}")
            if status:
                task["status"] = status
        self.repository.mutate(change)

    async def test_all_history_hits_skip_across_new_product_context_and_country(self):
        first = await self.create()
        original = await self.service.expand_search(first)
        second = await self.create("法国", "same video another search")
        context = self.service.context(second)
        again = await self.service.expand_search(second)
        state = self.repository.state()
        self.assertEqual(again["task_ids"], [])
        self.assertEqual(again["duplicate_count"], 3)
        self.assertEqual(again["new_source_count"], 0)
        self.assertEqual({row["canonical_task_id"] for row in again["duplicate_hits"]}, set(original["task_ids"]))
        self.assertEqual(len(state["collection_identities"]), 3)
        self.assertEqual(len(state["tasks"]), 4)
        skipped = find(state, "tasks", second)
        self.assertEqual(skipped["status"], "SKIPPED")
        self.assertEqual(skipped["context"]["task_context"], context)
        self.assertEqual(skipped["country"], "法国")
        self.assertEqual(skipped["context"]["collection_identity"]["status"], "DUPLICATE_SKIPPED")
        self.assertFalse(state["reviews"])
        self.assertFalse(state["assets"])
        self.assertEqual(self.adapter.collect_calls, 0)
        self.assertEqual(len([row for row in state["audits"] if row["action"] == "collector.duplicate_seen"]), 3)
        await self.service.expand_search(second)
        self.assertEqual(len(self.repository.state()["audits"]), len(state["audits"]))

    async def test_first_new_hit_uses_root_after_earlier_duplicates(self):
        first = await self.create()
        await self.service.expand_search(first)
        self.adapter.hits = [101, 102, 104, 105]
        second = await self.create("巴西")
        expanded = await self.service.expand_search(second)
        self.assertEqual(expanded["duplicate_count"], 2)
        self.assertEqual(expanded["task_ids"][0], second)
        self.assertEqual([self.service.context(task)["video_id"] for task in expanded["task_ids"]], ["104", "105"])
        self.assertEqual(len(self.repository.state()["collection_identities"]), 5)
        self.assertEqual(len(self.repository.state()["tasks"]), 5)

    async def test_same_title_and_author_are_not_a_video_identity(self):
        task = await self.create()
        async def same_title(context, **options):
            return {"status": "READY", "hits": [{"video_id": str(identifier),
                "url": f"https://www.tiktok.com/@same-author/video/{identifier}",
                "title": "Identical reused caption", "author": "same-author"} for identifier in (101, 102)],
                "paid_requests": 0, "_process": {"exit_code": 0}}
        self.adapter.discover = same_title
        expanded = await self.service.expand_search(task)
        self.assertEqual(expanded["duplicate_count"], 0)
        self.assertEqual(expanded["new_source_count"], 2)
        self.assertEqual(len(self.repository.state()["collection_identities"]), 2)

    async def test_same_native_id_with_different_caption_and_url_is_one_source(self):
        task = await self.create()
        async def differing_title(context, **options):
            return {"status": "READY", "hits": [
                {"video_id": "101", "url": "https://www.tiktok.com/@author/video/101", "title": "First caption"},
                {"video_id": "101", "url": "https://tiktok.com/@another-author/video/101/?share=1", "title": "Changed caption"}],
                "paid_requests": 0, "_process": {"exit_code": 0}}
        self.adapter.discover = differing_title
        expanded = await self.service.expand_search(task)
        self.assertEqual(expanded["new_source_count"], 1)
        self.assertEqual(len(expanded["task_ids"]), 1)
        self.assertEqual(len(self.repository.state()["collection_identities"]), 1)

    async def test_other_project_preserves_context_but_does_not_recollect_global_source(self):
        first = await self.create()
        original = await self.service.expand_search(first)
        self.repository.mutate(lambda state: state["projects"].append({"id": "project-other", "name": "Another project"}))
        second = (await self.service.create({"country": "法国", "keyword": "other keyword", "project_id": "project-other",
            "product_images": [str(self.settings.storage_root / "product.png")]}))["task_id"]
        expanded = await self.service.expand_search(second)
        self.assertEqual(expanded["task_ids"], [])
        self.assertEqual({row["canonical_task_id"] for row in expanded["duplicate_hits"]}, set(original["task_ids"]))
        self.assertEqual(find(self.repository.state(), "tasks", second)["project_id"], "project-other")
        self.assertEqual(find(self.repository.state(), "tasks", first)["project_id"], "proj-local")
        self.assertEqual(self.adapter.collect_calls, 0)

    async def test_queue_different_url_hits_existing_identity_before_preflight_or_ai(self):
        first = await self.create()
        await self.service.expand_search(first)
        second = await self.create("法国")
        self.bind(second, 101, "https://www.tiktok.com/@different/video/101/?is_from_webapp=1#share")
        result = await self.service.queue(second)
        self.assertEqual(result["status"], "duplicate")
        self.assertEqual(result["canonical_task_id"], first)
        self.assertIsNone(result["run_id"])
        self.assertEqual(self.adapter.preflight_calls, 0)
        self.assertEqual(self.adapter.collect_calls, 0)
        before = self.repository.state()
        await self.service.queue(second)
        after = self.repository.state()
        self.assertEqual(len(before["runs"]), len(after["runs"]))
        self.assertEqual(len(before["audits"]), len(after["audits"]))
        self.assertFalse(after["reviews"])

    async def test_running_entry_rechecks_identity_before_worker_submission(self):
        first = await self.create()
        await self.service.expand_search(first)
        second = await self.create("法国")
        self.bind(second, 104)
        queued = await self.service.queue(second)
        self.bind(second, 101, "https://www.tiktok.com/@history/video/101")
        await self.service.run(second, queued["run_id"])
        state = self.repository.state()
        self.assertEqual(self.adapter.collect_calls, 0)
        self.assertEqual(find(state, "tasks", second)["status"], "SKIPPED")
        self.assertEqual(find(state, "runs", queued["run_id"])["status"], "success")
        self.assertIsNone(find(state, "runs", queued["run_id"])["pid"])
        self.assertFalse(state["reviews"])
        self.assertFalse(state["assets"])

    async def test_result_native_id_drift_does_not_rebind_source_or_register_outputs(self):
        task = await self.create()
        self.bind(task, 104)
        queued = await self.service.queue(task)
        result = {"video_id": "105", "video_url": "https://www.tiktok.com/@result/video/105",
            "source_asset_metadata": None, "decision": "manual_review"}
        with self.assertRaises(HTTPException) as raised:
            self.service._finish(task, queued["run_id"], result, {}, time.monotonic())
        self.assertEqual(raised.exception.detail["code"], "COLLECTION_IDENTITY_CONFLICT")
        state = self.repository.state()
        self.assertEqual(state["collection_identities"][0]["native_video_id"], "104")
        self.assertEqual(find(state, "tasks", task)["context"]["collection_identity"]["status"], "CANONICAL")
        self.assertFalse(state["assets"])
        self.assertFalse(state["reviews"])
        self.assertFalse(find(state, "tasks", task)["collector_result"])

    async def test_known_native_url_create_returns_history_without_creating_any_business_record(self):
        first = await self.create()
        await self.service.expand_search(first)
        before = self.repository.state()
        original = find(before, "tasks", first)
        result = await self.service.create({"country": "法国", "keyword": "another caption",
            "product_images": [str(self.settings.storage_root / "product.png")],
            "video_url": "https://tiktok.com/@changed-author/video/101/?share=1#copy"})
        after = self.repository.state()
        self.assertEqual(result["status"], "duplicate")
        self.assertFalse(result["created"])
        self.assertEqual(result["canonical_task_id"], first)
        for key in ("tasks", "products", "collector", "reviews", "runs", "assets", "legacy_bindings"):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(find(after, "tasks", first), original)
        self.assertEqual(len(after["audits"]), len(before["audits"]) + 1)
        self.assertEqual(self.adapter.collect_calls, 0)
        self.assertEqual(self.adapter.preflight_calls, 0)

    async def test_known_new_native_input_claims_one_identity_and_preserves_country(self):
        result = await self.service.create({"country": "巴西", "keyword": "same wording",
            "product_images": [str(self.settings.storage_root / "product.png")], "video_id": "104"})
        state = self.repository.state()
        task = find(state, "tasks", result["task_id"])
        self.assertEqual(task["country"], "巴西")
        self.assertEqual(task["context"]["task_context"]["video_id"], "104")
        self.assertEqual(task["context"]["collection_identity"]["status"], "CANONICAL")
        self.assertEqual(state["collection_identities"][0]["canonical_task_id"], result["task_id"])
        self.assertEqual(len(state["tasks"]), 1)
        self.assertFalse(state["reviews"])
        self.assertFalse(state["runs"])

    async def test_concurrent_known_input_across_repositories_has_one_task_and_no_ai(self):
        other = StateRepository(self.settings.database_url, Path(__file__).parents[1] / "app/seed.json", app_mode="integration")
        service = CollectorService(other, self.service.hub, self.settings, IdentityTestAdapter())
        payload = {"country": "巴西", "keyword": "same wording",
            "product_images": [str(self.settings.storage_root / "product.png")], "video_id": "105"}
        try:
            results = await asyncio.gather(asyncio.to_thread(asyncio.run, self.service.create(payload)),
                asyncio.to_thread(asyncio.run, service.create({**payload, "country": "法国"})))
        finally:
            other.engine.dispose()
        state = self.repository.state()
        self.assertEqual(len(state["tasks"]), 1)
        self.assertEqual(len(state["products"]), 1)
        self.assertEqual(len(state["collection_identities"]), 1)
        self.assertEqual(len([row for row in results if row.get("status") == "duplicate"]), 1)
        self.assertEqual({row["task_id"] for row in results}, {state["tasks"][0]["id"]})
        self.assertFalse(state["reviews"])
        self.assertFalse(state["runs"])
        self.assertEqual(self.adapter.collect_calls, 0)
        self.assertEqual(service.adapter.collect_calls, 0)

    async def test_batch_known_history_is_not_added_to_created_selection(self):
        first = await self.create()
        await self.service.expand_search(first)
        inputs = CollectorInputService(self.service, self.settings)
        image = inputs.save_image(b"\x89PNG\r\n\x1a\n", "isolated.png")
        result = await inputs.create_batch([
            {"country": "法国", "keyword": "repeat", "product_images": [image["path"]], "video_id": "101"},
            {"country": "巴西", "keyword": "new", "product_images": [image["path"]], "video_id": "106"}])
        self.assertEqual(len(result["created"]), 1)
        self.assertEqual(len(result["duplicates"]), 1)
        self.assertNotEqual(result["created"][0]["task_id"], first)
        self.assertEqual(result["duplicates"][0]["canonical_task_id"], first)
        self.assertEqual(result["duplicates"][0]["index"], 0)
        self.assertFalse(result["started"])
        self.assertEqual(self.adapter.collect_calls, 0)

    async def test_known_short_url_create_isolated_until_native_id_resolves(self):
        with self.assertRaises(HTTPException) as raised:
            await self.service.create({"country": "墨西哥", "keyword": "body",
                "product_images": [str(self.settings.storage_root / "product.png")],
                "video_url": "https://vm.tiktok.com/unresolved/"})
        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("POSSIBLE_DUPLICATE", str(raised.exception.detail))
        self.assertFalse(self.repository.state()["tasks"])
        self.assertFalse(self.repository.state()["reviews"])
        self.assertEqual(self.adapter.collect_calls, 0)

    async def test_native_create_rejects_other_platform_and_conflicting_ids(self):
        for source in ({"video_url": "https://evil.example/video/101"},
                       {"video_id": "101", "video_url": "https://tiktok.com/@author/video/999"}):
            with self.assertRaises(ValueError):
                await self.service.create({"country": "墨西哥", "keyword": "body",
                    "product_images": [str(self.settings.storage_root / "product.png")], **source})
        self.assertFalse(self.repository.state()["tasks"])
        self.assertEqual(self.adapter.collect_calls, 0)

    async def test_queued_identity_conflict_finishes_run_without_stale_running_or_worker(self):
        task = await self.create()
        self.bind(task, 101)
        queued = await self.service.queue(task)
        self.bind(task, 101, "https://www.tiktok.com/@identity/video/999")
        await self.service.run(task, queued["run_id"])
        state = self.repository.state()
        run = find(state, "runs", queued["run_id"])
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["error_code"], "COLLECTION_IDENTITY_CONFLICT")
        self.assertIsNotNone(run["finished_at"])
        self.assertIsNone(run["pid"])
        self.assertFalse(self.service.active)
        self.assertEqual(self.adapter.collect_calls, 0)

    async def test_result_registration_guard_does_not_duplicate_asset_or_review(self):
        first = await self.create()
        await self.service.expand_search(first)
        second = await self.create("法国")
        run_id = "run-isolated-late-registration"
        self.repository.mutate(lambda state: state["runs"].append({"id": run_id, "run_id": run_id,
            "task_id": second, "stage": "collector", "provider": "LegacyCollectorAdapter", "status": "running",
            "progress": 0, "logs": [], "created_at": now()}))
        result = {"video_id": "101", "video_url": "https://www.tiktok.com/@result/video/101",
            "source_asset_metadata": None, "decision": "manual_review"}
        self.service._finish(second, run_id, result, {}, time.monotonic())
        state = self.repository.state()
        self.assertEqual(find(state, "tasks", second)["status"], "SKIPPED")
        self.assertEqual(find(state, "tasks", second)["collector_result"], {})
        self.assertFalse(state["assets"])
        self.assertFalse(state["reviews"])
        self.assertEqual(self.adapter.collect_calls, 0)

    async def test_unknown_historical_request_stays_frozen_and_new_alias_never_retries(self):
        first = await self.create()
        await self.service.expand_search(first)
        def unknown(state):
            original = find(state, "tasks", first)
            original.update(status="QUARANTINED", collector_result={"video_id": "101",
                "video_url": "https://www.tiktok.com/@identity/video/101", "error_code": "UNKNOWN_EXTERNAL_REQUEST"})
        self.repository.mutate(unknown)
        second = await self.create("法国")
        self.bind(second, 101)
        result = await self.service.queue(second)
        self.assertEqual(result["canonical_task_id"], first)
        state = self.repository.state()
        original = find(state, "tasks", first)
        self.assertEqual(original["status"], "QUARANTINED")
        self.assertEqual(original["collector_result"]["error_code"], "UNKNOWN_EXTERNAL_REQUEST")
        self.assertEqual(self.adapter.collect_calls, 0)

    async def test_same_task_retry_keeps_its_canonical_identity(self):
        task = await self.create()
        self.bind(task, 101, status="FAILED")
        result = await self.service.queue(task, retry=True)
        self.assertEqual(result["status"], "queued")
        state = self.repository.state()
        self.assertEqual(state["collection_identities"][0]["canonical_task_id"], task)
        self.assertEqual(find(state, "tasks", task)["context"]["collection_identity"]["status"], "CANONICAL")
        self.assertEqual(len(state["runs"]), 1)

    async def test_duplicate_cancelled_history_is_not_rewritten_as_new_skipped_task(self):
        first = await self.create()
        await self.service.expand_search(first)
        second = await self.create("法国")
        self.bind(second, 101, status="CANCELLED")
        result = await self.service.queue(second)
        self.assertEqual(result["status"], "duplicate")
        self.assertEqual(find(self.repository.state(), "tasks", second)["status"], "CANCELLED")
        self.assertEqual(self.adapter.collect_calls, 0)

    async def test_unresolved_short_url_cannot_submit_real_adapter(self):
        task = await self.create()
        self.repository.mutate(lambda state: find(state, "tasks", task)["context"]["task_context"].update(
            video_url="https://vm.tiktok.com/unresolved/"))
        self.service.adapter = LegacyCollectorAdapter(self.settings.storage_root / "legacy", self.settings.storage_root)
        with self.assertRaises(HTTPException) as raised:
            await self.service.queue(task)
        self.assertIn("POSSIBLE_DUPLICATE", str(raised.exception.detail))
        self.assertFalse(self.repository.state()["runs"])

    async def test_url_identity_conflict_creates_no_run_or_false_merge(self):
        task = await self.create()
        self.bind(task, 101, "https://www.tiktok.com/@identity/video/999")
        with self.assertRaises(HTTPException) as raised:
            await self.service.queue(task)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["code"], "COLLECTION_IDENTITY_CONFLICT")
        self.assertFalse(self.repository.state()["collection_identities"])
        self.assertFalse(self.repository.state()["runs"])

    def production(self):
        provider = RecordingProvider()
        production = ProductionService(self.repository, self.service.hub, self.settings, self.service,
            {name: provider for name in ("cutout", "prompt", "background", "compose")})
        production.readiness = ReadyDependencies()
        return production

    async def test_serial_batch_failure_continues_and_next_batch_skips_all_history(self):
        first = await self.create()
        self.adapter.failures = {"102"}
        production = self.production()
        queued = await production.start([first], stage="collector")
        await production.launch(queued["batch_id"])
        await production.jobs[queued["batch_id"]]
        state = self.repository.state()
        batch = find(state, "batches", queued["batch_id"])
        self.assertEqual(self.adapter.processed_ids, ["101", "102", "103"])
        self.assertEqual(batch["status"], "COMPLETED_WITH_ERRORS")
        self.assertEqual([row["status"] for row in batch["items"]], ["QUARANTINED", "FAILED", "QUARANTINED"])
        self.assertEqual(len(batch["task_ids"]), 3)
        before_review, before_assets = len(state["reviews"]), len(state["assets"])
        second = await self.create("法国")
        next_batch = await production.start([second], stage="collector")
        await production.launch(next_batch["batch_id"])
        await production.jobs[next_batch["batch_id"]]
        final = self.repository.state()
        final_batch = find(final, "batches", next_batch["batch_id"])
        self.assertEqual(final_batch["status"], "COMPLETED")
        self.assertEqual(final_batch["items"][0]["status"], "SKIPPED")
        self.assertEqual(self.adapter.collect_calls, 3)
        self.assertEqual(len(final["reviews"]), before_review)
        self.assertEqual(len(final["assets"]), before_assets)
        self.assertEqual(len(final["collection_identities"]), 3)
