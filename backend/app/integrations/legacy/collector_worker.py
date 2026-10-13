"""Single-context bridge, executed by the existing TikTok_Auto Python environment."""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import re
import subprocess
import sys
import time
from io import BytesIO
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from app.integrations.production_rules import factory_metric_gate, factory_presence_gate, verified_factory_rules
from app.integrations.external_budget import ExternalBudgetExceeded, reserve
from app.integrations.file_lock import lock_file
from app.integrations.legacy.browser_client import native_browser_class

KEY_NAMES = ("TIKTOK_AUTO_PRODUCT_VISION_API_KEY", "TIKTOK_AUTO_SEMANTIC_VISION_API_KEY")
DOWNLOAD_PHASES = {"download_wait", "download_verifying"}
SEMANTIC_PROTOCOL = "factory-semantic-enums-v1"
SEMANTIC_ENUM_GUIDANCE = (
    "响应协议 factory-semantic-enums-v1：只返回 JSON，不返回 Markdown。"
    "product_match.verdict 只能是 pass、fail、uncertain；"
    "ai_origin.verdict 只能是 likely_real、likely_ai、uncertain；"
    "cutout_suitability.verdict 只能是 pass、fail、uncertain。"
    "三个对象都必须包含 verdict、confidence、reason；confidence 是 0 到 1 的数字，reason 是非空字符串。"
    "不确定时使用 uncertain，不得把 true/false、match、real 或其它描述作为 verdict。"
)


class FactoryRuleGate(Exception):
    pass


def observation_provenance(row, legacy_root, mode, *, observed_at=None, checkpoint_path=None, field="metrics_provenance"):
    prior = row.get(field) or {}
    checkpoint = (prior.get("original_checkpoint") if mode != "fresh" else None) or {"checkpoint_key": row.get("checkpoint_key") or row.get("legacy_result_reference", {}).get("checkpoint_key"),
        "legacy_csv_path": str(legacy_root / "output" / "candidates.csv"),
        "processed_at": row.get("processed_at"), "screening_config_fingerprint": row.get("screening_config_fingerprint")}
    if checkpoint_path and not checkpoint.get("raw_result_path"):
        checkpoint = {**checkpoint, "raw_result_path": str(checkpoint_path), "run_id": Path(checkpoint_path).parent.name}
    return {"mode": mode, "observed_at": observed_at if mode == "fresh" else prior.get("observed_at"),
        "reused_at": datetime.now(timezone.utc).isoformat() if mode == "cached" else None,
        "original_checkpoint": checkpoint}


def regate_cached_row(row, snapshot):
    row = deepcopy(row)
    if snapshot is None or unknown_request(row):
        return row
    rules = verified_factory_rules(snapshot)
    row.pop("factory_gate", None)
    if row.get("likes") in (None, "") or row.get("views") in (None, ""):
        if row.get("decision") == "accepted":
            row.update(decision="manual_review", reason="factory_metrics_unverified", error="")
            row["factory_gate"] = {"decision": row["decision"], "reason": row["reason"], "rules_sha256": snapshot["sha256"]}
        return row
    try:
        likes, views = int(row["likes"]), int(row["views"])
        if min(likes, views) < 0:
            raise ValueError("negative metrics")
    except (TypeError, ValueError):
        gate = ("manual_review", "factory_metrics_unverified")
    else:
        gate = factory_metric_gate(rules, likes, views) or factory_presence_gate(rules, row)
    if not gate and row.get("decision") == "accepted":
        confidence = row.get("reference_product_confidence")
        if confidence in (None, ""):
            confidence = row.get("product_match_confidence")
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            gate = ("manual_review", "factory_product_confidence_unverified")
        else:
            if not 0 <= confidence <= 1:
                gate = ("manual_review", "factory_product_confidence_unverified")
            elif confidence < rules["product_match_min_confidence"]:
                gate = ("rejected", "factory_product_match_threshold")
    if gate:
        row.update(decision=gate[0], reason=gate[1], error="")
        row["factory_gate"] = {"decision": gate[0], "reason": gate[1], "rules_sha256": snapshot["sha256"]}
    return row


def cached_observations(row, legacy_root, checkpoint_path=None):
    row = deepcopy(row)
    for field in ("metrics_provenance", "reference_provenance", "semantic_provenance"):
        evidence = (row.get("likes") not in (None, "") and row.get("views") not in (None, "")) if field == "metrics_provenance" else (
            bool(row.get("analysis_status") or row.get("analysis_report")) if field == "semantic_provenance" else
            row.get("reference_product_match") in {"pass", "fail"})
        row[field] = observation_provenance(row, legacy_root, "cached" if evidence else "unknown", checkpoint_path=checkpoint_path, field=field)
    return row


def emit_metrics(row):
    if row.get("likes") not in (None, "") and row.get("views") not in (None, ""):
        emit("METRICS_UPDATED", type="METRICS_UPDATED", likes=int(row["likes"]), views=int(row["views"]),
             metric_source=row.get("metric_source"), metric_evidence=row.get("metric_evidence"),
             metrics_provenance=row.get("metrics_provenance"))


def record_analysis_observations(row, observations, legacy_root):
    for field in ("reference_provenance", "semantic_provenance"):
        if field in observations:
            row[field] = observation_provenance(row, legacy_root, "fresh", observed_at=observations[field], field=field)


def single_task_pipeline(base, legacy_root: Path, snapshot: dict | None):
    rules = verified_factory_rules(snapshot) if snapshot else None

    class FactoryPipeline(base):
        def _set_metrics(self, row, metrics):
            super()._set_metrics(row, metrics)
            self.factory_metrics_observed_at = datetime.now(timezone.utc).isoformat()
            row["metrics_provenance"] = observation_provenance(row, legacy_root, "fresh", observed_at=self.factory_metrics_observed_at)
            gate = (factory_metric_gate(rules, metrics.likes, metrics.views) or factory_presence_gate(rules, row)) if rules else None
            if gate:
                row["factory_gate"] = {"decision": gate[0], "reason": gate[1], "rules_sha256": snapshot["sha256"]}
                raise FactoryRuleGate(gate[1])

        def process_hit(self, browser, spec, hit, checkpoint):
            self.factory_metrics_observed_at = None
            key = self._checkpoint_key(spec.keyword, hit.video_id, spec.context_id) if spec is not None and hit is not None else None
            previous = checkpoint.get(key)
            copied = dict(checkpoint)
            if previous:
                copied[key] = deepcopy(previous)
                gated = regate_cached_row(previous, snapshot)
                if gated.get("factory_gate"):
                    row = gated
                else:
                    row = super().process_hit(browser, spec, hit, copied)
            else:
                row = super().process_hit(browser, spec, hit, copied)
            row = deepcopy(row)
            gate = row.get("factory_gate") if self.factory_metrics_observed_at else None
            if gate:
                row.update(decision=gate["decision"], reason=gate["reason"], error="")
                if gate["reason"] == "factory_detection_unverified":
                    row["error"] = "贴片/水印/字幕要求缺少真实识别证据；UNKNOWN，等待人工处理，未调用产品分析或提交下载"
            if not self.factory_metrics_observed_at and previous:
                row = regate_cached_row(row, snapshot)
            if row.get("likes") not in (None, "") and row.get("views") not in (None, ""):
                row["metrics_provenance"] = observation_provenance(row if self.factory_metrics_observed_at else previous or row, legacy_root,
                    "fresh" if self.factory_metrics_observed_at else "cached" if previous else "unknown",
                    observed_at=self.factory_metrics_observed_at)
            analysis_origin = cached_observations(previous or {}, legacy_root)
            for field in ("reference_provenance", "semantic_provenance"):
                row[field] = analysis_origin[field]
            return row

    pipeline = FactoryPipeline(legacy_root, legacy_root / "config.yaml")
    if rules:
        pipeline.config.update({key: rules[key] for key in ("min_views", "min_likes", "min_like_view_ratio", "product_match_min_confidence")})
        pipeline._screening_config_fingerprint = hashlib.sha256((pipeline._config_fingerprint() + snapshot["sha256"]).encode()).hexdigest()
    return pipeline


@contextmanager
def factory_sidecars(directory: Path):
    """Keep the legacy functions, but save their sidecars only in this Factory run."""
    module = sys.modules["src.pipeline"]
    originals = {name: getattr(module, name) for name in ("write_download_metadata", "save_analysis") if hasattr(module, name)}
    directory.mkdir(parents=True, exist_ok=True)

    def download_metadata(path, **values):
        target = directory / "source.mp4"
        originals["write_download_metadata"](target, **values)
        metadata = json.loads(target.with_suffix(".json").read_text(encoding="utf-8"))
        write_json(target.with_suffix(".json"), {**metadata, "path": str(path)})

    def analysis(_path, value):
        originals["save_analysis"](directory / "source.analysis.json", value)

    if "write_download_metadata" in originals:
        module.write_download_metadata = download_metadata
    if "save_analysis" in originals:
        module.save_analysis = analysis
    try:
        yield
    finally:
        for name, value in originals.items():
            setattr(module, name, value)


@contextmanager
def factory_paid_requests(storage_root: Path, task_id: str, outcome: dict, *, evidence_dir: Path | None = None):
    pipeline = sys.modules["src.pipeline"]
    analysis = sys.modules.get("src.video_analysis")
    observations = {}
    if analysis is None:
        yield observations
        return
    match, semantic = pipeline.match_product_references, analysis._semantic_open

    def debit(request_key):
        try:
            record = reserve(storage_root, "vision", request_key)
            emit("PAID_REQUEST_RESERVED", family="vision", request_key=request_key, reserved_at=record["reserved_at"])
        except ExternalBudgetExceeded as exc:
            code = str(exc).split(":", 1)[0]
            message = "此视觉请求已预留额度且结果未知；不重复提交，等待核实原请求" if code == "UNKNOWN_EXTERNAL_REQUEST" else "视觉API额度未获授权或已用尽；未提交此请求，已完成指标和其它任务保留"
            outcome.update(error_code=code, error_message=message)
            raise

    def guarded_match(keyword, references, previews, config):
        configured = analysis._api_configuration(config) is not None
        request_key = None
        if configured:
            identity = {"keyword": keyword, "model": config.get("product_match_api_model"), "endpoint": config.get("product_match_api_base_url"),
                        "images": [hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in (*references, *previews)]}
            request_key = task_id + ":reference:" + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
            debit(request_key)
        if configured and evidence_dir is not None:
            opener = analysis.urllib.request.urlopen
            def captured_product(request, timeout):
                return open_with_receipt(opener, request, timeout, "product", request_key)
            analysis.urllib.request.urlopen = captured_product
            try:
                result = match(keyword, references, previews, config)
            finally:
                analysis.urllib.request.urlopen = opener
        else:
            result = match(keyword, references, previews, config)
        if configured:
            observations["reference_provenance"] = datetime.now(timezone.utc).isoformat()
        return result

    def save_receipt(response, body, request, kind, request_key, *, http_error=False):
        if evidence_dir is None:
            return
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        choices = payload.get("choices")
        message = choices[0].get("message") if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict) and isinstance(part.get("text"), str))
        secrets = {value for name, value in os.environ.items() if value and any(word in name.casefold() for word in ("key", "token", "secret", "password", "cookie"))}
        for header in ("Authorization", "Cookie"):
            value = request.get_header(header)
            if value:
                secrets.add(value)
                if header == "Authorization":
                    secrets.add(value.split(" ", 1)[-1])
                else:
                    secrets.update(part.partition("=")[2].strip() for part in value.split(";") if "=" in part and part.partition("=")[2].strip())

        def redact(value):
            if isinstance(value, dict):
                return {key: "[REDACTED]" if re.search(r"(?:api.?key|token|secret|password|cookie|authorization)", key, re.I) else redact(item) for key, item in value.items()}
            if isinstance(value, list):
                return [redact(item) for item in value]
            if isinstance(value, str):
                for secret in sorted(secrets, key=len, reverse=True):
                    value = value.replace(secret, "[REDACTED]")
                value = re.sub(r"\bBearer\s+[^\s\"']+", "Bearer [REDACTED]", value, flags=re.I)
                return re.sub(r"data:image/[^;,\s]+;base64,[A-Za-z0-9+/=]+", "[REDACTED_IMAGE]", value)
            return value

        if isinstance(content, str):
            try:
                content = json.dumps(redact(json.loads(content)), ensure_ascii=False)
            except ValueError:
                content = redact(content)
        else:
            content = None
        usage_fields = {"prompt_tokens", "completion_tokens", "total_tokens", "input_tokens", "output_tokens", "cached_tokens",
            "cache_read_input_tokens", "cache_creation_input_tokens", "text_tokens", "image_tokens", "video_tokens", "audio_tokens",
            "reasoning_tokens", "accepted_prediction_tokens", "rejected_prediction_tokens"}
        detail_fields = {"prompt_tokens_details", "completion_tokens_details", "input_tokens_details", "output_tokens_details"}
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        counters = lambda values: {key: value for key, value in values.items() if key in usage_fields and isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0}
        usage_receipt = counters(usage)
        for key in detail_fields:
            if isinstance(usage.get(key), dict):
                usage_receipt[key] = counters(usage[key])
        headers = getattr(response, "headers", None)
        response_id = payload.get("id")
        request_id = payload.get("request_id") or payload.get("requestId")
        if not isinstance(request_id, str) and headers is not None:
            request_id = next((headers.get(name) for name in ("x-request-id", "request-id", "x-dashscope-request-id") if headers.get(name)), None)
        status = getattr(response, "status", None) or getattr(response, "code", None)
        request_hash = hashlib.sha256(request.data or b"").hexdigest()
        receipt = {"task_id": task_id, "request_key": request_key,
            "request_sha256": request_hash, "response_sha256": hashlib.sha256(body).hexdigest(),
            "received_at": datetime.now(timezone.utc).isoformat(),
            "http_status": status if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599 else None,
            "provider_response_id": redact(response_id[:256]) if isinstance(response_id, str) else None,
            "provider_request_id": redact(request_id[:256]) if isinstance(request_id, str) else None,
            "usage": usage_receipt, "status": "http_error" if http_error else "content_saved" if content is not None else "content_unavailable",
            "content": content, "validation": "legacy_strict_parser", "normalized": False}
        if kind == "semantic":
            receipt.update(protocol=SEMANTIC_PROTOCOL, protocol_sha256=hashlib.sha256(SEMANTIC_ENUM_GUIDANCE.encode()).hexdigest())
        try:
            evidence_dir.mkdir(parents=True, exist_ok=True)
            write_json(evidence_dir / (kind + "-response-" + request_hash + ".json"), receipt)
        except OSError as exc:
            emit("PAID_REQUEST_EVIDENCE_FAILED", family="vision", request_key=request_key, error_code=type(exc).__name__)

    @contextmanager
    def captured_response(response, request, kind, request_key):
        with response as incoming:
            body = incoming.read()
            save_receipt(incoming, body, request, kind, request_key)
        with BytesIO(body) as buffered:
            yield buffered

    def open_with_receipt(opener, request, timeout, kind, request_key):
        try:
            response = opener(request, timeout=timeout)
        except HTTPError as exc:
            body = exc.read()
            save_receipt(exc, body, request, kind, request_key, http_error=True)
            raise HTTPError(exc.url, exc.code, exc.msg, exc.headers, BytesIO(body)) from exc
        return captured_response(response, request, kind, request_key)

    def guarded_semantic(request, timeout):
        # The legacy semantic helper reaches this only after its persisted-cache check.
        payload = json.loads((request.data or b"").decode("utf-8"))
        messages = payload["messages"]
        system = next((item for item in messages if item.get("role") == "system" and isinstance(item.get("content"), str)), None)
        if system is None:
            messages.insert(0, {"role": "system", "content": SEMANTIC_ENUM_GUIDANCE})
        elif SEMANTIC_ENUM_GUIDANCE not in system["content"]:
            system["content"] += "\n" + SEMANTIC_ENUM_GUIDANCE
        request = Request(request.full_url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={name: value for name, value in request.header_items() if name.casefold() != "content-length"},
            origin_req_host=request.origin_req_host, unverifiable=request.unverifiable, method=request.get_method())
        request_hash = hashlib.sha256(request.data).hexdigest()
        request_key = task_id + ":semantic:" + request_hash
        debit(request_key)
        result = open_with_receipt(semantic, request, timeout, "semantic", request_key)
        observations["semantic_provenance"] = datetime.now(timezone.utc).isoformat()
        return result

    pipeline.match_product_references, analysis._semantic_open = guarded_match, guarded_semantic
    try:
        yield observations
    finally:
        pipeline.match_product_references, analysis._semantic_open = match, semantic


class LegacyOutputProtected(RuntimeError):
    pass


class LegacyOutputGuard:
    """Protect existing legacy files while allowing its merged CSV checkpoint writes."""
    def __init__(self, config: dict, root: Path):
        self.roots = tuple(Path(config.get(key, root / name)).resolve() for key, name in (
            ("downloads_dir", "downloads"), ("data_dir", "data"), ("output_dir", "output"), ("log_dir", "logs")))
        self.output_root = self.roots[2]
        self.log_root = self.roots[3]
        self.csv_paths = {self.output_root / name for name in ("candidates.csv", "accepted.csv", "manual_review.csv", "rejected.csv")}
        self.csv_parts = {path.with_name("." + path.name + ".part") for path in self.csv_paths}
        self.blocked_operations: list[dict] = []
        self.active = False
        sys.addaudithook(self._check)

    def __enter__(self):
        self.active = True
        return self

    def __exit__(self, *_args):
        self.active = False

    def _path(self, value):
        if isinstance(value, int):
            return None
        return Path(os.fsdecode(value)).absolute()

    def _protected(self, path: Path | None) -> bool:
        return path is not None and any(path.is_relative_to(root) or path.resolve().is_relative_to(root) for root in self.roots)

    def _deny(self, event: str, path: Path) -> None:
        self.blocked_operations.append({"operation": event, "path": str(path)})
        raise LegacyOutputProtected("LEGACY_OUTPUT_PROTECTED: " + str(path))

    def _check(self, event: str, arguments: tuple) -> None:
        if not self.active:
            return
        if event == "open":
            path = self._path(arguments[0])
            flags = arguments[2]
            writing = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_TRUNC | os.O_APPEND))
            if not writing or not self._protected(path) or not (path.exists() or path.is_symlink()):
                return
            if path in self.csv_parts and not path.is_symlink():
                return
            if path.is_relative_to(self.log_root) and flags & os.O_APPEND and not flags & os.O_TRUNC:
                return
            self._deny(event, path)
        elif event in {"os.remove", "os.rmdir", "os.truncate"}:
            path = self._path(arguments[0])
            if self._protected(path):
                self._deny(event, path)
        elif event == "os.rename":
            source, target = self._path(arguments[0]), self._path(arguments[1])
            if (source in self.csv_parts and target in self.csv_paths and
                    source.name == "." + target.name + ".part" and not source.is_symlink() and not target.is_symlink()):
                return
            if self._protected(source) or self._protected(target):
                self._deny(event, source if self._protected(source) else target)


def safe(value):
    if isinstance(value, dict):
        return {key: safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [safe(item) for item in value]
    if isinstance(value, str):
        for name in KEY_NAMES:
            secret = os.environ.get(name, "")
            if secret:
                value = value.replace(secret, "[REDACTED]")
    return value


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(json.dumps(safe(value), ensure_ascii=False, default=str), encoding="utf-8")
    temporary.replace(path)


def emit(stage: str, **values) -> None:
    print("FACTORY_EVENT " + json.dumps(safe({"type": "COLLECTOR_PROGRESS", "stage": stage, "at": time.time(), **values}), ensure_ascii=False), flush=True)


def source_metadata(row: dict, legacy_root: Path, vidbee_client=None) -> dict | None:
    value = row.get("download_path")
    if not value:
        return None
    path = Path(value).resolve()
    if not path.is_file() or not path.stat().st_size:
        raise FileNotFoundError("completed legacy source video is missing or empty")
    if not path.is_relative_to((legacy_root / "downloads").resolve()):
        raise ValueError("legacy source video is outside downloads")
    task = None
    download_proof = None
    expected_hash = row.get("download_source_sha256")
    # The legacy row and VidBee normalizers stringify an absent SHA as "None".
    missing_legacy_hash = expected_hash == "None"
    if missing_legacy_hash:
        expected_hash = None
    if row.get("download_source_type") == "vidbee_automation" or row.get("vidbee_task_id"):
        identifier = str(row.get("vidbee_task_id") or "")
        if not identifier:
            raise ValueError("source has no associated VidBee task ID")
        if vidbee_client is None:
            sys.path.insert(0, str(legacy_root))
            from src.config import load_config, resolve_paths
            from src.vidbee_handoff import VidBeeClient
            config = resolve_paths(legacy_root, load_config(legacy_root / "config.yaml"))
            vidbee_client = VidBeeClient(config.get("vidbee_automation_descriptor_path") or None, float(config["vidbee_automation_timeout_s"]))
        lookup = vidbee_client.lookup_task(identifier)
        task = lookup.task
        if not lookup.found or task is None or task.task_id != identifier or task.status != "completed":
            raise ValueError("VidBee lookup did not verify a completed source task")
        same_file = False
        task_path = Path(task.file_path).resolve() if task.file_path else None
        if task_path is not None and task_path.is_file():
            same_file = os.path.samefile(path, task_path)
        task_hash = None if task.sha256 == "None" else task.sha256
        if not same_file and not (task_hash or expected_hash):
            raise ValueError("source file cannot be linked to the completed VidBee task")
        download_proof = {"task_id": identifier, "status": task.status, "lookup_source": lookup.reason,
                          "association": "same_file" if same_file else "sha256",
                          "legacy_sha256": "missing_none_sentinel" if missing_legacy_hash else "provided" if expected_hash else "missing",
                          "vidbee_sha256": "missing_none_sentinel" if task.sha256 == "None" else "provided" if task_hash else "missing",
                          "vidbee_sha256_provided": bool(task_hash)}
    else:
        raise ValueError("source has no associated VidBee task ID")
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=codec_name,width,height,r_frame_rate,avg_frame_rate,time_base,nb_frames,duration:format=duration", "-of", "json", str(path)],
        check=True, capture_output=True, text=True, timeout=30,
    )
    data = json.loads(probe.stdout)
    streams = data.get("streams") or []
    if not streams:
        raise ValueError("ffprobe did not verify a video stream")
    stream = streams[0]
    rate = stream.get("avg_frame_rate")
    if not rate or rate == "0/0":
        rate = stream.get("r_frame_rate")
    fps = float(Fraction(rate)) if rate and rate != "0/0" else None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    sha256 = digest.hexdigest()
    if expected_hash and expected_hash != sha256:
        raise ValueError("legacy source SHA256 does not match the completed download")
    if task is not None and task_hash and task_hash != sha256:
        raise ValueError("source SHA256 does not match the verified VidBee task")
    frame_count = stream.get("nb_frames")
    duration = stream.get("duration") or data.get("format", {}).get("duration")
    if not fps or fps <= 0 or not duration or float(duration) <= 0 or not stream.get("codec_name") or not stream.get("width") or not stream.get("height"):
        raise ValueError("ffprobe returned incomplete video dimensions, duration, FPS or codec")
    return {"path": str(path), "original_source_video": row.get("original_source_video") or str(path), "verified": True,
            "sha256": sha256, "file_size": path.stat().st_size, "width": stream.get("width"), "height": stream.get("height"),
            "fps": fps, "r_frame_rate": stream.get("r_frame_rate"), "avg_frame_rate": stream.get("avg_frame_rate"),
            "time_base": stream.get("time_base"), "frame_count": int(frame_count) if frame_count and frame_count.isdigit() else None,
            "duration": float(duration) if duration else None, "codec": stream.get("codec_name"),
            "video_id": row.get("video_id"), "video_url": row.get("url"), "context_id": row.get("context_id"),
            "source_type": row.get("download_source_type"), "watermark_attestation": row.get("download_watermark_attestation"),
            "download_status": task.status if task is not None else "completed", "download_verification": download_proof}


def collector_result(context: dict, row: dict, legacy_root: Path, result_path: Path, *, error_code: str = "", error_message: str = "", vidbee_client=None) -> dict:
    def number(key):
        value = row.get(key)
        return float(value) if value not in (None, "") else None

    likes, views = number("likes"), number("views")
    source = None
    if row.get("download_path"):
        try:
            source = source_metadata(row, legacy_root, vidbee_client)
        except FileNotFoundError as exc:
            if not error_code:
                error_code, error_message = "OUTPUT_NOT_FOUND", str(exc)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            if not error_code:
                error_code, error_message = "SOURCE_NOT_VERIFIED", str(exc)
    elif row.get("decision") == "accepted":
        error_code, error_message = "SOURCE_NOT_VERIFIED", "旧结果没有可验证原始视频"
    if not error_code and row.get("reason") == "vidbee_download_pending" and row.get("vidbee_task_id"):
        error_code, error_message = "DOWNLOAD_PENDING", "原 VidBee 任务仍在等待；只能恢复该下载，不重新采集"
    if error_code:
        status = "WAITING_REVIEW" if error_code in {"UNKNOWN_EXTERNAL_REQUEST", "LOGIN_REQUIRED", "CAPTCHA_REQUIRED", "DOWNLOAD_PENDING", "RECOVERY_CONFLICT"} else "FAILED"
    else:
        status = "WAITING_REVIEW" if row.get("decision") == "manual_review" else "COMPLETED" if row.get("decision") == "accepted" else "REJECTED"
    product_confidence = number("product_match_confidence")
    if product_confidence is None:
        product_confidence = number("reference_product_confidence")
    provenance = {}
    for field in ("metrics_provenance", "reference_provenance", "semantic_provenance"):
        value = row.get(field)
        provenance[field] = observation_provenance(row, legacy_root, value["mode"], observed_at=value.get("observed_at"),
            checkpoint_path=result_path.with_name("collector-legacy-row.json"), field=field) if value else None
    return safe({**context, "video_id": row.get("video_id") or None, "video_url": row.get("url") or None,
                 "likes": int(likes) if likes is not None else None, "views": int(views) if views is not None else None,
                 "like_view_ratio": likes / views * 100 if likes is not None and views else None,
                 "metric_source": row.get("metric_source") or None, "metric_evidence": row.get("metric_evidence") or None,
                 **provenance,
                 "reference_product_match": row.get("reference_product_match") or "uncertain",
                 "product_match": row.get("product_match") or row.get("reference_product_match") or "uncertain",
                 "product_match_confidence": product_confidence,
                 "product_match_reason": row.get("product_match_reason") or row.get("reference_product_match_reason") or "",
                 "ai_origin": row.get("ai_origin") or "uncertain", "cutout_suitability": row.get("cutout_suitability") or "uncertain",
                 "overlay_detected": row.get("overlay_detected"), "watermark_detected": row.get("watermark_detected"),
                 "subtitle_detected": row.get("subtitle_detected"), "decision": "manual_review" if error_code else row.get("decision") or "manual_review",
                 "decision_reason": "unknown_external_request" if error_code == "UNKNOWN_EXTERNAL_REQUEST" else row.get("reason") or error_code,
                 "status": status, "error_code": error_code or None, "error_message": error_message or row.get("error") or None,
                 "retryable": error_code not in {"UNKNOWN_EXTERNAL_REQUEST", "LEGACY_OUTPUT_PROTECTED", "RECOVERY_CONFLICT"}, "legacy_csv_path": str(legacy_root / "output" / "candidates.csv"),
                 "legacy_result_reference": {"raw_result_path": str(result_path.with_name("collector-legacy-row.json")), "checkpoint_key": row.get("checkpoint_key"),
                                             "analysis_report": row.get("analysis_report"), "reason": row.get("reason"), "metric_source_is_extension_numeric": False},
                 "legacy_vidbee_task_id": row.get("vidbee_task_id") or None,
                 "download_status": source["download_status"] if source else "unverified" if row.get("download_path") else row.get("vidbee_task_status") or "not_started",
                 "source_asset_metadata": source})


def unknown_request(row: dict) -> bool:
    return row.get("analysis_status") == "response_unknown" or str(row.get("reference_product_match_reason", "")).startswith("vision_api_error:")


def source_verification_ready(state):
    result, row = state.get("result") or {}, state.get("row") or {}
    return bool(state.get("status") == "finished" and result.get("error_code") == "COLLECTOR_ERROR"
        and result.get("error_message") == "ValueError: legacy source SHA256 does not match the completed download"
        and row.get("download_source_sha256") == "None" and row.get("download_path") and row.get("vidbee_task_id")
        and row.get("analysis_status") in {"completed", "invalid_response", "http_error"} and not unknown_request(row))


def semantic_retry_ready(state):
    row, result = state.get("row") or {}, state.get("result") or {}
    verified = state.get("download_verification") or {}
    return bool(state.get("status") == "finished" and not result.get("error_code") and not state.get("semantic_retry")
        and row.get("analysis_status") == "invalid_response" and row.get("reference_product_match") == "pass"
        and row.get("vidbee_task_status") == "completed" and row.get("analysis_report") and row.get("contact_sheet")
        and verified.get("full_decode_verified") is True and verified.get("sha256") and not unknown_request(row))


def semantic_retry_inputs(row, context, task_data, verified, config, legacy_root):
    from src.video_analysis import _semantic_configuration, _semantic_identity, _semantic_reference_paths

    report_path = Path(row["analysis_report"]).resolve()
    sheet = Path(row["contact_sheet"]).resolve()
    if not report_path.is_relative_to(task_data / "semantic_analysis") or not sheet.is_relative_to(task_data / "samples"):
        raise ValueError("semantic origin is not Factory-owned")
    origin_state_path = report_path.parent / "state.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    origin_state = json.loads(origin_state_path.read_text(encoding="utf-8"))
    attempt = int(report.get("analysis_attempt", 0))
    attempt_dir = report_path.parent / f"attempt-{attempt:03d}"
    request_path, response_path = attempt_dir / "request.json", attempt_dir / "response.json"
    request = json.loads(request_path.read_text(encoding="utf-8"))
    response = json.loads(response_path.read_text(encoding="utf-8"))
    invalid_verdicts = {f"semantic response has invalid {name} verdict" for name in ("product_match", "ai_origin", "cutout_suitability")}
    if (attempt < 1 or origin_state.get("attempt") != attempt or origin_state.get("status") != "invalid_response"
            or report.get("analysis_status") != "invalid_response" or origin_state.get("result") != report
            or response.get("status") != "invalid_response" or response.get("error") not in invalid_verdicts
            or origin_state.get("identity") != row.get("analysis_identity") or report.get("analysis_identity") != row.get("analysis_identity")):
        raise ValueError("origin is not a received invalid enum response")
    expected_metadata = {key: context[key] for key in ("country", "keyword", "context_id")}
    expected_metadata["reference_product_match"] = "pass"
    references = [path.resolve() for path in _semantic_reference_paths(row)]
    context_paths = [Path(value).resolve() for value in context["product_images"]]
    expected_references = [{"name": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path in references]
    context_hashes = [hashlib.sha256(path.read_bytes()).hexdigest() for path in context_paths]
    if ([item["sha256"] for item in expected_references] != context_hashes or request.get("metadata") != expected_metadata
            or any(path not in context_paths and not path.is_relative_to(legacy_root / "data/product_references") for path in references)):
        raise ValueError("original semantic context changed")
    sheet_hash = hashlib.sha256(sheet.read_bytes()).hexdigest()
    sampling_path = sheet.parent / "sampling.json"
    sampling = json.loads(sampling_path.read_text(encoding="utf-8"))
    if (request.get("references") != expected_references or request.get("contact_sheet") != {"name": sheet.name, "sha256": sheet_hash}
            or sampling.get("identity", {}).get("source_sha256") != verified["sha256"]
            or Path(sampling.get("contact_sheet", "")).resolve() != sheet or not sampling.get("frames")
            or any(not Path(value).is_file() or not Path(value).resolve().is_relative_to(task_data / "samples") for value in sampling["frames"])):
        raise ValueError("original semantic image evidence changed")
    endpoint, model, api_key = _semantic_configuration(config)
    if not api_key or request.get("endpoint") != endpoint or request.get("model") != model:
        raise ValueError("semantic configuration is not ready or changed")
    origin_identity = _semantic_identity(sheet, references, row, config, endpoint, model)
    if origin_identity != row.get("analysis_identity"):
        raise ValueError("original semantic fingerprint changed")
    new_config = {**config, "semantic_guidance": str(config.get("semantic_guidance", "")) + "\n" + SEMANTIC_ENUM_GUIDANCE,
                  "retry_semantic": False}
    new_identity = _semantic_identity(sheet, references, row, new_config, endpoint, model)
    if new_identity == origin_identity or (task_data / "semantic_analysis" / new_identity).exists():
        raise ValueError("semantic protocol was already submitted")
    return new_config, {"protocol": SEMANTIC_PROTOCOL, "protocol_sha256": hashlib.sha256(SEMANTIC_ENUM_GUIDANCE.encode()).hexdigest(),
        "origin_identity": origin_identity, "analysis_identity": new_identity, "attempt_limit": 1,
        "origin_report_path": str(report_path), "origin_report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
        "origin_request_path": str(request_path), "origin_request_sha256": hashlib.sha256(request_path.read_bytes()).hexdigest(),
        "origin_response_path": str(response_path), "origin_response_sha256": hashlib.sha256(response_path.read_bytes()).hexdigest(),
        "origin_status": "invalid_response", "origin_request_may_have_been_billed": True,
        "reference_path_bindings": [{"origin_path": str(origin), "context_path": str(snapshot), "sha256": digest}
            for origin, snapshot, digest in zip(references, context_paths, context_hashes)],
        "verified_source_sha256": verified["sha256"], "search_count": 0, "download_submission_count": 0,
        "reference_match_submission_count": 0, "semantic_submission_limit": 1}


def recovery_evidence(pipeline, context, row, config_sha256):
    return {"country": context["country"], "keyword": context["keyword"], "context_id": context["context_id"],
            "source_document": context.get("source_document", "manual"), "source_row": int(context.get("source_row") or 0),
            "video_id": row.get("video_id"), "video_url": row.get("url"), "vidbee_task_id": row.get("vidbee_task_id"),
            "reference_image_fingerprint": row.get("reference_image_fingerprint"),
            "screening_config_fingerprint": getattr(pipeline, "_screening_config_fingerprint", ""), "config_sha256": config_sha256,
            "factory_rules_sha256": (context.get("factory_rules") or {}).get("sha256"),
            "rules_snapshot": {key: pipeline.config.get(key) for key in ("min_views", "min_like_view_ratio", "product_match_min_confidence",
                "reference_product_match_required", "accepted_product_match", "accepted_ai_origin", "accepted_cutout_suitability",
                "accepted_require_cutout_suitability", "manual_review_on_uncertain", "download_min_file_bytes",
                "download_require_resolution", "download_min_short_side", "download_min_long_side", "download_min_video_bitrate")},
            "completed_stages": [name for name, done in (("metrics", row.get("likes") not in (None, "") and row.get("views") not in (None, "")),
                ("reference_product_match", row.get("reference_product_match") == "pass"), ("semantic_analysis", row.get("analysis_status") == "completed")) if done]}


def verify_complete_download(pipeline, row, task, legacy_root):
    if task.task_id != row.get("vidbee_task_id") or task.status != "completed":
        raise ValueError("VidBee completion does not match the original task")
    candidate = {**row, "download_path": row.get("download_path") or task.file_path, "download_source_type": "vidbee_automation"}
    metadata = source_metadata(candidate, legacy_root, pipeline.vidbee)
    path = Path(metadata["path"])
    before = path.stat()
    if before.st_size < int(pipeline.config["download_min_file_bytes"]):
        raise ValueError("completed video is below the configured minimum size")
    subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(path), "-map", "0:v:0", "-map", "0:a?", "-f", "null", "-"],
                   check=True, capture_output=True, text=True, timeout=max(60, min(900, metadata["duration"] * 4)))
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise ValueError("completed file changed during full decode verification")
    return {**metadata, "full_decode_verified": True, "mtime_ns": after.st_mtime_ns}


def resume_download(pipeline, context, spec, state, state_path, result_path, legacy_root, config_sha256, *, semantic_retry=False):
    row = dict(state.get("row") or {})
    evidence = state.get("recovery_evidence") or {}
    hit = state.get("selected_hit") or {}
    fields = ("country", "keyword", "context_id", "source_document", "source_row")
    expected = {**context, "source_document": context.get("source_document", "manual"), "source_row": int(context.get("source_row") or 0)}
    conflicts = [key for key in fields if str(row.get(key, "")) != str(expected[key]) or str(evidence.get(key, "")) != str(expected[key])]
    conflicts.extend(key for key, value in (("video_id", hit.get("video_id")), ("video_url", hit.get("url")))
                     if not value or evidence.get(key) != value or row.get("video_id" if key == "video_id" else "url") != value)
    if not evidence.get("vidbee_task_id") or row.get("vidbee_task_id") != evidence["vidbee_task_id"]:
        conflicts.append("vidbee_task_id")
    if evidence.get("reference_image_fingerprint") != spec.reference_fingerprint or row.get("reference_image_fingerprint") != spec.reference_fingerprint:
        conflicts.append("reference_image_fingerprint")
    if (not config_sha256 or evidence.get("config_sha256") != config_sha256 or
            evidence.get("screening_config_fingerprint") != pipeline._screening_config_fingerprint or
            row.get("screening_config_fingerprint") != pipeline._screening_config_fingerprint):
        conflicts.append("production_rules")
    if not {"metrics", "reference_product_match"}.issubset(evidence.get("completed_stages", [])):
        conflicts.append("completed_stages")
    if conflicts:
        return row, "RECOVERY_CONFLICT", "原下载恢复证据不匹配：" + ", ".join(sorted(set(conflicts)))
    if unknown_request(row):
        return row, "UNKNOWN_EXTERNAL_REQUEST", "已有付费分析响应未知，禁止重新提交"
    if semantic_retry:
        if not semantic_retry_ready(state):
            return row, "RECOVERY_CONFLICT", "没有可显式重试的已知无效响应；不提交付费请求"
        lookup = pipeline.vidbee.lookup_task(row["vidbee_task_id"])
        if not lookup.found or lookup.task is None:
            return row, "SOURCE_NOT_VERIFIED", "原 VidBee 文件状态不可核实；未提交API请求"
        try:
            verified = verify_complete_download(pipeline, row, lookup.task, legacy_root)
            previous = state["download_verification"]
            if not verified.get("full_decode_verified") or verified["sha256"] != previous["sha256"] or Path(verified["path"]).resolve() != Path(previous["path"]).resolve():
                raise ValueError("verified original source changed")
            runtime_config, retry = semantic_retry_inputs(row, context, Path(pipeline.config["data_dir"]).resolve(), verified, pipeline.config, legacy_root)
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
            return row, "RECOVERY_CONFLICT", "原语义重试证据未通过：" + type(exc).__name__
        pipeline.config = runtime_config
        write_json(result_path.parent / "semantic-retry-protocol.json", {**retry, "guidance": SEMANTIC_ENUM_GUIDANCE})
        state = {**state, "download_verification": verified, "semantic_retry": {**retry, "status": "submitted"}}
        write_json(state_path, {**state, "status": "external_inflight", "operation": "explicit_semantic_protocol_retry", "row": row})
        emit("SEMANTIC_ANALYSIS", video_id=row["video_id"], resumed=True, protocol=SEMANTIC_PROTOCOL, attempt_limit=1)
        row = pipeline._analyze_download(row)
        attempt_dir = Path(pipeline.config["data_dir"]) / "semantic_analysis" / retry["analysis_identity"] / "attempt-001"
        request_path, response_path = attempt_dir / "request.json", attempt_dir / "response.json"
        state["semantic_retry"].update(status=row.get("analysis_status"), analysis_report=row.get("analysis_report"),
            semantic_submission_count=int(request_path.is_file()), response_received=response_path.is_file(),
            request_path=str(request_path) if request_path.is_file() else None,
            request_sha256=hashlib.sha256(request_path.read_bytes()).hexdigest() if request_path.is_file() else None,
            response_path=str(response_path) if response_path.is_file() else None,
            response_sha256=hashlib.sha256(response_path.read_bytes()).hexdigest() if response_path.is_file() else None)
        write_json(state_path, {**state, "status": "result_received", "row": row})
        if unknown_request(row):
            return row, "UNKNOWN_EXTERNAL_REQUEST", "显式语义重试响应未知；禁止再次计费"
        return row, "", ""
    if source_verification_ready(state):
        emit("RECOVERY_SOURCE_VERIFY", video_id=row["video_id"], legacy_vidbee_task_id=row["vidbee_task_id"])
        lookup = pipeline.vidbee.lookup_task(row["vidbee_task_id"])
        if not lookup.found or lookup.task is None:
            return row, "SOURCE_NOT_VERIFIED", "原下载无法核实；没有搜索、提交下载或API请求"
        try:
            verification = verify_complete_download(pipeline, row, lookup.task, legacy_root)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            return row, "SOURCE_NOT_VERIFIED", "原下载完整核验未通过：" + type(exc).__name__
        write_json(state_path, {**state, "status": "result_received", "row": row, "download_verification": verification,
            "source_revalidation": {"origin_result_reference": state["result"].get("legacy_result_reference", {}),
                "reason": "legacy_missing_sha_normalized", "search_count": 0, "download_submission_count": 0,
                "semantic_submission_count": 0, "verified_sha256": verification["sha256"]}})
        return row, "", ""
    if row.get("analysis_status") == "completed" and row.get("decision") in {"accepted", "rejected", "manual_review"}:
        emit("RECOVERY_CACHED_DECISION", video_id=row["video_id"], legacy_vidbee_task_id=row["vidbee_task_id"])
        return row, "", ""
    if row.get("reason") != "vidbee_download_pending":
        return row, "RECOVERY_CONFLICT", "仅允许恢复原 vidbee_download_pending；不重算其它筛选结果"
    deadline = time.monotonic() + max(0, float(pipeline.config["vidbee_wait_timeout_s"]))
    interval = max(.2, float(pipeline.config["vidbee_poll_interval_s"]))
    while True:
        write_json(state_path, {**state, "status": "download_wait", "row": row})
        emit("WAITING_DOWNLOAD", legacy_vidbee_task_id=row["vidbee_task_id"], download_status=row.get("vidbee_task_status"), resumed=True)
        lookup = pipeline.vidbee.lookup_task(row["vidbee_task_id"])
        if not lookup.found or lookup.task is None:
            row["error"] = lookup.reason
            return row, "DOWNLOAD_PENDING", "原下载状态暂不可读，保留原 ID"
        task = lookup.task
        if task.task_id != row["vidbee_task_id"]:
            return row, "RECOVERY_CONFLICT", "返回的 VidBee task id 与原下载不一致"
        row["vidbee_task_status"] = task.status
        if task.status == "completed":
            write_json(state_path, {**state, "status": "download_verifying", "row": row})
            try:
                verification = verify_complete_download(pipeline, row, task, legacy_root)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                return row, "SOURCE_NOT_VERIFIED", "原下载完整核验未通过：" + type(exc).__name__
            state = {**state, "download_verification": verification}
            write_json(state_path, {**state, "status": "external_inflight", "operation": "semantic_analysis", "row": row})
            emit("SEMANTIC_ANALYSIS", video_id=row["video_id"], resumed=True)
            row = pipeline._complete_vidbee_task(row, task)
            if unknown_request(row):
                return row, "UNKNOWN_EXTERNAL_REQUEST", "语义 API 响应未知，禁止自动重试"
            write_json(state_path, {**state, "status": "result_received", "row": row})
            return row, "", ""
        if task.status in {"failed", "cancelled"}:
            row.update(decision="manual_review", reason="vidbee_download_failed", error=task.error)
            return row, "VIDBEE_DOWNLOAD_FAILED", "原 VidBee 下载失败；没有提交替代任务"
        if task.status not in {"queued", "running", "processing", "paused", "retry-scheduled"}:
            return row, "DOWNLOAD_PENDING", "原下载状态尚未确认，保留原 ID"
        if time.monotonic() >= deadline:
            return row, "DOWNLOAD_PENDING", "原 VidBee 任务仍在等待"
        time.sleep(interval)


def execute(context: dict, legacy_root: Path, storage_root: Path, result_path: Path, state_path: Path | None = None, *, resume_only=False, semantic_retry=False) -> dict:
    state_path = state_path or result_path.with_name("collector-state.json")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with state_path.with_suffix(".lease").open("a") as lease:
        try:
            lock_file(lease, blocking=False)
        except BlockingIOError:
            return collector_result(context, {}, legacy_root, result_path, error_code="RECOVERY_BUSY", error_message="此 Task 已有推进者；未执行搜索或下载")
        return _execute(context, legacy_root, storage_root, result_path, state_path, resume_only=resume_only, semantic_retry=semantic_retry)


def discover(context: dict, legacy_root: Path, storage_root: Path, result_path: Path, state_path: Path) -> dict:
    """Use the legacy search paginator without any metrics, download or paid analysis."""
    identity = hashlib.sha256(json.dumps(context, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    if state.get("identity") and state["identity"] != identity:
        return {"task_id": context["task_id"], "status": "FAILED", "error_code": "CONTEXT_CHANGED", "hits": []}
    if state.get("status") == "finished" and state.get("result", {}).get("status") == "READY":
        emit("SEARCH_SNAPSHOT_REUSED", count=len(state["result"]["hits"]), paid_requests=0)
        return state["result"]
    sys.path.insert(0, str(legacy_root))
    from src.browser import BrowserClient, LoginRequired, VerificationRequired
    BrowserClient = native_browser_class(BrowserClient)
    from src.config import load_config, resolve_paths
    from src.tiktok_search import search_keyword

    config = resolve_paths(legacy_root, load_config(legacy_root / "config.yaml"))
    # Debug files belong to Factory; the configured profile and search limits stay unchanged.
    config = {**config, "data_dir": result_path.parent / "search-data", "log_dir": result_path.parent}
    config["data_dir"].mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("factory.collector.discovery")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        logger.addHandler(logging.StreamHandler())
    result = {"task_id": context["task_id"], "country": context["country"], "keyword": context["keyword"],
              "context_id": context["context_id"], "hits": [], "status": "FAILED", "error_code": "", "error_message": "",
              "paid_requests": 0, "download_submission_count": 0,
              "search_config": {key: config[key] for key in ("search_max_results", "search_max_scrolls", "search_scroll_wait_ms", "search_stable_rounds")}}
    browser = None
    write_json(state_path, {"identity": identity, "status": "searching"})
    with (legacy_root / ".pipeline.lock").open("a") as lock:
        try:
            lock_file(lock, blocking=False)
        except BlockingIOError:
            return {**result, "error_code": "LEGACY_BUSY", "error_message": "旧采集项目正在运行；未启动第二个浏览器会话"}
        try:
            emit("SEARCH", keyword=context["keyword"], country=context["country"], search_limit=config["search_max_results"], paid_requests=0)
            browser = BrowserClient(config, legacy_root, logger).connect()
            browser.ensure_tiktok_search_login()
            hits = search_keyword(browser, context["keyword"])
            seen = set()
            for hit in hits:
                if hit.video_id not in seen:
                    result["hits"].append({"video_id": hit.video_id, "url": hit.url})
                    seen.add(hit.video_id)
            result.update(status="READY" if result["hits"] else "FAILED", error_code="" if result["hits"] else "NO_SEARCH_RESULT",
                          error_message="" if result["hits"] else "真实搜索未找到视频")
        except (LoginRequired, VerificationRequired) as exc:
            result.update(status="WAITING_MANUAL", error_code="LOGIN_REQUIRED" if isinstance(exc, LoginRequired) else "CAPTCHA_REQUIRED", error_message=str(exc))
        except Exception as exc:
            result.update(error_code="SEARCH_ERROR", error_message="真实搜索失败：" + type(exc).__name__)
        finally:
            if browser:
                browser.close()
    write_json(state_path, {"identity": identity, "status": "finished", "result": result})
    emit("SEARCH_COMPLETED", count=len(result["hits"]), status=result["status"], error_code=result["error_code"], paid_requests=0)
    return result


def _execute(context: dict, legacy_root: Path, storage_root: Path, result_path: Path, state_path: Path, *, resume_only=False, semantic_retry=False) -> dict:
    state_path = state_path or result_path.with_name("collector-state.json")
    identity = hashlib.sha256(json.dumps(context, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    state = json.loads(state_path.read_text()) if state_path.is_file() else {}
    if state.get("identity") and state["identity"] != identity:
        return collector_result(context, {}, legacy_root, result_path, error_code="CONTEXT_CHANGED", error_message="任务上下文已变化，请创建新任务")
    if state.get("status") == "external_inflight":
        return collector_result(context, state.get("row", {}), legacy_root, result_path,
                                error_code="UNKNOWN_EXTERNAL_REQUEST", error_message="上次外部调用中断，禁止自动重试")
    cached = state.get("result")
    safe_failed_retry = (isinstance(cached, dict) and cached.get("status") == "FAILED"
                         and not state.get("legacy_processing_entered", bool(cached.get("video_id"))))
    waiting_for_login = isinstance(cached, dict) and cached.get("error_code") in {"LOGIN_REQUIRED", "CAPTCHA_REQUIRED"}
    frozen_unknown = isinstance(cached, dict) and cached.get("error_code") == "UNKNOWN_EXTERNAL_REQUEST"
    if (resume_only or semantic_retry) and frozen_unknown:
        return collector_result(context, state.get("row", {}), legacy_root, result_path,
                                error_code="UNKNOWN_EXTERNAL_REQUEST", error_message="已有外部请求响应未知，禁止下载恢复重发")
    saved_row = state.get("row") or {}
    if semantic_retry and not semantic_retry_ready(state):
        return collector_result(context, saved_row, legacy_root, result_path,
                                error_code="RECOVERY_CONFLICT", error_message="显式语义重试证据缺失或唯一尝试已使用；未调用 API")
    pending_download = (not frozen_unknown and (source_verification_ready(state) or state.get("status") in DOWNLOAD_PHASES or
        saved_row.get("reason") == "vidbee_download_pending" and saved_row.get("vidbee_task_id") or
        isinstance(cached, dict) and cached.get("decision_reason") == "vidbee_download_pending" and cached.get("legacy_vidbee_task_id")))
    if resume_only and not pending_download and state.get("status") != "result_received":
        return collector_result(context, state.get("row", {}), legacy_root, result_path,
                                error_code="RECOVERY_CONFLICT", error_message="没有原下载等待快照；禁止重新搜索或下载")
    if isinstance(cached, dict) and not safe_failed_retry and not waiting_for_login and not pending_download and not semantic_retry:
        emit("CACHED_RESULT", decision=state["result"].get("decision"))
        cached = state["result"]
        row_path = Path(cached.get("legacy_result_reference", {}).get("raw_result_path") or "")
        if row_path.resolve().is_relative_to(storage_root) and row_path.is_file():
            saved_row = json.loads(row_path.read_text(encoding="utf-8"))
            saved_row = cached_observations(regate_cached_row(saved_row, context.get("factory_rules")), legacy_root, row_path)
            emit_metrics(saved_row)
            write_json(result_path.with_name("collector-legacy-row.json"), saved_row)
            return collector_result(context, saved_row, legacy_root, result_path,
                                    error_code=cached.get("error_code") or "", error_message=cached.get("error_message") or "")
        if cached.get("source_asset_metadata"):
            return safe({**cached, "source_asset_metadata": None, "download_status": "unverified",
                         "status": "WAITING_REVIEW" if frozen_unknown else "FAILED", "decision": "manual_review",
                         "error_code": "UNKNOWN_EXTERNAL_REQUEST" if frozen_unknown else "SOURCE_NOT_VERIFIED",
                         "error_message": cached.get("error_message") if frozen_unknown else "缓存素材缺少原始旧结果，无法重新验证下载关联"})
        if frozen_unknown:
            return cached
        row = cached_observations(regate_cached_row({**cached, "reason": cached.get("decision_reason")}, context.get("factory_rules")), legacy_root)
        emit_metrics(row)
        result = {**cached, **{field: row[field] for field in ("metrics_provenance", "reference_provenance", "semantic_provenance")}}
        if row.get("factory_gate"):
            result.update(decision=row["decision"], decision_reason=row["reason"],
                status="REJECTED" if row["decision"] == "rejected" else "WAITING_REVIEW")
        return result
    state = {**state, "identity": identity, "legacy_processing_entered": bool(state.get("legacy_processing_entered")) if pending_download or resume_only or semantic_retry else False}
    sys.path.insert(0, str(legacy_root))
    from src.browser import BrowserClient, LoginRequired, VerificationRequired
    BrowserClient = native_browser_class(BrowserClient)
    from src.excel_input import KeywordSpec
    from src.pipeline import Pipeline
    from src.tiktok_search import SearchHit, search_keyword, video_id
    from src.tencent_docs import TencentDocsLoginRequired

    row = {}
    browser = None
    with (legacy_root / ".pipeline.lock").open("a") as lock:
        try:
            lock_file(lock, blocking=False)
        except BlockingIOError:
            return collector_result(context, row, legacy_root, result_path, error_code="LEGACY_BUSY", error_message="旧采集项目正写入结果")
        pipeline = single_task_pipeline(Pipeline, legacy_root, context.get("factory_rules"))
        config_path = legacy_root / "config.yaml"
        config_sha256 = hashlib.sha256(config_path.read_bytes()).hexdigest() if config_path.is_file() else ""
        checkpoint = {} if pending_download or resume_only or semantic_retry else pipeline._load_checkpoint(fill_authors=False)
        references = tuple(Path(value).resolve() for value in context.get("product_images", []))
        if not references:
            return collector_result(context, row, legacy_root, result_path, error_code="PRODUCT_IMAGES_REQUIRED", error_message="当前产品没有参考图")
        for path in references:
            if not path.is_file() or not (path.is_relative_to(storage_root) or path.is_relative_to(legacy_root / "data" / "product_references")):
                return collector_result(context, row, legacy_root, result_path, error_code="REFERENCE_IMAGE_UNAVAILABLE", error_message="产品图不存在或不属于已授权本地存储")
        hashes = sorted({hashlib.sha256(path.read_bytes()).hexdigest() for path in references})
        fingerprint = hashlib.sha256("\n".join(hashes).encode()).hexdigest()[:16]
        spec = KeywordSpec(str(context["keyword"]), references, fingerprint, "", str(context["country"]),
                           int(context.get("source_row") or 0), str(context.get("source_document") or "manual"), str(context["context_id"]))
        output_guard = LegacyOutputGuard(pipeline.config, legacy_root)
        task_data = state_path.parent / "legacy-work" / "data"
        task_data.mkdir(parents=True, exist_ok=True)
        pipeline.config = {**pipeline.config, "data_dir": task_data}
        # Factory owns country lineage and registers the existing file by reference.
        # Legacy archiving can remove prior unclassified aliases, so it stays off here.
        pipeline._archive_active = False
        error_code = error_message = ""
        budget_outcome = {}
        try:
            if pending_download or resume_only or semantic_retry:
                state["row"] = cached_observations(regate_cached_row(state.get("row") or {}, context.get("factory_rules")), legacy_root,
                    Path((state.get("result") or {}).get("legacy_result_reference", {}).get("raw_result_path") or result_path.with_name("collector-legacy-row.json")))
                if state["row"].get("factory_gate"):
                    row = state["row"]
                else:
                    with factory_paid_requests(storage_root, context["task_id"], budget_outcome, evidence_dir=result_path.parent) as observations, output_guard, factory_sidecars(result_path.parent):
                        row, error_code, error_message = resume_download(pipeline, context, spec, state, state_path, result_path, legacy_root, config_sha256, semantic_retry=semantic_retry)
                    record_analysis_observations(row, observations, legacy_root)
                emit_metrics(row)
                if budget_outcome:
                    error_code, error_message = budget_outcome["error_code"], budget_outcome["error_message"]
                if output_guard.blocked_operations and error_code != "UNKNOWN_EXTERNAL_REQUEST":
                    error_code, error_message = "LEGACY_OUTPUT_PROTECTED", "恢复尝试写入旧资产，已阻止并保留历史"
                write_json(result_path.with_name("collector-legacy-row.json"), row)
                result = collector_result(context, row, legacy_root, result_path, error_code=error_code, error_message=error_message, vidbee_client=pipeline.vidbee)
                current = json.loads(state_path.read_text(encoding="utf-8"))
                if result.get("source_asset_metadata") and current.get("download_verification", {}).get("sha256") == result["source_asset_metadata"]["sha256"]:
                    result["source_asset_metadata"].update(full_decode_verified=True, mtime_ns=current["download_verification"].get("mtime_ns"))
                elif semantic_retry and result.get("source_asset_metadata"):
                    result.update(status="FAILED", decision="manual_review", error_code="SOURCE_NOT_VERIFIED",
                        error_message="语义重试后原视频SHA发生变化，保留结果但不登记成功", source_asset_metadata=None, download_status="unverified")
                result["legacy_result_reference"]["download_recovery"] = {**state.get("recovery_evidence", {}), "state_path": str(state_path), "search_count": 0, "download_submission_count": 0}
                if current.get("source_revalidation"):
                    result["legacy_result_reference"]["source_revalidation"] = current["source_revalidation"]
                if current.get("semantic_retry"):
                    result["legacy_result_reference"]["semantic_retry"] = current["semantic_retry"]
                write_json(state_path, {**current, "status": "finished", "row": row, "result": result})
                emit("DONE", decision=result["decision"], status=result["status"], error_code=result["error_code"], resumed=True)
                return result
            emit("CONNECT_CHROME", video_id=context.get("video_id"), search_limit=pipeline.config.get("search_max_results", 0))
            browser = BrowserClient(pipeline.config, legacy_root, pipeline.logger).connect()
            hit_value = state.get("selected_hit")
            if hit_value:
                hit = SearchHit(hit_value["url"], hit_value["video_id"])
            elif context.get("video_url"):
                identifier = video_id(context["video_url"])
                if not identifier:
                    raise ValueError("video_url must contain a TikTok video ID")
                if context.get("video_id") and context["video_id"] != identifier:
                    raise ValueError("video_id does not match the bound TikTok video URL")
                hit = SearchHit(context["video_url"], identifier)
            else:
                emit("SEARCH", keyword=spec.keyword, country=spec.country)
                browser.ensure_tiktok_search_login()
                hits = search_keyword(browser, spec.keyword)
                if not hits:
                    return collector_result(context, row, legacy_root, result_path, error_code="NO_SEARCH_RESULT", error_message="单关键词真实搜索未找到视频")
                if len(hits) != 1:
                    return collector_result(context, row, legacy_root, result_path, error_code="SEARCH_EXPANSION_REQUIRED",
                        error_message=f"搜索返回 {len(hits)} 条视频；必须先由 Factory 创建独立视频任务，未调用付费分析或下载")
                hit = hits[0]
            state = {"identity": identity, "status": "selected", "legacy_processing_entered": False,
                     "selected_hit": {"url": hit.url, "video_id": hit.video_id}}
            write_json(state_path, state)
            previous = checkpoint.get(pipeline._checkpoint_key(spec.keyword, hit.video_id, spec.context_id))
            if previous and unknown_request(previous):
                row = previous
                error_code, error_message = "UNKNOWN_EXTERNAL_REQUEST", "旧项目保存了未知API结果，禁止自动重试"
            else:
                row = pipeline._base_row(spec, hit)
                if "/photo/" in hit.url:
                    row.update(decision="rejected", reason="unsupported_photo_source", error="搜索来源为图片帖，不是可抠像的视频；未调用视觉API或提交下载")
                    emit("NON_VIDEO_SOURCE", video_id=hit.video_id, source_type="photo", decision="rejected")
                else:
                    state["legacy_processing_entered"] = True
                    write_json(state_path, {**state, "status": "external_inflight", "row": row})
                    emit("METRICS_PRODUCT_DOWNLOAD", video_id=hit.video_id)
                    with factory_paid_requests(storage_root, context["task_id"], budget_outcome, evidence_dir=result_path.parent) as observations, output_guard, factory_sidecars(result_path.parent):
                        row = pipeline.process_hit(browser, spec, hit, checkpoint)
                    record_analysis_observations(row, observations, legacy_root)
                emit_metrics(row)
                if unknown_request(row):
                    error_code, error_message = "UNKNOWN_EXTERNAL_REQUEST", "产品或语义API响应未知，禁止自动重试"
                else:
                    state = {**state, "status": "result_received", "row": row,
                             "recovery_evidence": recovery_evidence(pipeline, context, row, config_sha256)}
                    write_json(state_path, state)
            if not error_code and row.get("reason") == "vidbee_download_pending" and row.get("vidbee_task_id"):
                write_json(state_path, {**state, "status": "download_wait", "row": row})
                deadline = time.monotonic() + max(0, float(pipeline.config["vidbee_wait_timeout_s"]))
                interval = max(0.2, float(pipeline.config["vidbee_poll_interval_s"]))
                while time.monotonic() < deadline:
                    emit("WAITING_DOWNLOAD", legacy_vidbee_task_id=row["vidbee_task_id"], download_status=row.get("vidbee_task_status"))
                    lookup = pipeline.vidbee.lookup_task(row["vidbee_task_id"])
                    if not lookup.found or lookup.task is None:
                        row.update(reason="vidbee_status_unavailable", error=lookup.reason)
                        break
                    task = lookup.task
                    row["vidbee_task_status"] = task.status
                    if task.status == "completed":
                        write_json(state_path, {**state, "status": "download_verifying", "row": row})
                        verification = verify_complete_download(pipeline, row, task, legacy_root)
                        state = {**state, "download_verification": verification}
                        write_json(state_path, {**state, "status": "external_inflight", "operation": "semantic_analysis", "row": row})
                        emit("SEMANTIC_ANALYSIS", video_id=hit.video_id)
                        with factory_paid_requests(storage_root, context["task_id"], budget_outcome, evidence_dir=result_path.parent) as observations, output_guard, factory_sidecars(result_path.parent):
                            row = pipeline._complete_vidbee_task(row, task)
                        record_analysis_observations(row, observations, legacy_root)
                        if unknown_request(row):
                            error_code, error_message = "UNKNOWN_EXTERNAL_REQUEST", "语义API响应未知，禁止自动重试"
                        else:
                            write_json(state_path, {**state, "status": "result_received", "row": row})
                        break
                    if task.status in {"failed", "cancelled"}:
                        row.update(decision="manual_review", reason="vidbee_download_failed", error=task.error)
                        break
                    time.sleep(interval)
            if not error_code and row.get("download_path") and row.get("vidbee_task_id") and not state.get("download_verification"):
                lookup = pipeline.vidbee.lookup_task(row["vidbee_task_id"])
                if not lookup.found or lookup.task is None:
                    error_code, error_message = "SOURCE_NOT_VERIFIED", "已下载结果没有可核实的原 VidBee 任务"
                else:
                    write_json(state_path, {**state, "status": "download_verifying", "row": row})
                    state["download_verification"] = verify_complete_download(pipeline, row, lookup.task, legacy_root)
            checkpoint[row["checkpoint_key"]] = row
            pipeline.rows = list(checkpoint.values())
            with output_guard:
                pipeline._write_outputs()
        except (LoginRequired, VerificationRequired, TencentDocsLoginRequired) as exc:
            error_code = "LOGIN_REQUIRED" if isinstance(exc, (LoginRequired, TencentDocsLoginRequired)) else "CAPTCHA_REQUIRED"
            error_message = str(exc)
        except Exception as exc:
            error_code, error_message = "COLLECTOR_ERROR", f"{type(exc).__name__}: {exc}"
            current_state = json.loads(state_path.read_text()) if state_path.is_file() else {}
            if pending_download or resume_only or semantic_retry:
                state, row = current_state, current_state.get("row", row)
            if current_state.get("status") == "external_inflight":
                error_code, error_message = "UNKNOWN_EXTERNAL_REQUEST", "外部调用未完成，禁止自动重试"
        finally:
            if browser:
                browser.close()
        if budget_outcome:
            error_code, error_message = budget_outcome["error_code"], budget_outcome["error_message"]
        if output_guard.blocked_operations and error_code != "UNKNOWN_EXTERNAL_REQUEST":
            error_code, error_message = "LEGACY_OUTPUT_PROTECTED", "旧程序尝试覆盖或移除已有文件；已阻止，历史文件保留，未自动重试"
        write_json(result_path.with_name("collector-legacy-row.json"), row)
        result = collector_result(context, row, legacy_root, result_path, error_code=error_code, error_message=error_message, vidbee_client=pipeline.vidbee)
        if result.get("source_asset_metadata") and state.get("download_verification", {}).get("sha256") == result["source_asset_metadata"]["sha256"]:
            result["source_asset_metadata"].update(full_decode_verified=True, mtime_ns=state["download_verification"].get("mtime_ns"))
        if output_guard.blocked_operations:
            result["legacy_result_reference"]["protected_operations"] = output_guard.blocked_operations
        write_json(state_path, {**state, "status": "finished", "row": row, "result": result})
        emit("DONE", decision=result["decision"], status=result["status"], error_code=result["error_code"])
        return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--legacy-root", required=True, type=Path)
    parser.add_argument("--storage-root", required=True, type=Path)
    parser.add_argument("--context", required=True, type=Path)
    parser.add_argument("--result", required=True, type=Path)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--resume-download", action="store_true")
    parser.add_argument("--retry-semantic-once", action="store_true")
    parser.add_argument("--discover-only", action="store_true")
    args = parser.parse_args()
    context = json.loads(args.context.read_text(encoding="utf-8"))
    result = discover(context, args.legacy_root.resolve(), args.storage_root.resolve(), args.result.resolve(), args.state.resolve()) if args.discover_only else execute(context, args.legacy_root.resolve(), args.storage_root.resolve(), args.result.resolve(), args.state.resolve(), resume_only=args.resume_download, semantic_retry=args.retry_semantic_once)
    write_json(args.result, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
