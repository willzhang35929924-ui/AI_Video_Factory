"""Read back the real disposable-family browser deletion; never performs deletion."""
import argparse
import json
import re
import sqlite3
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

from app.integrations.asset_registry import file_hash

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suffix", default="")
    args = parser.parse_args()
    assert not args.suffix or re.fullmatch(r"[a-z0-9-]{1,40}", args.suffix), "Unsafe TEST suffix"
    suffix = "-" + args.suffix if args.suffix else ""
    before = json.loads((ROOT / ("docs/acceptance/closure-family-before" + suffix + ".json")).read_text())
    plan = json.loads((ROOT / ("docs/acceptance/closure-family-plan" + suffix + ".json")).read_text())
    assert Path(before["database"]).resolve() == (ROOT / "backend/delivery-20261009.db").resolve(), "Isolated TEST database only"
    db = sqlite3.connect("file:" + before["database"] + "?mode=ro", uri=True)
    selected = plan["expanded_asset_ids"]
    deleted_paths = {row["path"] for row in plan["files"] if row["action"] == "DELETE"}
    result = {"task_id": before["task_id"], "deleted_asset_ids": selected, "files": [], "api": [], "database": {}}
    for row in before["files"]:
        path = Path(row["path"])
        expected_deleted = str(path) in deleted_paths
        result["files"].append({**row, "expected_deleted": expected_deleted, "exists_after": path.exists(),
            "sha256_after": file_hash(path) if path.exists() else None})
        assert path.exists() != expected_deleted, str(path)
        if not expected_deleted:
            assert file_hash(path) == row["sha256"]
    for identifier in selected:
        for endpoint in ("registry", "content"):
            try:
                with urlopen(f"http://127.0.0.1:8030/api/assets/{identifier}/{endpoint}") as response:
                    status = response.status
            except HTTPError as error:
                status = error.code
            result["api"].append({"asset_id": identifier, "endpoint": endpoint, "http_status": status})
            assert status == 404, (identifier, endpoint, status)
    marks = ",".join("?" for _ in selected)
    for table, column in (("video_assets", "id"), ("asset_identities", "id"), ("reviews", "asset_id"), ("cutout_assets", "asset_id"), ("background_assets", "asset_id"), ("composition_assets", "asset_id")):
        count = db.execute(f"select count(*) from {table} where {column} in ({marks})", selected).fetchone()[0]
        result["database"][table] = count
        assert count == 0
    result["database"]["foreign_key_errors"] = db.execute("pragma foreign_key_check").fetchall()
    assert not result["database"]["foreign_key_errors"]
    assert db.execute("select count(*) from video_assets where id=?", (before["control_id"],)).fetchone()[0] == 1
    context = json.loads(db.execute("select context from tasks where id=?", (before["task_id"],)).fetchone()[0])
    assert context["production"]["asset_family_deleted"]
    result["task_guard"] = context["production"]["asset_family_deleted"]
    audit = db.execute("select id,message from audit_logs where action='asset.family_delete' and entity_id=? order by id desc limit 1", (before["root_asset_id"],)).fetchone()
    assert audit
    result["audit_id"], result["receipt"] = audit[0], json.loads(audit[1])
    result["status"] = "PASS_TEST"
    target = ROOT / ("docs/acceptance/closure-family-after" + suffix + ".json")
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": result["status"], "asset_count": len(selected), "deleted_file_count": len(deleted_paths), "logical_bytes": plan["bytes_to_release"], "audit_id": result["audit_id"], "foreign_key_errors": []}))


if __name__ == "__main__":
    main()
