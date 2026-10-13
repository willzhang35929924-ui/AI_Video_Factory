"""Offline-copy-first identity backfill; no Task/Run/Review/media deletion."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import sqlite3
from datetime import datetime, timezone

from app.integrations.collection_identity import canonical_record, history_groups


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def file_hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def decode(row):
    value = dict(row)
    for key in ("context", "collector_result", "legacy_reference", "probe_metadata"):
        if key in value:
            value[key] = json.loads(value[key] or "{}")
    return value


def rows(connection, table):
    return [dict(row) for row in connection.execute('SELECT * FROM "' + table + '" ORDER BY id')]


def state(connection):
    return {"tasks": [decode(row) for row in rows(connection, "tasks")],
            "assets": [decode(row) for row in rows(connection, "video_assets")],
            "legacy_bindings": rows(connection, "legacy_bindings")}


def snapshot(connection):
    tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    counts, primary_keys, content = {}, {}, {}
    for table in tables:
        columns = connection.execute('PRAGMA table_info("' + table + '")').fetchall()
        keys = [row[1] for row in columns if row[5]]
        values = [dict(row) for row in connection.execute('SELECT * FROM "' + table + '"')]
        counts[table] = len(values)
        primary_keys[table] = digest(sorted(tuple(str(row[key]) for key in keys) for row in values))
        if table == "tasks":
            for row in values:
                context = json.loads(row["context"] or "{}")
                context.pop("collection_identity", None)
                row["context"] = json.dumps(context, sort_keys=True, ensure_ascii=False)
        content[table] = digest(sorted(values, key=lambda row: json.dumps(row, sort_keys=True)))
    return {"counts": counts, "primary_key_digests": primary_keys, "content_digests": content,
            "integrity_check": connection.execute("PRAGMA integrity_check").fetchone()[0],
            "foreign_key_check": [list(row) for row in connection.execute("PRAGMA foreign_key_check")],
            "task_statuses": dict(connection.execute("SELECT status,count(*) FROM tasks GROUP BY status")),
            "active_runs": [dict(row) for row in connection.execute("SELECT id,task_id,status,pid,last_heartbeat FROM processing_runs WHERE status IN ('queued','running')")],
            "active_batches": [dict(row) for row in connection.execute("SELECT id,status,active_task_id FROM production_batches WHERE status IN ('QUEUED','RUNNING','PAUSED')")]}


def make_plan(connection):
    current = state(connection)
    groups, conflicts = history_groups(current)
    identities, mapping, clusters = [], [], []
    for source_key, entries in sorted(groups.items()):
        identity = canonical_record(entries)
        identities.append(identity)
        cluster = {"source_identity_key": source_key, "canonical_task_id": identity["canonical_task_id"], "records": []}
        for task, native in sorted(entries, key=lambda value: (value[0]["created_at"], value[0]["id"])):
            role = "CANONICAL" if task["id"] == identity["canonical_task_id"] else "DUPLICATE_SKIPPED"
            mapping.append({"original_task_id": task["id"], "canonical_task_id": identity["canonical_task_id"],
                            "source_identity_key": source_key, "disposition": role, "original_status": task["status"],
                            "source_video_id": task.get("source_video_id") or ""})
            cluster["records"].append({"task_id": task["id"], "source_video_id": task.get("source_video_id"),
                "country": task["country"], "keyword": task["keyword"], "product_id": task["product_id"],
                "created_at": task["created_at"], "status": task["status"], "decision": task["collector_result"].get("decision"),
                "source_row": task["context"].get("task_context", {}).get("source_row"),
                "context_id": task["context"].get("task_context", {}).get("context_id"), "canonical_url": native["canonical_url"],
                "runs": [dict(row) for row in connection.execute("SELECT id,provider,status,error_code,pid,created_at FROM processing_runs WHERE task_id=? ORDER BY created_at,id", (task["id"],))],
                "reviews": [dict(row) for row in connection.execute("SELECT id,asset_id,status FROM reviews WHERE task_id=? ORDER BY id", (task["id"],))],
                "batches": [row["id"] for row in connection.execute("SELECT id,task_ids FROM production_batches") if task["id"] in json.loads(row["task_ids"])]})
        if len(entries) > 1:
            clusters.append(cluster)
    return {"version": 1, "identities": identities, "mapping": mapping, "duplicate_clusters": clusters,
            "conflicts": conflicts, "unresolved_tasks": len(current["tasks"]) - len(mapping),
            "disposition": "CANONICAL_INDEX_AND_HISTORY_ALIASES_ONLY; PRESERVE_ALL_TASK_STATUS_RUN_REVIEW_ASSET_PUBLISH"}


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("before", "inspect-backup", "apply", "after"))
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--plan", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(args.database.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    checked = snapshot(connection)
    if checked["integrity_check"] != "ok" or checked["foreign_key_check"]:
        raise RuntimeError("DATABASE_INTEGRITY_FAILED")
    if args.phase in {"before", "inspect-backup"}:
        if args.backup is None:
            raise RuntimeError("BACKUP_PATH_REQUIRED")
        if args.phase == "before":
            if args.backup.exists():
                raise RuntimeError("NEW_EXCLUSIVE_BACKUP_PATH_REQUIRED")
            args.backup.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(args.backup) as backup:
                connection.backup(backup)
            args.backup.chmod(0o600)
        elif not args.backup.is_file() or args.backup.is_symlink():
            raise RuntimeError("VERIFIED_EXISTING_BACKUP_REQUIRED")
        with sqlite3.connect(args.backup) as stable:
            stable.row_factory = sqlite3.Row
            checked = snapshot(stable)
            plan = make_plan(stable)
        plan["fingerprint"] = digest(plan)
        checked.update(timestamp=datetime.now(timezone.utc).isoformat(), database=str(args.database.resolve()),
                       backup_path=str(args.backup.resolve()), backup_sha256=file_hash(args.backup),
                       plan_fingerprint=plan["fingerprint"], duplicate_group_count=len(plan["duplicate_clusters"]),
                       canonical_source_count=len(plan["identities"]), duplicate_alias_count=sum(row["disposition"] == "DUPLICATE_SKIPPED" for row in plan["mapping"]))
        save(args.output / "collection-dedupe-before.json", checked)
        save(args.output / "collection-dedupe-plan.json", plan)
        with (args.output / "collection-dedupe-mapping.csv").open("w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(plan["mapping"][0]) if plan["mapping"] else ["original_task_id", "canonical_task_id", "source_identity_key", "disposition"])
            writer.writeheader()
            writer.writerows(plan["mapping"])
        print(json.dumps({key: checked[key] for key in ("backup_path", "backup_sha256", "duplicate_group_count", "canonical_source_count", "duplicate_alias_count")}))
    elif args.phase == "apply":
        if args.plan is None:
            raise RuntimeError("VERIFIED_PLAN_REQUIRED")
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
        fingerprint = plan.pop("fingerprint")
        if digest(plan) != fingerprint:
            raise RuntimeError("PLAN_FINGERPRINT_CHANGED")
        connection.close()
        with sqlite3.connect(args.database, timeout=30) as target:
            target.row_factory = sqlite3.Row
            target.execute("PRAGMA foreign_keys=ON")
            target.execute("BEGIN IMMEDIATE")
            current = snapshot(target)
            if current["active_runs"] or any(row["status"] in {"QUEUED", "RUNNING"} for row in current["active_batches"]):
                raise RuntimeError("ACTIVE_PRODUCTION_BLOCKS_BACKFILL")
            if digest(make_plan(target)) != fingerprint:
                raise RuntimeError("HISTORY_CHANGED_REPLAN_REQUIRED")
            for identity in plan["identities"]:
                target.execute("INSERT INTO collection_identities(id,platform,native_video_id,canonical_task_id,canonical_url,confidence,first_seen_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
                    tuple(identity[key] for key in ("id", "platform", "native_video_id", "canonical_task_id", "canonical_url", "confidence", "first_seen_at")))
                owner = target.execute("SELECT canonical_task_id FROM collection_identities WHERE id=?", (identity["id"],)).fetchone()[0]
                if owner != identity["canonical_task_id"]:
                    raise RuntimeError("CANONICAL_OWNER_CONFLICT")
            for row in plan["mapping"]:
                context = json.loads(target.execute("SELECT context FROM tasks WHERE id=?", (row["original_task_id"],)).fetchone()[0])
                context["collection_identity"] = {"source_identity_key": row["source_identity_key"], "canonical_task_id": row["canonical_task_id"], "status": row["disposition"]}
                target.execute("UPDATE tasks SET context=? WHERE id=?", (json.dumps(context, ensure_ascii=False), row["original_task_id"]))
            audit_id = "audit-dedupe-" + fingerprint[:24]
            target.execute("INSERT INTO audit_logs(id,task_id,action,entity_id,message,created_at) VALUES (?,NULL,'collector.identity_backfill',?,?,?) ON CONFLICT(id) DO NOTHING",
                (audit_id, fingerprint, json.dumps({"canonical": len(plan["identities"]), "aliases": sum(row["disposition"] == "DUPLICATE_SKIPPED" for row in plan["mapping"]), "history_preserved": True}), datetime.now(timezone.utc).isoformat()))
            if target.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("BACKFILL_FOREIGN_KEY_FAILED")
        print(json.dumps({"status": "APPLIED", "plan_fingerprint": fingerprint, "canonical_sources": len(plan["identities"]), "task_status_changes": 0, "deleted_records": 0, "paid_requests": 0}))
    else:
        identities = [dict(row) for row in connection.execute("SELECT * FROM collection_identities ORDER BY id")]
        checked.update(timestamp=datetime.now(timezone.utc).isoformat(), database=str(args.database.resolve()),
            canonical_source_count=len(identities), identity_digest=digest(identities),
            duplicate_alias_count=connection.execute("SELECT count(*) FROM tasks WHERE json_extract(context,'$.collection_identity.status')='DUPLICATE_SKIPPED'").fetchone()[0],
            unique_conflicts=[list(row) for row in connection.execute("SELECT platform,native_video_id,count(*) FROM collection_identities GROUP BY platform,native_video_id HAVING count(*)>1")])
        save(args.output / "collection-dedupe-after.json", checked)
        print(json.dumps({key: checked[key] for key in ("canonical_source_count", "duplicate_alias_count", "unique_conflicts", "integrity_check", "foreign_key_check")}))
    connection.close()


if __name__ == "__main__":
    main()
