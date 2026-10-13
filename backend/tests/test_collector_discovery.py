import asyncio
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from io import BytesIO
from types import ModuleType, SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request

from app.config import Settings
from app.domain import find
from app.integrations.collector_service import CollectorService
from app.integrations.external_budget import ExternalBudgetExceeded
from app.integrations.legacy.collector_worker import SEMANTIC_ENUM_GUIDANCE, SEMANTIC_PROTOCOL, discover, factory_paid_requests
from app.repository import StateRepository
from tests.test_real_collector import RULES


class IsolatedDiscoveryAdapter:
    def __init__(self):
        self.calls = 0
        self.status = "READY"

    async def discover(self, context, on_event, cancel_event, run_id):
        self.calls += 1
        await asyncio.to_thread(on_event, {"type": "PROCESS_STARTED", "pid": 4321, "command": ["unit-discovery"], "cwd": "/isolated"})
        await asyncio.to_thread(on_event, {"type": "PROCESS_HEARTBEAT", "pid": 4321, "last_heartbeat": 1700000000})
        return {"task_id": context["task_id"], "status": self.status, "error_code": "LOGIN_REQUIRED" if self.status != "READY" else "",
            "hits": [{"video_id": str(value), "url": f"https://www.tiktok.com/@unit/video/{value}"} for value in (101, 102, 101, 103)] if self.status == "READY" else [],
            "search_config": {"search_max_results": 0, "search_max_scrolls": 60}, "paid_requests": 0,
            "_process": {"pid": 4321, "exit_code": 0, "stdout_log_path": "/unit/stdout.log", "stderr_log_path": "/unit/stderr.log",
                         "last_heartbeat": 1700000000, "termination_signal": None, "timed_out": False, "resource_info": {"cpu_seconds": .1}}}


class CollectorDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.settings = Settings(app_mode="integration", integration_dry_run=False, storage_root=root / "storage", database_url="sqlite:///" + str(root / "unit.db"))
        self.repository = StateRepository(self.settings.database_url, Path(__file__).parents[1] / "app/seed.json", app_mode="integration")
        self.events = []
        async def broadcast(event):
            self.events.append(event)
        self.adapter = IsolatedDiscoveryAdapter()
        self.service = CollectorService(self.repository, SimpleNamespace(broadcast=broadcast), self.settings, self.adapter)
        self.rules_patch = patch("app.integrations.collector_service.read_production_rules", return_value=RULES)
        self.rules_patch.start()

    async def asyncTearDown(self):
        self.rules_patch.stop()
        self.repository.engine.dispose()
        self.temp.cleanup()

    async def create(self, country="墨西哥"):
        return (await self.service.create({"country": country, "keyword": "body", "product_images": [str(self.settings.storage_root / "product.png")],
            "source_document": "https://docs.qq.com/sheet/real-context", "source_row": 7}))["task_id"]

    async def test_three_distinct_bound_tasks_preserve_context_and_immutable_rules(self):
        root_id = await self.create()
        before = self.service.context(root_id)
        discover = self.adapter.discover
        async def check_status(*args, **kwargs):
            self.assertEqual(find(self.repository.state(), "tasks", root_id)["status"], "COLLECTING")
            return await discover(*args, **kwargs)
        self.adapter.discover = check_status
        expanded = await self.service.expand_search(root_id)
        self.assertEqual(expanded["discovered_count"], 3)
        self.assertEqual(len(set(expanded["task_ids"])), 3)
        self.assertEqual(expanded["task_ids"][0], root_id)
        contexts = [self.service.context(value) for value in expanded["task_ids"]]
        self.assertEqual([value["video_id"] for value in contexts], ["101", "102", "103"])
        for context in contexts:
            for key in ("country", "keyword", "product_id", "product_images", "source_document", "source_row", "context_id", "factory_rules"):
                self.assertEqual(context[key], before[key])
        state = self.repository.state()
        run = find(state, "runs", expanded["run_id"])
        self.assertEqual(run["provider"], "LegacyCollectorDiscoveryAdapter")
        self.assertEqual(run["status"], "success")
        self.assertEqual(run["pid"], 4321)
        self.assertTrue(run["last_heartbeat"].startswith("2023-11-14T22:13:20"))
        self.assertEqual(run["resource_info"]["cpu_seconds"], .1)
        self.assertEqual(run["resource_info"]["collector_environment"]["status"], "READY")
        self.assertFalse(run["timed_out"])
        self.assertEqual(find(state, "tasks", root_id)["status"], "CREATED")
        self.assertTrue(all(not task["collector_result"] and not task["source_video_id"] for task in state["tasks"]))
        self.assertFalse(state["assets"])
        again = await self.service.expand_search(root_id)
        self.assertEqual(again["task_ids"], expanded["task_ids"])
        self.assertEqual(self.adapter.calls, 1)
        self.assertEqual(len(self.repository.state()["tasks"]), 3)

    async def test_cancelled_parent_does_not_open_discovery_or_create_run(self):
        from fastapi import HTTPException
        import threading
        task_id = await self.create()
        stop = threading.Event()
        stop.set()
        with self.assertRaises(HTTPException) as cancelled:
            await self.service.expand_search(task_id, cancel_event=stop)
        self.assertEqual(cancelled.exception.status_code, 409)
        self.assertEqual(self.adapter.calls, 0)
        self.assertFalse(self.repository.state()["runs"])
        self.assertEqual(find(self.repository.state(), "tasks", task_id)["status"], "CREATED")

    async def test_countries_with_same_keyword_are_never_merged(self):
        first, second = await self.create("美国"), await self.create("法国")
        one, two = await self.service.expand_search(first), await self.service.expand_search(second)
        self.assertFalse(set(one["task_ids"]) & set(two["task_ids"]))
        self.assertNotEqual(self.service.context(first)["context_id"], self.service.context(second)["context_id"])

    async def test_login_wait_creates_no_fake_video_task_or_asset(self):
        task_id = await self.create()
        self.adapter.status = "WAITING_MANUAL"
        result = await self.service.expand_search(task_id)
        self.assertEqual(result["error_code"], "LOGIN_REQUIRED")
        state = self.repository.state()
        self.assertEqual(len(state["tasks"]), 1)
        self.assertEqual(state["tasks"][0]["status"], "QUARANTINED")
        self.assertEqual(state["tasks"][0]["context"]["collector_auto_triage"]["reason"], "LOGIN_REQUIRED")
        self.assertIsNone(state["tasks"][0]["source_video_id"])
        self.assertFalse(state["assets"])
        self.assertEqual(state["runs"][0]["status"], "failed")

    async def test_failed_collector_is_retryable_inside_its_own_active_production_queue(self):
        identifier = await self.create()
        self.adapter.dry_run = lambda context: {"ok": True}
        def queued(state):
            task = find(state, "tasks", identifier)
            task.update(status="QUEUED")
            task["context"]["production"] = {"batch_id": "unit-batch", "previous_status": "FAILED"}
            state["batches"].append({"id": "unit-batch", "task_ids": [identifier], "status": "RUNNING", "active_task_id": identifier,
                "items": [{"task_id": identifier, "status": "RUNNING", "stage": "collector"}], "summary": {}, "created_at": "2026-10-08T00:00:00+00:00", "updated_at": "2026-10-08T00:00:00+00:00"})
        self.repository.mutate(queued)
        result = await self.service.queue(identifier, retry=True)
        run = find(self.repository.state(), "runs", result["run_id"])
        self.assertEqual(run["retry_count"], 1)
        self.assertEqual(find(self.repository.state(), "tasks", identifier)["status"], "COLLECTING")


class DiscoveryWorkerTests(unittest.TestCase):
    def test_legacy_search_pagination_limits_are_preserved_and_snapshot_is_reusable(self):
        calls = []
        config = {"search_max_results": 0, "search_max_scrolls": 123, "search_scroll_wait_ms": 1800, "search_stable_rounds": 5}
        class Browser:
            def __init__(self, values, *_args):
                self.config = values
            def connect(self):
                return self
            def ensure_tiktok_search_login(self):
                pass
            def close(self):
                pass
        def search(browser, keyword):
            calls.append(keyword)
            self.assertEqual(browser.config["search_max_results"], 0)
            self.assertEqual(browser.config["search_max_scrolls"], 123)
            return [SimpleNamespace(video_id=str(index), url=f"https://www.tiktok.com/@unit/video/{index}") for index in (1, 2, 3)]
        modules = {}
        for name, values in {"src.browser": {"BrowserClient": Browser, "LoginRequired": type("LoginRequired", (Exception,), {}), "VerificationRequired": type("VerificationRequired", (Exception,), {})},
            "src.config": {"load_config": lambda path: config, "resolve_paths": lambda root, values: values}, "src.tiktok_search": {"search_keyword": search}}.items():
            module = ModuleType(name)
            module.__dict__.update(values)
            modules[name] = module
        with tempfile.TemporaryDirectory() as temporary, patch.dict(sys.modules, modules), patch.object(sys, "path", sys.path.copy()):
            root = Path(temporary)
            context = {"task_id": "unit", "country": "墨西哥", "keyword": "body", "context_id": "row-context"}
            first = discover(context, root, root, root / "results.json", root / "state.json")
            second = discover(context, root, root, root / "results.json", root / "state.json")
            self.assertEqual(len(first["hits"]), 3)
            self.assertEqual(first, second)
            self.assertEqual(calls, ["body"])
            self.assertEqual(first["paid_requests"], 0)
            self.assertEqual(first["download_submission_count"], 0)

    def test_paid_vision_guard_reserves_each_actual_request_and_denies_replay(self):
        pipeline, analysis = ModuleType("src.pipeline"), ModuleType("src.video_analysis")
        calls = []
        pipeline.match_product_references = lambda *_args: calls.append("reference") or {"product_match": "pass"}
        analysis._api_configuration = lambda config: ("endpoint", "model", "secret-not-logged", 5)
        analysis._semantic_open = lambda request, timeout: calls.append("semantic") or "response"
        with tempfile.TemporaryDirectory() as temporary, patch.dict(sys.modules, {"src.pipeline": pipeline, "src.video_analysis": analysis}):
            root = Path(temporary)
            image = root / "unit.png"
            image.write_bytes(b"unit-only-fixture")
            reserved, outcome = [], {}
            def debit(storage, family, key):
                if key in reserved:
                    raise ExternalBudgetExceeded("UNKNOWN_EXTERNAL_REQUEST: unit replay")
                reserved.append(key)
                return {"reserved_at": "unit-time"}
            with patch("app.integrations.legacy.collector_worker.reserve", side_effect=debit), factory_paid_requests(root, "unit-task", outcome) as observations:
                pipeline.match_product_references("keyword", [image], [image], {})
                request = Request("https://unit.invalid/chat/completions", data=json.dumps({"messages": [{"role": "system", "content": "unit reviewer"}]}).encode())
                analysis._semantic_open(request, 5)
                with self.assertRaises(ExternalBudgetExceeded):
                    analysis._semantic_open(request, 5)
            self.assertEqual(calls, ["reference", "semantic"])
            self.assertEqual(len(reserved), 2)
            self.assertEqual(outcome["error_code"], "UNKNOWN_EXTERNAL_REQUEST")
            self.assertEqual(set(observations), {"reference_provenance", "semantic_provenance"})
            self.assertTrue(all(value.endswith("+00:00") for value in observations.values()))


class SemanticResponseEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.sent = []
        self.body = json.dumps({"choices": [{"message": {"content": "{}"}}]}).encode()
        self.pipeline, self.analysis = ModuleType("src.pipeline"), ModuleType("src.video_analysis")
        self.pipeline.match_product_references = lambda *_args: {}
        self.analysis._api_configuration = lambda _config: None
        def transport(request, timeout):
            self.sent.append(request)
            return BytesIO(self.body)
        self.analysis._semantic_open = transport
        modules = patch.dict(sys.modules, {"src.pipeline": self.pipeline, "src.video_analysis": self.analysis})
        modules.start()
        self.addCleanup(modules.stop)
        self.budget = patch("app.integrations.legacy.collector_worker.reserve", return_value={"reserved_at": "unit-time"})
        self.reserve = self.budget.start()
        self.addCleanup(self.budget.stop)

    def request(self, guidance="unit reviewer"):
        return Request("https://unit.invalid/chat/completions", data=json.dumps({"model": "unit-model", "messages": [
            {"role": "system", "content": guidance}, {"role": "user", "content": [{"type": "text", "text": "untrusted video"}]}]}).encode(),
            headers={"Authorization": "Bearer unit-auth-secret", "Cookie": "sid=unit-cookie-secret"})

    def read(self, request):
        with factory_paid_requests(self.root, "unit-task", {}, evidence_dir=self.root / "run-evidence"):
            with self.analysis._semantic_open(request, 5) as response:
                return response.read()

    def evidence(self):
        paths = list((self.root / "run-evidence").glob("semantic-response-*.json"))
        self.assertEqual(len(paths), 1)
        return json.loads(paths[0].read_text())

    def test_first_attempt_adds_enums_and_saves_only_redacted_content(self):
        content = json.dumps({"product_match": {"verdict": "uncertain", "confidence": .9,
            "reason": "unit-auth-secret sid=unit-cookie-secret unit-extra-token unit-env-cookie"}})
        self.body = json.dumps({"choices": [{"message": {"content": content}}], "headers": {"secret": "must-not-save-provider-headers"}}).encode()
        request = self.request()
        original_data = request.data
        with patch.dict(os.environ, {"FACTORY_TEST_TOKEN": "unit-extra-token", "FACTORY_TEST_COOKIE": "unit-env-cookie"}):
            self.assertEqual(self.read(request), self.body)
        self.assertEqual(request.data, original_data)
        sent = json.loads(self.sent[0].data)
        self.assertIn(SEMANTIC_ENUM_GUIDANCE, sent["messages"][0]["content"])
        self.assertEqual(sent["messages"][1], json.loads(original_data)["messages"][1])
        evidence = self.evidence()
        self.assertEqual(evidence["protocol"], SEMANTIC_PROTOCOL)
        self.assertEqual(evidence["request_sha256"], hashlib.sha256(self.sent[0].data).hexdigest())
        self.assertEqual(evidence["response_sha256"], hashlib.sha256(self.body).hexdigest())
        self.assertEqual(json.loads(evidence["content"])["product_match"]["verdict"], "uncertain")
        for secret in ("unit-auth-secret", "unit-cookie-secret", "unit-extra-token", "unit-env-cookie", "must-not-save-provider-headers"):
            self.assertNotIn(secret, json.dumps(evidence))
        self.assertNotIn("headers", evidence)
        self.assertFalse(evidence["normalized"])

    def test_boolean_verdict_is_saved_unchanged_and_never_guessed_as_pass(self):
        content = json.dumps({"product_match": {"verdict": True, "confidence": .99, "reason": "unit invalid enum"}})
        self.body = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
        self.assertEqual(self.read(self.request()), self.body)
        self.assertIs(json.loads(self.evidence()["content"])["product_match"]["verdict"], True)
        self.assertEqual(self.evidence()["validation"], "legacy_strict_parser")
        self.assertFalse(self.evidence()["normalized"])

    def test_existing_protocol_is_not_duplicated_and_unknown_replay_never_reaches_transport(self):
        self.read(self.request(SEMANTIC_ENUM_GUIDANCE))
        self.assertEqual(json.dumps(json.loads(self.sent[0].data), ensure_ascii=False).count(SEMANTIC_PROTOCOL), 1)
        before = self.evidence()
        self.reserve.side_effect = ExternalBudgetExceeded("UNKNOWN_EXTERNAL_REQUEST: unit reservation already consumed")
        with self.assertRaises(ExternalBudgetExceeded):
            self.read(self.request(SEMANTIC_ENUM_GUIDANCE))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.evidence(), before)

    def test_invalid_http_body_is_returned_to_legacy_parser_without_fake_content(self):
        self.body = b"invalid unit HTTP response"
        self.assertEqual(self.read(self.request()), self.body)
        self.assertEqual(self.evidence()["status"], "content_unavailable")
        self.assertIsNone(self.evidence()["content"])

    def test_semantic_receipt_records_only_real_status_ids_and_numeric_usage(self):
        self.body = json.dumps({"id": "provider-response-unit", "choices": [{"message": {"content": "{}"}}],
            "usage": {"prompt_tokens": 80, "completion_tokens": 20, "total_tokens": 100, "api_key": "unit-usage-secret",
                "prompt_tokens_details": {"cached_tokens": 10, "token_secret": "unit-detail-secret"}, "unknown_counter": 99}}).encode()
        def transport(request, timeout):
            self.sent.append(request)
            response = BytesIO(self.body)
            response.status = 200
            response.headers = {"x-request-id": "provider-http-unit", "Set-Cookie": "unit-provider-cookie", "Authorization": "unit-provider-auth"}
            return response
        self.analysis._semantic_open = transport
        self.assertEqual(self.read(self.request()), self.body)
        evidence = self.evidence()
        self.assertEqual(evidence["http_status"], 200)
        self.assertEqual(evidence["provider_response_id"], "provider-response-unit")
        self.assertEqual(evidence["provider_request_id"], "provider-http-unit")
        self.assertEqual(evidence["usage"], {"prompt_tokens": 80, "completion_tokens": 20, "total_tokens": 100,
            "prompt_tokens_details": {"cached_tokens": 10}})
        self.assertEqual(evidence["request_key"], "unit-task:semantic:" + evidence["request_sha256"])
        for secret in ("unit-usage-secret", "unit-detail-secret", "unit-provider-cookie", "unit-provider-auth"):
            self.assertNotIn(secret, json.dumps(evidence))

    def test_http_error_receipt_preserves_status_and_error_body_for_legacy_consumer(self):
        body = json.dumps({"request_id": "provider-error-unit", "error": {"message": "unit-auth-secret"}}).encode()
        def transport(request, timeout):
            self.sent.append(request)
            raise HTTPError(request.full_url, 429, "unit rate limit", {"Set-Cookie": "unit-error-cookie"}, BytesIO(body))
        self.analysis._semantic_open = transport
        with self.assertRaises(HTTPError) as raised:
            self.read(self.request())
        self.assertEqual(raised.exception.code, 429)
        self.assertEqual(raised.exception.read(), body)
        evidence = self.evidence()
        self.assertEqual(evidence["http_status"], 429)
        self.assertEqual(evidence["provider_request_id"], "provider-error-unit")
        self.assertEqual(evidence["status"], "http_error")
        self.assertIsNone(evidence["content"])
        self.assertNotIn("unit-auth-secret", json.dumps(evidence))
        self.assertNotIn("unit-error-cookie", json.dumps(evidence))

    def test_missing_provider_metadata_is_unknown_not_a_fabricated_success_receipt(self):
        self.read(self.request())
        evidence = self.evidence()
        self.assertIsNone(evidence["http_status"])
        self.assertIsNone(evidence["provider_response_id"])
        self.assertIsNone(evidence["provider_request_id"])
        self.assertEqual(evidence["usage"], {})


class ProductResponseEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.image = self.root / "product.png"
        self.image.write_bytes(b"unit fixture image")
        self.body = json.dumps({"id": "product-response-unit", "request_id": "product-request-unit", "choices": [{"message": {
            "content": json.dumps({"product_match": "pass", "confidence": .94, "reason": "visible unit product"})}}],
            "usage": {"input_tokens": 50, "output_tokens": 15, "total_tokens": 65}}).encode()
        self.sent, self.read_bytes, self.errors = [], [], []
        self.pipeline, self.analysis = ModuleType("src.pipeline"), ModuleType("src.video_analysis")
        def transport(request, timeout):
            self.sent.append(request)
            response = BytesIO(self.body)
            response.status = 200
            response.headers = {"x-request-id": "ignored-header-id", "Set-Cookie": "unit-not-business-cookie"}
            return response
        self.transport = transport
        self.analysis.urllib = SimpleNamespace(request=SimpleNamespace(urlopen=transport))
        self.analysis._api_configuration = lambda _config: ("https://unit.invalid/chat/completions", "unit-model", "unit-product-key", 5)
        self.analysis._semantic_open = transport
        def match(keyword, references, previews, config):
            request = Request("https://unit.invalid/chat/completions", data=json.dumps({"model": "unit-model",
                "messages": [{"role": "user", "content": keyword}]}).encode(), headers={"Authorization": "Bearer unit-product-key"})
            try:
                with self.analysis.urllib.request.urlopen(request, timeout=5) as response:
                    body = response.read()
                self.read_bytes.append(body)
                return json.loads(json.loads(body)["choices"][0]["message"]["content"])
            except HTTPError as exc:
                self.errors.append((exc.code, exc.read()))
                return {"product_match": "uncertain", "reason": "vision_api_error:HTTPError"}
        self.pipeline.match_product_references = match
        modules = patch.dict(sys.modules, {"src.pipeline": self.pipeline, "src.video_analysis": self.analysis})
        modules.start()
        self.addCleanup(modules.stop)
        budget = patch("app.integrations.legacy.collector_worker.reserve", return_value={"reserved_at": "unit-time"})
        self.reserve = budget.start()
        self.addCleanup(budget.stop)
        self.config = {"product_match_api_model": "unit-model", "product_match_api_base_url": "https://unit.invalid"}

    def read(self):
        with factory_paid_requests(self.root, "unit-task", {}, evidence_dir=self.root / "run-evidence"):
            result = self.pipeline.match_product_references("unit product", [self.image], [self.image], self.config)
        self.assertIs(self.analysis.urllib.request.urlopen, self.transport)
        return result

    def evidence(self):
        paths = list((self.root / "run-evidence").glob("product-response-*.json"))
        self.assertEqual(len(paths), 1)
        return json.loads(paths[0].read_text())

    def test_actual_product_transport_receipt_keeps_original_consumer_bytes_and_verdict(self):
        self.assertEqual(self.read(), {"product_match": "pass", "confidence": .94, "reason": "visible unit product"})
        self.assertEqual(self.read_bytes, [self.body])
        evidence = self.evidence()
        self.assertEqual(evidence["http_status"], 200)
        self.assertEqual(evidence["provider_response_id"], "product-response-unit")
        self.assertEqual(evidence["provider_request_id"], "product-request-unit")
        self.assertEqual(evidence["usage"], {"input_tokens": 50, "output_tokens": 15, "total_tokens": 65})
        self.assertEqual(evidence["request_sha256"], hashlib.sha256(self.sent[0].data).hexdigest())
        self.assertEqual(evidence["response_sha256"], hashlib.sha256(self.body).hexdigest())
        self.assertTrue(evidence["request_key"].startswith("unit-task:reference:"))
        self.assertEqual(self.reserve.call_count, 1)
        self.assertNotIn("unit-not-business-cookie", json.dumps(evidence))
        self.assertFalse(evidence["normalized"])

    def test_business_evidence_redacts_credential_keys_and_echoed_request_secrets(self):
        content = {"product_match": True, "confidence": .94, "reason": "unit-product-key unit-env-token Bearer unknown-bearer",
            "api_key": "unknown-payload-key", "nested": {"cookie": "unknown-payload-cookie"}}
        self.body = json.dumps({"id": "response-unit-product-key", "choices": [{"message": {"content": json.dumps(content)}}],
            "usage": {"total_tokens": 65, "output_tokens": True, "input_tokens": -1, "secret": "unknown-usage-secret"}}).encode()
        with patch.dict(os.environ, {"UNIT_ACCESS_TOKEN": "unit-env-token"}):
            self.assertEqual(self.read(), content)
        saved = self.evidence()
        self.assertIs(json.loads(saved["content"])["product_match"], True)
        self.assertEqual(saved["usage"], {"total_tokens": 65})
        for secret in ("unit-product-key", "unit-env-token", "unknown-bearer", "unknown-payload-key", "unknown-payload-cookie", "unknown-usage-secret"):
            self.assertNotIn(secret, json.dumps(saved))
        self.assertEqual(self.read_bytes, [self.body])

    def test_product_http_error_returns_same_uncertain_result_and_keeps_error_receipt(self):
        body = json.dumps({"request_id": "product-error-unit", "error": {"message": "unit-product-key"}}).encode()
        def transport(request, timeout):
            self.sent.append(request)
            raise HTTPError(request.full_url, 401, "unit authentication failure", {}, BytesIO(body))
        self.transport = transport
        self.analysis.urllib.request.urlopen = transport
        self.assertEqual(self.read(), {"product_match": "uncertain", "reason": "vision_api_error:HTTPError"})
        self.assertEqual(self.errors, [(401, body)])
        self.assertEqual(self.evidence()["http_status"], 401)
        self.assertEqual(self.evidence()["status"], "http_error")
        self.assertNotIn("unit-product-key", json.dumps(self.evidence()))

    def test_replay_reservation_denial_never_reaches_product_transport_or_rewrites_receipt(self):
        self.read()
        before = self.evidence()
        self.reserve.side_effect = ExternalBudgetExceeded("UNKNOWN_EXTERNAL_REQUEST: unit duplicate")
        with self.assertRaises(ExternalBudgetExceeded):
            self.read()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.evidence(), before)
        self.assertIs(self.analysis.urllib.request.urlopen, self.transport)

    def test_receipt_disk_failure_does_not_change_legacy_product_decision(self):
        with patch("app.integrations.legacy.collector_worker.write_json", side_effect=OSError("unit evidence disk failure")):
            self.assertEqual(self.read()["product_match"], "pass")
        self.assertEqual(self.read_bytes, [self.body])
        self.assertFalse(list((self.root / "run-evidence").glob("*.json")))


if __name__ == "__main__":
    unittest.main()
