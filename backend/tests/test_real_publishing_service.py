"""Isolated SQLite contracts. UNIT_ONLY bytes/adapters are not publishing evidence."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
import hashlib
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from app import models as m
from app.domain import now
from app.repository import StateRepository
from app.integrations.publishing.config import PublishingConfig, load_publishing_config
from app.integrations.publishing.description_selector import DescriptionSelector
from app.integrations.publishing.hubstudio import HubStudioAdapter
from app.integrations.publishing.routes import create_publishing_router
from app.integrations.publishing.schemas import AccountConfirmation, AccountInput, ProductInput, TaskInput
from app.integrations.publishing.service import PublishingService


class UnitObservedAdapter:
    def __init__(self):
        self.steps = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.block_upload = False
        self.raise_upload = False
        self.reconcile_calls = 0

    @staticmethod
    def completion(payload, *, read_only=False):
        filename = Path(payload["video"]["path"]).name
        return {"status": "READY", "checkpoint": {"unit_only": True, "video_sha256": payload["video"]["sha256"], "account_id": payload["account"]["id"], "environment_id": payload["account"]["hubstudio_env_id"], "filename": filename},
            "evidence": {"unit_only": True, "uploaded": True, "source": "OBSERVED_BROWSER", "read_only": read_only, "verified_at": now(),
                "dom_evidence": {"filename": filename, "upload_complete": True, "completion_text": "UNIT_ONLY_COMPLETION", "url": "https://www.tiktok.com/tiktokstudio/upload"}}}

    async def preflight(self, payload):
        return {"status": "READY", "checks": [], "evidence": {"unit_only": True,
            "identity": {"source": "OBSERVED_BROWSER", "username": payload["account"]["expected_username"], "environment_id": payload["account"]["hubstudio_env_id"], "verified_at": now()},
            "vpn": {"source": "OBSERVED_NETWORK", "country": payload["account"]["country"], "verified_at": now()}}}

    async def execute_step(self, name, payload):
        self.steps.append(name)
        self.entered.set()
        if self.block_upload:
            await self.release.wait()
        if self.raise_upload:
            raise RuntimeError("UNIT_EXTERNAL_FAILURE_NOT_PRODUCTION")
        return self.completion(payload)

    async def reconcile_upload(self, payload):
        self.reconcile_calls += 1
        return self.completion(payload, read_only=True)


class SQLitePublishingFixture:
    def __init__(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="publishing-unit-")
        self.root = Path(self.temporary.name)
        self.repo = StateRepository("sqlite:///" + str(self.root / "unit.db"), Path(__file__).resolve().parents[1] / "app" / "seed.json", app_mode="integration")
        self.config = PublishingConfig(source_roots=[self.root])
        self.adapter = UnitObservedAdapter()
        self.service = PublishingService(self.repo, self.config, self.adapter)
        self.product = self.service.save_product(ProductInput(name="UNIT_ONLY_PRODUCT", country="US", platform_product_id="unit-platform-id",
            descriptions=[{"id": "01", "text": "UNIT ONLY one"}, {"id": "02", "text": "UNIT ONLY two", "weight": 2}],
            publishing_context={"source_document": "UNIT_ONLY_SOURCE", "source_row": 1}))
        self.account = self.service.save_account(AccountInput(name="UNIT_ONLY_ACCOUNT", country="US", product_ids=[self.product["id"]],
            hubstudio_env_id="unit-env", cdp_endpoint="http://127.0.0.1:9999", expected_username="unit_username", publishing_context={"phone": "UNIT_ONLY_NOT_A_PHONE"}))
        template = deepcopy(self.repo.seed()["tasks"][0])
        template.update(id="unit-source-task", project_id="proj-local", country="US", product_id=self.product["id"], source_video_id=None,
            status="COMPLETED", current_stage="review", execution_mode="integration", created_at=now(), updated_at=now(),
            context={"production": {"current_person_asset_id": "unit-person"}}, collector_result={})
        self.files = {}
        assets = []
        for kind, asset_type in (("person", "PERSON_VIDEO"), ("compose", "COMPOSITION_VIDEO"), ("final", "FINAL_VIDEO")):
            path = self.root / ("unit-" + kind + ".mp4")
            path.write_bytes(("UNIT_ONLY_NON_MEDIA_" + kind).encode())
            self.files[kind] = path
            reference = {"execution_scope": "PRODUCTION", "unit_only": True}
            if kind == "compose":
                reference["person_asset_id"] = "unit-person"
            if kind == "final":
                reference.update(composition_asset_id="unit-compose", review_id="unit-review-01")
            assets.append({"id": "unit-" + kind, "task_id": template["id"], "type": kind, "asset_type": asset_type, "name": "UNIT_ONLY",
                "thumbnail": "", "duration": 1, "width": 16, "height": 16, "fps": 1, "codec": "unit", "country": "US", "keyword": template["keyword"],
                "status": "approved", "storage_path": str(path), "external_path": str(path), "created_at": now(), "registry_status": "registered",
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "legacy_reference": reference})
        def setup(state):
            state["tasks"].append(template)
            state["assets"].extend(assets)
            state["reviews"].append({"id": "unit-review-01", "task_id": template["id"], "asset_id": "unit-compose", "status": "approved", "issues": [], "note": "UNIT_ONLY"})
        self.repo.mutate(setup)

    def task(self, account_id=None, *, upload_only=False, caption=""):
        return self.service.create_task(TaskInput(task_id="unit-source-task", video_id="unit-final", product_id=self.product["id"], account_id=account_id or self.account["id"], upload_only=upload_only, caption=caption))

    def close(self):
        self.repo.engine.dispose()
        self.temporary.cleanup()


class PublishingCatalogTests(unittest.TestCase):
    def setUp(self):
        self.f = SQLitePublishingFixture()
        self.addCleanup(self.f.close)

    def test_crud_context_and_schema_compatibility_persist_without_whole_state_rewrite(self):
        statements = []
        def before(connection, cursor, statement, parameters, context, many):
            if statement.lstrip().upper().startswith("UPDATE"):
                statements.append(statement.split()[1])
        event.listen(self.f.repo.engine, "before_cursor_execute", before)
        self.addCleanup(event.remove, self.f.repo.engine, "before_cursor_execute", before)
        with patch.object(self.f.repo, "_read", side_effect=AssertionError("NO_WHOLE_STATE_READ")), patch.object(self.f.repo, "_write", side_effect=AssertionError("NO_WHOLE_STATE_WRITE")):
            row = self.f.service.save_product(ProductInput(name="UNIT_UPDATED", country="US", descriptions=[{"id": "20", "text": "UNIT_ONLY"}], publishing_context={"source_document": "UNIT_DOC"}), self.f.product["id"])
            self.assertEqual(row["descriptions"][0]["id"], "20")
            self.f.service.control_account(self.f.account["id"], False)
        self.assertTrue(set(statements) <= {"products", "publishing_accounts"})
        self.assertEqual(self.f.repo.state()["accounts"][0]["status"], "DISABLED")
        reopened = StateRepository(str(self.f.repo.engine.url), self.f.repo.seed_path, app_mode="integration")
        self.addCleanup(reopened.engine.dispose)
        self.assertEqual(reopened.state()["products"][0]["publishing_context"]["source_document"], "UNIT_DOC")

    def test_secret_rejection_redaction_and_user_attestation_is_not_observed_identity(self):
        with self.assertRaises(HTTPException):
            self.f.service.save_account(AccountInput(name="unit", country="US", publishing_context={"password": "UNIT_SECRET"}))
        with Session(self.f.repo.engine) as session:
            row = session.get(m.PublishingAccount, self.f.account["id"])
            row.secret_ref, row.authorization = "UNIT_SECRET_REFERENCE", "UNIT_SECRET_AUTH"
            session.commit()
        account = self.f.service.confirm_account(self.f.account["id"], AccountConfirmation(username="unit_username", country="US", vpn_country="US", note="UNIT_USER_ATTESTATION"))
        self.assertEqual(account["status"], "WAITING_MANUAL")
        self.assertEqual(account["identity_status"], "WAITING_MANUAL")
        self.assertEqual(account["identity_verification"]["source"], "USER_ATTESTATION")
        self.assertNotIn("UNIT_SECRET", str(account))

    def test_delete_guard_and_real_unreferenced_catalog_delete(self):
        self.f.task()
        for function, entity_id in ((self.f.service.delete_product, self.f.product["id"]), (self.f.service.delete_account, self.f.account["id"])):
            with self.assertRaises(HTTPException) as blocked:
                function(entity_id)
            self.assertEqual(blocked.exception.status_code, 409)
        other = self.f.service.save_account(AccountInput(name="UNIT_DELETE", country="US"))
        self.assertTrue(self.f.service.delete_account(other["id"])["deleted"])

    def test_final_compose_review_lineage_and_original_paths_are_used(self):
        task = self.f.task()
        self.assertEqual(task["input_sha256"], hashlib.sha256(self.f.files["final"].read_bytes()).hexdigest())
        with self.f.service.session() as session:
            payload = self.f.service.input(session, session.get(m.PublishingTask, task["id"]))
        self.assertEqual(payload["video"]["path"], str(self.f.files["final"].resolve()))
        self.assertNotEqual(self.f.config.output_root, self.f.files["final"].parent)
        self.assertEqual(self.f.repo.state()["publishing"][0]["current_step"], "PREFLIGHT")

    def test_late_review_rejection_current_person_change_technical_and_hash_tamper_block(self):
        for guard, upload_only in ((guard, scope) for guard in ("pending", "rejected", "current_person", "technical", "hash") for scope in (False, True)):
            with self.subTest(guard=guard, upload_only=upload_only):
                f = SQLitePublishingFixture()
                try:
                    with Session(f.repo.engine) as session:
                        if guard in {"pending", "rejected"}:
                            session.add(m.Review(id="unit-review-99", task_id="unit-source-task", asset_id="unit-compose", status=guard, issues=[], note="UNIT_ONLY"))
                        elif guard == "current_person":
                            session.get(m.Task, "unit-source-task").context = {"production": {"current_person_asset_id": "unit-other"}}
                        elif guard == "technical":
                            session.get(m.VideoAsset, "unit-final").legacy_reference = {"execution_scope": "TECHNICAL_VALIDATION"}
                        else:
                            f.files["final"].write_bytes(b"UNIT_TAMPER")
                        session.commit()
                    with self.assertRaises(HTTPException):
                        f.task(upload_only=upload_only)
                    with Session(f.repo.engine) as session:
                        self.assertEqual(session.scalar(select(func.count()).select_from(m.PublishingTask)), 0)
                finally:
                    f.close()

    def test_router_actual_json_and_required_fk_input_validation(self):
        app = FastAPI()
        app.include_router(create_publishing_router(self.f.repo, self.f.config))
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/publishing/accounts").json()[0]["expected_username"], "unit_username")
            self.assertEqual(client.post("/api/publishing/tasks", json={"video_path": "UNIT"}).status_code, 422)
            payload = {"task_id": "unit-source-task", "video_id": "unit-final", "product_id": self.f.product["id"], "account_id": self.f.account["id"]}
            response = client.post("/api/publishing/tasks", json=payload)
            self.assertEqual(response.status_code, 201, response.text)
            self.assertEqual(response.json()["submission_state"], "NOT_SUBMITTED")
            self.assertEqual(client.patch("/api/publishing/accounts/" + self.f.account["id"], json={"enabled": False}).status_code, 200)

    def test_account_detail_uses_publishing_identity_control_and_persisted_run_history(self):
        app = FastAPI()
        app.include_router(create_publishing_router(self.f.repo, self.f.config))
        account_id = self.f.account["id"]
        with TestClient(app) as client:
            account = next(row for row in client.get("/api/publishing/accounts").json() if row["id"] == account_id)
            self.assertEqual(account["status"], "WAITING_MANUAL")
            self.assertFalse(account["connection_verified"])
            self.assertNotIn("authorization", account)
            self.assertEqual(client.patch(f"/api/publishing/accounts/{account_id}", json={"enabled": False}).json()["status"], "DISABLED")
            payload = {"name": "UNIT_ONLY_DETAIL_EDIT", "country": "US", "status": "NOT_CONNECTED", "enabled": False,
                "product_ids": account["product_ids"], "hubstudio_env_id": account["hubstudio_env_id"], "cdp_endpoint": account["cdp_endpoint"],
                "expected_username": account["expected_username"], "cooldown_seconds": account["cooldown_seconds"], "publishing_context": account["publishing_context"]}
            saved = client.put(f"/api/publishing/accounts/{account_id}", json=payload)
            self.assertEqual(saved.status_code, 200, saved.text)
            self.assertEqual(saved.json()["status"], "DISABLED")
            resumed = client.patch(f"/api/publishing/accounts/{account_id}", json={"enabled": True}).json()
            self.assertEqual(resumed["status"], "WAITING_MANUAL")
            self.assertFalse(resumed["connection_verified"])
            self.assertEqual(resumed["expected_username"], "unit_username")
            self.assertEqual(resumed["hubstudio_env_id"], "unit-env")
            self.assertEqual(resumed["publishing_context"], account["publishing_context"])
            self.assertEqual(client.put(f"/api/publishing/accounts/{account_id}", json={**payload, "status": "CONNECTED"}).status_code, 422)

            task = self.f.task(upload_only=True)
            with Session(self.f.repo.engine) as session:
                session.add(m.PublishingBatch(id="unit-detail-batch", status="PARTIAL", concurrency=1))
                session.flush()
                session.add(m.PublishRun(id="unit-detail-run", publishing_task_id=task["id"], batch_id="unit-detail-batch",
                    status="WAITING_MANUAL", attempt=1, started_at=m.utc_now(), finished_at=m.utc_now(), last_heartbeat=m.utc_now(),
                    logs=[{"stage": "PREFLIGHT", "message": "UNIT_ONLY_NOT_PRODUCTION", "api_key": "UNIT_SECRET"}],
                    error_code="LOGIN_REQUIRED", error_message="UNIT_ONLY"))
                session.flush()
                session.add(m.StepExecution(id="unit-detail-step", publish_run_id="unit-detail-run", publishing_task_id=task["id"],
                    name="PREFLIGHT", status="WAITING_MANUAL", started_at=m.utc_now(), finished_at=m.utc_now(),
                    error_code="LOGIN_REQUIRED", error_message="UNIT_ONLY"))
                session.commit()
            tasks = client.get("/api/publishing/tasks").json()
            self.assertIn(task["id"], [row["id"] for row in tasks if row["account_id"] == account_id])
            history = client.get(f"/api/publishing/tasks/{task['id']}").json()
            self.assertEqual(history["runs"][0]["id"], "unit-detail-run")
            self.assertEqual(history["steps"][0]["error_code"], "LOGIN_REQUIRED")
            self.assertEqual(history["runs"][0]["status"], "WAITING_MANUAL")
            self.assertNotIn("api_key", history["runs"][0]["logs"][0])
            self.assertEqual(client.get("/api/publishing/tasks/not-a-real-task").status_code, 404)

    def test_source_unknown_external_request_blocks_without_creating_new_publish_task(self):
        with Session(self.f.repo.engine) as session:
            session.add(m.ProcessingRun(id="unit-source-unknown", task_id="unit-source-task", stage="background", provider="UNIT_ONLY_PAID_PROVIDER",
                status="failed", progress=0, logs=[], created_at=m.utc_now(), error_code="UNKNOWN_EXTERNAL_REQUEST", resource_info={}))
            session.commit()
        with self.assertRaises(HTTPException) as blocked:
            self.f.task()
        self.assertEqual(blocked.exception.detail, "SOURCE_SUBMISSION_UNKNOWN")
        self.assertEqual(self.f.service.tasks(), [])

    def test_legacy_mock_seed_roundtrip_retains_valid_new_defaults(self):
        repo = StateRepository("sqlite:///" + str(self.f.root / "unit-mock.db"), self.f.repo.seed_path, app_mode="mock")
        self.addCleanup(repo.engine.dispose)
        before = repo.state()
        repo.mutate(lambda state: None)
        after = repo.state()
        self.assertEqual(before, after)
        self.assertTrue(all(row["created_at"] for row in after["publishing"]))
        service = PublishingService(repo, self.f.config)
        self.assertEqual(service.tasks(), [])
        self.assertEqual(service.products(), [])
        self.assertEqual(service.accounts(), [])
        with service.session() as session:
            account = session.scalar(select(m.PublishingAccount))
            derived = service.account_row(session, account)
            self.assertEqual(derived["status"], "NOT_CONNECTED")
            self.assertFalse(derived["connection_verified"])
            self.assertEqual(account.status, before["accounts"][0]["status"])

    def test_description_selector_weighted_random_excludes_id_and_identical_content(self):
        pool = [{"id": "01", "text": "A", "weight": 1}, {"id": "02", "text": "B", "weight": 9}, {"id": "03", "text": "A", "weight": 1}]
        recent = [{"description_id": "01", "description_sha256": hashlib.sha256(b"A").hexdigest()}]
        selector = DescriptionSelector(random.Random(4))
        for mode in ("random", "weighted"):
            self.assertEqual(selector.select(pool, {"mode": mode, "exclude_last_n": 1}, recent)["id"], "02")
        rng = Mock()
        rng.choices.return_value = [pool[1]]
        DescriptionSelector(rng).select(pool, {"mode": "weighted", "exclude_last_n": 0})
        self.assertEqual(rng.choices.call_args.kwargs["weights"], [1, 9, 1])
        with self.assertRaisesRegex(ValueError, "DESCRIPTION_POOL_EXHAUSTED"):
            selector.select(pool[:1], {"exclude_last_n": 1}, recent)

    def test_config_is_scoped_no_keys_and_more_than_one_concurrency_is_explicit_unsupported(self):
        unit_config = self.f.root / 'UNIT_ONLY_empty.yaml'
        unit_config.write_text('publishing: {}\n', encoding='utf-8')
        with patch("app.integrations.publishing.config.dotenv_values", return_value={}):
            config = load_publishing_config(unit_config)
        with patch.dict("os.environ", {"HUBSTUDIO_LOCAL_API_KEY": "UNIT_SECRET"}):
            self.assertNotIn("UNIT_SECRET", str(config.public()))
        self.assertEqual(PublishingConfig(concurrency=4).public()["concurrency_status"], "NOT_SUPPORTED")
        self.assertFalse(config.description_ai.enabled)


class PublishingQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = SQLitePublishingFixture()
        self.task = self.f.task()

    async def asyncTearDown(self):
        await self.f.service.shutdown()
        self.f.close()

    async def batch(self):
        batch = await self.f.service.create_batch([self.task["id"]])
        await self.f.service.worker
        return batch

    async def queued_pair(self):
        account = self.f.service.save_account(AccountInput(name="UNIT_SECOND_ACCOUNT", country="US", product_ids=[self.f.product["id"]],
            hubstudio_env_id="unit-second-env", cdp_endpoint="http://127.0.0.1:9998", expected_username="unit_second_username"))
        second = self.f.task(account["id"])
        with patch.object(self.f.service, "launch"):
            batch = await self.f.service.create_batch([self.task["id"], second["id"]])
        return batch, second

    async def test_scheduled_first_task_does_not_block_runnable_next_task(self):
        batch, second = await self.queued_pair()
        with self.f.service.session() as session:
            session.get(m.PublishingTask, self.task["id"]).schedule = m.utc_now() + timedelta(hours=1)
        claimed = self.f.service.claim()
        self.assertEqual(claimed[0], second["id"])
        self.assertEqual(self.f.service.task(self.task["id"])["error"], "SCHEDULE_REQUIRES_EXPLICIT_RESUME")
        self.assertEqual(self.f.service.task(self.task["id"])["status"], "WAITING_MANUAL")
        self.assertEqual(self.f.service.batches()[0]["status"], "QUEUED")
        self.f.service.finish(*claimed, "FAILED", "UNIT_ONLY_SECOND_FAILURE")
        self.assertIsNone(self.f.service.claim())
        self.assertEqual(self.f.service.batches()[0]["status"], "WAITING_MANUAL")
        self.assertEqual(self.f.service.task(second["id"])["batch_id"], batch["id"])

    async def test_account_cooldown_first_task_does_not_block_other_account(self):
        _, second = await self.queued_pair()
        with self.f.service.session() as session:
            session.get(m.PublishingAccount, self.f.account["id"]).last_publish = m.utc_now()
        claimed = self.f.service.claim()
        self.assertEqual(claimed[0], second["id"])
        self.assertEqual(self.f.service.task(self.task["id"])["error"], "ACCOUNT_COOLDOWN")
        self.assertEqual(self.f.service.batches()[0]["status"], "QUEUED")

    async def test_resource_blocked_first_task_does_not_block_independent_account(self):
        _, second = await self.queued_pair()
        key = "account:" + self.f.account["id"]
        with self.f.service.session() as session:
            session.add(m.ResourceLock(resource_key=key, publishing_task_id=second["id"]))
        claimed = self.f.service.claim()
        self.assertEqual(claimed[0], second["id"])
        self.assertEqual(self.f.service.task(self.task["id"])["error"], "RESOURCE_IN_USE_REQUIRES_REVIEW")
        with self.f.service.session() as session:
            self.assertIsNone(session.get(m.ResourceLock, key).released_at)
        self.assertEqual(self.f.service.batches()[0]["status"], "QUEUED")

    async def test_first_failed_run_releases_lock_and_next_task_is_claimed(self):
        _, second = await self.queued_pair()
        first = self.f.service.claim()
        self.assertEqual(first[0], self.task["id"])
        self.f.service.finish(*first, "FAILED", "UNIT_ONLY_FIRST_FAILURE")
        claimed = self.f.service.claim()
        self.assertEqual(claimed[0], second["id"])
        self.assertEqual(self.f.service.task(self.task["id"])["status"], "FAILED")
        self.f.service.finish(*claimed, "PUBLISHED")
        self.assertIsNone(self.f.service.claim())
        self.assertEqual(self.f.service.batches()[0]["status"], "PARTIAL")

    async def test_real_sqlite_steps_upload_stop_and_retry_uses_same_description_no_reupload(self):
        batch = await self.batch()
        detail = self.f.service.task(self.task["id"])
        self.assertEqual(detail["status"], "WAITING_MANUAL")
        self.assertEqual(detail["error"], "UPLOAD_VERIFIED_STOP_BEFORE_PRODUCT_AND_PUBLISH")
        self.assertEqual([row["name"] for row in detail["steps"]], ["PREFLIGHT", "SELECT_DESCRIPTION", "UPLOAD_VIDEO"])
        self.assertEqual(self.f.adapter.steps, ["UPLOAD_VIDEO"])
        selected = detail["selected_description"]
        await self.f.service.retry(self.task["id"])
        await self.f.service.worker
        self.assertEqual(self.f.service.task(self.task["id"])["selected_description"], selected)
        self.assertEqual(self.f.adapter.steps, ["UPLOAD_VIDEO"])
        self.assertEqual(self.f.service.task(self.task["id"])["batch_id"], batch["id"])
        with Session(self.f.repo.engine) as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(m.DescriptionUsage)), 1)
        self.assertEqual(len(self.f.service.queue()["locks"]), 2)

    async def test_preflight_ready_without_actual_identity_or_vpn_is_waiting_manual(self):
        self.f.adapter.preflight = AsyncMock(return_value={"status": "READY", "evidence": {"identity": {"source": "USER_ATTESTATION", "username": "unit_username"}, "vpn": {"source": "HUB_PROXY_LABEL", "country": "US"}}})
        result = await self.f.service.preflight(self.task["id"])
        self.assertEqual(result["status"], "WAITING_MANUAL")
        await self.batch()
        self.assertEqual(self.f.adapter.steps, [])
        self.assertIsNone(self.f.service.task(self.task["id"])["selected_description"])

    async def test_unknown_external_outcome_retains_lock_and_blocks_retry_resume(self):
        self.f.adapter.raise_upload = True
        batch = await self.batch()
        detail = self.f.service.task(self.task["id"])
        self.assertEqual(detail["status"], "WAITING_MANUAL")
        self.assertEqual(detail["checkpoint"]["external_outcome"], "UNKNOWN")
        self.assertEqual(len(self.f.service.queue()["locks"]), 3)
        for action in (self.f.service.retry(self.task["id"]), self.f.service.control(batch["id"], "resume")):
            with self.assertRaises(HTTPException) as blocked:
                await action
            self.assertEqual(blocked.exception.status_code, 409)
        await self.f.service.control(batch["id"], "cancel")
        self.assertEqual(self.f.service.task(self.task["id"])["status"], "WAITING_MANUAL")
        self.assertEqual(len(self.f.adapter.steps), 1)

    async def test_pause_during_external_step_finishes_checkpoint_then_holds_original_batch(self):
        self.f.adapter.block_upload = True
        batch = await self.f.service.create_batch([self.task["id"]])
        await self.f.adapter.entered.wait()
        await self.f.service.control(batch["id"], "pause")
        self.f.adapter.release.set()
        await self.f.service.worker
        self.assertEqual(self.f.service.task(self.task["id"])["status"], "PAUSED")
        self.assertEqual(self.f.service.batches()[0]["status"], "PAUSED")
        await self.f.service.control(batch["id"], "resume")
        await self.f.service.worker
        self.assertEqual(self.f.adapter.steps, ["UPLOAD_VIDEO"])

    async def test_shutdown_inflight_and_reopen_keep_unknown_no_auto_work(self):
        self.f.adapter.block_upload = True
        batch = await self.f.service.create_batch([self.task["id"]])
        await self.f.adapter.entered.wait()
        await self.f.service.shutdown()
        reopened = PublishingService(self.f.repo, self.f.config, UnitObservedAdapter())
        await reopened.recover()
        self.assertIsNone(reopened.worker)
        self.assertEqual(reopened.task(self.task["id"])["checkpoint"]["external_outcome"], "UNKNOWN")
        with self.assertRaises(HTTPException):
            await reopened.control(batch["id"], "resume")
        self.assertEqual(self.f.adapter.steps, ["UPLOAD_VIDEO"])
        self.assertEqual(reopened.task(self.task["id"])["steps"][-1]["status"], "WAITING_MANUAL")

    async def test_true_cancel_is_not_resumed_and_original_source_task_is_untouched(self):
        with patch.object(self.f.service, "launch"):
            batch = await self.f.service.create_batch([self.task["id"]])
        await self.f.service.control(batch["id"], "cancel")
        with self.assertRaises(HTTPException):
            await self.f.service.control(batch["id"], "resume")
        self.assertEqual(self.f.repo.state()["tasks"][0]["status"], "COMPLETED")
        self.assertEqual(self.f.service.task(self.task["id"])["status"], "CANCELLED")
        self.assertEqual(self.f.adapter.steps, [])

    async def test_serial_claim_across_services_and_checkpoint_recovery_without_inflight(self):
        with patch.object(self.f.service, "launch"):
            batch = await self.f.service.create_batch([self.task["id"]])
        task_id, run_id = self.f.service.claim()
        other = PublishingService(self.f.repo, self.f.config, UnitObservedAdapter())
        self.assertIsNone(other.claim())
        await other.recover()
        self.assertEqual(other.queue()["locks"], [])
        self.assertEqual(other.batches()[0]["status"], "PAUSED")
        self.assertEqual(other.task(task_id)["runs"][0]["id"], run_id)
        self.assertEqual(other.task(task_id)["status"], "WAITING_MANUAL")
        self.assertEqual(other.task(task_id)["batch_id"], batch["id"])

    async def test_more_than_one_concurrency_rejected_not_fake_consumption(self):
        self.f.service.config = PublishingConfig(source_roots=[self.f.root], concurrency=2)
        with self.assertRaises(HTTPException) as blocked:
            await self.f.service.create_batch([self.task["id"]])
        self.assertEqual(blocked.exception.detail, "CONCURRENCY_NOT_SUPPORTED")
        self.assertEqual(self.f.service.batches(), [])

    async def test_completed_upload_checkpoint_recovery_keeps_environment_and_account_locks(self):
        with patch.object(self.f.service, "launch"):
            await self.f.service.create_batch([self.task["id"]])
        task_id, run_id = self.f.service.claim()
        with self.f.service.session() as session:
            session.get(m.PublishingTask, task_id).checkpoint = {"UPLOAD_VIDEO": {"status": "READY", "evidence": {"unit_only": True}}}
        await self.f.service.recover()
        self.assertEqual(len(self.f.service.queue()["locks"]), 2)
        self.assertNotIn("queue:global", [row["resource_key"] for row in self.f.service.queue()["locks"]])
        self.assertEqual(self.f.service.task(task_id)["runs"][0]["id"], run_id)

    async def test_uploaded_checkpoint_prevents_switching_environment_or_account_product_bindings(self):
        await self.batch()
        account = self.f.account
        for change in ({"hubstudio_env_id": "unit-other-env"}, {"cdp_endpoint": "http://127.0.0.1:9998"}, {"expected_username": "unit-other-user"}, {"product_ids": []}):
            with self.subTest(change=change):
                payload = AccountInput(name=account["name"], country=account["country"], product_ids=account["product_ids"], hubstudio_env_id=account["hubstudio_env_id"],
                    cdp_endpoint=account["cdp_endpoint"], expected_username=account["expected_username"])
                payload = AccountInput.model_validate({**payload.model_dump(), **change})
                with self.assertRaises(HTTPException) as blocked:
                    self.f.service.save_account(payload, account["id"])
                self.assertEqual(blocked.exception.detail, "ACCOUNT_ENVIRONMENT_HAS_UNRESOLVED_CHECKPOINT")
        self.assertEqual(self.f.service.accounts()[0]["hubstudio_env_id"], "unit-env")
        self.f.service.control_account(account["id"], False)
        with self.assertRaises(HTTPException):
            await self.f.service.retry(self.task["id"])
        self.assertEqual(self.f.adapter.steps, ["UPLOAD_VIDEO"])

    async def test_independent_repositories_simultaneous_claim_create_only_one_run(self):
        with patch.object(self.f.service, "launch"):
            await self.f.service.create_batch([self.task["id"]])
        repo = StateRepository(str(self.f.repo.engine.url), self.f.repo.seed_path, app_mode="integration")
        other = PublishingService(repo, self.f.config, UnitObservedAdapter())
        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                claimed = list(executor.map(lambda service: service.claim(), [self.f.service, other]))
            self.assertEqual(sum(value is not None for value in claimed), 1)
            with Session(repo.engine) as session:
                self.assertEqual(session.scalar(select(func.count()).select_from(m.PublishRun)), 1)
        finally:
            repo.engine.dispose()

    async def test_broken_preflight_observer_and_malformed_external_result_are_manual_not_success(self):
        self.f.adapter.preflight = AsyncMock(side_effect=RuntimeError("UNIT_ONLY_EXCEPTION"))
        await self.batch()
        self.assertEqual(self.f.service.task(self.task["id"])["error"], "PREFLIGHT_OBSERVATION_FAILED")
        self.assertEqual(self.f.adapter.steps, [])
        self.f.adapter.preflight = UnitObservedAdapter().preflight
        self.f.adapter.execute_step = AsyncMock(return_value={"status": {}, "evidence": {}})
        await self.f.service.retry(self.task["id"])
        await self.f.service.worker
        detail = self.f.service.task(self.task["id"])
        self.assertEqual(detail["error"], "INVALID_ADAPTER_RESULT")
        self.assertEqual(detail["checkpoint"]["external_outcome"], "UNKNOWN")

    async def test_hubstudio_code26_remains_manual_and_only_readonly_endpoint_is_called(self):
        response = Mock()
        response.json.return_value = {"code": 26, "msg": "UNIT_PLAN_BLOCK"}
        client = AsyncMock()
        client.post.return_value = response
        with patch.dict("os.environ", {self.f.config.hubstudio_api_key_env: ""}), patch("app.integrations.publishing.hubstudio.httpx.AsyncClient") as factory:
            factory.return_value.__aenter__.return_value = client
            result = await HubStudioAdapter(self.f.config).preflight({"account": self.f.account})
        self.assertEqual(result["status"], "WAITING_MANUAL")
        self.assertEqual(result["checks"][0]["reason"], "API_PLAN_UNAVAILABLE")
        client.post.assert_awaited_once_with("http://127.0.0.1:6873/api/v1/browser/all-browser-status", json={})

    async def test_local_api_key_is_not_sent_using_unverified_header(self):
        with patch.dict("os.environ", {"HUBSTUDIO_LOCAL_API_KEY": "UNIT_ONLY_SECRET"}), patch("app.integrations.publishing.hubstudio.httpx.AsyncClient") as client:
            result = await HubStudioAdapter(self.f.config).preflight({"account": self.f.account})
        client.assert_not_called()
        self.assertEqual(result["checks"][0]["reason"], "DOCUMENTED_LOCAL_API_KEY_TRANSPORT_REQUIRED")
        self.assertNotIn("UNIT_ONLY_SECRET", str(result))

    async def test_upload_ready_without_hash_account_environment_completion_evidence_is_unknown(self):
        self.f.adapter.execute_step = AsyncMock(return_value={"status": "READY", "evidence": {"uploaded": True}})
        await self.batch()
        detail = self.f.service.task(self.task["id"])
        self.assertEqual(detail["steps"][-1]["status"], "WAITING_MANUAL")
        self.assertEqual(detail["steps"][-1]["error_code"], "UPLOAD_COMPLETION_EVIDENCE_REQUIRED")
        self.assertEqual(detail["checkpoint"]["external_outcome"], "UNKNOWN")

    async def test_read_only_reconcile_unknown_upload_keeps_old_history_and_never_reuploads(self):
        self.f.adapter.raise_upload = True
        await self.batch()
        before = self.f.service.task(self.task["id"])
        old_run, old_step = deepcopy(before["runs"][0]), deepcopy(before["steps"][-1])
        result = await self.f.service.reconcile_upload(self.task["id"])
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["public_submission"], "NOT_PERFORMED")
        after = result["task"]
        self.assertEqual(after["runs"][0]["id"], old_run["id"])
        self.assertEqual(after["runs"][0]["status"], old_run["status"])
        self.assertEqual(after["steps"][-2], old_step)
        self.assertEqual(after["steps"][-1]["name"], "RECONCILE_UPLOAD")
        self.assertNotIn("external_outcome", after["checkpoint"])
        self.assertEqual(after["submission_state"], "NOT_SUBMITTED")
        self.assertEqual(len(self.f.service.queue()["locks"]), 2)
        await self.f.service.retry(self.task["id"])
        await self.f.service.worker
        self.assertEqual(self.f.adapter.steps, ["UPLOAD_VIDEO"])
        self.assertEqual(self.f.adapter.reconcile_calls, 1)

    async def test_read_only_reconcile_rejects_missing_dom_hash_env_and_mutating_proof(self):
        self.f.adapter.raise_upload = True
        await self.batch()
        with self.f.service.session() as session:
            payload = self.f.service.input(session, session.get(m.PublishingTask, self.task["id"]))
        for field in ("hash", "environment", "dom", "readonly"):
            with self.subTest(field=field):
                proof = self.f.adapter.completion(payload, read_only=True)
                if field == "hash":
                    proof["checkpoint"]["video_sha256"] = "UNIT_WRONG_HASH"
                elif field == "environment":
                    proof["checkpoint"]["environment_id"] = "unit-other-env"
                elif field == "dom":
                    proof["evidence"]["dom_evidence"]["upload_complete"] = False
                else:
                    proof["evidence"]["read_only"] = False
                self.f.adapter.reconcile_upload = AsyncMock(return_value=proof)
                result = await self.f.service.reconcile_upload(self.task["id"])
                self.assertEqual(result["status"], "WAITING_MANUAL")
                self.assertEqual(result["task"]["checkpoint"]["external_outcome"], "UNKNOWN")
                self.assertEqual(len(self.f.service.queue()["locks"]), 3)
        self.assertEqual(self.f.adapter.steps, ["UPLOAD_VIDEO"])

    async def test_reconcile_upload_does_not_resolve_public_submission_unknown(self):
        self.f.adapter.raise_upload = True
        await self.batch()
        with self.f.service.session() as session:
            session.get(m.PublishingTask, self.task["id"]).submission_state = "UNKNOWN"
        with self.assertRaises(HTTPException) as blocked:
            await self.f.service.reconcile_upload(self.task["id"])
        self.assertEqual(blocked.exception.detail, "UNKNOWN_UPLOAD_CHECKPOINT_REQUIRED")
        self.assertEqual(self.f.adapter.reconcile_calls, 0)

    async def test_missing_description_pool_is_specific_manual_gate_not_fake_generated_caption(self):
        with self.f.service.session() as session:
            session.get(m.Product, self.f.product["id"]).descriptions = []
        await self.batch()
        detail = self.f.service.task(self.task["id"])
        self.assertEqual(detail["status"], "WAITING_MANUAL")
        self.assertEqual(detail["error"], "PUBLISHING_DATA_INCOMPLETE")
        self.assertEqual(detail["caption"], "")
        self.assertEqual(self.f.adapter.steps, [])

    async def test_explicit_upload_only_uses_original_final_without_descriptions_or_product_link(self):
        with self.f.service.session() as session:
            original = session.get(m.PublishingTask, self.task["id"])
            original.status = "CANCELLED"
            product = session.get(m.Product, self.f.product["id"])
            product.platform_product_id, product.binding_reference, product.descriptions = None, "", []
        self.task = self.f.task(upload_only=True)
        before = self.f.repo.state()["tasks"][0]
        with self.f.service.session() as session:
            asset_count = session.scalar(select(func.count()).select_from(m.VideoAsset))
        preflight = await self.f.service.preflight(self.task["id"])
        self.assertEqual(preflight["status"], "READY")
        self.assertEqual([row["status"] for row in preflight["checks"] if row["name"] in {"product_link", "description"}], ["NOT_REQUIRED", "NOT_REQUIRED"])
        self.f.service.config = PublishingConfig(source_roots=[self.f.root], stop_after_upload=False, allow_publish=True)
        await self.batch()
        detail = self.f.service.task(self.task["id"])
        self.assertEqual(detail["checkpoint"]["execution_scope"], "UPLOAD_ONLY")
        self.assertEqual([row["name"] for row in detail["steps"]], ["PREFLIGHT", "UPLOAD_VIDEO"])
        self.assertEqual(self.f.adapter.steps, ["UPLOAD_VIDEO"])
        self.assertEqual(detail["status"], "WAITING_MANUAL")
        self.assertEqual(detail["submission_state"], "NOT_SUBMITTED")
        self.assertEqual(detail["product_link_status"], "NOT_CHECKED")
        self.assertEqual(detail["caption"], "")
        self.assertIsNone(detail["selected_description"])
        self.assertEqual(self.f.repo.state()["tasks"][0], before)
        with self.f.service.session() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(m.VideoAsset)), asset_count)
            self.assertEqual(session.scalar(select(func.count()).select_from(m.DescriptionUsage)), 0)

    async def test_default_full_scope_missing_real_product_id_remains_manual_no_upload(self):
        self.f.service.config = PublishingConfig(source_roots=[self.f.root], product_selection_mode="exact_id")
        with self.f.service.session() as session:
            product = session.get(m.Product, self.f.product["id"])
            product.platform_product_id = None
            session.get(m.PublishingTask, self.task["id"]).product_binding = ""
            session.get(m.PublishingTask, self.task["id"]).checkpoint = {"product_selection_mode": "exact_id"}
        await self.batch()
        self.assertEqual(self.f.service.task(self.task["id"])["error"], "PUBLISHING_DATA_INCOMPLETE")
        self.assertEqual(self.f.adapter.steps, [])

    async def test_upload_only_unknown_reconciliation_stays_hold_even_publish_config_enabled(self):
        with self.f.service.session() as session:
            session.get(m.PublishingTask, self.task["id"]).checkpoint = {"execution_scope": "UPLOAD_ONLY"}
        self.f.service.config = PublishingConfig(source_roots=[self.f.root], stop_after_upload=False, allow_publish=True)
        self.f.adapter.raise_upload = True
        await self.batch()
        result = await self.f.service.reconcile_upload(self.task["id"])
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["task"]["error"], "UPLOAD_VERIFIED_STOP_BEFORE_PRODUCT_AND_PUBLISH")
        await self.f.service.retry(self.task["id"])
        await self.f.service.worker
        detail = self.f.service.task(self.task["id"])
        self.assertEqual(detail["status"], "WAITING_MANUAL")
        self.assertEqual(detail["submission_state"], "NOT_SUBMITTED")
        self.assertEqual(self.f.adapter.steps, ["UPLOAD_VIDEO"])
        self.assertFalse(any(step["name"] in {"SELECT_DESCRIPTION", "ATTACH_PRODUCT", "SUBMIT"} for step in detail["steps"]))

    async def test_manual_caption_usage_tracks_the_actual_user_text_not_random_pool_entry(self):
        with self.f.service.session() as session:
            row = session.get(m.PublishingTask, self.task["id"])
            row.caption = "UNIT_USER_TEXT_NO_INVENTED_FACTS"
        await self.batch()
        detail = self.f.service.task(self.task["id"])
        self.assertEqual(detail["selected_description"]["text"], detail["caption"])
        self.assertEqual(detail["selected_description"]["source"], "USER_PROVIDED")
        with self.f.service.session() as session:
            used = session.scalar(select(m.DescriptionUsage))
            self.assertEqual(used.description_id, "manual-caption")
            self.assertEqual(used.description_sha256, hashlib.sha256(detail["caption"].encode()).hexdigest())


if __name__ == "__main__":
    unittest.main()
