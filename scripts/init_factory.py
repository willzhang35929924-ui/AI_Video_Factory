#!/usr/bin/env python3
"""Create a new, empty integration database; never reset an existing database."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
ACCOUNT_FIELDS = {"id", "name", "country", "hubstudio_env_id", "cdp_endpoint", "expected_username", "enabled", "cooldown_seconds", "publishing_context"}
CONTEXT_FIELDS = {"account_name", "telephone_number", "keyword", "source_document", "source_row", "context_id"}

INITIALIZE = r'''
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from alembic import command
from alembic.config import Config
from app.schemas import AccountView, SettingsView
from app.repository import StateRepository

database, storage, local = Path(sys.argv[1]), Path(sys.argv[2]), json.loads(sys.stdin.read())
settings = SettingsView.model_validate(local["settings"]).model_dump(mode="json") if "settings" in local else None
accounts = [AccountView.model_validate(row).model_dump(mode="json") for row in local.get("accounts", [])]
descriptor = os.open(database, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
os.close(descriptor)
config = Config(str(Path.cwd() / "alembic.ini"))
config.set_main_option("sqlalchemy.url", "sqlite:///" + str(database).replace("%", "%%"))
command.upgrade(config, "head")
repository = StateRepository("sqlite:///" + str(database), Path.cwd() / "app/seed.json", app_mode="integration")
try:
    def restore(state):
        if settings is not None:
            state["settings"] = settings
        state["accounts"] = accounts
    if settings is not None or accounts:
        repository.mutate(restore)
    state = repository.state()
    counts = {key: len(value) for key, value in state.items() if isinstance(value, list)}
finally:
    repository.engine.dispose()
storage.mkdir(parents=True, exist_ok=True)
with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
    if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)] or connection.execute("PRAGMA foreign_key_check").fetchall():
        raise RuntimeError("INITIAL_DATABASE_INTEGRITY_FAILED")
    revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
print(json.dumps({"status": "PASS_LOCAL", "database": str(database), "storage": str(storage), "migration": revision,
                  "counts": counts, "accounts_status": "NOT_CONNECTED", "sha256": hashlib.sha256(database.read_bytes()).hexdigest()}, ensure_ascii=False))
'''


def scoped_path(root, value, kind):
    path = Path(value)
    if ".." in path.parts:
        raise ValueError("PATH_TRAVERSAL_NOT_ALLOWED")
    path = path if path.is_absolute() else root / path
    if not path.is_relative_to(root) or path == root:
        raise ValueError("PATH_MUST_STAY_IN_FACTORY_ROOT")
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError("SYMLINK_PATH_NOT_ALLOWED")
        if parent == root:
            break
    if kind == "database":
        if path.suffix not in {".db", ".sqlite", ".sqlite3"}:
            raise ValueError("DATABASE_EXTENSION_REQUIRED")
        if any(candidate.exists() or candidate.is_symlink() for candidate in (path, *(Path(str(path) + suffix) for suffix in ("-wal", "-shm", "-journal")))):
            raise ValueError("DATABASE_ALREADY_EXISTS")
    elif kind == "storage":
        if path.relative_to(root).parts[0] in {"backend", "frontend", "scripts", "config", "workflows", ".git"}:
            raise ValueError("STORAGE_CANNOT_BE_SOURCE_DIRECTORY")
        if path.exists() and not path.is_dir():
            raise ValueError("STORAGE_MUST_BE_DIRECTORY")
    return path


def local_configuration(root, value):
    if value is None:
        return {}
    path = scoped_path(root, value, "configuration")
    if not path.is_file():
        raise ValueError("LOCAL_CONFIGURATION_FILE_REQUIRED")
    local = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(local, dict) or set(local) - {"settings", "accounts"} or not isinstance(local.get("accounts", []), list):
        raise ValueError("LOCAL_CONFIGURATION_SCHEMA_INVALID")
    accounts = []
    for row in local.get("accounts", []):
        if not isinstance(row, dict) or set(row) - ACCOUNT_FIELDS:
            raise ValueError("ACCOUNT_CONFIGURATION_FIELDS_INVALID")
        context = row.get("publishing_context", {})
        if not isinstance(context, dict) or set(context) - CONTEXT_FIELDS:
            raise ValueError("ACCOUNT_CONTEXT_FIELDS_INVALID")
        accounts.append({**row, "status": "NOT_CONNECTED", "authorization": "需要重新核实实际账号登录及身份", "last_publish": None,
                         "queue": 0, "errors": [], "product_ids": [], "identity_verification": {}, "vpn_verification": {}})
    if len({row.get("id") for row in accounts}) != len(accounts):
        raise ValueError("DUPLICATE_ACCOUNT_ID")
    return {**local, "accounts": accounts}


def initialize(root, database_path="backend/factory.db", storage_root="storage", local_config=None, *, confirm_create=False):
    if not confirm_create:
        raise ValueError("EXPLICIT_CREATE_CONFIRMATION_REQUIRED")
    root = Path(root).absolute()
    if root.is_symlink() or not root.is_dir():
        raise ValueError("FACTORY_ROOT_INVALID")
    root = root.resolve()
    if not all((root / filename).is_file() for filename in ("backend/alembic.ini", "backend/app/repository.py", "backend/app/seed.json")):
        raise ValueError("FACTORY_SOURCE_NOT_FOUND")
    database = scoped_path(root, database_path, "database")
    storage = scoped_path(root, storage_root, "storage")
    local = local_configuration(root, local_config)
    expected_venv = (root / "backend/.venv").resolve()
    if Path(sys.prefix).resolve() != expected_venv:
        raise ValueError("USE_FACTORY_BACKEND_VENV")
    database.parent.mkdir(parents=True, exist_ok=True)
    environment = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL", "HOME") if key in os.environ}
    environment.update(APP_MODE="integration", INTEGRATION_DRY_RUN="true", PRODUCTION_RECOVERY_ENABLED="false", EXTERNAL_RECOVERY_ENABLED="false",
                       DATABASE_URL="sqlite:///" + str(database), STORAGE_ROOT=str(storage))
    completed = subprocess.run([sys.executable, "-B", "-c", INITIALIZE, str(database), str(storage)], cwd=root / "backend", shell=False,
                               env=environment, input=json.dumps(local, ensure_ascii=False), capture_output=True, text=True, timeout=120)
    if completed.returncode:
        raise RuntimeError("INITIALIZATION_FAILED; no existing database was reset; inspect schema or local configuration")
    return json.loads(completed.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--database-path", default="backend/factory.db")
    parser.add_argument("--storage-root", default="storage")
    parser.add_argument("--local-config")
    parser.add_argument("--confirm-create", action="store_true", help="Explicitly create a new database; existing files are always refused")
    args = parser.parse_args()
    try:
        print(json.dumps(initialize(args.root, args.database_path, args.storage_root, args.local_config, confirm_create=args.confirm_create), ensure_ascii=False, indent=2))
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(type(error).__name__ + ": " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
