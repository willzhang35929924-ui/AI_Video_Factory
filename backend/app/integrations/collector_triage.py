"""Zero-request, evidence-bound collector decisions. Media review stays separate."""
import copy
import hashlib
import json
import math
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain import audit, new_id, now
from app.integrations.production_rules import factory_metric_gate, factory_presence_gate, verified_factory_rules
from app.models import AuditLog, CollectorRule, ProcessingRun, ProductionBatch, Review, Task, TaskStage, VideoAsset
from app.repository import record

POLICY_SCHEMA = "collector-unattended-v1"
SAFE_RETRY_ERRORS = {"NETWORK_ERROR", "CONNECTION_ERROR", "PROCESS_INTERRUPTED", "TIMEOUT"}
UNCERTAIN_ERRORS = {"UNKNOWN_EXTERNAL_REQUEST", "RECOVERY_CONFLICT", "LOGIN_REQUIRED", "CAPTCHA_REQUIRED",
    "EXTERNAL_AUTHORIZATION_REQUIRED", "EXTERNAL_BUDGET_EXHAUSTED", "DOWNLOAD_PENDING", "RESOURCE_BUSY"}


class UnattendedPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = Field(default=True, strict=True)
    safe_pre_submit_retry_limit: int = Field(default=2, ge=0, le=5)
    retry_backoff_seconds: float = Field(default=2, ge=.1, le=30)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str).encode()).hexdigest()


def load_policy(settings):
    path = Path(settings.storage_root) / "collector_inputs/unattended-policy.json"
    values = json.loads(path.read_text()) if path.is_file() else {"enabled": settings.collector_unattended_enabled}
    return UnattendedPolicy.model_validate(values).model_dump()


def machine_decision(task, result):
    """Never equate a low/unknown model score or transport success with a pass."""
    code = result.get("error_code") or ""
    if code in UNCERTAIN_ERRORS:
        return {"decision": "QUARANTINED", "reason": code, "next_status": "QUARANTINED"}
    if code:
        return {"decision": "AUTO_FAILED", "reason": code, "next_status": "FAILED"}
    if result.get("decision_reason") == "vidbee_download_failed" and result.get("download_status") == "failed":
        if "Log in for access" in (result.get("error_message") or ""):
            return {"decision": "QUARANTINED", "reason": "SOURCE_AUTH_REQUIRED", "next_status": "QUARANTINED"}
        return {"decision": "AUTO_FAILED", "reason": "VIDBEE_DOWNLOAD_FAILED", "next_status": "FAILED"}
    context = task.get("context", {})
    legacy = context.get("production_rules", {}).get("rules", {})
    snapshot = context.get("task_context", {}).get("factory_rules")
    if not snapshot or not legacy:
        return {"decision": "QUARANTINED", "reason": "RULE_SNAPSHOT_MISSING", "next_status": "QUARANTINED"}
    try:
        rules = verified_factory_rules(snapshot)
    except (ValueError, TypeError):
        return {"decision": "QUARANTINED", "reason": "RULE_SNAPSHOT_INVALID", "next_status": "QUARANTINED"}
    likes, views = result.get("likes"), result.get("views")
    if (type(likes) is not int or type(views) is not int or min(likes, views) < 0
            or not result.get("metric_source") or not result.get("metric_evidence")):
        return {"decision": "QUARANTINED", "reason": "METRIC_EVIDENCE_MISSING", "next_status": "QUARANTINED"}
    gate = factory_metric_gate(rules, likes, views)
    if gate:
        return {"decision": "AUTO_REJECTED", "reason": gate[1], "next_status": "REJECTED"}
    reference, match = result.get("reference_product_match"), result.get("product_match")
    if {reference, match} == {"pass", "fail"}:
        return {"decision": "QUARANTINED", "reason": "PRODUCT_EVIDENCE_CONFLICT", "next_status": "QUARANTINED"}
    if reference == "fail" or match == "fail":
        return {"decision": "AUTO_REJECTED", "reason": "PRODUCT_MISMATCH", "next_status": "REJECTED"}
    if reference != "pass" or match != legacy.get("accepted_product_match", "pass"):
        return {"decision": "QUARANTINED", "reason": "PRODUCT_IDENTITY_UNCERTAIN", "next_status": "QUARANTINED"}
    confidence = result.get("product_match_confidence")
    if (type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= 1):
        return {"decision": "QUARANTINED", "reason": "PRODUCT_CONFIDENCE_UNVERIFIED", "next_status": "QUARANTINED"}
    if confidence < rules["product_match_min_confidence"]:
        return {"decision": "QUARANTINED", "reason": "PRODUCT_CONFIDENCE_BELOW_THRESHOLD", "next_status": "QUARANTINED"}
    origin = result.get("ai_origin")
    if origin == "likely_ai" and legacy.get("accepted_ai_origin", "likely_real") == "likely_real":
        return {"decision": "AUTO_REJECTED", "reason": "AI_ORIGIN_NOT_ACCEPTED", "next_status": "REJECTED"}
    if origin != legacy.get("accepted_ai_origin", "likely_real"):
        return {"decision": "QUARANTINED", "reason": "AI_ORIGIN_UNCERTAIN", "next_status": "QUARANTINED"}
    if legacy.get("accepted_require_cutout_suitability", True):
        if result.get("cutout_suitability") == "fail":
            return {"decision": "AUTO_REJECTED", "reason": "CUTOUT_NOT_SUITABLE", "next_status": "REJECTED"}
        if result.get("cutout_suitability") != legacy.get("accepted_cutout_suitability", "pass"):
            return {"decision": "QUARANTINED", "reason": "CUTOUT_SUITABILITY_UNCERTAIN", "next_status": "QUARANTINED"}
    presence = factory_presence_gate(rules, result)
    if presence:
        return {"decision": "AUTO_REJECTED" if presence[0] == "rejected" else "QUARANTINED",
            "reason": presence[1], "next_status": "REJECTED" if presence[0] == "rejected" else "QUARANTINED"}
    if result.get("decision") == "rejected":
        return {"decision": "AUTO_REJECTED", "reason": result.get("decision_reason") or "LEGACY_REJECTED", "next_status": "REJECTED"}
    # A watermark or other missing proof is not removed just because geometry/product passed.
    if result.get("decision") != "accepted":
        return {"decision": "QUARANTINED", "reason": result.get("decision_reason") or "BUSINESS_EVIDENCE_INCOMPLETE", "next_status": "QUARANTINED"}
    source = result.get("source_asset_metadata") or {}
    proof = source.get("download_verification") or {}
    if (source.get("verified") is not True or source.get("full_decode_verified") is not True or not source.get("sha256")
            or result.get("download_status") != "completed" or proof.get("status") != "completed"
            or proof.get("task_id") != result.get("legacy_vidbee_task_id") or not result.get("legacy_vidbee_task_id")
            or not source.get("codec") or any(not source.get(key) for key in ("width", "height", "fps", "duration", "frame_count"))):
        return {"decision": "QUARANTINED", "reason": "SOURCE_NOT_VERIFIED", "next_status": "QUARANTINED"}
    path = Path(source.get("path") or "")
    if not path.is_absolute() or not path.is_file():
        return {"decision": "QUARANTINED", "reason": "SOURCE_FILE_MISSING", "next_status": "QUARANTINED"}
    sha = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                sha.update(chunk)
        size = path.stat().st_size
    except OSError:
        return {"decision": "QUARANTINED", "reason": "SOURCE_READ_FAILED", "next_status": "QUARANTINED"}
    if sha.hexdigest() != source["sha256"] or size != source.get("file_size"):
        return {"decision": "QUARANTINED", "reason": "SOURCE_FINGERPRINT_CHANGED", "next_status": "QUARANTINED"}
    return {"decision": "AUTO_ACCEPTED", "reason": "VERIFIED_COLLECTOR_RESULT", "next_status": "COLLECTED"}


def record_machine_decision(state, task, result, policy, *, run_id=None):
    if not policy["enabled"]:
        return None
    outcome = machine_decision(task, result)
    identity = {"task_id": task["id"], "result": result, "rules": task["context"].get("task_context", {}).get("factory_rules"),
        "policy": policy, "schema": POLICY_SCHEMA, "run_id": run_id}
    key = digest(identity)
    history = task["context"].setdefault("collector_auto_triage_history", [])
    repeated = next((value for value in history if value["idempotency_key"] == key), None)
    if repeated:
        return repeated
    saved = {"id": new_id("triage"), "task_id": task["id"], "run_id": run_id, "schema": POLICY_SCHEMA, "policy": policy,
        **outcome, "previous_status": task["status"], "idempotency_key": key, "decided_at": now(), "paid_requests_submitted": 0,
        "evidence_sha256": digest(result), "quality_approved": False, "publish_authorized": False}
    history.append(saved)
    task["context"]["collector_auto_triage"] = saved
    task.update(status=outcome["next_status"], updated_at=now())
    audit(state, "collector.auto_triage", saved["id"], outcome["decision"] + "：" + outcome["reason"] + "；0新增请求；保留原结果", task["id"])
    return saved


class ApplyTriage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    confirmed: Literal[True]


class CollectorAutoTriage:
    def __init__(self, collector):
        self.collector, self.repository, self.settings = collector, collector.repository, collector.settings
        self.root = Path(self.settings.storage_root) / "collector_triage"

    def _rows(self, session, task_ids=None):
        query = select(Task).where(Task.execution_mode == "integration")
        query = query.where(Task.id.in_(task_ids)) if task_ids is not None else query.where(Task.status == "WAITING_REVIEW")
        tasks = session.scalars(query.order_by(Task.created_at, Task.id)).all()
        ids = [row.id for row in tasks]
        runs = list(session.scalars(select(ProcessingRun).where(ProcessingRun.task_id.in_(ids)).order_by(ProcessingRun.created_at, ProcessingRun.id)))
        assets = list(session.scalars(select(VideoAsset).where(VideoAsset.task_id.in_(ids))))
        reviews = list(session.scalars(select(Review).where(Review.task_id.in_(ids))))
        batches = list(session.scalars(select(ProductionBatch)))
        return tasks, runs, assets, reviews, batches

    def _detail(self, row, runs, assets, reviews, batches, policy):
        task = record(row)
        context, result = task["context"], task["collector_result"] or {}
        own_runs = [record(run) for run in runs if run.task_id == row.id]
        collector_runs = [run for run in own_runs if run["provider"] == "LegacyCollectorAdapter"]
        latest = collector_runs[-1] if collector_runs else {}
        own_assets = [record(asset) for asset in assets if asset.task_id == row.id]
        downstream = [asset["id"] for asset in own_assets if asset["type"] not in {"source", "source_candidate"}]
        memberships = [{"batch_id": batch.id, "status": batch.status, "item": copy.deepcopy(item)}
            for batch in batches for item in batch.items if item.get("task_id") == row.id]
        active = (any(run["status"] in {"queued", "running"} for run in own_runs)
            or any(value["status"] in {"QUEUED", "RUNNING", "PAUSED"} and value["item"]["status"] in {"QUEUED", "RUNNING"} for value in memberships))
        reason = result.get("decision_reason") or latest.get("error_code") or "MISSING_COLLECTOR_RESULT"
        effective = {**result}
        if not effective.get("error_code") and latest.get("error_code"):
            effective["error_code"] = latest["error_code"]
        outcome = machine_decision(task, effective)
        excluded = "ACTIVE_TASK" if active else "DOWNSTREAM_REVIEW_NOT_COLLECTOR" if downstream or row.current_stage not in {"collector", "analysis", "download"} else None
        if excluded:
            outcome = {"decision": "NOT_COLLECTOR_REVIEW", "reason": excluded, "next_status": row.status}
        reference = result.get("legacy_result_reference") or {}
        evidence = []
        for kind, value in (("raw_result", reference.get("raw_result_path")), ("analysis_report", reference.get("analysis_report")),
                ("stdout", latest.get("stdout_log_path")), ("stderr", latest.get("stderr_log_path"))):
            if value:
                path = Path(value)
                local = path.resolve().is_relative_to(Path(self.settings.storage_root).resolve())
                exists = local and path.is_file() and not path.is_symlink()
                evidence.append({"kind": kind, "path": str(path), "exists": exists,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest() if exists and path.stat().st_size <= 32 * 1024 ** 2 else None})
        observations = {key: result.get(key) for key in ("views", "likes", "metric_source", "metric_evidence", "reference_product_match",
            "product_match", "product_match_confidence", "product_match_reason", "ai_origin", "cutout_suitability", "decision", "decision_reason",
            "download_status", "legacy_vidbee_task_id", "error_code", "error_message", "metrics_provenance", "reference_provenance", "semantic_provenance")}
        identity = {"task_id": row.id, "status": row.status, "stage": row.current_stage, "updated_at": str(row.updated_at),
            "result": result, "rules": context.get("task_context", {}).get("factory_rules"), "policy": policy,
            "run_id": latest.get("id"), "run_status": latest.get("status"), "downstream": downstream, "active": active,
            "candidate_review": context.get("collector_candidate_decision"), "evidence": evidence}
        return {"task_id": row.id, "record_ids": [], "country": row.country, "keyword": row.keyword,
            "context_id": context.get("task_context", {}).get("context_id"), "video_id": result.get("video_id") or context.get("task_context", {}).get("video_id"),
            "video_url": result.get("video_url") or context.get("task_context", {}).get("video_url"), "previous_status": row.status,
            "stage": row.current_stage, "entered_at": str(row.updated_at), "original_reason": reason, "error_code": effective.get("error_code"),
            "run_id": latest.get("id"), "run_status": latest.get("status"), "pid": latest.get("pid"), "exit_code": latest.get("exit_code"),
            "batch_ids": [value["batch_id"] for value in memberships], "batches": memberships, "downstream_asset_ids": downstream,
            "source_asset_ids": [value["id"] for value in own_assets if value["type"] == "source"],
            "review_ids": [value.id for value in reviews if value.task_id == row.id], "observations": observations, "evidence": evidence,
            "existing_external_request_unknown": effective.get("error_code") == "UNKNOWN_EXTERNAL_REQUEST", "prior_cost": "UNKNOWN",
            "paid_requests_submitted": 0, "fingerprint": digest(identity), "eligible": not excluded, **outcome}

    def plan(self):
        policy = load_policy(self.settings)
        with self.repository.lock, Session(self.repository.engine) as session:
            rows, runs, assets, reviews, batches = self._rows(session)
            details = [self._detail(row, runs, assets, reviews, batches, policy) for row in rows]
            for detail in details:
                detail["record_ids"] = list(session.scalars(select(CollectorRule.id).where(CollectorRule.last_task_id == detail["task_id"])))
        plan = {"schema": POLICY_SCHEMA, "plan_id": uuid4().hex, "created_at": now(), "policy": policy, "items": details,
            "summary": dict(Counter(value["decision"] for value in details)), "reason_counts": dict(Counter(value["original_reason"] for value in details)),
            "total": len(details), "eligible": sum(value["eligible"] for value in details), "paid_requests_submitted": 0,
            "production_started": False, "batch_resumed": False}
        plan["fingerprint"] = digest(plan)
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / (plan["plan_id"] + ".json")).open("x", encoding="utf-8") as handle:
            json.dump(plan, handle, ensure_ascii=False, indent=2)
        return plan

    def apply(self, payload):
        if not payload.confirmed:
            raise HTTPException(422, "AUTO_TRIAGE_CONFIRM_REQUIRED")
        path = self.root / (payload.plan_id + ".json")
        try:
            plan = json.loads(path.read_text())
        except (OSError, ValueError):
            raise HTTPException(404, "AUTO_TRIAGE_PLAN_NOT_FOUND") from None
        if plan.get("fingerprint") != payload.fingerprint or digest({key: value for key, value in plan.items() if key != "fingerprint"}) != payload.fingerprint:
            raise HTTPException(409, "AUTO_TRIAGE_PLAN_INVALID")
        if not load_policy(self.settings)["enabled"] or load_policy(self.settings) != plan["policy"]:
            raise HTTPException(409, "AUTO_TRIAGE_POLICY_CHANGED")
        saved = []
        ids = [value["task_id"] for value in plan["items"]]
        with self.repository.lock, Session(self.repository.engine) as session:
            rows, runs, assets, reviews, batches = self._rows(session, ids)
            indexed = {value.id: value for value in rows}
            for prior in plan["items"]:
                row = indexed.get(prior["task_id"])
                if row is None:
                    saved.append({"task_id": prior["task_id"], "status": "CHANGED", "reason": "TASK_NOT_FOUND"})
                    continue
                key = digest({"schema": POLICY_SCHEMA, "task_id": row.id, "evidence": prior["fingerprint"]})
                previous = next((value for value in row.context.get("collector_auto_triage_history", []) if value["idempotency_key"] == key), None)
                if previous:
                    saved.append({"task_id": row.id, "status": "IDEMPOTENT", **previous})
                    continue
                current = self._detail(row, runs, assets, reviews, batches, plan["policy"])
                if not prior["eligible"] or not current["eligible"]:
                    saved.append({"task_id": row.id, "status": "NOT_COLLECTOR_REVIEW", "reason": current["reason"]})
                    continue
                if current["fingerprint"] != prior["fingerprint"] or row.status != "WAITING_REVIEW":
                    saved.append({"task_id": row.id, "status": "CHANGED", "reason": "EVIDENCE_CHANGED"})
                    continue
                decided = {"id": new_id("triage"), "task_id": row.id, "run_id": current["run_id"], "plan_id": plan["plan_id"],
                    "decision": current["decision"], "reason": current["reason"], "previous_status": row.status,
                    "next_status": current["next_status"], "decided_at": now(), "policy": plan["policy"], "idempotency_key": key,
                    "evidence_sha256": prior["fingerprint"], "paid_requests_submitted": 0, "batch_resumed": False,
                    "quality_approved": False, "publish_authorized": False}
                row.context = {**row.context, "collector_auto_triage": decided,
                    "collector_auto_triage_history": [*row.context.get("collector_auto_triage_history", []), decided]}
                row.status, row.updated_at = current["next_status"], datetime.fromisoformat(now())
                for stage in session.scalars(select(TaskStage).where(TaskStage.task_id == row.id, TaskStage.name == "collector")):
                    if current["next_status"] == "QUARANTINED":
                        stage.status, stage.error = "skipped", "AUTO_QUARANTINED:" + current["reason"]
                for rule in session.scalars(select(CollectorRule).where(CollectorRule.last_task_id == row.id)):
                    rule.status = row.status
                for batch in batches:
                    if any(value.get("task_id") == row.id for value in batch.items):
                        batch.items = [{**value, "status": row.status, "auto_triage_id": decided["id"], "error": current["reason"]}
                            if value.get("task_id") == row.id else value for value in batch.items]
                        # The batch's pause/cancellation/completion status is deliberately unchanged.
                session.add(AuditLog(id=new_id("audit"), task_id=row.id, action="collector.auto_triage",
                    entity_id=decided["id"], message=json.dumps(decided, ensure_ascii=False), created_at=datetime.fromisoformat(decided["decided_at"])))
                saved.append({"task_id": row.id, "status": "APPLIED", **decided})
            session.commit()
        result = {"plan_id": plan["plan_id"], "executed_at": now(), "items": saved,
            "summary": dict(Counter(value["status"] for value in saved)), "paid_requests_submitted": 0, "batch_resumed": False}
        receipt = self.root / (plan["plan_id"] + "-applied-" + uuid4().hex + ".json")
        receipt.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        result["receipt_path"] = str(receipt)
        return result


def create_collector_triage_router(collector):
    service = CollectorAutoTriage(collector)
    router = APIRouter(tags=["collector-auto-triage"])

    @router.get("/unattended-policy")
    def policy():
        return {"schema": POLICY_SCHEMA, **load_policy(service.settings), "paid_requests_submitted": 0}

    @router.put("/unattended-policy")
    def save_policy(payload: UnattendedPolicy):
        path = Path(service.settings.storage_root) / "collector_inputs/unattended-policy.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".part")
        temporary.write_text(json.dumps(payload.model_dump()), encoding="utf-8")
        temporary.replace(path)
        return {"schema": POLICY_SCHEMA, **load_policy(service.settings), "paid_requests_submitted": 0}

    @router.post("/auto-triage/plan")
    def plan():
        return service.plan()

    @router.post("/auto-triage/apply")
    def apply(payload: ApplyTriage):
        return service.apply(payload)

    return router
