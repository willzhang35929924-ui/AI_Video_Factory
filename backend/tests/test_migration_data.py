import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import closing
from unittest import mock

spec = importlib.util.spec_from_file_location("migration_data", Path(__file__).parents[2] / "scripts/migration_data.py")
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


class MigrationDataTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.db = self.root / "source.db"
        self.mapping = [("/Users/xiaoyao/AI_Video_Factory", "D:\\AI_Video_Factory")]
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executescript("CREATE TABLE tasks(id TEXT PRIMARY KEY, context JSON, status TEXT);"
                                     "CREATE TABLE video_assets(id TEXT PRIMARY KEY,task_id TEXT REFERENCES tasks(id),storage_path TEXT,legacy_reference JSON);"
                                     "CREATE TABLE workflow_snapshots(id TEXT PRIMARY KEY,workflow JSON);"
                                     "CREATE TABLE processing_runs(id TEXT PRIMARY KEY,command JSON);")
            connection.execute("INSERT INTO tasks VALUES(?,?,?)", ("task-1", json.dumps({"path": "/Users/xiaoyao/AI_Video_Factory/storage/a.mp4", "sha256": "a" * 64, "input_fingerprint": "b" * 64, "api_key": "do-not-export", "request": {"path": "/Users/xiaoyao/AI_Video_Factory/old-request"}}), "PAUSED"))
            connection.execute("INSERT INTO video_assets VALUES(?,?,?,?)", ("asset-1", "task-1", "/Users/xiaoyao/AI_Video_Factory/storage/a.mp4", json.dumps({"manifest_path": "/Users/xiaoyao/AI_Video_Factory/storage/manifest.json", "manifest_sha256": "c" * 64})))
            connection.execute("INSERT INTO workflow_snapshots VALUES(?,?)", ("workflow-1", json.dumps({"source": "/Users/xiaoyao/AI_Video_Factory/storage/a.mp4"})))
            connection.execute("INSERT INTO processing_runs VALUES(?,?)", ("run-1", json.dumps(["python", "/Users/xiaoyao/AI_Video_Factory/old-request.py"])))

    def tearDown(self):
        self.temporary.cleanup()

    def test_backup_is_readable_and_source_unchanged(self):
        before = migration.digest(self.db)
        report = migration.backup(self.db, self.root / "backup.db")
        self.assertEqual(report["status"], "PASS_LOCAL")
        self.assertEqual(report["integrity_check"], ["ok"])
        self.assertEqual(report["foreign_key_check"], [])
        self.assertEqual(migration.digest(self.db), before)
        self.assertEqual(report["tables"]["video_assets"]["count"], 1)

    def test_backup_of_live_wal_includes_committed_rows(self):
        with closing(sqlite3.connect(self.db)) as writer, writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("INSERT INTO tasks VALUES('task-2','{}','QUEUED')")
            writer.commit()
            report = migration.backup(self.db, self.root / "wal-backup.db")
        self.assertEqual(report["tables"]["tasks"]["count"], 2)

    def test_backup_refuses_overwrite(self):
        with self.assertRaisesRegex(ValueError, "OUTPUT_ALREADY_EXISTS"):
            migration.backup(self.db, self.db)

    def test_backup_refuses_symlink_parent(self):
        target = self.root / "target"
        target.mkdir()
        (self.root / "link").symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "OUTPUT_SYMLINK_PARENT"):
            migration.backup(self.db, self.root / "link" / "backup.db")

    def test_mapping_requires_complete_directory_prefix(self):
        self.assertEqual(migration.mapped_path("/Users/xiaoyao/AI_Video_Factory2/a.mp4", self.mapping), "/Users/xiaoyao/AI_Video_Factory2/a.mp4")
        self.assertEqual(migration.mapped_path("/Users/xiaoyao/AI_Video_Factory/中文 空格/a.mp4", self.mapping), "D:\\AI_Video_Factory\\中文 空格\\a.mp4")

    def test_mapping_rejects_traversal(self):
        with self.assertRaisesRegex(ValueError, "PATH_TRAVERSAL"):
            migration.mapped_path("/Users/xiaoyao/AI_Video_Factory/../outside", self.mapping)

    def test_mapping_rejects_mixed_separators(self):
        with self.assertRaisesRegex(ValueError, "MIXED_SEPARATOR"):
            migration.mapped_path("/Users/xiaoyao/AI_Video_Factory/a\\..\\b", self.mapping)

    def test_mapping_refuses_invalid_windows_filename(self):
        for name in ("a:b.mp4", "file?.mp4", "NUL.mp4", "trailing. "):
            with self.assertRaisesRegex(ValueError, "WINDOWS_FILENAME"):
                migration.mapped_path("/Users/xiaoyao/AI_Video_Factory/" + name, self.mapping)

    def test_mapping_longest_prefix_first(self):
        mapping = self.root / "map.json"
        mapping.write_text(json.dumps([{"from": "/Users/xiaoyao/AI_Video_Factory", "to": "D:\\Factory"}, {"from": "/Users/xiaoyao/AI_Video_Factory/storage", "to": "E:\\Media"}]))
        self.assertEqual(migration.mapped_path("/Users/xiaoyao/AI_Video_Factory/storage/a", migration.load_mapping(mapping)), "E:\\Media\\a")

    def test_mapping_rejects_duplicate_prefixes(self):
        mapping = self.root / "map.json"
        mapping.write_text(json.dumps([{"from": self.mapping[0][0], "to": "D:\\A"}, {"from": self.mapping[0][0], "to": "D:\\B"}]))
        with self.assertRaisesRegex(ValueError, "DUPLICATE"):
            migration.load_mapping(mapping)

    def test_plan_exports_only_paths_not_secret_values(self):
        report = migration.path_plan(self.db, self.mapping)
        self.assertNotIn("do-not-export", json.dumps(report))
        self.assertFalse(report["production_ready"])
        self.assertEqual(report["update_count"], 3)
        self.assertEqual(len(report["historical_paths_preserved"]), 3)

    def test_apply_only_to_new_copy_preserves_ids_hashes_and_historical_requests(self):
        original = migration.digest(self.db)
        report = migration.apply_paths(self.db, self.root / "windows-test.db", self.mapping, original)
        self.assertTrue(report["source_unchanged"])
        self.assertEqual(migration.digest(self.db), original)
        with closing(sqlite3.connect(self.root / "windows-test.db")) as connection:
            identity, context, status = connection.execute("SELECT * FROM tasks").fetchone()
            context = json.loads(context)
            self.assertEqual(identity, "task-1")
            self.assertEqual(status, "PAUSED")
            self.assertEqual(context["sha256"], "a" * 64)
            self.assertEqual(context["input_fingerprint"], "b" * 64)
            self.assertEqual(context["path"], "D:\\AI_Video_Factory\\storage\\a.mp4")
            self.assertEqual(context["request"]["path"], "/Users/xiaoyao/AI_Video_Factory/old-request")
            self.assertEqual(json.loads(connection.execute("SELECT workflow FROM workflow_snapshots").fetchone()[0])["source"], "/Users/xiaoyao/AI_Video_Factory/storage/a.mp4")
            self.assertEqual(json.loads(connection.execute("SELECT command FROM processing_runs").fetchone()[0])[1], "/Users/xiaoyao/AI_Video_Factory/old-request.py")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_apply_refuses_changed_source(self):
        with self.assertRaisesRegex(ValueError, "SOURCE_CHANGED"):
            migration.apply_paths(self.db, self.root / "copy.db", self.mapping, "0" * 64)
        self.assertFalse((self.root / "copy.db").exists())

    def test_apply_refuses_existing_formal_database(self):
        with self.assertRaisesRegex(ValueError, "OUTPUT_ALREADY_EXISTS"):
            migration.apply_paths(self.db, self.db, self.mapping, migration.digest(self.db))

    def test_storage_hash_manifest_and_arrival_readback(self):
        storage = self.root / "storage"
        storage.mkdir()
        (storage / "person.mp4").write_bytes(b"rgb bytes")
        (storage / "alpha.mkv").write_bytes(b"alpha bytes")
        report = migration.inventory(self.root, self.db, [], hash_storage=True)
        self.assertTrue(report["storage_sha256_complete"])
        self.assertEqual(report["storage_file_count"], 2)
        self.assertTrue(all(len(row["sha256"]) == 64 for row in report["storage_files"]))
        manifest = self.root / "manifest.json"
        manifest.write_text(json.dumps(report))
        result = migration.verify_storage(self.root, manifest)
        self.assertEqual(result["status"], "PASS_LOCAL")
        self.assertEqual(result["verified_count"], 2)
        self.assertFalse(result["production_ready"])
        (storage / "person.mp4").write_bytes(b"changed rgb bytes")
        result = migration.verify_storage(self.root, manifest)
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["failed_count"], 1)

    def test_inventory_refuses_missing_storage_instead_of_empty_success(self):
        with self.assertRaisesRegex(ValueError, "STORAGE_ROOT_REQUIRES"):
            migration.inventory(self.root, self.db, [], hash_storage=True)

    def test_verify_storage_cli_uses_explicit_acceptance_root(self):
        original = self.root / "storage"
        original.mkdir()
        (original / "person.mp4").write_bytes(b"source rgb")
        manifest = self.root / "manifest.json"
        manifest.write_text(json.dumps(migration.inventory(self.root, self.db, [], hash_storage=True)))
        isolated = original / "windows-acceptance"
        isolated.mkdir()
        (isolated / "person.mp4").write_bytes(b"source rgb")
        (original / "person.mp4").write_bytes(b"different production rgb")
        report = self.root / "acceptance-verification.json"
        run = subprocess.run([sys.executable, str(Path(migration.__file__)), "verify-storage", "--project", str(self.root),
                              "--storage-root", str(isolated), "--manifest", str(manifest), "--report", str(report)], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0)
        result = json.loads(report.read_text())
        self.assertEqual(result["verified_count"], 1)
        self.assertEqual(result["storage_root"], str(isolated))
        self.assertFalse(result["production_ready"])

    def test_storage_snapshot_fails_when_files_change_during_hash(self):
        storage = self.root / "storage"
        storage.mkdir()
        media = storage / "person.mp4"
        media.write_bytes(b"original")
        actual = migration.stable_digest
        def changed(path, expected=None):
            sha = actual(path, expected)
            media.write_bytes(b"changed after hashing")
            return sha
        with mock.patch.object(migration, "stable_digest", side_effect=changed):
            with self.assertRaisesRegex(ValueError, "STORAGE_CHANGED_DURING_SNAPSHOT"):
                migration.inventory(self.root, self.db, [], hash_storage=True)

    def test_stable_digest_refuses_changed_expected_stat_or_symlink(self):
        media = self.root / "person.mp4"
        media.write_bytes(b"original")
        info = media.stat()
        expected = {"device": info.st_dev, "inode": info.st_ino, "bytes": info.st_size, "mtime_ns": info.st_mtime_ns}
        media.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "MEDIA_CHANGED_DURING_SNAPSHOT"):
            migration.stable_digest(media, expected)
        link = self.root / "link.mp4"
        link.symlink_to(media)
        with self.assertRaisesRegex(ValueError, "MEDIA_NOT_REGULAR_FILE"):
            migration.stable_digest(link)

    def test_arrival_readback_refuses_traversal_unhashed_and_symlink(self):
        storage = self.root / "storage"
        storage.mkdir()
        media = self.root / "outside.mp4"
        media.write_bytes(b"outside")
        (storage / "link.mp4").symlink_to(media)
        manifest = self.root / "manifest.json"
        manifest.write_text(json.dumps({"storage_sha256_complete": False, "storage_files": []}))
        with self.assertRaisesRegex(ValueError, "FULL_STORAGE_SHA256_MANIFEST_REQUIRED"):
            migration.verify_storage(self.root, manifest)
        data = {"storage_sha256_complete": True, "storage_files": [{"relative_path": "../outside.mp4", "sha256": migration.digest(media), "bytes": media.stat().st_size}]}
        manifest.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "UNSAFE_STORAGE_MANIFEST_PATH"):
            migration.verify_storage(self.root, manifest)
        data["storage_files"][0]["relative_path"] = "link.mp4"
        manifest.write_text(json.dumps(data))
        result = migration.verify_storage(self.root, manifest)
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["files"][0]["status"], "UNSAFE_SYMLINK")

    def test_verify_storage_cli_reports_missing_media_with_nonzero_exit(self):
        manifest = self.root / "manifest.json"
        manifest.write_text(json.dumps({"storage_sha256_complete": True, "storage_files": [{"relative_path": "missing.mp4", "sha256": "a" * 64, "bytes": 100}]}))
        report = self.root / "verification.json"
        run = subprocess.run([sys.executable, str(Path(migration.__file__)), "verify-storage", "--project", str(self.root),
                              "--manifest", str(manifest), "--report", str(report)], capture_output=True, text=True)
        self.assertEqual(run.returncode, 1)
        saved = json.loads(report.read_text())
        self.assertEqual(saved["status"], "FAIL")
        self.assertEqual(saved["files"][0]["status"], "MISSING")

    def test_code_bundle_supports_unborn_git_without_fabricating_commit(self):
        project = self.root / "unborn-project"
        project.mkdir()
        subprocess.run(["git", "init", "-q", str(project)], check=True)
        (project / "app.py").write_text("print('current source')\n")
        subprocess.run(["git", "add", "app.py"], cwd=project, check=True)
        report = migration.code_bundle(project, self.root / "unborn.zip")
        self.assertIsNone(report["manifest"]["head"])
        self.assertTrue(report["manifest"]["unborn"])
        with zipfile.ZipFile(self.root / "unborn.zip") as archive:
            self.assertEqual(archive.read("app.py"), b"print('current source')\n")
        self.assertEqual(subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=project,
                         capture_output=True).returncode, 128)

    def test_code_bundle_includes_verified_extension_dependencies_not_generic_media(self):
        project = self.root / "extension-project"
        project.mkdir()
        subprocess.run(["git", "init", "-q", str(project)], check=True)
        allowed = "extensions/darenjingling/src/assets/img/logo128.png"
        content = b"\x89PNG\r\n\x1a\nunit-test-static-extension-icon"
        icon = project / allowed
        icon.parent.mkdir(parents=True)
        icon.write_bytes(content)
        excluded = ("extensions/darenjingling/assets/unknown-image.png", "media.png", "video.mp4", "model.safetensors", "storage/person.mp4", "logs/runtime.log")
        for name in excluded:
            path = project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        subprocess.run(["git", "add", "."], cwd=project, check=True)
        with mock.patch.dict(migration.EXTENSION_STATIC_HASHES, {allowed: migration.digest(icon)}):
            report = migration.code_bundle(project, self.root / "extension.zip")
        with zipfile.ZipFile(self.root / "extension.zip") as archive:
            self.assertEqual(archive.read(allowed), content)
            for name in excluded:
                self.assertNotIn(name, archive.namelist())
        included = {row["path"]: row for row in report["manifest"]["included"]}
        self.assertEqual(included[allowed]["sha256"], migration.digest(icon))

    def test_extension_allowlist_refuses_changed_bytes_and_symlinks(self):
        relative = Path("extensions/darenjingling/src/assets/img/logo128.png")
        path = self.root / relative
        path.parent.mkdir(parents=True)
        path.write_bytes(b"not the reviewed static image")
        self.assertIn("manual SHA review", migration.package_reason(relative, path))
        path.unlink()
        outside = self.root / "outside.png"
        outside.write_bytes(b"outside")
        path.symlink_to(outside)
        self.assertIn("symlink", migration.package_reason(relative, path))

    def test_code_bundle_preserves_dirty_untracked_but_omits_secrets_media_logs(self):
        project = self.root / "project"
        project.mkdir()
        subprocess.run(["git", "init", "-q", str(project)], check=True)
        (project / "app.py").write_text("print('original')\n")
        (project / "deleted.py").write_text("print('deleted')\n")
        subprocess.run(["git", "add", "."], cwd=project, check=True)
        subprocess.run(["git", "-c", "user.name=MigrationTest", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture"], cwd=project, check=True)
        (project / "app.py").write_text("print('dirty')\n")
        (project / "deleted.py").unlink()
        (project / "new.py").write_text("print('untracked')\n")
        (project / ".env").write_text("API_KEY=SECRET\n")
        (project / "config.yaml").write_text("api_key: SECRET\n")
        (project / "secret.txt").write_text("api_key: abcdefg-never-export\n")
        (project / "media.mp4").write_bytes(b"media")
        (project / "runtime.log").write_text("private log\n")
        report = migration.code_bundle(project, self.root / "code.zip")
        with zipfile.ZipFile(self.root / "code.zip") as archive:
            self.assertEqual(archive.read("app.py"), b"print('dirty')\n")
            self.assertEqual(archive.read("new.py"), b"print('untracked')\n")
            for excluded in (".env", "config.yaml", "secret.txt", "media.mp4", "runtime.log", "deleted.py"):
                self.assertNotIn(excluded, archive.namelist())
            manifest = json.loads(archive.read("MIGRATION_CODE_MANIFEST.json"))
            self.assertIn(" D deleted.py", manifest["git_status"])
        self.assertEqual(report["status"], "PASS_LOCAL")

    def test_lockfile_dependency_versions_are_not_cookies(self):
        lock = {"packages": {"node_modules/express": {"dependencies": {"cookie": "^0.7.2", "cookie-signature": "1.0.7"}}}}
        self.assertIsNone(migration.secret_assignments(json.dumps(lock), Path("package-lock.json")))
        lock["api_key"] = "private-key-never-transfer"
        self.assertIn("credential", migration.secret_assignments(json.dumps(lock), Path("package-lock.json")))

    def test_mock_seed_labels_are_not_authorization_tokens(self):
        seed = {"accounts": [{"authorization": "示例授权 · MockAdapter"}]}
        self.assertIsNone(migration.secret_assignments(json.dumps(seed), Path("seed.json")))
        seed["accounts"][0]["authorization"] = "private-token-never-transfer"
        self.assertIn("credential", migration.secret_assignments(json.dumps(seed), Path("seed.json")))


if __name__ == "__main__":
    unittest.main()
