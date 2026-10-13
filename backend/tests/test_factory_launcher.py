import importlib.util
import hashlib
import io
import json
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


script = Path(__file__).resolve().parents[2] / "scripts/factory_launcher.py"
spec = importlib.util.spec_from_file_location("factory_launcher", script)
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class LauncherTests(unittest.TestCase):
    def test_actual_external_run_tables_block_stop(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "isolated.db"
            with sqlite3.connect(database) as db:
                db.execute("CREATE TABLE publish_runs (status TEXT)")
                db.execute("INSERT INTO publish_runs VALUES ('RUNNING')")
                db.execute("CREATE TABLE description_generations (status TEXT)")
                db.execute("INSERT INTO description_generations VALUES ('SUBMITTED')")
            self.assertEqual(len(launcher.active_work(database)), 2)

    def test_same_project_listener_without_parent_chain_is_not_owned(self):
        with patch.object(launcher, "process_info", return_value=None):
            self.assertFalse(launcher.descendant({"pid": 999, "parent": 888}, 777))
            self.assertTrue(launcher.descendant({"pid": 777, "parent": 1}, 777))

    def test_defaults_are_web_only(self):
        with tempfile.TemporaryDirectory() as folder:
            config = launcher.load_config(Path(folder), None)
            self.assertEqual(config["backend_port"], 8000)
            self.assertEqual(config["frontend_port"], 3000)
            self.assertEqual(config["environment"], "production")

    def test_windows_defaults_never_target_formal_database_or_storage(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(launcher, "WINDOWS", True):
            config = launcher.load_config(Path(folder), None)
            self.assertEqual(config["environment"], "acceptance")
            self.assertEqual(config["database_path"], "backend/factory.windows-acceptance.db")
            self.assertEqual(config["storage_root"], "storage/windows-acceptance")
            self.assertFalse(config["production_cutover_confirmed"])

    def test_explicit_missing_configuration_never_silently_uses_defaults(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, "配置不存在"):
                launcher.load_config(Path(folder), Path(folder) / "missing.json")

    def test_windows_production_requires_explicit_cutover_confirmation(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(launcher, "WINDOWS", True):
            path = Path(folder) / "local.json"
            path.write_text(json.dumps({"environment": "production"}))
            with self.assertRaisesRegex(ValueError, "正式切换未确认"):
                launcher.load_config(Path(folder), path)
            path.write_text(json.dumps({"environment": "production", "production_cutover_confirmed": True}))
            self.assertEqual(launcher.load_config(Path(folder), path)["database_path"], "backend/factory.db")

    def test_boolean_is_not_a_port(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "local.json"
            path.write_text(json.dumps({"backend_port": True}))
            with self.assertRaises(ValueError):
                launcher.load_config(Path(folder), path)

    def test_acceptance_rejects_formal_paths_even_when_database_exists(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = {"environment": "acceptance", "database_path": "backend/factory.db", "storage_root": "storage/windows-acceptance"}
            with self.assertRaisesRegex(RuntimeError, "不能指向正式"):
                launcher.runtime_paths(root, config)

    def test_runtime_paths_validate_real_sqlite_copy_and_storage(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "backend").mkdir()
            (root / "storage/windows-acceptance").mkdir(parents=True)
            database = root / "backend/factory.windows-acceptance.db"
            with sqlite3.connect(database) as db:
                for table in ("tasks", "processing_runs", "asset_identities", "collection_identities"):
                    db.execute(f'CREATE TABLE "{table}" (id TEXT PRIMARY KEY)')
            with patch.object(launcher, "WINDOWS", True):
                config = launcher.load_config(root, None)
            self.assertEqual(launcher.runtime_paths(root, config), (database.resolve(), (root / "storage/windows-acceptance").resolve()))

    def test_invalid_foreign_keys_refuse_start_without_modification(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            database = root / "isolated.db"
            with sqlite3.connect(database) as db:
                db.execute('CREATE TABLE parent (id TEXT PRIMARY KEY)')
                db.execute('CREATE TABLE child (parent_id TEXT REFERENCES parent(id))')
                db.execute("INSERT INTO child VALUES ('missing')")
            before = hashlib.sha256(database.read_bytes()).hexdigest()
            with self.assertRaisesRegex(RuntimeError, "foreign_key_check"):
                launcher.runtime_paths(root, {"environment": "acceptance", "database_path": str(database), "storage_root": str(root / "media")})
            self.assertEqual(hashlib.sha256(database.read_bytes()).hexdigest(), before)

    def test_unknown_paid_and_publication_receipts_block_safe_stop(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "isolated.db"
            with sqlite3.connect(database) as db:
                db.execute("CREATE TABLE processing_runs (status TEXT, error_code TEXT)")
                db.execute("INSERT INTO processing_runs VALUES ('FAILED', 'UNKNOWN_EXTERNAL_REQUEST')")
                db.execute("CREATE TABLE publishing_tasks (status TEXT, submission_state TEXT)")
                db.execute("INSERT INTO publishing_tasks VALUES ('WAITING_MANUAL', 'UNKNOWN_EXTERNAL_RESULT')")
            self.assertEqual(len(launcher.active_work(database)), 2)

    def test_invalid_port_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "local.json"
            path.write_text(json.dumps({"backend_port": -1}))
            with self.assertRaises(ValueError):
                launcher.load_config(Path(folder), path)

    def test_unknown_process_not_owned(self):
        with patch.object(launcher, "WINDOWS", False):
            self.assertFalse(launcher.belongs({"command": "python -m http.server", "cwd": "/tmp"}, Path("/tmp"), "backend"))

    def test_foreign_project_not_owned(self):
        with patch.object(launcher, "WINDOWS", False):
            self.assertFalse(launcher.belongs({"command": "python -m uvicorn app.main:app", "cwd": "/other/backend"}, Path("/tmp/factory"), "backend"))

    def test_windows_requires_project_executable(self):
        with patch.object(launcher, "WINDOWS", True):
            self.assertFalse(launcher.belongs({"command": "C:/Other/python.exe -m uvicorn app.main:app"}, Path("D:/Factory"), "backend"))

    def test_windows_backend_matches_real_executable_not_a_fake_argument(self):
        root = Path("D:/Factory")
        with patch.object(launcher, "WINDOWS", True):
            self.assertTrue(launcher.belongs({"command": r'D:\Factory\backend\.venv\Scripts\python.exe -m uvicorn app.main:app', "executable": r'D:\Factory\backend\.venv\Scripts\python.exe'}, root, "backend"))
            self.assertFalse(launcher.belongs({"command": r'C:\Other\python.exe -m uvicorn app.main:app D:\Factory\backend\.venv\Scripts\python.exe', "executable": r'C:\Other\python.exe'}, root, "backend"))

    def test_windows_next_matches_node_and_project_script(self):
        root = Path("D:/Factory")
        with patch.object(launcher, "WINDOWS", True):
            command = r'"C:\Program Files\nodejs\node.exe" "D:\Factory\frontend\node_modules\next\dist\bin\next" dev'
            self.assertTrue(launcher.belongs({"command": command, "executable": r'C:\Program Files\nodejs\node.exe'}, root, "frontend"))
            self.assertFalse(launcher.belongs({"command": command, "executable": r'C:\Other\malware.exe'}, root, "frontend"))

    def test_windows_venv_redirector_matches_cfg_and_first_interpreter(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(launcher, "WINDOWS", True):
            root = Path(folder)
            (root / "backend/.venv").mkdir(parents=True)
            (root / "backend/.venv/pyvenv.cfg").write_text('home = C:\\Python312\nexecutable = C:\\Python312\\python.exe\n')
            expected = root / "backend/.venv/Scripts/python.exe"
            info = {"pid": 123, "parent": 1, "command": f'"{expected}" -m uvicorn app.main:app --port 8000', "executable": r'C:\Python312\python.exe'}
            self.assertTrue(launcher.belongs(info, root, "backend"))
            for actual in (r'C:\Other\python.exe', r'C:\Python313\python.exe'):
                self.assertFalse(launcher.belongs({**info, "executable": actual}, root, "backend"))

    def test_windows_venv_redirector_does_not_accept_appended_project_arguments(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(launcher, "WINDOWS", True), patch.object(launcher, "process_info", return_value=None):
            root = Path(folder)
            (root / "backend/.venv").mkdir(parents=True)
            (root / "backend/.venv/pyvenv.cfg").write_text('home = C:\\Python312\n')
            project_python = root / "backend/.venv/Scripts/python.exe"
            info = {"pid": 123, "parent": 1, "command": f'C:\\Python312\\python.exe -m uvicorn app.main:app "{project_python}"', "executable": r'C:\Python312\python.exe'}
            self.assertFalse(launcher.belongs(info, root, "backend"))

    def test_windows_venv_redirector_child_requires_verified_project_parent(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(launcher, "WINDOWS", True):
            root = Path(folder)
            (root / "backend/.venv").mkdir(parents=True)
            (root / "backend/.venv/pyvenv.cfg").write_text('home = C:\\Python312\n')
            project_python = root / "backend/.venv/Scripts/python.exe"
            child = {"pid": 123, "parent": 122, "command": r'C:\Python312\python.exe -m uvicorn app.main:app', "executable": r'C:\Python312\python.exe'}
            parent = {"pid": 122, "parent": 121, "command": f'"{project_python}" -m uvicorn app.main:app', "executable": str(project_python)}
            with patch.object(launcher, "process_info", return_value=parent):
                self.assertTrue(launcher.belongs(child, root, "backend"))
            with patch.object(launcher, "process_info", return_value={**parent, "executable": r'C:\Other\python.exe'}):
                self.assertFalse(launcher.belongs(child, root, "backend"))
            with patch.object(launcher, "process_info", return_value=None):
                self.assertFalse(launcher.belongs(child, root, "backend"))

    def test_windows_venv_redirector_refuses_missing_or_conflicting_cfg(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(launcher, "WINDOWS", True):
            root = Path(folder)
            project_python = root / "backend/.venv/Scripts/python.exe"
            info = {"command": f'"{project_python}" -m uvicorn app.main:app', "executable": r'C:\Python312\python.exe'}
            self.assertFalse(launcher.belongs(info, root, "backend"))
            (root / "backend/.venv").mkdir(parents=True)
            (root / "backend/.venv/pyvenv.cfg").write_text('home = C:\\Python312\nexecutable = C:\\Other\\python.exe\n')
            self.assertFalse(launcher.belongs(info, root, "backend"))

    def test_windows_backend_requires_actual_uvicorn_module_position(self):
        root = Path("D:/Factory")
        project_python = str(root / "backend/.venv/Scripts/python.exe")
        with patch.object(launcher, "WINDOWS", True):
            self.assertFalse(launcher.belongs({"executable": project_python, "command": f'"{project_python}" -m http.server uvicorn app.main:app'}, root, "backend"))

    def test_windows_frontend_requires_same_origin_proxy(self):
        base = {"application": "AI Video Factory", "api_origin": "http://127.0.0.1:8000", "api_base_path": "/factory-api", "api_transport": "same-origin"}
        with patch.object(launcher, "WINDOWS", True), patch.object(launcher.HTTP, "open", return_value=io.BytesIO(json.dumps(base).encode())):
            self.assertTrue(launcher.frontend_api_matches("http://127.0.0.1:3000/", 8000))
        absolute = {**base, "api_base_path": "/", "api_transport": "direct"}
        with patch.object(launcher, "WINDOWS", True), patch.object(launcher.HTTP, "open", return_value=io.BytesIO(json.dumps(absolute).encode())):
            self.assertFalse(launcher.frontend_api_matches("http://127.0.0.1:3000/", 8000))
        with patch.object(launcher, "WINDOWS", False), patch.object(launcher.HTTP, "open", return_value=io.BytesIO(json.dumps(absolute).encode())):
            self.assertTrue(launcher.frontend_api_matches("http://127.0.0.1:3000/", 8000))

    def test_windows_backend_reuse_rejects_recovery_consumers(self):
        database = Path("D:/Factory/backend/factory.windows-acceptance.db")
        data = {"database_identity": hashlib.sha256(str(database).encode()).hexdigest(), "app_mode": "integration", "integration_dry_run": True,
                "production_execution_enabled": False, "external_recovery_enabled": False}
        with patch.object(launcher, "WINDOWS", True), patch.object(launcher.HTTP, "open", return_value=io.BytesIO(json.dumps(data).encode())):
            self.assertTrue(launcher.backend_database_matches("http://127.0.0.1:8000/api/health", {"pid": 123}, database))
        for key in ("production_execution_enabled", "external_recovery_enabled"):
            unsafe = {**data, key: True}
            with self.subTest(key=key), patch.object(launcher, "WINDOWS", True), patch.object(launcher.HTTP, "open", return_value=io.BytesIO(json.dumps(unsafe).encode())):
                self.assertFalse(launcher.backend_database_matches("http://127.0.0.1:8000/api/health", {"pid": 123}, database))

    def test_port_diagnostics_redact_known_credentials(self):
        text = launcher.redacted_command({"command": "python server.py --api-key secret123 --token=secret456 --password 'secret 789' https://user:pass@example.com sk-projectABC"})
        for secret in ("secret123", "secret456", "secret 789", "user:pass", "sk-projectABC"):
            self.assertNotIn(secret, text)
        self.assertIn("[REDACTED]", text)

    def test_port_diagnostics_hide_opaque_arguments_not_only_known_key_names(self):
        text = launcher.redacted_command({"command": "python custom_server.py opaque-credential --random-option totally-unknown-secret --port 3000"})
        self.assertNotIn("opaque-credential", text)
        self.assertNotIn("totally-unknown-secret", text)
        self.assertIn("--port 3000", text)

    def test_stop_does_not_touch_reused_services(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            state = root / "state.json"
            launcher.save_state(state, {"root": str(root), "services": [{"managed": False, "role": "backend", "listener": {"pid": 123}}]})
            with patch.object(launcher.os, "kill") as kill:
                launcher.stop_services(root, state, lambda text: None)
                kill.assert_not_called()

    def test_pid_reuse_does_not_signal(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            state = root / "state.json"
            launcher.save_state(state, {"root": str(root), "services": [{"managed": True, "role": "frontend", "listener": {"pid": 123, "start": "old"}, "parent": {"pid": 123, "start": "old"}}]})
            with patch.object(launcher, "process_info", return_value={"pid": 123, "start": "new"}), patch.object(launcher.os, "kill") as kill:
                with self.assertRaises(RuntimeError):
                    launcher.stop_services(root, state, lambda text: None)
                kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
