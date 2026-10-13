import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.integrations import rpa_probe
from app.integrations.legacy.video_adapter import LegacyPromptAdapter
from app.integrations.production_readiness import ProductionReadiness
from app.integrations.production_routes import create_production_router


class RPAProbeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.consumer = self.root / "consumer.py"
        self.consumer.write_text("def main(args):\n    if args['action'] == 'probe':\n        return {'snapshot': {}}\n")
        self.consumer_hash = hashlib.sha256(self.consumer.read_bytes()).hexdigest()
        patcher = patch.object(rpa_probe, "CONSUMER_SHA256", self.consumer_hash)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.url = "https://chatgpt.com/c/6ac458cf-cc34-83ea-b98c-7aa5aac937a7"

    def create(self):
        result = rpa_probe.create_probe(self.root, self.url, self.consumer)
        self.request_path = Path(result["request_path"])
        self.request = json.loads(self.request_path.read_text())
        return result

    def respond(self, *, snapshot=None, **changes):
        ticket = {key: self.request[key] for key in ("identity", "execution_id")}
        ticket.update(request_path=str(self.request_path), claimed_at=int(time.time()))
        rpa_probe.write_json(self.root / "rpa/rpa_queue" / (self.request["execution_id"] + ".claim.json"), ticket)
        response = {"identity": self.request["identity"], "execution_id": self.request["execution_id"],
                    "status": "completed", "snapshot": snapshot or {"url": self.url, "editors": [{"input_text": ""}]}}
        rpa_probe.write_json(Path(self.request["response_path"]), {**response, **changes})
        return rpa_probe.probe_status(self.root)

    def test_ticket_is_unpaid_probe_in_factory_and_not_ready_without_consumer(self):
        result = self.create()
        self.assertEqual(self.request["action"], "probe")
        self.assertNotIn("instruction", self.request)
        for key in ("response_path", "download_dir"):
            self.assertTrue(Path(self.request[key]).is_relative_to(self.root / "rpa"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["paid_requests"], 0)
        self.assertEqual(result["desktop_loop"], "UNKNOWN")

    def test_current_claim_response_and_empty_editor_confirm_readiness(self):
        self.create()
        result = self.respond()
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["desktop_loop"], "CONNECTED")
        self.assertNotIn("snapshot", result)
        self.assertEqual(result["paid_requests"], 0)

    def test_wrong_response_identity_never_confirms_readiness(self):
        self.create()
        result = self.respond(identity="other")
        self.assertEqual(result["status"], "ERROR")
        self.assertFalse(result["ok"])

    def test_draft_is_preserved_and_not_exposed_in_public_status(self):
        self.create()
        result = self.respond(snapshot={"url": self.url, "editors": [{"input_text": "existing private draft"}]})
        self.assertEqual(result["error_code"], "CHATGPT_EXISTING_DRAFT")
        self.assertTrue(result["draft_present"])
        self.assertNotIn("existing private draft", json.dumps(result))

    def test_login_captcha_and_limit_never_pass(self):
        for snapshot in ({"url": self.url, "editors": []},
                         {"url": self.url, "editors": [{"input_text": ""}], "text": "Verify you are human"},
                         {"url": self.url, "editors": [{"input_text": ""}], "text": "达到图片生成上限"}):
            self.create()
            self.assertFalse(self.respond(snapshot=snapshot)["ok"])

    def test_expired_or_repointed_evidence_does_not_pass(self):
        self.create()
        self.respond()
        state_path = self.root / "rpa/probe.json"
        os.utime(Path(self.request["response_path"]), (time.time()-121, time.time()-121))
        self.assertEqual(rpa_probe.probe_status(self.root)["status"], "MISSING")
        self.create()
        rpa_probe.write_json(self.request_path, {**self.request, "action": "generate"})
        self.assertEqual(rpa_probe.probe_status(self.root)["status"], "ERROR")

    def test_changed_consumer_cannot_receive_probe_ticket(self):
        self.consumer.write_text("def main(args):\n    return generate(args)\n")
        with self.assertRaisesRegex(ValueError, "CONSUMER_CHANGED"):
            rpa_probe.create_probe(self.root, self.url, self.consumer)
        self.assertFalse((self.root / "rpa").exists())

    def test_pending_refresh_reuses_original_execution_without_duplicate_ticket(self):
        original = self.create()
        for _ in range(3):
            result = rpa_probe.refresh_probe(self.root, self.url, self.consumer, wait_s=0)
            self.assertEqual(result["execution_id"], original["execution_id"])
        self.assertEqual(len(list((self.root / "rpa/rpa_queue").glob("*.json"))), 1)

    def test_expired_reply_refresh_keeps_previous_request_claim_and_response(self):
        original = self.create()
        self.respond()
        response = Path(self.request["response_path"])
        os.utime(response, (time.time()-121, time.time()-121))
        result = rpa_probe.refresh_probe(self.root, self.url, self.consumer, wait_s=0)
        self.assertNotEqual(result["execution_id"], original["execution_id"])
        self.assertTrue(response.exists())
        self.assertTrue(Path(original["request_path"]).exists())

    def test_late_current_response_is_not_rejected_for_old_submission_time(self):
        self.create()
        state_path = self.root / "rpa/probe.json"
        state = json.loads(state_path.read_text())
        rpa_probe.write_json(state_path, {**state, "created_at": time.time()-300})
        self.assertEqual(self.respond()["status"], "READY")

    def test_disabled_editor_or_failed_response_does_not_pass_or_leak_content(self):
        self.create()
        result = self.respond(snapshot={"url": self.url, "editors": [{"input_text": "", "disabled": True}]})
        self.assertEqual(result["error_code"], "CHATGPT_EDITOR_DISABLED")
        result = self.respond(status="failed", error="private response with api-key-like text 123")
        self.assertEqual(result["error_code"], "RPA_PROBE_FAILED")
        self.assertNotIn("private response", json.dumps(result))

    def test_only_explicit_refresh_http_requests_probe_and_replaces_stale_check(self):
        prompt = LegacyPromptAdapter(self.root, storage_root=self.root)
        prompt.readiness = AsyncMock(return_value={"metadata": {"checks": [
            {"name": "RPA", "status": "READY", "ok": True},
            {"name": "AI API", "status": "READY", "ok": True},
        ]}})
        collector = SimpleNamespace(rules=lambda: {"sha256": "unit-fixture"}, dry_run=lambda task_id=None: {"status": "READY", "checks": []})
        vidbee = SimpleNamespace(readiness=AsyncMock(return_value={"status": "READY"}))
        video = SimpleNamespace(assets=SimpleNamespace(readiness=AsyncMock(return_value={"metadata": {"checks": [
            {"name": "RPA", "status": "MISSING", "ok": False},
            {"name": "AI API", "status": "MISSING", "ok": False},
        ]}})))
        service = ProductionReadiness(SimpleNamespace(storage_root=self.root, integration_dry_run=False, app_mode="integration"), collector, vidbee, video, prompt)
        service._tiktok = lambda: {"status": "READY"}
        service._quality = lambda: {"status": "READY"}
        app = FastAPI()
        app.include_router(create_production_router(SimpleNamespace(readiness=service)))
        with TestClient(app) as client:
            self.assertEqual(prompt.readiness.call_count, 0)
            self.assertEqual(client.get("/api/production/readiness").status_code, 200)
            prompt.readiness.assert_called_once_with()
            response = client.get("/api/production/readiness?refresh=true")
            self.assertEqual(response.status_code, 200)
            prompt.readiness.assert_called_with(refresh=True)
            checks = [row for row in response.json()["checks"] if row["name"] == "RPA"]
            self.assertEqual(len(checks), 1)
            self.assertEqual(checks[0]["status"], "READY")
            checks = [row for row in response.json()["checks"] if row["name"] == "AI API"]
            self.assertEqual(len(checks), 1)
            self.assertEqual(checks[0]["status"], "READY")
