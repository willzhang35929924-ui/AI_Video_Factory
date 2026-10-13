import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.main import create_app
from app.models import BackgroundAsset, CompositionAsset, Prompt


class MockWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.config = Settings(app_mode="mock", database_url="sqlite:///" + str(self.root / "mock.db"), storage_root=self.root / "storage")
        self.app = create_app(self.config)
        self.client = TestClient(self.app)
        self.client.__enter__()
        settings = self.state()["settings"]
        self.action("save_settings", payload={**settings, "mock_delay_ms": 100})

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.directory.cleanup()

    def state(self):
        response = self.client.get("/api/state")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def action(self, action, entity_id=None, payload=None, expected=200):
        response = self.client.post("/api/actions", json={"action": action, "entity_id": entity_id, "payload": payload or {}})
        self.assertEqual(response.status_code, expected, response.text)
        return response.json()

    def wait(self, run_id, status="success"):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            run = next(item for item in self.state()["runs"] if item["id"] == run_id)
            if run["status"] in ("success", "failed"):
                self.assertEqual(run["status"], status, run)
                return run
            time.sleep(0.05)
        self.fail("Mock simulation timed out")

    def task(self, task_id):
        return next(item for item in self.state()["tasks"] if item["id"] == task_id)

    def test_collect_cutout_background_compose_review_publish_and_provenance(self):
        result = self.action("collect", payload={"row_ids": ["col-1"]})
        task_id = result["entity_id"]
        self.wait(result["run_id"])
        self.assertEqual(self.task(task_id)["current_stage"], "cutout")
        result = self.action("start_cutout", task_id)
        self.action("start_cutout", task_id, expected=409)
        self.wait(result["run_id"])
        self.action("accept_cutout", task_id)
        self.assertEqual(self.task(task_id)["current_stage"], "background")
        prompt = self.action("generate_prompt", task_id, {"scene_type": "城市阳台", "color": "冷灰"})
        result = self.action("generate_background", task_id, {"prompt_id": prompt["prompt_id"], "provider": "chatgpt_rpa"})
        self.wait(result["run_id"])
        parameters = {"exposure": 0.5, "contrast": 1.2, "saturation": 0.9, "crop": "fit", "width": 1080, "height": 1920, "fps": 30}
        result = self.action("compose", task_id, parameters)
        self.wait(result["run_id"])
        state = self.state()
        review = next(item for item in state["reviews"] if item["task_id"] == task_id)
        self.assertEqual(review["status"], "pending")
        self.action("review_approve", review["id"], {"note": "Mock 验证通过"})
        row = next(item for item in self.state()["publishing"] if item["task_id"] == task_id)
        self.action("publish_now", row["id"], expected=409)
        self.action("generate_caption", row["id"])
        result = self.action("publish_now", row["id"])
        self.wait(result["run_id"])
        self.assertEqual(self.task(task_id)["status"], "completed")
        self.action("publish_now", row["id"], expected=409)
        metadata = self.client.get(f"/api/tasks/{task_id}/metadata").json()
        self.assertEqual(metadata["source_video"]["fps"], 29.97)
        self.assertEqual(metadata["source_video"]["duration"], 24.6)
        self.assertEqual(metadata["composition"]["fps"], 30)
        self.assertEqual(metadata["quality"]["product_match_score"], 0.96)
        self.assertEqual(metadata["publishing"]["status"], "published")
        with Session(self.app.state.repository.engine) as session:
            composition = session.scalar(select(CompositionAsset).where(CompositionAsset.task_id == task_id))
            self.assertEqual(composition.parameters, parameters)
            self.assertIsNotNone(composition.person_asset_id)
            self.assertIsNotNone(composition.background_id)
        self.assertFalse(self.config.storage_root.exists())

    def test_collector_derived_metrics_threshold_failure_and_retry(self):
        rows = self.state()["collector"]
        rows[0].update(views=999999, likes=999999, status="success", min_views=9999999)
        self.action("save_collector", payload={"rows": rows})
        saved = self.state()["collector"][0]
        self.assertEqual(saved["views"], 128000)
        self.assertEqual(saved["likes"], 3420)
        result = self.action("collect", payload={"row_ids": [saved["id"]]})
        self.wait(result["run_id"], "failed")
        self.assertEqual(self.task(result["entity_id"])["status"], "failed")
        self.assertTrue(any("Mock 指标未通过" in item["message"] for item in self.state()["errors"]))
        rows = self.state()["collector"]
        rows[0]["min_views"] = 100000
        self.action("save_collector", payload={"rows": rows})
        retried = self.action("retry_task", result["entity_id"])
        self.wait(retried["run_id"])
        self.assertEqual(self.task(result["entity_id"])["current_stage"], "cutout")

    def test_pause_duplicate_reset_and_recovery(self):
        self.assertTrue(any(item["code"] == "MOCK_INTERRUPTED" for item in self.state()["errors"]))
        started = self.action("start_cutout", "tsk-1001")
        self.action("start_cutout", "tsk-1001", expected=409)
        self.action("reset_demo", expected=409)
        self.action("pause_task", "tsk-1001")
        time.sleep(0.6)
        run = next(item for item in self.state()["runs"] if item["id"] == started["run_id"])
        self.assertEqual(run["status"], "failed")
        self.assertEqual(self.task("tsk-1001")["status"], "waiting")
        self.action("reset_demo")
        self.assertEqual(len(self.state()["tasks"]), 6)
        self.assertFalse(any(item["status"] in ("queued", "running") for item in self.state()["runs"]))

    def test_prompt_order_and_immutable_background_prompt_link(self):
        repository = self.app.state.repository
        with Session(repository.engine) as session:
            original = session.scalar(select(BackgroundAsset).where(BackgroundAsset.task_id == "tsk-1003"))
            original_id, original_prompt = original.id, original.prompt_id
        first = self.action("generate_prompt", "tsk-1003", {"scene_type": "产品摄影工作室", "color": "白"})
        second = self.action("generate_prompt", "tsk-1003", {"scene_type": "自然花园", "color": "绿"})
        task_prompts = [item for item in self.state()["prompts"] if item["task_id"] == "tsk-1003"]
        self.assertEqual(task_prompts[-1]["id"], second["prompt_id"])
        started = self.action("generate_background", "tsk-1003", {"prompt_id": first["prompt_id"], "provider": "other"})
        self.wait(started["run_id"])
        self.action("generate_prompt", "tsk-1003", {"scene_type": "城市阳台", "color": "蓝"})
        metadata = self.client.get("/api/tasks/tsk-1003/metadata").json()
        self.assertIn("产品摄影工作室", metadata["background"]["prompt"])
        self.assertEqual(metadata["background"]["provider"], "other")
        with Session(repository.engine) as session:
            self.assertEqual(session.get(BackgroundAsset, original_id).prompt_id, original_prompt)
            self.assertEqual(len(list(session.scalars(select(Prompt).where(Prompt.task_id == "tsk-1003")))), 4)

    def test_accounts_publishing_gate_and_unknown_secrets(self):
        self.action("publish_now", "pub-001", expected=409)
        self.assertTrue(any(item["code"] == "PUBLISH_GATE_BLOCKED" for item in self.state()["errors"]))
        self.action("authorize_account", "acc-002")
        self.action("publish_now", "pub-001", expected=409)
        self.action("review_approve", "rev-001")
        scheduled = self.action("schedule_publish", "pub-001", {"schedule": "2099-01-01T10:30:00Z"})
        self.assertIn("Mock", scheduled["message"])
        result = self.action("publish_now", "pub-001")
        self.wait(result["run_id"])
        for payload in ({"token": "not-real"}, {"cookie": "not-real"}, {"password": "not-real"}):
            self.action("authorize_account", "acc-002", payload, expected=422)
        self.assertNotIn("not-real", json.dumps(self.state()))

    def test_imports_product_and_auto_review(self):
        self.action("import_collector", payload={"source": "tencent", "url": "https://docs.qq.com/sheet/demo"})
        self.action("import_collector", payload={"source": "excel", "filename": "demo.xlsx"})
        self.action("import_collector", payload={"source": "batch", "lines": "产品一\n产品二"})
        settings = self.state()["settings"]
        self.action("save_settings", payload={**settings, "review_required": False})
        result = self.action("upload_background", "tsk-1003", {"filename": "bg.jpg"})
        self.wait(result["run_id"])
        self.action("upload_background", "tsk-1003", {"filename": "../../bg.jpg"}, expected=422)
        result = self.action("compose", "tsk-1003")
        self.wait(result["run_id"])
        reviews = [item for item in self.state()["reviews"] if item["task_id"] == "tsk-1003"]
        self.assertEqual(reviews[-1]["status"], "approved")
        self.assertEqual(self.task("tsk-1003")["current_stage"], "publish")

    def test_websocket_mock_progress(self):
        with self.client.websocket_connect("/ws/tasks/tsk-1001") as socket:
            self.assertEqual(socket.receive_json()["type"], "snapshot")
            started = self.action("start_cutout", "tsk-1001")
            events = [socket.receive_json() for _ in range(5)]
            self.assertEqual([item["status"] for item in events], ["queued", "running", "running", "running", "success"])
            self.assertEqual(events[-1]["run_id"], started["run_id"])

    def test_remaining_page_actions_and_publishing_draft_save(self):
        created = self.action("create_task", payload={"country": "巴西", "keyword": "手动新产品场景", "product_id": "prd-001"})
        task_id = created["entity_id"]
        self.wait(self.action("retry_task", task_id)["run_id"])
        for action in ("retry_interval", "regenerate_mask", "manual_fix"):
            self.wait(self.action(action, task_id, {"start": 1, "end": 3})["run_id"])
        self.action("accept_cutout", task_id)
        prompt = self.action("generate_prompt", task_id)
        self.wait(self.action("generate_background", task_id, {"prompt_id": prompt["prompt_id"], "provider": "local_comfyui"})["run_id"])
        self.wait(self.action("update_composition", task_id, {"exposure": -0.2})["run_id"])
        review = next(item for item in self.state()["reviews"] if item["task_id"] == task_id)
        self.action("review_manual", review["id"], {"note": "Mock 人工修复说明"})
        self.action("review_reject", review["id"], {"note": "示例拒绝"})
        self.wait(self.action("review_retry", review["id"], {"note": "示例复核重试"})["run_id"])
        self.action("review_approve", review["id"])
        rows = self.state()["publishing"]
        own = next(item for item in rows if item["task_id"] == task_id)
        own["caption"] = "手动保存的Mock文案"
        self.action("save_publishing", payload={"rows": rows})
        self.action("update_account_status", "acc-001", {"status": "expired"})
        self.action("publish_now", own["id"], expected=409)
        self.action("authorize_account", "acc-001")
        product = next(item for item in self.state()["products"] if item["id"] == "prd-001")
        product["description"] = "本地产品资料编辑测试"
        self.action("save_product", payload=product)
        self.assertEqual(next(item for item in self.state()["products"] if item["id"] == "prd-001")["description"], product["description"])

    def test_new_collector_metrics_cannot_be_supplied(self):
        rows = self.state()["collector"]
        new = {**rows[0], "id": "col-new", "keyword": "新关键词", "views": 999999999, "likes": 99999999, "status": "success"}
        self.action("save_collector", payload={"rows": [*rows, new]})
        stored = next(item for item in self.state()["collector"] if item["id"] == "col-new")
        self.assertEqual((stored["views"], stored["likes"], stored["status"]), (0, 0, "draft"))

    def test_compose_uses_saved_defaults_and_explicit_overrides(self):
        settings = self.state()["settings"]
        self.action("save_settings", payload={**settings, "output_width": 900, "output_height": 1600, "output_fps": 48})
        self.wait(self.action("compose", "tsk-1003")["run_id"])
        metadata = self.client.get("/api/tasks/tsk-1003/metadata").json()
        self.assertEqual((metadata["composition"]["width"], metadata["composition"]["height"], metadata["composition"]["fps"]), (900, 1600, 48))
        with Session(self.app.state.repository.engine) as session:
            composition = session.scalar(select(CompositionAsset).where(CompositionAsset.task_id == "tsk-1003").order_by(CompositionAsset.created_at.desc()))
            self.assertEqual((composition.parameters["width"], composition.parameters["height"], composition.parameters["fps"]), (900, 1600, 48))
        self.wait(self.action("compose", "tsk-1003", {"width": 1, "height": 8000, "fps": 240})["run_id"])
        metadata = self.client.get("/api/tasks/tsk-1003/metadata").json()
        self.assertEqual((metadata["composition"]["width"], metadata["composition"]["height"], metadata["composition"]["fps"]), (1, 8000, 240))


if __name__ == "__main__":
    unittest.main()
