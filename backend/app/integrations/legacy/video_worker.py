"""Factory bridge executed only by the existing video_auto interpreter."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

KEY_NAMES = ("VIDEO_AUTO_PROMPT_API_KEY", "VIDEO_AUTO_QUALITY_API_KEY", "VIDEO_AUTO_WAN_API_KEY")
SECRET_VALUES: list[str] = []


def credential_field(name, value):
    name = name.lower()
    return isinstance(value, str) and bool(value) and (name in {"api_key", "token", "secret", "password"}
        or name.endswith(("_api_key", "_token", "_secret", "_password")))


def safe(value):
    if isinstance(value, dict):
        return {key: safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [safe(item) for item in value]
    if isinstance(value, str):
        for secret in sorted(SECRET_VALUES, key=len, reverse=True):
            if secret:
                value = value.replace(secret, "[REDACTED]")
    return value


class RedactedStream:
    def __init__(self, stream):
        self.stream = stream
        self.pending = ""

    def write(self, value):
        self.pending += value
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            self.stream.write(safe(line) + "\n")
        return len(value)

    def flush(self):
        # Do not flush partial lines: a secret can arrive across separate writes.
        self.stream.flush()

    def finish(self):
        if self.pending:
            self.stream.write(safe(self.pending))
            self.pending = ""
        self.stream.flush()


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(json.dumps(safe(value), ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def emit(context: dict, stage: str, message: str, **values) -> None:
    print("FACTORY_EVENT " + json.dumps(safe({"type": "STAGE_PROGRESS", "task_id": context.get("task_id"),
          "stage": stage, "message": message, "at": time.time(), **values}), ensure_ascii=False), flush=True)


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def load_legacy(root: Path) -> dict:
    env_file = root / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.strip().partition("=")
            if separator and key.strip() in KEY_NAMES:
                os.environ[key.strip()] = value.strip().strip("\"'")
    SECRET_VALUES.extend(os.environ[name] for name in KEY_NAMES if os.environ.get(name))
    sys.path.insert(0, str(root))
    from common import load_config
    config = load_config(root / "config.yaml")
    SECRET_VALUES.extend(value for key, value in config.items() if credential_field(key, value))
    return config


def base_result(stage: str, context: dict) -> dict:
    return {"task_id": context.get("task_id"), "context_id": context.get("context_id"), "stage": stage,
            "provider": "video_auto." + stage, "mode": "REAL", "status": "READY", "error_code": None,
            "error_message": None, "retryable": False, "prompts": [], "assets": [], "metadata": {}}


def describe_contracts() -> dict:
    return {"prompt": {"module": "prompts", "function": "prepare_prompts(entry, directory, config, retry=False)",
                       "output": "20 independent prompt records"},
            "prompt_sync": {"module": "tencent_sync", "function": "sync_prompts(entry, prompts, directory, config)",
                            "identity_fields": ["country", "keyword", "video_id", "context_id"]},
            "background": {"module": "chatgpt_rpa", "function": "prepare_background_image(entry, job, directory, media, config)",
                           "lifecycle": ["ticket", "claim", "response", "background.png"]},
            "cutout": {"module": "comfy_client", "function": "ImageWorkflow / ComfyClient.preflight",
                       "execution_boundary": "CUTOUT_OUTPUT_BOUNDARY"},
            "compose": {"module": "run", "function": "render_job(entry, job, directory, workflow, client, config, manual_test)",
                        "execution_boundary": "legacy render_job requires source video and repeats cutout; no PERSON_VIDEO input"}}


def workflow_description(config: dict) -> tuple[dict, dict]:
    path = Path(str(config["workflow_api"])).expanduser().resolve()
    graph = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(graph, dict) or not graph or any(not isinstance(node, dict) or "class_type" not in node for node in graph.values()):
        raise ValueError("configured_workflow_is_not_api_json")
    classes = {str(node_id): node["class_type"] for node_id, node in graph.items()}
    outputs = {node_id: {"class_type": node["class_type"], "images": node.get("inputs", {}).get("images"),
                          "format": node.get("inputs", {}).get("format")}
               for node_id, node in graph.items() if node["class_type"] in {"VHS_VideoCombine", "SaveVideo", "SaveAnimatedPNG", "SaveAnimatedWEBP"}}
    repair = {node_id: node.get("inputs", {}) for node_id, node in graph.items() if node["class_type"] == "VideoAutoMatteRepair"}
    composites = {node_id: node.get("inputs", {}) for node_id, node in graph.items() if node["class_type"] == "LayerUtility: ImageCompositeHandleMask"}
    # Evidence records exact graph connections. No composite output can become a person asset.
    description = {"path": str(path), "sha256": digest(path), "node_count": len(graph), "classes": classes,
                   "repair_nodes": repair, "composite_nodes": composites, "video_outputs": outputs,
                   "person_export_supported": False, "compose_from_person_supported": False,
                   "boundary_code": "CUTOUT_OUTPUT_BOUNDARY",
                   "boundary_reason": "当前 API workflow 仅输出合成视频；修补 RGB/Alpha 未独立导出，旧 render_job 会对每张背景重新抠像。需要另行确认 workflow 的独立导出与纯人像输入契约。"}
    return graph, description


def readiness(config: dict, *, refresh=False) -> dict:
    from chatgpt_rpa import require_rpa
    from comfy_client import ComfyClient, ImageWorkflow
    from prompts import _vision_configuration
    result = base_result("readiness", {})
    checks = []
    required_env = str(config.get("prompt_api_key_env", "VIDEO_AUTO_PROMPT_API_KEY"))
    prompt_api = bool(str(config.get("prompt_api_key", "")).strip() or os.environ.get(required_env))
    endpoint_ok = True
    if config.get("prompt_source", "generated") == "generated" and config.get("prompt_backend", "vision") == "vision":
        try:
            _vision_configuration(config)
        except (ValueError, RuntimeError):
            endpoint_ok = False
        api_ready = prompt_api and endpoint_ok
    else:
        api_ready = True
    checks.append({"name": "AI API", "status": "READY" if api_ready else "MISSING", "ok": api_ready,
                   "model": config.get("prompt_api_model"), "key_environment": required_env,
                   "key_present": prompt_api, "api_called": False})
    try:
        _graph, workflow = workflow_description(config)
        instance = ImageWorkflow(Path(workflow["path"]))
        checks.append({"name": "Configuration", "status": "READY", "ok": True,
                       "workflow_api": workflow["path"], "workflow_sha256": workflow["sha256"], "node_count": workflow["node_count"]})
        try:
            ComfyClient({**config, "request_timeout_s": 3, "read_retries": 1}).preflight(instance)
            checks.append({"name": "ComfyUI", "status": "READY", "ok": True, "method": "GET object_info", "submitted": False})
        except (OSError, ValueError, RuntimeError) as exc:
            checks.append({"name": "ComfyUI", "status": "MISSING", "ok": False, "error": safe(str(exc)), "submitted": False})
        checks.append({"name": "Person Asset", "status": "NOT_CONNECTED", "ok": False,
                       "error_code": "CUTOUT_OUTPUT_BOUNDARY", "message": workflow["boundary_reason"]})
        result["metadata"]["workflow"] = workflow
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        checks.append({"name": "Configuration", "status": "ERROR", "ok": False, "error": safe(str(exc))})
    try:
        queue = require_rpa(config)
        if config.get("_factory_storage_root"):
            from app.integrations.rpa_probe import probe_status, refresh_probe
            status = refresh_probe(Path(config["_factory_storage_root"]), config["rpa_chatgpt_url"],
                                   Path(config["_factory_config_path"]).parent / "shadowbot_chatgpt.py") if refresh else probe_status(Path(config["_factory_storage_root"]))
            checks.append({"name": "RPA", **status})
        else:
            checks.append({"name": "RPA", "status": "WAITING_MANUAL", "ok": False, "configured": True,
                       "queue_dir": str(queue), "queue_exists": queue.is_dir(), "desktop_loop": "UNKNOWN",
                       "message": "现有影刀工作流没有心跳接口，无法由已安装或历史回执确认桌面循环正在运行"})
    except (ValueError, KeyError) as exc:
        checks.append({"name": "RPA", "status": "MISSING", "ok": False, "configured": False, "error": safe(str(exc))})
    result["metadata"].update(checks=checks, contracts=describe_contracts(), business_executed=False,
                              paid_requests=0, config_sha256=digest(Path(config["_factory_config_path"])))
    result["status"] = "READY" if all(check["ok"] for check in checks) else "NOT_CONNECTED"
    result["error_code"] = None if result["status"] == "READY" else "PRODUCTION_DEPENDENCIES_MISSING"
    return result


def snapshot_workflow(context: dict, config: dict, directory: Path) -> dict:
    graph, description = workflow_description(config)
    for node in graph.values():
        for name, value in node.get("inputs", {}).items():
            if credential_field(name, value):
                raise ValueError("workflow_snapshot_contains_secret")
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "workflow-api.json"
    original = Path(description["path"]).read_bytes()
    if target.exists() and target.read_bytes() != original:
        raise ValueError("workflow_snapshot_is_immutable")
    if not target.exists():
        target.write_bytes(original)
    record = {"task_id": context["task_id"], "processing_run_id": context.get("run_id"),
              "workflow_snapshot_id": "wfs-" + hashlib.sha256(str(target).encode()).hexdigest()[:24],
              "prompt_id": None, "path": str(target), "sha256": digest(target), "submitted": False,
              "boundary": description}
    write_json(directory / "snapshot.json", record)
    return record


def person_boundary(stage: str, context: dict, config: dict, directory: Path) -> dict:
    result = base_result(stage, context)
    snapshot = snapshot_workflow(context, config, directory)
    result.update(status="NOT_CONNECTED", error_code="CUTOUT_OUTPUT_BOUNDARY",
                  error_message=snapshot["boundary"]["boundary_reason"], retryable=False)
    result["metadata"] = {"workflow_snapshot": snapshot, "submitted": False, "person_asset_created": False,
                          "compose_from_person_supported": False}
    return result


def entry_for(context: dict) -> dict:
    for field in ("task_id", "project_id", "country", "keyword", "product_id", "context_id", "video_id", "source_video", "source_document", "source_row"):
        if field not in context or context[field] is None or (isinstance(context[field], str) and not context[field].strip()):
            raise ValueError("missing_task_context:" + field)
    source = Path(context["source_video"]).resolve()
    references = [Path(path).resolve() for path in context["product_images"]]
    if not source.is_file() or not source.stat().st_size:
        raise ValueError("source_video_missing")
    if not references or any(not path.is_file() for path in references):
        raise ValueError("product_image_missing")
    return {"task_id": context["task_id"], "project_id": context["project_id"], "country": context["country"],
            "keyword": context["keyword"], "product_id": context["product_id"], "context_id": context["context_id"],
            "video_id": context["video_id"], "source_video": str(source), "reference_images": [str(path) for path in references],
            "source_document": context["source_document"], "source_row": context["source_row"]}


def cache_identity(entry: dict, config: dict) -> str:
    fields = {key: value for key, value in config.items() if key.startswith("prompt_") and not any(
              token in key.lower() for token in ("key", "token", "secret", "password"))}
    values = {"context": entry, "source_hash": digest(Path(entry["source_video"])),
              "reference_hashes": [digest(Path(path)) for path in entry["reference_images"]], "config": fields}
    return hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def prompt_stage(stage: str, context: dict, directory: Path, config: dict) -> dict:
    from prompts import prepare_prompts
    from tencent_sync import read_synced_prompts, sync_prompts
    entry = entry_for(context)
    identity = cache_identity(entry, config)
    cache = directory / "cache" / identity
    cache.mkdir(parents=True, exist_ok=True)
    if stage == "prompt_sync" and not (cache / "prompts.json").is_file():
        raise ValueError("PROMPTS_NOT_READY: sync cannot submit a new paid prompt request")
    result = base_result(stage, context)
    emit(context, stage, "读取或生成当前视频的 20 组提示词")
    # Only the export cache moves to Factory storage; source document and matching rules remain legacy-owned.
    sync_config = {**config, "output_dir": str(cache / "sync_cache")}
    if config.get("prompt_source", "generated") == "tencent_docs":
        prompt_set = read_synced_prompts(entry, cache, sync_config)
    else:
        if context.get("external_budget_required") and not (cache / "prompts.json").is_file() and not (cache / "prompt_request.json").is_file():
            from app.integrations.external_budget import reserve
            reserve(context["external_budget_root"], "prompt", entry["task_id"] + ":" + identity)
        prompt_set = prepare_prompts(entry, cache, config, retry=False)
    jobs = prompt_set.get("jobs", [])
    if len(jobs) != 20 or {job.get("id") for job in jobs} != {f"BG{number:02d}" for number in range(1, 21)}:
        raise ValueError("legacy_prompt_set_must_contain_twenty_distinct_ids")
    for job in jobs:
        path = cache / "records" / (job["id"] + ".json")
        record = {"task_id": entry["task_id"], "context_id": entry["context_id"], "country": entry["country"],
                  "keyword": entry["keyword"], "product_id": entry["product_id"], "video_id": entry["video_id"],
                  "source_document": entry["source_document"], "source_row": entry["source_row"], **job,
                  "status": "READY", "cache_identity": identity}
        if path.exists() and json.loads(path.read_text()) != record:
            raise ValueError("immutable_prompt_record_changed")
        if not path.exists():
            write_json(path, record)
        result["prompts"].append({**record, "path": str(path), "sha256": digest(path)})
    result["metadata"] = {"cache_identity": identity, "method": prompt_set.get("method"), "count": 20,
                          "legacy_prompt_path": str(cache / ("cloud_prompts.json" if prompt_set.get("method") == "tencent_docs" else "prompts.json"))}
    if config.get("prompt_source", "generated") == "generated" and config.get("tencent_sync_enabled"):
        try:
            synced = sync_prompts(entry, prompt_set, cache, sync_config)
            result["metadata"]["tencent_sync"] = synced
            if synced.get("status") != "verified":
                raise RuntimeError("tencent_sync_not_verified")
        except (OSError, RuntimeError, ValueError) as exc:
            result.update(status="WAITING_REVIEW", error_code="TENCENT_SYNC_UNCONFIRMED",
                          error_message=safe(str(exc)), retryable=True)
    emit(context, stage, "提示词已逐条登记", count=len(result["prompts"]), status=result["status"])
    return result


def background_stage(context: dict, directory: Path, config: dict) -> dict:
    from chatgpt_rpa import image_dimensions, prepare_background_image, require_rpa
    from common import probe
    entry = entry_for(context)
    job = context.get("job") or {}
    if not re.fullmatch(r"BG(?:0[1-9]|1[0-9]|20)", str(job.get("id", ""))) or not all(
            isinstance(job.get(field), str) and job[field].strip() for field in ("prompt", "negative_prompt")):
        raise ValueError("background_requires_one_saved_prompt_pair")
    require_rpa(config)
    media = probe(Path(entry["source_video"]))
    identity = hashlib.sha256(json.dumps({"entry": entry, "job": job, "source_sha256": digest(Path(entry["source_video"])),
        "app_id": config["rpa_app_id"], "chatgpt_url": config["rpa_chatgpt_url"],
        "width": media["width"], "height": media["height"]}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if not config.get("_factory_storage_root"):
        raise ValueError("FACTORY_STORAGE_REQUIRED: RPA 不能写入旧 video_auto output")
    task_name = re.sub(r"[^A-Za-z0-9_-]", "_", entry["task_id"])[:100]
    output_root = Path(config["output_dir"]).expanduser().resolve()
    if output_root != Path(config["_factory_storage_root"]).resolve() / "rpa":
        raise ValueError("RPA_OUTPUT_OUTSIDE_FACTORY")
    job_directory = output_root / "backgrounds" / task_name / identity / job["id"]
    job_directory.mkdir(parents=True, exist_ok=True)
    result = base_result("background", context)
    emit(context, "background", "等待桌面背景生成流程", job_id=job["id"])
    if context.get("external_budget_required") and not (job_directory / "background_image_request.json").is_file():
        from app.integrations.external_budget import reserve
        reserve(context["external_budget_root"], "background", entry["task_id"] + ":" + identity + ":" + job["id"])
    image, provenance = prepare_background_image(entry, job, job_directory, media, config)
    image = Path(image).resolve()
    if not image.is_relative_to(job_directory.resolve()) or not image.is_file() or not image.stat().st_size:
        raise ValueError("background_output_not_found")
    width, height = image_dimensions(image)
    sha256 = digest(image)
    if sha256 != provenance.get("image_sha256"):
        raise ValueError("background_output_hash_mismatch")
    result["assets"] = [{"asset_type": "BACKGROUND", "path": str(image), "sha256": sha256,
                         "size": image.stat().st_size, "mime_type": "image/png", "width": width, "height": height,
                         "status": "READY", "prompt_id": job["id"], "source_system": "video_auto",
                         "legacy_reference": {"execution_id": provenance.get("execution_id"), "identity": identity,
                                              "context_id": entry["context_id"], "video_id": entry["video_id"]}}]
    result["metadata"] = {"job_id": job["id"], "execution_id": provenance.get("execution_id"),
                          "request_path": str(job_directory / "background_image_request.json"),
                          "response_path": str(job_directory / "rpa_response.json"), "cache_identity": identity}
    return result


def execute_stage(stage: str, context: dict, directory: Path, config: dict, run_directory: Path | None = None) -> dict:
    if stage == "readiness":
        return readiness(config, refresh=bool(context.get("_refresh_rpa_probe")))
    if stage in {"prompt", "prompt_sync"}:
        return prompt_stage(stage, context, directory, config)
    if stage == "background":
        return background_stage(context, directory, config)
    if stage in {"cutout", "person", "compose"}:
        return person_boundary(stage, context, config, (run_directory or directory) / "workflow")
    raise ValueError("unsupported_video_stage")


def main() -> int:
    parser = argparse.ArgumentParser()
    for argument in ("legacy-root", "stage", "context", "result", "directory"):
        parser.add_argument("--" + argument, required=True)
    parser.add_argument("--factory-storage-root")
    args = parser.parse_args()
    context = json.loads(Path(args.context).read_text(encoding="utf-8"))
    original_stdout, original_stderr = sys.stdout, sys.stderr
    stdout, stderr = RedactedStream(original_stdout), RedactedStream(original_stderr)
    result = base_result(args.stage, context)
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            config = load_legacy(Path(args.legacy_root).resolve())
            config["_factory_config_path"] = str(Path(args.legacy_root).resolve() / "config.yaml")
            if args.factory_storage_root:
                sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
                config["_factory_storage_root"] = str(Path(args.factory_storage_root).resolve())
                config["output_dir"] = Path(config["_factory_storage_root"]) / "rpa"
                config["rpa_queue_dir"] = config["output_dir"] / "rpa_queue"
            result = execute_stage(args.stage, context, Path(args.directory).resolve(), config, Path(args.result).parent)
    except (OSError, ValueError, RuntimeError, KeyError, ImportError) as exc:
        message = safe(str(exc))
        budget = next((code for code in ("EXTERNAL_BUDGET_EXHAUSTED", "EXTERNAL_AUTHORIZATION_REQUIRED") if code in message), None)
        download_pending = args.stage == "background" and "chatgpt_control_not_unique:download:0" in message
        identity_pending = args.stage == "background" and "FACTORY_RPA_IMAGE_IDENTITY_UNCONFIRMED" in message
        unknown = any(marker in message for marker in ("UNKNOWN_EXTERNAL_REQUEST", "request_not_retried", "vision_prompt_api_failed", "response_invalid", "response_unknown", "wait_timeout", "submission_uncertain", "rpa_retry_unconfirmed"))
        result.update(status="WAITING_REVIEW" if unknown or budget or download_pending or identity_pending else "FAILED",
                      error_code=budget or ("BACKGROUND_DOWNLOAD_PENDING" if download_pending else "BACKGROUND_IMAGE_IDENTITY_UNCONFIRMED" if identity_pending else "UNKNOWN_EXTERNAL_REQUEST" if unknown else "LEGACY_STAGE_FAILED"),
                      error_message="背景生成已提交，下载按钮尚未就绪；仅恢复原票据下载，不重发提示词。原对话、请求和失败回执保留。" if download_pending else "已提交背景图身份尚未确认；只读DOM证据已保存，仅恢复原票据下载，不重发提示词。" if identity_pending else message,
                      retryable=False)
    finally:
        stdout.finish()
        stderr.finish()
    write_json(Path(args.result), result)
    print("FACTORY_EVENT " + json.dumps(safe({"type": "STAGE_RESULT", "task_id": context.get("task_id"),
          "stage": args.stage, "status": result["status"], "error_code": result["error_code"]}), ensure_ascii=False), flush=True)
    return 0 if result["status"] == "READY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
