import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateTable

from app.config import Settings
from app.main import create_app
from app.models import Base, PublishingAccount, SourceVideo, Task
from app.repository import StateRepository
from app.schemas import VideoMetadata
from app.storage import FOLDERS, StoragePaths


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.config = Settings(app_mode="mock", database_url="sqlite:///" + str(self.root / "test.db"), storage_root=self.root / "storage")
        self.app = create_app(self.config)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        self.app.state.repository.engine.dispose()
        self.directory.cleanup()

    def test_all_requested_read_routes(self):
        routes = ["health", "state", "projects", "tasks", "tasks/tsk-1002", "tasks/tsk-1002/stages",
                  "assets", "assets/ast-1002-2", "collector", "cutout", "background", "compose",
                  "review", "accounts", "accounts/acc-002", "products", "publish", "prompts", "runs", "errors", "audits"]
        for route in routes:
            with self.subTest(route=route):
                response = self.client.get("/api/" + route)
                self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get("/api/tasks/not-found").status_code, 404)
        self.assertEqual(self.client.get("/api/assets/not-found").status_code, 404)
        self.assertEqual(self.client.get("/api/accounts/not-found").status_code, 404)

    def test_configured_frontend_private_network_preflight_is_allowed(self):
        headers = {"Origin": self.config.frontend_origin,
            "Access-Control-Request-Method": "DELETE", "Access-Control-Request-Headers": "content-type",
            "Access-Control-Request-Private-Network": "true"}
        response = self.client.options("/api/assets/unit-only", headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["access-control-allow-private-network"], "true")
        self.assertEqual(response.headers["access-control-allow-origin"], self.config.frontend_origin)
        headers["Origin"] = "https://untrusted.example"
        rejected = self.client.options("/api/assets/unit-only", headers=headers)
        self.assertEqual(rejected.status_code, 400)
        self.assertNotIn("access-control-allow-origin", rejected.headers)

    def test_metadata_and_workflow_trace_current_task(self):
        response = self.client.get("/api/tasks/tsk-1002/metadata")
        self.assertEqual(response.status_code, 200)
        metadata = VideoMetadata.model_validate(response.json())
        self.assertEqual(metadata.country, "墨西哥")
        self.assertEqual(metadata.product_id, "prd-002")
        self.assertEqual(metadata.source_video.original_filename, "source_tsk-1002.mp4")
        self.assertEqual(metadata.source_video.fps, 30)
        self.assertEqual(metadata.source_video.duration, 33)
        self.assertEqual(metadata.background.provider, "chatgpt_rpa")
        self.assertEqual(metadata.publishing.account_id, "acc-002")
        self.assertEqual(metadata.quality.mask_flicker_score, 0.2)
        workflow = self.client.get("/api/tasks/tsk-1002/workflow").json()
        self.assertEqual(workflow["task_id"], metadata.task_id)
        self.assertEqual(workflow["workflow"]["app_mode"], "mock")
        self.assertFalse(self.config.storage_root.exists())

    def test_logical_storage_path_containment(self):
        storage = StoragePaths(self.config.storage_root)
        paths = self.client.get("/api/tasks/tsk-1002/storage").json()["paths"]
        self.assertTrue(all(folder in paths for folder in FOLDERS))
        for project, task in [("../escape", "tsk"), ("project", "../../tmp"), ("project", "a/b"), ("project", "a\\b")]:
            with self.assertRaises(ValueError):
                storage.task_paths(project, task)
        with self.assertRaises(ValueError):
            storage.asset_path("project", "task", "source", "../secret.mp4")
        self.assertFalse(self.config.storage_root.exists())

    def test_postgresql_ddl_compiles_for_all_entities(self):
        required = {"users", "projects", "tasks", "task_stages", "countries", "keywords", "products", "source_videos",
                    "video_assets", "cutout_assets", "background_assets", "composition_assets", "reviews", "publishing_accounts",
                    "publishing_tasks", "prompts", "workflow_snapshots", "processing_runs", "error_logs", "audit_logs"}
        self.assertTrue(required.issubset(Base.metadata.tables))
        for table in Base.metadata.tables.values():
            ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
            self.assertIn("CREATE TABLE", ddl)
        account_columns = set(PublishingAccount.__table__.columns.keys())
        self.assertIn("secret_ref", account_columns)
        self.assertTrue(account_columns.isdisjoint({"password", "cookie", "token", "access_token"}))

    def test_sqlite_entity_foreign_keys_and_restart_persistence(self):
        repository = self.app.state.repository
        repository.mutate(lambda state: state["settings"].update({"output_fps": 24}))
        fresh = StateRepository(self.config.database_url, Path(__file__).parents[1] / "app/seed.json")
        self.assertEqual(fresh.state()["settings"]["output_fps"], 24)
        with Session(fresh.engine) as session:
            task = session.get(Task, "tsk-1002")
            source = session.get(SourceVideo, task.source_video_id)
            self.assertEqual(source.task_id, task.id)
            self.assertEqual(session.scalar(select(Task.country).where(Task.id == "tsk-1005")), "美国")
        fresh.engine.dispose()

    def test_seed_market_associations(self):
        state = self.client.get("/api/state").json()
        products = {item["id"]: item for item in state["products"]}
        tasks = {item["id"]: item for item in state["tasks"]}
        for task in tasks.values():
            self.assertEqual(task["country"], products[task["product_id"]]["country"])
        history = next(item for item in state["publishing"] if item["id"] == "pub-002")
        self.assertEqual(history["account_id"], "acc-004")
        self.assertEqual(history["product_id"], "prd-004")
        exception = next(item for item in state["assets"] if item["id"] == "ast-exception")
        self.assertEqual(exception["country"], tasks[exception["task_id"]]["country"])

    def test_mock_mode_and_input_contract(self):
        with self.assertRaises(ValidationError):
            Settings(app_mode="production")
        with self.assertRaises(ValidationError):
            Settings(database_url="postgresql://localhost/factory")
        response = self.client.post("/api/actions", json={"action": "authorize_account", "password": "not-a-secret"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.client.post("/api/actions", json={"action": "start_cutout", "payload": []}).status_code, 422)
        self.assertEqual(self.client.post("/api/actions", json={"action": "start_cutout", "payload": {}}).status_code, 422)

    def test_websocket_snapshot_task_routing(self):
        with self.client.websocket_connect("/ws/production") as socket:
            event = socket.receive_json()
            self.assertEqual(event["type"], "snapshot")
            self.assertIsNone(event["task_id"])
        with self.client.websocket_connect("/ws/tasks/tsk-1002") as socket:
            event = socket.receive_json()
            self.assertEqual(event["type"], "snapshot")
            self.assertEqual(event["task_id"], "tsk-1002")


if __name__ == "__main__":
    unittest.main()
