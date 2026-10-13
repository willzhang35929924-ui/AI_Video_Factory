import argparse
import json
import sys
import subprocess
import time
from pathlib import Path

from collector_worker import emit, source_metadata, write_json


def verify(client, row, root, timeout, interval):
    identifier = row.get("vidbee_task_id")
    if not identifier:
        return {"status": "FAILED", "error_code": "VIDBEE_TASK_MISSING", "message": "采集结果没有 VidBee task id", "retryable": False}
    deadline = time.monotonic() + timeout
    while True:
        lookup = client.lookup_task(identifier)
        if not lookup.found or lookup.task is None:
            return {"status": "FAILED", "error_code": "VIDBEE_STATUS_UNAVAILABLE", "message": "无法读取关联下载状态", "retryable": True}
        task = lookup.task
        emit("DOWNLOAD", legacy_vidbee_task_id=identifier, download_status=task.status)
        if task.status == "completed":
            source = {**row, "download_path": row.get("download_path") or task.file_path,
                      "download_source_type": "vidbee_automation"}
            if not source["download_path"] or not Path(source["download_path"]).is_file():
                return {"status": "FAILED", "error_code": "OUTPUT_NOT_FOUND", "message": "VidBee 已完成，但原文件不存在", "retryable": False}
            try:
                metadata = source_metadata(source, root, client)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                return {"status": "FAILED", "error_code": "SOURCE_NOT_VERIFIED", "message": type(exc).__name__, "retryable": False}
            return {"status": "READY", "download_status": "completed", "legacy_vidbee_task_id": identifier, "asset": metadata}
        if task.status in {"failed", "cancelled"}:
            return {"status": "FAILED", "error_code": "VIDBEE_DOWNLOAD_FAILED", "message": "VidBee 下载失败；保留原任务记录", "retryable": False}
        if time.monotonic() >= deadline:
            return {"status": "WAITING", "error_code": "DOWNLOAD_PENDING", "message": "下载尚未完成；可继续检查原任务", "retryable": True}
        time.sleep(interval)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--legacy-root", type=Path, required=True)
    parser.add_argument("--health", action="store_true")
    parser.add_argument("--input", type=Path)
    parser.add_argument("--result", type=Path)
    parser.add_argument("--require-completed", action="store_true")
    args = parser.parse_args()
    sys.path.insert(0, str(args.legacy_root))
    from src.config import load_config, resolve_paths
    from src.vidbee_handoff import VidBeeClient
    config = resolve_paths(args.legacy_root, load_config(args.legacy_root / "config.yaml"))
    client = VidBeeClient(config.get("vidbee_automation_descriptor_path") or None, config["vidbee_automation_timeout_s"])
    if args.health:
        try:
            client._health()
            result = {"status": "READY", "message": "本地 VidBee Automation API 在线"}
        except Exception:
            result = {"status": "MISSING", "message": "本地 VidBee Automation API 未连接"}
        print(json.dumps(result, ensure_ascii=False))
        return
    data = json.loads(args.input.read_text())
    result = verify(client, data["row"], args.legacy_root, 0 if args.require_completed else float(config["vidbee_wait_timeout_s"]), max(.2, float(config["vidbee_poll_interval_s"])))
    write_json(args.result, result)


if __name__ == "__main__":
    main()
