"""Database contracts on isolated SQLite files and compiled PostgreSQL DDL only."""

from io import StringIO
from pathlib import Path
import tempfile
import unittest

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateTable

from app import models as m


class PublishingModelsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.engine = create_engine("sqlite:///" + str(self.root / "isolated.db"))
        self.addCleanup(self.engine.dispose)
        self.config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        self.config.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "migrations"))
        self.config.set_main_option("sqlalchemy.url", str(self.engine.url))

    def test_existing_phase6_catalog_fields_are_preserved_by_explicit_migration(self):
        command.upgrade(self.config, "0006_production_supervision")
        with self.engine.begin() as connection:
            connection.execute(text("INSERT INTO countries (id,name) VALUES ('unit-country','US')"))
            connection.execute(text("INSERT INTO products (id,country_id,name,country,image,url,binding_reference,description,status,keywords) "
                "VALUES ('unit-product','unit-country','Unit product','US','','','unit-binding','retained description','active','[]')"))
            connection.execute(text("INSERT INTO publishing_accounts (id,country_id,name,country,status,authorization,queue,errors) "
                "VALUES ('unit-account','unit-country','Unit account','US','NOT_CONNECTED','retained authorization',0,'[]')"))
        command.upgrade(self.config, "head")
        with Session(self.engine) as session:
            product, account = session.get(m.Product, "unit-product"), session.get(m.PublishingAccount, "unit-account")
            self.assertEqual(product.binding_reference, "unit-binding")
            self.assertEqual(product.description, "retained description")
            self.assertEqual(product.descriptions, [])
            self.assertEqual(account.authorization, "retained authorization")
            self.assertTrue(account.enabled)
            self.assertEqual(account.identity_verification, {})
            self.assertEqual(account.cooldown_seconds, 120)
        self.assertIn("publishing_batches", inspect(self.engine).get_table_names())
        foreign = inspect(self.engine).get_foreign_keys("publishing_tasks")
        self.assertTrue(any(row["referred_table"] == "publishing_batches" for row in foreign))
        self.assertFalse(next(row for row in inspect(self.engine).get_columns("publishing_tasks") if row["name"] == "task_id")["nullable"])

    def test_fresh_metadata_can_be_adopted_without_recreating_publishing_tables(self):
        m.Base.metadata.create_all(self.engine)
        command.stamp(self.config, "0006_production_supervision")
        command.upgrade(self.config, "head")
        with Session(self.engine) as session:
            session.add(m.PublishingBatch(id="unit-batch", status="PAUSED"))
            session.commit()
        command.upgrade(self.config, "head")
        with Session(self.engine) as session:
            self.assertEqual(session.scalar(select(m.PublishingBatch.status)), "PAUSED")

    def test_new_tables_compile_to_postgresql_and_serial_constraint_is_real(self):
        names = ("publishing_batches", "publish_runs", "publishing_step_executions", "publishing_resource_locks", "publishing_description_usage")
        for name in names:
            ddl = str(CreateTable(m.Base.metadata.tables[name]).compile(dialect=postgresql.dialect()))
            self.assertIn("CREATE TABLE " + name, ddl)
            self.assertIn("PRIMARY KEY", ddl)
        m.Base.metadata.create_all(self.engine)
        with self.engine.begin() as connection:
            connection.execute(text("INSERT INTO publishing_batches (id,status,concurrency,settings_snapshot,created_at,updated_at) "
                "VALUES ('unit-one','PAUSED',1,'{}',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        with self.assertRaises(IntegrityError):
            with self.engine.begin() as connection:
                connection.execute(text("INSERT INTO publishing_batches (id,status,concurrency,settings_snapshot,created_at,updated_at) "
                    "VALUES ('unit-two','QUEUED',2,'{}',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))

    def test_all_forward_migrations_emit_postgresql_ddl_without_database_connection(self):
        output = StringIO()
        config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"), output_buffer=output)
        config.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "migrations"))
        config.set_main_option("sqlalchemy.url", "postgresql://unit:unused@127.0.0.1/never_connected")
        command.upgrade(config, "head", sql=True)
        ddl = output.getvalue()
        self.assertIn("CREATE TABLE publishing_batches", ddl)
        self.assertIn("fk_publishing_task_batch", ddl)
        self.assertIn("publishing_description_usage", ddl)
        self.assertIn("0007_real_publishing", ddl)


if __name__ == "__main__":
    unittest.main()
