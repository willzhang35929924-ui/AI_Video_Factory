import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from pydantic import ValidationError
from sqlalchemy import MetaData, create_engine, inspect, select
from sqlalchemy import Boolean
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateTable

from app.config import Settings
from app.models import Base, SourceVideo
from app.repository import StateRepository, context_id
from app.schemas import AssetView, ProductionEvent, RunView

BACKEND = Path(__file__).resolve().parents[1]
SEED = BACKEND / "app/seed.json"


class CollectorPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.url = "sqlite:///" + str(Path(self.directory.name) / "test.db")
        self.repositories = []

    def tearDown(self):
        for repository in self.repositories:
            repository.engine.dispose()
        self.directory.cleanup()

    def migration_config(self, url=None, output=None):
        config = Config(str(BACKEND / "alembic.ini"), output_buffer=output)
        config.set_main_option("sqlalchemy.url", url or self.url)
        return config

    def repository(self):
        repository = StateRepository(self.url, SEED)
        self.repositories.append(repository)
        return repository

    def test_empty_database_migration_and_postgresql_sql(self):
        command.upgrade(self.migration_config(), "head")
        repository = self.repository()
        self.assertTrue(repository.state()["tasks"])
        self.assertIn("legacy_bindings", inspect(repository.engine).get_table_names())
        for table in Base.metadata.tables.values():
            self.assertIn("CREATE TABLE", str(CreateTable(table).compile(dialect=postgresql.dialect())))
        output = io.StringIO()
        command.upgrade(self.migration_config("postgresql://unused/factory", output), "head", sql=True)
        sql = output.getvalue()
        self.assertIn("CREATE TABLE legacy_bindings", sql)
        self.assertIn("ALTER TABLE processing_runs ADD COLUMN started_at", sql)
        self.assertIn("fk_task_source", sql)
        self.assertIn("BIGINT", sql)
        self.assertIn("ALTER TABLE workflow_snapshots ADD COLUMN execution JSON DEFAULT '{}' NOT NULL", sql)

    def test_existing_phase_one_migrates_without_data_loss(self):
        command.upgrade(self.migration_config(), "0001_phase1")
        seed = json.loads(SEED.read_text(encoding="utf-8"))
        task = seed["tasks"][1].copy()
        task["source_video_id"] = None
        product = next(item for item in seed["products"] if item["id"] == task["product_id"])
        country_key = context_id("country", task["country"])
        keyword_key = context_id("keyword", task["country"] + "|" + task["keyword"] + "|" + task["product_id"])
        engine = create_engine(self.url)
        metadata = MetaData()
        metadata.reflect(engine)
        with engine.begin() as connection:
            connection.execute(metadata.tables["users"].insert(), {"id": "user-local", "name": "original-user", "created_at": datetime.now(timezone.utc)})
            connection.execute(metadata.tables["projects"].insert(), {"id": task["project_id"], "user_id": "user-local", "name": "original-project"})
            connection.execute(metadata.tables["countries"].insert(), {"id": country_key, "name": task["country"]})
            connection.execute(metadata.tables["products"].insert(), {**product, "country_id": country_key})
            connection.execute(metadata.tables["keywords"].insert(), {"id": keyword_key, "country_id": country_key, "product_id": product["id"], "text": task["keyword"]})
            for key in ("created_at", "updated_at"):
                task[key] = datetime.fromisoformat(task[key].replace("Z", "+00:00"))
            connection.execute(metadata.tables["tasks"].insert(), {**task, "country_id": country_key, "keyword_id": keyword_key})
            connection.execute(metadata.tables["factory_settings"].insert(), {"id": "settings", **seed["settings"]})
            connection.execute(metadata.tables["processing_runs"].insert(), {"id": "old-run", "task_id": task["id"], "stage": "collector", "status": "success", "progress": 100, "logs": ["original log"], "created_at": datetime.now(timezone.utc)})
            connection.execute(metadata.tables["error_logs"].insert(), {"id": "old-error", "task_id": task["id"], "stage": "collector", "code": "ORIGINAL_FAILURE", "message": "original error", "created_at": datetime.now(timezone.utc)})
            connection.execute(metadata.tables["workflow_snapshots"].insert(), {"id": "original-workflow", "task_id": task["id"], "version": "prior-workflow",
                "workflow": {"original": True}, "created_at": datetime.now(timezone.utc)})
        engine.dispose()
        with self.assertRaisesRegex(RuntimeError, "需显式迁移"):
            self.repository()
        command.upgrade(self.migration_config(), "head")
        migrated = self.repository()
        state = migrated.state()
        self.assertEqual(migrated.workflow(task["id"])["execution"], {})
        self.assertEqual(migrated.workflow(task["id"])["workflow"], {"original": True})
        self.assertEqual(len(state["tasks"]), 1)
        self.assertEqual(state["tasks"][0]["keyword"], task["keyword"])
        self.assertEqual(state["tasks"][0]["execution_mode"], "mock")
        self.assertEqual(state["runs"][0]["logs"], ["original log"])
        self.assertEqual(state["runs"][0]["run_id"], "old-run")
        self.assertEqual(state["runs"][0]["provider"], "mock")
        self.assertFalse(state["errors"][0]["retryable"])
        command.upgrade(self.migration_config(), "head")
        self.assertEqual(self.repository().state()["tasks"], state["tasks"])

    def test_real_context_run_collector_and_binding_survive_restart(self):
        repository = self.repository()
        task_id = repository.state()["tasks"][0]["id"]
        now = datetime.now(timezone.utc).isoformat()
        def change(state):
            task = next(item for item in state["tasks"] if item["id"] == task_id)
            task.update(execution_mode="integration", status="COLLECTED", context={"country": task["country"], "keyword": task["keyword"], "production_rules": {"min_views": 100}}, collector_result={"provider": "LegacyCollectorAdapter", "videos_found": 1})
            state["runs"].append({"id": "real-run", "task_id": task_id, "stage": "collector", "status": "success", "progress": 100, "logs": ["real log"], "created_at": now, "provider": "LegacyCollectorAdapter", "command": ["python", "collector_bridge.py"], "cwd": "/legacy/root", "pid": 1234, "started_at": now, "finished_at": now, "duration_ms": 200, "exit_code": 0, "stdout_log_path": "/factory/stdout.log", "stderr_log_path": "/factory/stderr.log", "error_code": None, "error_message": None, "retry_count": 1, "idempotency_key": "real-idempotency"})
            state["collector"][0].update(input_source="tiktok_url", video_url="https://www.tiktok.com/@demo/video/123", raw_keyword="original keyword", product_images=["/legacy/product.png"], context_id="ctx-real", source_document="document-id", source_row=2, last_task_id=task_id, last_run_id="real-run", result={"videos_found": 1}, views=1000, likes=27)
            state["legacy_bindings"].append({"id": "binding-real", "task_id": task_id, "project_id": task["project_id"], "product_id": task["product_id"], "context_id": "ctx-real", "source_document": "document-id", "source_row": 2, "video_id": "123", "download_task_id": "vidbee-real", "source_path": "/legacy/video.mp4", "sha256": "a" * 64, "created_at": now})
            state["errors"].append({"id": "error-real", "task_id": task_id, "stage": "collector", "code": "LOGIN_REQUIRED", "message": "Manual login required", "created_at": now, "provider": "LegacyCollectorAdapter", "details": {"page": "tiktok"}, "retryable": False})
        repository.mutate(change)
        state = self.repository().state()
        task = next(item for item in state["tasks"] if item["id"] == task_id)
        self.assertEqual(task["collector_result"]["videos_found"], 1)
        self.assertEqual(task["context"]["production_rules"]["min_views"], 100)
        run = next(item for item in state["runs"] if item["id"] == "real-run")
        self.assertEqual(run["run_id"], run["id"])
        self.assertEqual(run["pid"], 1234)
        self.assertEqual(run["duration_ms"], 200)
        self.assertEqual(run["command"], ["python", "collector_bridge.py"])
        self.assertEqual(state["legacy_bindings"][0]["download_task_id"], "vidbee-real")
        self.assertEqual(state["legacy_bindings"][0]["legacy_download_task_id"], "vidbee-real")
        self.assertEqual(state["legacy_bindings"][0]["legacy_context_id"], "ctx-real")
        self.assertEqual(state["legacy_bindings"][0]["legacy_project"], "TikTok_Auto")
        failure = next(item for item in state["errors"] if item["id"] == "error-real")
        self.assertEqual(failure["error_code"], "LOGIN_REQUIRED")
        self.assertEqual(failure["provider"], "LegacyCollectorAdapter")
        self.assertEqual(failure["details"], {"page": "tiktok"})
        self.assertFalse(failure["retryable"])
        row = next(item for item in state["collector"] if item["last_run_id"] == "real-run")
        self.assertEqual(row["input_source"], "tiktok_url")
        self.assertEqual(row["like_view_ratio_percent"], 2.7)

    def test_source_url_probe_and_registry_are_preserved_by_unrelated_mutation(self):
        repository = self.repository()
        state = repository.state()
        task = next(item for item in state["tasks"] if item["source_video_id"])
        source_asset = next(item for item in state["assets"] if item["task_id"] == task["id"] and item["type"] == "source")
        with Session(repository.engine) as session:
            source = session.get(SourceVideo, task["source_video_id"])
            source.source_url = "https://www.tiktok.com/@demo/video/987"
            source.download_task_id = "vidbee-987"
            source.r_frame_rate = "30000/1001"
            source.avg_frame_rate = "30000/1001"
            source.probe_metadata = {"original_fps": "30000/1001"}
            session.commit()
        def change(current):
            asset = next(item for item in current["assets"] if item["id"] == source_asset["id"])
            asset.update(external_path="/legacy/video.mp4", sha256="b" * 64, size=4_000_000_000, mime_type="video/mp4", source_system="TikTok_Auto", registry_status="registered", legacy_reference={"context_id": "ctx-987"})
        repository.mutate(change)
        repository.mutate(lambda current: current["settings"].update(output_fps=25))
        fresh = self.repository()
        metadata = fresh.metadata(task["id"])["source_video"]
        self.assertEqual(metadata["source_url"], "https://www.tiktok.com/@demo/video/987")
        self.assertEqual(metadata["external_path"], "/legacy/video.mp4")
        self.assertEqual(metadata["size"], 4_000_000_000)
        self.assertEqual(metadata["download_task_id"], "vidbee-987")
        self.assertEqual(metadata["probe_metadata"], {"original_fps": "30000/1001"})
        self.assertEqual(metadata["r_frame_rate"], "30000/1001")

    def test_candidate_unknown_dimensions_and_real_events(self):
        repository = self.repository()
        state = repository.state()
        asset = state["assets"][0].copy()
        asset.update(id="candidate", type="source_candidate", asset_type="SOURCE_CANDIDATE", width=0, height=0, fps=0, duration=0, registry_status="unverified")
        AssetView.model_validate(asset)
        repository.mutate(lambda current: current["assets"].append(asset))
        self.assertEqual(next(item for item in self.repository().state()["assets"] if item["id"] == "candidate")["width"], 0)
        with self.assertRaises(ValidationError):
            AssetView.model_validate({**asset, "type": "source"})
        event = ProductionEvent(type="COLLECTOR_COMPLETED", stage="download", data={"videos_found": 1})
        self.assertEqual(event.data["videos_found"], 1)
        run = state["runs"][0].copy()
        with self.assertRaises(ValidationError):
            RunView.model_validate({**run, "run_id": "wrong"})

    def test_integration_defaults_are_safe_and_mock_is_explicit(self):
        config = Settings(_env_file=None)
        self.assertEqual(config.app_mode, "integration")
        self.assertTrue(config.integration_dry_run)
        self.assertEqual(config.legacy_tiktok_auto_python, config.legacy_tiktok_auto_root / ".venv/bin/python")
        self.assertEqual(Settings(app_mode="mock", _env_file=None).app_mode, "mock")

    def test_early_revision_false_default_is_repaired_without_real_flag_changes(self):
        command.upgrade(self.migration_config(), "head")
        repository = self.repository()
        command.stamp(self.migration_config(), "0002_collector_real")
        task_id = repository.state()["tasks"][0]["id"]
        engine = create_engine(self.url)
        with engine.begin() as connection:
            operations = Operations(MigrationContext.configure(connection))
            with operations.batch_alter_table("error_logs", recreate="always") as batch:
                batch.alter_column("retryable", existing_type=Boolean(), server_default="false", existing_nullable=False)
            connection.exec_driver_sql("INSERT INTO error_logs (id,task_id,stage,code,message,created_at) VALUES (?,?,?,?,?,?)", ("early-mock", task_id, "collector", "EARLY", "old failure", datetime.now(timezone.utc).isoformat()))
            connection.exec_driver_sql("INSERT INTO error_logs (id,task_id,stage,code,message,created_at,provider,retryable) VALUES (?,?,?,?,?,?,?,?)", ("real-retry", task_id, "collector", "NETWORK", "retryable failure", datetime.now(timezone.utc).isoformat(), "LegacyCollectorAdapter", 1))
            connection.exec_driver_sql("INSERT INTO error_logs (id,task_id,stage,code,message,created_at,provider,retryable) VALUES (?,?,?,?,?,?,?,?)", ("real-stop", task_id, "collector", "LOGIN_REQUIRED", "manual login", datetime.now(timezone.utc).isoformat(), "LegacyCollectorAdapter", 0))
        self.assertEqual(inspect(engine).get_columns("error_logs")[-1]["default"], "'false'")
        command.upgrade(self.migration_config(), "0003_error_retryable")
        failures = {item["id"]: item for item in self.repository().state()["errors"]}
        self.assertFalse(failures["early-mock"]["retryable"])
        self.assertTrue(failures["real-retry"]["retryable"])
        self.assertFalse(failures["real-stop"]["retryable"])
        default = next(column["default"] for column in inspect(engine).get_columns("error_logs") if column["name"] == "retryable")
        self.assertEqual(default, "0")
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
