#!/usr/bin/env python3
"""Offline Factory migration: online SQLite backup, inventory, code snapshot, test-copy path mapping.

This tool never starts a Worker, changes its source database, rewrites a media manifest,
or sends an external request. A mapped database is deliberately NOT production-ready.
"""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sqlite3
import stat
import subprocess
import zipfile
from contextlib import closing
from datetime import datetime, timezone


HISTORICAL_COLUMNS = {("workflow_snapshots", "workflow"), ("processing_runs", "command"),
                      ("description_generations", "request")}
HISTORICAL_KEYS = {"request", "request_payload", "response", "raw_response", "submitted_graph", "command", "history"}
SECRET_KEY = re.compile(r"(?i)(api[_-]?key|access[_-]?token|refresh[_-]?token|password|cookie|authorization|client[_-]?secret)")
HASH_KEY = re.compile(r"(?i)(sha256|fingerprint|(?:^|_)hash)$")
SECRET_VALUE = re.compile(r"(?i)(?:\bsk-[A-Za-z0-9_-]{12,}|\bBearer\s+[A-Za-z0-9._-]{12,})")
MEDIA_EXTENSIONS = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".wav", ".mp3", ".safetensors", ".pth", ".pt", ".onnx"}
OMIT_DIRECTORIES = {".git", ".venv", "venv", "node_modules", ".next", "__pycache__", "storage", "output", "backups", ".playwright-cli", "logs", ".runtime"}
# Exact static dependencies referenced by the installed extension's manifest/JS/CSS.
EXTENSION_STATIC_HASHES = {
    "extensions/darenjingling/assets/aicomment.jpg": "2c22f27f930e1df5d69b7c8ef52a58b4f7f69f250ba96f5ed708cf1e7fb93a25",
    "extensions/darenjingling/assets/doubao.png": "0582b657a4bf5894b0ba6e82954e0524a012bc1771a4011ef0852e3f69c055e0",
    "extensions/darenjingling/assets/en-logo-blue.png": "7348d8feda61ab07cacfee5b20c55cd5d98e477a2d8fc2544d3a536673985811",
    "extensions/darenjingling/assets/en-logo-white.png": "463d4a1616c12c460493db3280362412db83f7f656f22a89e3d9f14d87489670",
    "extensions/darenjingling/assets/iconfont.ttf": "f9fd6f2134aa48f97f237bae5ca3e0cb148985ecc2d57b4a8ad561c445031d43",
    "extensions/darenjingling/assets/iconfont.woff": "71f1132e5eeba790368e3cdd985ce79446c5caf04e55b36bfc26f3f848292957",
    "extensions/darenjingling/assets/iconfont.woff2": "8ab7960d2356c15e0f7a24945dd8bedaac31b25e98d947535093aa2d3b1e4358",
    "extensions/darenjingling/assets/kefu.jpg": "32ed792214b70fb8a8656648345bfd810fd207590e9a8c4d912fc869a64520fa",
    "extensions/darenjingling/assets/logo-text-row-white.png": "d79e44827ae2a8a9f392f724e49f60995b537f83a06fb3fd173913ef60ad982f",
    "extensions/darenjingling/assets/logo-text-row.png": "7ef72d0c9b5e0495a24eb87d151040d947afe8e5404e5ddd84085a7114735bbf",
    "extensions/darenjingling/assets/logo-white.png": "2c8ce39ce9573c0e084b0aea5eecbdeafa07e9dee4265866d80d2164d428e014",
    "extensions/darenjingling/assets/logo128.png": "e08cc363521e9dbc6d892c88997c88440f13a0dbaafab753f208d7aa81f8ec24",
    "extensions/darenjingling/assets/option-whats-app.jpg": "79b0743cd5436fb9c7204a4dfe6ea410a170f1d196e8ceb44f27c94d0dcbcbb8",
    "extensions/darenjingling/assets/optionsqiwei.jpg": "cfb94fc66de187f2d355d6d525b947c8eda9454cf869309255c447ad69ef636d",
    "extensions/darenjingling/assets/whats-app.jpg": "cd91397cb60b84ee098c4fb63455609b75480549554bfb4e847e7fe954198c1e",
    "extensions/darenjingling/assets/wxcode.jpg": "6a9565e3f3852fdec14631665d0f36319baf9f727735ecd66f78cf13aeca0b53",
    "extensions/darenjingling/src/assets/icon/iconfont.ttf": "f9fd6f2134aa48f97f237bae5ca3e0cb148985ecc2d57b4a8ad561c445031d43",
    "extensions/darenjingling/src/assets/icon/iconfont.woff": "71f1132e5eeba790368e3cdd985ce79446c5caf04e55b36bfc26f3f848292957",
    "extensions/darenjingling/src/assets/icon/iconfont.woff2": "8ab7960d2356c15e0f7a24945dd8bedaac31b25e98d947535093aa2d3b1e4358",
    "extensions/darenjingling/src/assets/img/icon/collaborate.png": "fa52b416367fbec25874e70cbbb3fdc486096dbdeb6f101b76140275293688bb",
    "extensions/darenjingling/src/assets/img/logo128.png": "81ca40cbf5e66e9b3cac509ca378890ec31f1886a89642cb6e0d7f0ad8cd03a1",
}


def credential_literal(key, value):
    if not SECRET_KEY.search(key) or key.lower().endswith(("_env", "_ref", "_name")) or not isinstance(value, str):
        return False
    value = value.strip()
    return bool(value and value.lower() not in {"redacted", "changeme", "your_key", "your_api_key", "test-private-key", "test-secret"}
                and not value.startswith(("$", "<", "test-", "sk-testing-")))


def secret_assignments(contents, path):
    if path.suffix == ".json":
        try:
            data = json.loads(contents)
        except ValueError:
            return "unparseable JSON: review before code transfer"
        def inspect(value, pointer=()):
            if isinstance(value, dict):
                for key, child in value.items():
                    dependency_version = (path.name == "package-lock.json" and len(pointer) == 3
                                          and pointer[0] == "packages" and pointer[2] in {"dependencies", "devDependencies", "optionalDependencies", "peerDependencies"}
                                          and isinstance(child, str) and re.fullmatch(r"[~^]?[0-9][0-9.a-z+<>= |*-]*", child))
                    mock_label = key == "authorization" and child in ("示例授权 · MockAdapter", "示例授权已过期", "等待示例授权", "示例连接失败")
                    if not dependency_version and not mock_label and credential_literal(key, child):
                        return "possible credential literal: manual review required"
                    reason = inspect(child, pointer + (key,))
                    if reason:
                        return reason
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    reason = inspect(child, pointer + (index,))
                    if reason:
                        return reason
            return None
        return inspect(data)
    if path.suffix == ".py":
        try:
            tree = ast.parse(contents)
        except SyntaxError:
            return "unparseable Python: review before code transfer"
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if isinstance(key, ast.Constant) and isinstance(key.value, str) and isinstance(value, ast.Constant) and credential_literal(key.value, value.value):
                        return "possible credential literal: manual review required"
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Constant):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(isinstance(target, ast.Name) and credential_literal(target.id, node.value.value) for target in targets):
                    return "possible credential literal: manual review required"
        return None
    for line in contents.splitlines():
        match = re.match(r"\s*[\"']?([A-Za-z][A-Za-z0-9_-]*)[\"']?\s*[:=]\s*[\"']?([^\s\"',#]+)", line)
        if match and credential_literal(match[1], match[2]) and not match[2].startswith(("process.", "settings.", "config.", "self.")):
            return "possible credential assignment: manual review required"
    return None


def utc():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def quoted(name):
    return '"' + name.replace('"', '""') + '"'


def readonly(path):
    path = Path(path).resolve(strict=True)
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=30)


def new_output(path):
    path = Path(path).absolute()
    if path.exists() or path.is_symlink():
        raise ValueError(f"OUTPUT_ALREADY_EXISTS: {path}")
    for parent in path.parents:
        if parent.is_symlink():
            raise ValueError(f"OUTPUT_SYMLINK_PARENT: {parent}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def write_report(path, result):
    with new_output(path).open("x", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        json.dump(result, stream, ensure_ascii=False, indent=2)


def tables(connection):
    return [r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def hash_leaves(value, pointer=""):
    if isinstance(value, dict):
        for key, child in value.items():
            if HASH_KEY.search(key):
                yield pointer + "/" + key, child
            else:
                yield from hash_leaves(child, pointer + "/" + key)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from hash_leaves(child, pointer + "/" + str(index))


def db_summary(connection):
    integrity = [r[0] for r in connection.execute("PRAGMA integrity_check")]
    foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    result = {"integrity_check": integrity, "foreign_key_check": foreign_keys, "tables": {}}
    for table in tables(connection):
        columns = connection.execute(f"PRAGMA table_info({quoted(table)})").fetchall()
        primary = [r[1] for r in sorted(columns, key=lambda r: r[5]) if r[5]]
        identity = hashlib.sha256()
        if primary:
            names = ",".join(map(quoted, primary))
            for row in connection.execute(f"SELECT {names} FROM {quoted(table)} ORDER BY {names}"):
                identity.update(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode())
                identity.update(b"\n")
        count = connection.execute(f"SELECT COUNT(*) FROM {quoted(table)}").fetchone()[0]
        chain = hashlib.sha256()
        protected = [(index, column) for index, column in enumerate(columns) if HASH_KEY.search(column[1]) or column[2].upper() == "JSON"]
        for row in connection.execute(f"SELECT rowid,* FROM {quoted(table)} ORDER BY rowid"):
            for index, column in protected:
                value = row[index + 1]
                leaves = hash_leaves(json.loads(value)) if column[2].upper() == "JSON" and isinstance(value, str) else [("", value)] if HASH_KEY.search(column[1]) else []
                for pointer, value in leaves:
                    chain.update(json.dumps([row[0], column[1], pointer, value], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
                    chain.update(b"\n")
        result["tables"][table] = {"count": count, "primary_keys": primary, "identity_sha256": identity.hexdigest() if primary else None,
                                  "preserved_hash_chain_sha256": chain.hexdigest()}
    result["status"] = "PASS_LOCAL" if integrity == ["ok"] and not foreign_keys else "FAIL"
    return result


def backup(source, destination):
    destination = new_output(destination)
    with destination.open("xb"):
        os.chmod(destination, 0o600)
    with closing(readonly(source)) as incoming, closing(sqlite3.connect(destination)) as outgoing:
        incoming.backup(outgoing, pages=256, sleep=.05)
    with closing(readonly(destination)) as connection:
        result = db_summary(connection)
    result.update({"captured_at": utc(), "method": "sqlite3.Connection.backup (source mode=ro)",
                   "source": str(Path(source).resolve()), "destination": str(destination),
                   "sha256": digest(destination), "bytes": destination.stat().st_size,
                   "warning": "The backup is sensitive local data. Do not commit it or upload it publicly."})
    if result["status"] != "PASS_LOCAL":
        raise ValueError("BACKUP_VALIDATION_FAILED: preserve backup and inspect integrity/foreign keys")
    return result


def candidate_path(value):
    return isinstance(value, str) and (value.startswith(("/Users/", "/Applications/", "/Volumes/", "/opt/", "/private/"))
                                      or re.match(r"^[A-Za-z]:[\\/]", value))


def path_leaves(value, pointer="", historical=False):
    if isinstance(value, dict):
        for key, child in value.items():
            if SECRET_KEY.search(key) or HASH_KEY.search(key):
                continue
            yield from path_leaves(child, pointer + "/" + str(key).replace("~", "~0").replace("/", "~1"),
                                   historical or key.lower() in HISTORICAL_KEYS)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from path_leaves(child, pointer + "/" + str(index), historical)
    elif candidate_path(value):
        yield pointer, value, historical


def database_paths(connection):
    for table in tables(connection):
        columns = connection.execute(f"PRAGMA table_info({quoted(table)})").fetchall()
        names = [r[1] for r in columns]
        for row in connection.execute(f"SELECT rowid,* FROM {quoted(table)}"):
            row_id = row[1 + names.index("id")] if "id" in names else str(row[0])
            for column, sql_type, value in ((r[1], r[2], row[index + 1]) for index, r in enumerate(columns)):
                if not isinstance(value, str) or SECRET_KEY.search(column):
                    continue
                decoded = json.loads(value) if sql_type.upper() == "JSON" else value
                for pointer, path, historical in path_leaves(decoded, historical=(table, column) in HISTORICAL_COLUMNS):
                    yield {"table": table, "rowid": row[0], "row_id": row_id, "column": column,
                           "pointer": pointer, "path": path, "historical": historical}


def storage_listing(root):
    root = Path(root)
    if not root.is_dir() or root.is_symlink():
        raise ValueError("STORAGE_ROOT_REQUIRES_EXISTING_REGULAR_DIRECTORY")
    files, symlinks = [], []
    for directory, child_dirs, names in os.walk(root, followlinks=False):
        for name in [*child_dirs, *names]:
            path = Path(directory) / name
            if path.is_symlink():
                symlinks.append(str(path))
            elif path.is_file():
                info = path.stat()
                files.append({"path": str(path), "relative_path": path.relative_to(root).as_posix(),
                              "bytes": info.st_size, "mtime_ns": info.st_mtime_ns,
                              "device": info.st_dev, "inode": info.st_ino})
        child_dirs[:] = [name for name in child_dirs if not (Path(directory) / name).is_symlink()]
    return sorted(files, key=lambda row: row["relative_path"]), sorted(symlinks)


def stable_digest(path, expected=None):
    path = Path(path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"MEDIA_NOT_REGULAR_FILE: {path}")
    signature = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    if expected and signature(before) != tuple(expected[key] for key in ("device", "inode", "bytes", "mtime_ns")):
        raise ValueError(f"MEDIA_CHANGED_DURING_SNAPSHOT: {path}")
    h = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        if signature(os.fstat(stream.fileno())) != signature(before):
            raise ValueError(f"MEDIA_CHANGED_DURING_SNAPSHOT: {path}")
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
        if signature(os.fstat(stream.fileno())) != signature(before):
            raise ValueError(f"MEDIA_CHANGED_DURING_SNAPSHOT: {path}")
    if signature(path.lstat()) != signature(before):
        raise ValueError(f"MEDIA_CHANGED_DURING_SNAPSHOT: {path}")
    return h.hexdigest()


def inventory(project, database, hash_assets, hash_storage=False):
    project = Path(project).resolve(strict=True)
    with closing(readonly(database)) as connection:
        result = db_summary(connection)
        paths = list(database_paths(connection))
        hashes = []
        if "video_assets" in tables(connection):
            for asset_id in hash_assets:
                row = connection.execute("SELECT id,task_id,storage_path,external_path,sha256,size FROM video_assets WHERE id=?", (asset_id,)).fetchone()
                if not row:
                    raise ValueError(f"ASSET_NOT_FOUND: {asset_id}")
                path = Path(row[3] or row[2])
                item = {"asset_id": row[0], "task_id": row[1], "path": str(path), "exists": path.is_file(), "registered_sha256": row[4]}
                if path.is_file():
                    item.update({"bytes": path.stat().st_size, "actual_sha256": digest(path)})
                    item["hash_matches"] = item["actual_sha256"] == item["registered_sha256"]
                hashes.append(item)
    files, symlinks = storage_listing(project / "storage")
    if hash_storage:
        for row in files:
            row["sha256"] = stable_digest(row["path"], row)
        after, after_symlinks = storage_listing(project / "storage")
        before = [{key: value for key, value in row.items() if key != "sha256"} for row in files]
        if before != after or symlinks != after_symlinks:
            raise ValueError("STORAGE_CHANGED_DURING_SNAPSHOT: pause at a safe stage boundary and take a new inventory")
    result.update({"captured_at": utc(), "project": str(project), "database": str(Path(database).resolve()),
                   "absolute_path_references": paths, "storage_files": files, "storage_symlinks": symlinks,
                   "storage_file_count": len(files), "storage_logical_bytes": sum(row["bytes"] for row in files),
                   "hashed_assets": hashes, "storage_sha256_complete": hash_storage, "new_external_requests": 0,
                   "warning": "Source snapshot only; no media copied and no Windows arrival verified."})
    return result


def verify_storage(project, manifest, storage_root=None):
    project = Path(project).resolve(strict=True)
    data = json.loads(Path(manifest).read_text(encoding="utf-8"))
    if not data.get("storage_sha256_complete") or not isinstance(data.get("storage_files"), list):
        raise ValueError("FULL_STORAGE_SHA256_MANIFEST_REQUIRED")
    root = Path(storage_root).absolute() if storage_root else project / "storage"
    results = []
    seen = set()
    for row in data["storage_files"]:
        relative = PurePosixPath(row["relative_path"])
        if relative.is_absolute() or ".." in relative.parts or "\\" in row["relative_path"]:
            raise ValueError("UNSAFE_STORAGE_MANIFEST_PATH")
        if row["relative_path"] in seen:
            raise ValueError("DUPLICATE_STORAGE_MANIFEST_PATH")
        seen.add(row["relative_path"])
        path = root.joinpath(*relative.parts)
        result = {"relative_path": row["relative_path"], "path": str(path), "expected_sha256": row["sha256"], "expected_bytes": row["bytes"]}
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
            result["status"] = "UNSAFE_SYMLINK"
        elif not path.is_file():
            result["status"] = "MISSING"
        else:
            result.update({"actual_sha256": stable_digest(path), "actual_bytes": path.stat().st_size})
            result["status"] = "PASS_LOCAL" if result["actual_sha256"] == row["sha256"] and result["actual_bytes"] == row["bytes"] else "MISMATCH"
        results.append(result)
    passed = sum(row["status"] == "PASS_LOCAL" for row in results)
    return {"captured_at": utc(), "status": "PASS_LOCAL" if passed == len(results) else "FAIL", "project": str(project), "storage_root": str(root),
            "manifest": str(Path(manifest).resolve()), "manifest_sha256": digest(manifest), "file_count": len(results),
            "verified_count": passed, "failed_count": len(results) - passed, "files": results, "new_external_requests": 0,
            "production_ready": False, "warning": "Byte verification only: validate database paths, workflow/runtime readiness and timeline separately."}


def load_mapping(path):
    mapping = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(mapping, list) or not mapping:
        raise ValueError("MAPPING_EXPECTED_NONEMPTY_ARRAY")
    result = []
    for item in mapping:
        old, new = item["from"].rstrip("/"), item["to"].rstrip("\\/")
        if not old.startswith("/Users/") or any(part in (".", "..") for part in old.split("/")):
            raise ValueError("MAPPING_FROM_REQUIRES_NORMALIZED_MAC_USER_PREFIX")
        if not re.fullmatch(r"[A-Za-z]:[\\/].+", new) or any(part == ".." for part in PureWindowsPath(new).parts):
            raise ValueError("MAPPING_TO_REQUIRES_ABSOLUTE_WINDOWS_DIRECTORY")
        if any(old == previous[0] for previous in result):
            raise ValueError("DUPLICATE_MAPPING_PREFIX")
        result.append((old, str(PureWindowsPath(new))))
    return sorted(result, key=lambda item: len(item[0]), reverse=True)


def mapped_path(value, mapping):
    for old, new in mapping:
        if value == old or value.startswith(old + "/"):
            suffix = value[len(old):].lstrip("/")
            if any(part in (".", "..") for part in suffix.split("/")) or "\\" in suffix:
                raise ValueError("PATH_TRAVERSAL_OR_MIXED_SEPARATOR")
            for part in PurePosixPath(suffix).parts:
                if re.search(r'[<>:"|?*]', part) or part.endswith((" ", ".")) or part.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *("COM" + str(i) for i in range(1, 10)), *("LPT" + str(i) for i in range(1, 10))}:
                    raise ValueError("WINDOWS_FILENAME_REQUIRES_EXPLICIT_RENAME_PLAN")
            return str(PureWindowsPath(new).joinpath(*PurePosixPath(suffix).parts))
    return value


def map_tree(value, mapping, historical=False, key=""):
    if historical or SECRET_KEY.search(key) or HASH_KEY.search(key):
        return value
    if isinstance(value, dict):
        return {name: map_tree(child, mapping, name.lower() in HISTORICAL_KEYS, name) for name, child in value.items()}
    if isinstance(value, list):
        return [map_tree(child, mapping) for child in value]
    return mapped_path(value, mapping) if candidate_path(value) else value


def path_plan(database, mapping):
    with closing(readonly(database)) as connection:
        paths = list(database_paths(connection))
        summary = db_summary(connection)
    updates, historical, unmapped = [], [], []
    for row in paths:
        new = mapped_path(row["path"], mapping)
        if new == row["path"]:
            unmapped.append(row)
        elif row["historical"]:
            historical.append({**row, "mapped_runtime_locator": new})
        else:
            updates.append({**row, "to": new})
    by_windows_path = {}
    for row in updates:
        by_windows_path.setdefault(row["to"].casefold(), set()).add(row["path"])
    case_aliases = [sorted(values) for values in by_windows_path.values() if len(values) > 1]
    return {"captured_at": utc(), "database": str(Path(database).resolve()), "source_sha256": digest(database),
            "mapping": [{"from": old, "to": new} for old, new in mapping], "database_summary": summary,
            "updates": updates, "historical_paths_preserved": historical, "unmapped_paths": unmapped,
            "update_count": len(updates), "distinct_cells": len({(r["table"], r["rowid"], r["column"]) for r in updates}),
            "windows_long_path_references": sum(len(row["to"]) >= 260 for row in updates),
            "windows_case_aliases_to_verify": case_aliases,
            "status": "PASS_TEST", "production_ready": False,
            "required_revalidation": ["All changed JSON cells: preserve existing IDs/hashes but revalidate configuration/input/step fingerprints.",
                                      "Media manifests, workflow graphs and provider requests remain byte-identical, including old embedded paths.",
                                      "Manifest hash chains must not be rewritten or synthesized. Runtime locator compatibility is still required.",
                                      "Validate actual Windows files/SHA/FPS/frame counts/audio lineage and source model/node implementations.",
                                      "Verify Windows long-path support and case-insensitive path aliases before media transfer.",
                                      "Keep all production/publishing queues paused; unknown paid/publish requests require read-only reconciliation."]}


def apply_paths(source, output, mapping, expected_sha):
    if digest(source) != expected_sha:
        raise ValueError("SOURCE_CHANGED: rerun the plan against this exact backup")
    output = new_output(output)
    if output.resolve() == Path(source).resolve():
        raise ValueError("SOURCE_DATABASE_MUST_REMAIN_READONLY")
    plan = path_plan(source, mapping)
    backup(source, output)
    cells = {(row["table"], row["rowid"], row["column"]) for row in plan["updates"]}
    with closing(sqlite3.connect(output)) as connection, connection:
        connection.execute("PRAGMA foreign_keys=ON")
        for table, rowid, column in sorted(cells):
            types = {r[1]: r[2] for r in connection.execute(f"PRAGMA table_info({quoted(table)})")}
            value = connection.execute(f"SELECT {quoted(column)} FROM {quoted(table)} WHERE rowid=?", (rowid,)).fetchone()[0]
            decoded = json.loads(value) if types[column].upper() == "JSON" else value
            replacement = map_tree(decoded, mapping)
            encoded = json.dumps(replacement, ensure_ascii=False, separators=(",", ":")) if types[column].upper() == "JSON" else replacement
            connection.execute(f"UPDATE {quoted(table)} SET {quoted(column)}=? WHERE rowid=?", (encoded, rowid))
        after = db_summary(connection)
        if after["status"] != "PASS_LOCAL" or after["tables"] != plan["database_summary"]["tables"]:
            raise ValueError("PATH_MAPPING_CHANGED_IDENTITIES_OR_DATABASE_INTEGRITY")
    plan.update({"output": str(output), "output_sha256": digest(output), "after": after,
                 "status": "PASS_TEST", "production_ready": False, "source_unchanged": digest(source) == expected_sha})
    return plan


def package_reason(relative, path):
    parts = relative.parts
    if any(part in OMIT_DIRECTORIES or part.startswith(".next-") for part in parts):
        return "runtime/dependency/media directory"
    if path.is_symlink():
        return "symlink must be migrated and verified separately"
    if path.exists() and not path.resolve().is_relative_to(path.parents[len(relative.parts) - 1].resolve()):
        return "path escapes project through a symlink"
    if not path.is_file():
        return "working-tree deletion or missing file"
    if path.name.startswith(".env") and path.name != ".env.example":
        return "local secrets/environment"
    expected_static = EXTENSION_STATIC_HASHES.get(relative.as_posix())
    if expected_static:
        if path.stat().st_size > 10 * 1024 * 1024 or stable_digest(path) != expected_static:
            return "static extension dependency changed: manual SHA review required"
        return None
    if path.name in {"config.yaml", "config.yml", ".DS_Store"} or path.suffix.lower() in MEDIA_EXTENSIONS:
        return "local config/model/media: transfer separately after secret review"
    if path.suffix.lower() in {".db", ".sqlite", ".sqlite3", ".log", ".pyc", ".tsbuildinfo", ".pem", ".key", ".p12"} or ".db-" in path.name:
        return "database/log/cache/private key"
    if path.stat().st_size > 10 * 1024 * 1024:
        return "large file: transfer separately"
    try:
        contents = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, ValueError):
        return "binary file: transfer separately"
    candidates = SECRET_VALUE.findall(contents)
    if any(not value.startswith("sk-testing-") for value in candidates):
        return "possible credential value: manual review required"
    return secret_assignments(contents, path)


def code_bundle(project, destination):
    project = Path(project).resolve(strict=True)
    destination = new_output(destination)
    git = lambda *args: subprocess.run(["git", *args], cwd=project, check=True, stdout=subprocess.PIPE).stdout
    tracked = git("ls-files", "-z").decode().split("\0")
    untracked = git("ls-files", "--others", "--exclude-standard", "-z").decode().split("\0")
    entries = sorted(set(tracked + untracked) - {""})
    revision = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=project,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    head = revision.stdout.decode().strip() if revision.returncode == 0 else None
    included, excluded = [], []
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        os.chmod(destination, 0o600)
        for name in entries:
            relative = PurePosixPath(name)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("UNSAFE_GIT_ENTRY")
            path = project / name
            reason = package_reason(relative, path)
            if reason:
                excluded.append({"path": name, "reason": reason, "tracked": name in tracked})
                continue
            archive.write(path, name)
            included.append({"path": name, "sha256": digest(path), "bytes": path.stat().st_size, "tracked": name in tracked})
        manifest = {"captured_at": utc(), "project": str(project), "branch": git("branch", "--show-current").decode().strip(),
                    "head": head, "unborn": head is None,
                    "git_status": git("status", "--short", "--untracked-files=all").decode(),
                    "included": included, "excluded": excluded,
                    "warning": "Working-tree snapshot, not git archive and not a database/media/secret backup. Review excluded files. No .git history included. Extract only to a new empty Windows directory."}
        archive.writestr("MIGRATION_CODE_MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    with zipfile.ZipFile(destination) as archive:
        bad = archive.testzip()
        if bad:
            raise ValueError("CODE_BUNDLE_CRC_FAILED")
    return {"captured_at": utc(), "status": "PASS_LOCAL", "bundle": str(destination), "sha256": digest(destination),
            "bytes": destination.stat().st_size, "included_count": len(included), "excluded_count": len(excluded),
            "manifest": manifest}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    p = sub.add_parser("backup")
    p.add_argument("--source", required=True); p.add_argument("--dest", required=True)
    p = sub.add_parser("inventory")
    p.add_argument("--project", required=True); p.add_argument("--database", required=True)
    p.add_argument("--hash-asset-id", action="append", default=[])
    p.add_argument("--hash-storage", action="store_true", help="SHA-256 every storage file; fail closed if the source changes")
    p = sub.add_parser("verify-storage")
    p.add_argument("--project", required=True); p.add_argument("--manifest", required=True)
    p.add_argument("--storage-root", help="Existing isolated media root; defaults to PROJECT/storage")
    p = sub.add_parser("bundle")
    p.add_argument("--project", required=True); p.add_argument("--dest", required=True)
    p = sub.add_parser("plan-paths")
    p.add_argument("--database", required=True); p.add_argument("--mapping", required=True)
    p = sub.add_parser("apply-paths")
    p.add_argument("--source", required=True); p.add_argument("--output", required=True)
    p.add_argument("--mapping", required=True); p.add_argument("--expected-sha256", required=True)
    for p in sub.choices.values():
        p.add_argument("--report", required=True)
    args = parser.parse_args()
    if args.operation == "backup": result = backup(args.source, args.dest)
    elif args.operation == "inventory": result = inventory(args.project, args.database, args.hash_asset_id, args.hash_storage)
    elif args.operation == "verify-storage": result = verify_storage(args.project, args.manifest, args.storage_root)
    elif args.operation == "bundle": result = code_bundle(args.project, args.dest)
    elif args.operation == "plan-paths": result = path_plan(args.database, load_mapping(args.mapping))
    else: result = apply_paths(args.source, args.output, load_mapping(args.mapping), args.expected_sha256)
    write_report(args.report, result)
    print(json.dumps({key: result[key] for key in ("status", "destination", "bundle", "output", "sha256", "output_sha256", "update_count", "production_ready", "storage_file_count", "storage_logical_bytes") if key in result}, ensure_ascii=False))
    if result["status"] == "FAIL":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
