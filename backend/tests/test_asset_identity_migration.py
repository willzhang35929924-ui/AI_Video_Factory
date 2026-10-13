import tempfile
import unittest
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, create_engine, inspect, select, text
from sqlalchemy.orm import Session

from app import models as m


class AssetIdentityMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="factory-identity-unit-")
        self.addCleanup(self.temporary.cleanup)
        self.engine = create_engine("sqlite:///" + str(Path(self.temporary.name) / "unit.db"))
        self.addCleanup(self.engine.dispose)
        backend = Path(__file__).parents[1]
        self.config = Config(str(backend / "alembic.ini"))
        self.config.set_main_option("script_location", str(backend / "migrations"))
        self.config.set_main_option("sqlalchemy.url", str(self.engine.url))

    def test_migration_resumes_empty_identity_table_preserving_review_then_allows_registry_delete(self):
        command.upgrade(self.config, "0009_description_generation")
        metadata = MetaData()
        stamp = datetime.now(timezone.utc)
        rows = {
            "users": {"id": "unit-user", "name": "Unit", "created_at": stamp},
            "projects": {"id": "unit-project", "user_id": "unit-user", "name": "Unit"},
            "countries": {"id": "unit-country", "name": "US"},
            "products": {"id": "unit-product", "country_id": "unit-country", "name": "Unit", "country": "US",
                "image": "", "url": "", "binding_reference": "", "description": "", "status": "active", "keywords": []},
            "keywords": {"id": "unit-keyword", "country_id": "unit-country", "product_id": "unit-product", "text": "Unit"},
            "tasks": {"id": "unit-task", "project_id": "unit-project", "country_id": "unit-country", "keyword_id": "unit-keyword",
                "country": "US", "keyword": "Unit", "product_id": "unit-product", "current_stage": "review", "status": "REJECTED",
                "progress": 100, "thumbnail": "", "created_at": stamp, "updated_at": stamp, "execution_mode": "integration", "context": {}, "collector_result": {}},
            "video_assets": {"id": "unit-person", "task_id": "unit-task", "type": "person", "name": "Unit.mkv", "thumbnail": "",
                "duration": 1, "width": 10, "height": 10, "fps": 1, "codec": "ffv1", "country": "US", "keyword": "Unit",
                "status": "rejected", "storage_path": "/unit-only/RGB.mkv", "created_at": stamp, "registry_status": "registered",
                "legacy_reference": {"version": 13}, "probe_metadata": {}},
            "reviews": {"id": "unit-review", "task_id": "unit-task", "asset_id": "unit-person", "status": "rejected",
                "issues": ["BODY_MISSING"], "note": "Preserve real historical decision"},
        }
        with self.engine.begin() as connection:
            for name, row in rows.items():
                connection.execute(Table(name, metadata, autoload_with=self.engine).insert(), row)
        m.AssetIdentity.__table__.create(self.engine)
        command.upgrade(self.config, "head")
        command.upgrade(self.config, "head")
        with Session(self.engine) as session:
            self.assertEqual(session.get(m.AssetIdentity, "unit-person").task_id, "unit-task")
            self.assertEqual(session.get(m.VideoAsset, "unit-person").legacy_reference, {"version": 13})
            identity = session.get(m.AssetIdentity, "unit-person")
            identity.deleted_at, identity.deletion_snapshot = stamp, {"unit": True}
            session.delete(session.get(m.VideoAsset, "unit-person"))
            session.commit()
            self.assertEqual(session.get(m.Review, "unit-review").asset_id, "unit-person")
            self.assertEqual(session.get(m.Review, "unit-review").status, "rejected")
            self.assertEqual(session.scalar(select(m.Task.status)), "REJECTED")
            self.assertEqual(session.execute(text("PRAGMA foreign_key_check")).all(), [])
        for table, columns in {"reviews": ["asset_id"], "composition_assets": ["asset_id", "person_asset_id"], "publishing_tasks": ["video_id"]}.items():
            keys = inspect(self.engine).get_foreign_keys(table)
            for column in columns:
                self.assertTrue(any(key["constrained_columns"] == [column] and key["referred_table"] == "asset_identities" for key in keys))

    def test_fresh_model_schema_can_adopt_migration_without_changing_foreign_keys(self):
        m.Base.metadata.create_all(self.engine)
        command.stamp(self.config, "0009_description_generation")
        command.upgrade(self.config, "head")
        self.assertTrue(inspect(self.engine).has_table("asset_identities"))
        self.assertTrue(any(key["referred_table"] == "asset_identities" for key in inspect(self.engine).get_foreign_keys("video_assets")))

    def test_postgresql_offline_migration_uses_identity_foreign_keys(self):
        output = StringIO()
        self.config.output_buffer = output
        self.config.set_main_option("sqlalchemy.url", "postgresql://unit:unused@127.0.0.1/never_connect")
        command.upgrade(self.config, "head", sql=True)
        ddl = output.getvalue()
        self.assertIn("CREATE TABLE asset_identities", ddl)
        self.assertIn("REFERENCES asset_identities (id)", ddl)
        self.assertIn("fk_video_assets_identity", ddl)


if __name__ == "__main__":
    unittest.main()
