import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("init_factory", SOURCE / "scripts/init_factory.py")
initializer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(initializer)


class CleanInitializationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="factory-clean-init-")
        self.root = Path(self.temporary.name).resolve()
        (self.root / "backend/app").mkdir(parents=True)
        for name in ("__init__.py", "models.py", "schemas.py", "repository.py", "config.py", "runtime_paths.py", "seed.json"):
            shutil.copy2(SOURCE / "backend/app" / name, self.root / "backend/app" / name)
        shutil.copy2(SOURCE / "backend/alembic.ini", self.root / "backend/alembic.ini")
        shutil.copytree(SOURCE / "backend/migrations", self.root / "backend/migrations", ignore=shutil.ignore_patterns("__pycache__"))
        (self.root / "backend/.venv").symlink_to(Path(sys.prefix), target_is_directory=True)

    def tearDown(self):
        self.temporary.cleanup()

    def create(self, **options):
        return initializer.initialize(self.root, confirm_create=True, **options)

    def test_new_database_uses_migrations_without_business_demo_records(self):
        result = self.create()
        self.assertEqual(result["status"], "PASS_LOCAL")
        self.assertEqual(result["migration"], "0011_collection_identity")
        self.assertEqual(result["counts"]["projects"], 1)
        self.assertTrue((self.root / "storage").is_dir())
        for key in ("tasks", "assets", "reviews", "runs", "accounts", "products", "prompts", "batches", "publishing", "collection_identities"):
            self.assertEqual(result["counts"][key], 0, key)
        self.assertEqual(result["sha256"], hashlib.sha256((self.root / "backend/factory.db").read_bytes()).hexdigest())

    def test_existing_database_refused_without_change(self):
        self.create()
        database = self.root / "backend/factory.db"
        before = database.read_bytes()
        with self.assertRaisesRegex(ValueError, "DATABASE_ALREADY_EXISTS"):
            self.create()
        self.assertEqual(database.read_bytes(), before)

    def test_explicit_confirmation_required(self):
        with self.assertRaisesRegex(ValueError, "EXPLICIT_CREATE_CONFIRMATION_REQUIRED"):
            initializer.initialize(self.root)
        self.assertFalse((self.root / "backend/factory.db").exists())

    def test_outside_paths_and_traversal_refused(self):
        for value in (str(self.root.parent / "outside.db"), "../outside.db"):
            with self.assertRaises(ValueError):
                self.create(database_path=value)
        with self.assertRaisesRegex(ValueError, "STORAGE_CANNOT_BE_SOURCE_DIRECTORY"):
            self.create(storage_root="frontend")
        self.assertFalse((self.root / "backend/factory.db").exists())

    def test_symlink_and_stale_wal_refused(self):
        external = self.root / "external"
        external.mkdir()
        (self.root / "linked").symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "SYMLINK_PATH_NOT_ALLOWED"):
            self.create(database_path="linked/factory.db")
        (self.root / "backend/factory.db-wal").write_bytes(b"UNIT_TEST_ONLY")
        with self.assertRaisesRegex(ValueError, "DATABASE_ALREADY_EXISTS"):
            self.create()
        self.assertFalse((self.root / "backend/factory.db").exists())

    def test_private_settings_and_account_mapping_restore_without_authorization(self):
        settings = json.loads((SOURCE / "backend/app/seed.json").read_text())["settings"]
        settings.update(theme="dark", production_policy={"concurrency": 1})
        (self.root / "config").mkdir()
        configuration = self.root / "config/development.local.json"
        configuration.write_text(json.dumps({"settings": settings, "accounts": [{"id": "local-account", "name": "UNIT_TEST_ACCOUNT",
            "country": "墨西哥", "hubstudio_env_id": "18", "expected_username": "UNIT_TEST_ONLY", "enabled": True,
            "publishing_context": {"telephone_number": "18", "keyword": "UNIT_TEST_KEYWORD"}}]}))
        result = self.create(local_config="config/development.local.json")
        self.assertEqual(result["counts"]["accounts"], 1)
        with sqlite3.connect(self.root / "backend/factory.db") as database:
            row = database.execute("SELECT status,identity_verification,vpn_verification,last_publish,queue,errors FROM publishing_accounts").fetchone()
            self.assertEqual(row, ("NOT_CONNECTED", "{}", "{}", None, 0, "[]"))
            self.assertEqual(database.execute("SELECT COUNT(*) FROM account_products").fetchone()[0], 0)
            self.assertEqual(database.execute("SELECT theme FROM factory_settings").fetchone()[0], "dark")
            self.assertEqual(database.execute("SELECT COUNT(*) FROM publishing_tasks").fetchone()[0], 0)

    def test_historical_or_secret_account_fields_are_not_imported(self):
        path = self.root / "local.json"
        path.write_text(json.dumps({"accounts": [{"id": "unit", "name": "unit", "country": "unit", "authorization": "DO_NOT_IMPORT"}]}))
        with self.assertRaisesRegex(ValueError, "ACCOUNT_CONFIGURATION_FIELDS_INVALID"):
            self.create(local_config="local.json")
        self.assertFalse((self.root / "backend/factory.db").exists())
        path.write_text(json.dumps({"accounts": [{"id": "unit", "name": "unit", "country": "unit", "publishing_context": {"api_key": "DO_NOT_IMPORT"}}]}))
        with self.assertRaisesRegex(ValueError, "ACCOUNT_CONTEXT_FIELDS_INVALID"):
            self.create(local_config="local.json")

    def test_invalid_schema_does_not_create_database(self):
        path = self.root / "local.json"
        path.write_text(json.dumps({"settings": {"theme": "INVALID_UNIT_TEST"}}))
        with self.assertRaisesRegex(RuntimeError, "INITIALIZATION_FAILED"):
            self.create(local_config="local.json")
        self.assertFalse((self.root / "backend/factory.db").exists())

    def test_configuration_must_be_an_existing_regular_file(self):
        (self.root / "config").mkdir()
        for path in ("config", "config/missing.local.json"):
            with self.assertRaisesRegex(ValueError, "LOCAL_CONFIGURATION_FILE_REQUIRED"):
                self.create(local_config=path)
        self.assertFalse((self.root / "backend/factory.db").exists())
