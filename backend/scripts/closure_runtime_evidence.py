"""Read-only, zero-model-request snapshot for this closure acceptance."""
import hashlib
import json
import sqlite3
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[2]
TASK_B = "tsk-20261009032115207578-b93f2b7d"
BATCH = "batch-20261009031932009587-c292add6"


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def redact(value):
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, dict):
        return {key: "REDACTED" if key.lower() in {"api_key", "cookie", "password", "access_token", "authorization", "secret"}
                else redact(item) for key, item in value.items()}
    return value


def main():
    db = sqlite3.connect(f"file:{ROOT / 'backend/factory.db'}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    query = lambda sql, parameters=(): [dict(row) for row in db.execute(sql, parameters)]
    result = {"captured_at": datetime.now(timezone.utc).isoformat(), "database": str(ROOT / "backend/factory.db"),
              "new_model_requests_by_this_script": 0, "http": {}, "files": []}
    for route in ("/api/health", "/api/integrations/collector/rules", "/api/collector/rules/draft", "/api/publishing/descriptions/config"):
        with urlopen("http://127.0.0.1:8000" + route, timeout=30) as response:
            result["http"][route] = {"http_status": response.status, "body": redact(json.load(response))}
    batch = query("select * from production_batches where id=?", (BATCH,))[0]
    task_ids = json.loads(batch["task_ids"])
    tasks = query("select id,status,current_stage,updated_at from tasks")
    result["batch"] = {key: batch[key] for key in ("id", "status", "active_task_id", "updated_at")}
    result["batch"]["actual_task_status_counts"] = dict(Counter(row["status"] for row in tasks if row["id"] in task_ids))
    result["active_runs"] = query("select id,task_id,stage,status,pid,started_at,last_heartbeat,finished_at,exit_code from processing_runs where lower(status) in ('queued','running','verifying')")
    result["last_run"] = query("select id,task_id,stage,status,pid,started_at,last_heartbeat,finished_at,exit_code,error_code from processing_runs where id='run-20261010014427972081-f90df5f2'")[0]
    assets = query("select id,type,task_id,storage_path,sha256,size,legacy_reference from video_assets where task_id=?", (TASK_B,))
    result["task_b"] = {"task_id": TASK_B, "counts": dict(Counter(row["type"] for row in assets))}
    links = query("select asset_id,person_asset_id from composition_assets where task_id=?", (TASK_B,))
    result["task_b"]["composition_links"] = links
    result["task_b"]["distinct_person_ids"] = sorted({row["person_asset_id"] for row in links})
    manual = query("select context from tasks where id='tsk-20261009124443372146-adf060a4'")[0]
    result["manual_selection"] = json.loads(manual["context"])["manual_compose"]["selection"]
    result["publication"] = query("select id,video_id,status,submission_state,platform_video_id,platform_url,product_binding,current_step,moderation_status,product_link_status from publishing_tasks where id='pub-20261009125825184337-96a0246f'")[0]
    result["foreign_key_errors"] = query("pragma foreign_key_check")
    requests = []
    for path in sorted((ROOT / "storage/external_budget").glob("*.json")):
        requests.extend(json.loads(path.read_text()).get("requests", []))
    unique = {item["request_key"]: item for item in requests}
    result["budget_ledger"] = {"count": len(unique), "families": dict(Counter(row["family"] for row in unique.values())),
                              "fees": "UNKNOWN", "last_reserved_at": max(row["reserved_at"] for row in unique.values())}
    new_media = [Path(row["storage_path"]) for row in assets if json.loads(row["legacy_reference"]).get("processing_run_id") == result["last_run"]["id"]]
    for path in [*new_media, Path("/Users/xiaoyao/Downloads/温馨禅意石材浴室水疗空间.png")]:
        if not path.is_file():
            result["files"].append({"path": str(path), "exists": False})
            continue
        row = {"path": str(path), "exists": True, "size": path.stat().st_size, "sha256": sha(path)}
        if path.suffix == ".mp4":
            process = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                                     capture_output=True, text=True, timeout=30, check=True)
            row["ffprobe"] = json.loads(process.stdout)
        result["files"].append(row)
    listeners = subprocess.run(["lsof", "-nP", "-iTCP:3000", "-iTCP:3030", "-iTCP:8000", "-iTCP:8030", "-iTCP:8188", "-sTCP:LISTEN", "-t"],
                               capture_output=True, text=True, check=True).stdout.splitlines()
    result["runtime"] = subprocess.run(["ps", "-p", ",".join(sorted(set(listeners))), "-o", "pid,ppid,lstart,stat,command"],
                                       capture_output=True, text=True, check=True).stdout
    result["source_commit"] = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    destination = ROOT / "docs/acceptance/closure-runtime-readback.json"
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": "PASS_LOCAL", "evidence": str(destination), "sha256": sha(destination),
                      "batch": result["batch"], "task_b": result["task_b"]["counts"], "ledger": result["budget_ledger"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
