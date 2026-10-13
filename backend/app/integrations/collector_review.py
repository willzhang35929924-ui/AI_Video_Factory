"""Candidate consent is separate from quality approval and paid authorization."""
import hashlib
import json
import re
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.domain import audit, find, new_id, now, task_stage
from app.schemas import ProductionEvent

RESULT_FIELDS = ("video_id", "video_url", "views", "likes", "like_view_ratio", "metric_source", "metric_evidence",
    "reference_product_match", "product_match", "product_match_confidence", "product_match_reason", "ai_origin", "cutout_suitability",
    "overlay_detected", "watermark_detected", "subtitle_detected", "decision", "decision_reason", "download_status", "legacy_vidbee_task_id", "error_code", "error_message")


def verified_video_url(url, video_id):
    if not isinstance(url, str) or not isinstance(video_id, str) or not video_id.isdigit() or any(ord(char) < 33 for char in url):
        return None
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname not in {"tiktok.com", "www.tiktok.com"}
                or parsed.username or parsed.password or parsed.port not in {None, 443} or parsed.query or parsed.fragment
                or not re.fullmatch(r"/@[^/]+/(?:video|photo)/" + re.escape(video_id), parsed.path)):
            return None
    except ValueError:
        return None
    return url


class CandidateDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    action: Literal["accept", "reject"]
    reason: str = Field(min_length=1, max_length=2000)
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    confirmed: Literal[True]


class CandidateBatch(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    action: Literal["accept", "reject"]
    reason: str = Field(min_length=1, max_length=2000)
    candidates: dict[str, str] = Field(min_length=1, max_length=100)
    confirmed: Literal[True]


class CollectorCandidateReview:
    def __init__(self, collector):
        self.collector, self.repository = collector, collector.repository

    def _detail(self, state, task_id):
        task = find(state, "tasks", task_id)
        if task["execution_mode"] != "integration":
            raise HTTPException(409, "NOT_REAL_CANDIDATE")
        context = task["context"].get("task_context", {})
        source = task["context"].get("collection_source", {})
        result = task.get("collector_result") or {}
        identifier = str(result.get("video_id") or context.get("video_id") or source.get("video_id") or "")
        url = result.get("video_url") or context.get("video_url") or source.get("video_url")
        runs = [row for row in state["runs"] if row["task_id"] == task_id and row["provider"] == "LegacyCollectorAdapter"]
        run = runs[-1] if runs else {}
        code = result.get("error_code") or run.get("error_code") or ""
        reason = result.get("error_message") or run.get("error_message") or result.get("decision_reason") or "尚未得到真实采集结果"
        history = task["context"].get("collector_candidate_reviews", [])
        decision = history[-1]["decision"] if history else "PENDING"
        active = (any(row["task_id"] == task_id and row["status"] in {"queued", "running"} for row in state["runs"])
            or any(item["task_id"] == task_id and item["status"] in {"QUEUED", "RUNNING"}
                for batch in state["batches"] if batch["status"] in {"QUEUED", "RUNNING", "PAUSED"} for item in batch["items"]))
        downstream = any(row["task_id"] == task_id and row["type"] in {"person", "background", "compose", "final"} for row in state["assets"])
        safe_url = verified_video_url(url, identifier)
        eligible = (task["status"] == "WAITING_REVIEW" and bool(safe_url) and bool(runs)
            and not active and not downstream and decision != "REJECTED" and result.get("decision_reason") != "watermark_unverified")
        identity = task["context"].get("collection_identity", {})
        duplicate = identity.get("status") == "DUPLICATE_SKIPPED"
        if duplicate:
            eligible = False
        waiting = "WAITING_EXTERNAL_BUDGET" if code == "EXTERNAL_BUDGET_EXHAUSTED" else "WAITING_MANUAL" if code else "WAITING_REVIEW"
        if task["status"] == "REJECTED" or decision == "REJECTED":
            waiting = "REJECTED"
        elif task["status"] == "QUARANTINED":
            waiting = "QUARANTINED"
        elif task["status"] in {"COLLECTED", "COMPLETED"}:
            waiting = task["status"]
        message = {"WAITING_EXTERNAL_BUDGET": "外部额度已耗尽。同意只保存候选意向；新增授权和额度满足后才能恢复，当前不发送任何付费请求。",
            "WAITING_MANUAL": "外部运行条件尚未满足；请按错误原因完成登录、验证或外部请求核实。同意不会重提 UNKNOWN 请求。",
            "WAITING_REVIEW": "候选同意不等于 AI、原片水印或成片质量通过；仍须满足已有分析和原片准入条件。",
            "REJECTED": "候选已拒绝，停止后续生产；原结果、Task、Run、日志及资产历史保留。"}.get(waiting, "沿用已保存的真实采集状态。")
        if waiting == "QUARANTINED":
            message = "无人值守策略已隔离此候选，不参与生产；保留结果/请求回执供补证对账，同意候选不能覆盖未知请求或自动重复计费。"
        if duplicate:
            waiting = "DUPLICATE_SKIPPED"
            message = "全历史视频身份已存在；请查看原任务 " + identity["canonical_task_id"] + "。不重复创建审核或分析请求。"
        snapshot = {"task_id": task_id, "context": context, "video_id": identifier, "video_url": url,
            "task_status": task["status"], "result": result, "run_id": run.get("id"), "run_status": run.get("status"),
            "error_code": code, "review_history": history, "active": active, "downstream": downstream}
        signature = hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        return {"task_id": task_id, "country": task["country"], "keyword": task["keyword"], "context_id": context.get("context_id"),
            "platform": "TikTok", "video_id": identifier or None, "video_url": safe_url,
            "video_url_error": None if safe_url else "缺少可核验的 TikTok 视频 URL 或视频 ID 不一致；未拼造链接。",
            "title": result.get("title") or result.get("legacy_result_reference", {}).get("title"),
            "author": result.get("author") or result.get("legacy_result_reference", {}).get("author"),
            "result": {key: result.get(key) for key in RESULT_FIELDS}, "task_status": task["status"], "state": waiting, "candidate_decision": decision,
            "wait_reason": reason, "error_code": code or None, "next_step": message,
            "reviewable": eligible, "review_unavailable_reason": None if eligible else "仅未进入后续生产、未运行且身份可核验的待审核候选可决定；原片水印请使用原片审核入口。",
            "fingerprint": signature, "history": history, "updated_at": task["updated_at"],
            "runs": [{key: row.get(key) for key in ("id", "provider", "status", "pid", "started_at", "finished_at", "error_code", "error_message")} for row in runs],
            "batch_ids": [row["id"] for row in state["batches"] if task_id in row["task_ids"]],
            "paid_requests_submitted": 0, "quality_approved": False, "publish_authorized": False}

    def detail(self, task_id):
        return self._detail(self.repository.state(), task_id)

    async def decide(self, task_id, payload):
        def change(state):
            detail = self._detail(state, task_id)
            task = find(state, "tasks", task_id)
            history = task["context"].setdefault("collector_candidate_reviews", [])
            repeated = next((row for row in history if row["input_fingerprint"] == payload.fingerprint
                and row["decision"] == ("ACCEPTED" if payload.action == "accept" else "REJECTED") and row["reason"] == payload.reason), None)
            if repeated:
                return {"record": repeated, "idempotent": True}
            if detail["fingerprint"] != payload.fingerprint:
                raise HTTPException(409, "CANDIDATE_CHANGED：请重新检查候选，再确认决定")
            if not detail["reviewable"]:
                raise HTTPException(409, "CANDIDATE_NOT_REVIEWABLE：" + detail["review_unavailable_reason"])
            record = {"id": new_id("candidate-review"), "task_id": task_id, "context_id": detail["context_id"],
                "video_id": detail["video_id"], "video_url": detail["video_url"], "operator": "local_user",
                "decision": "ACCEPTED" if payload.action == "accept" else "REJECTED", "reason": payload.reason,
                "decided_at": now(), "input_fingerprint": detail["fingerprint"], "previous_task_status": task["status"],
                "next_task_status": "REJECTED" if payload.action == "reject" else task["status"],
                "waiting_state": "REJECTED" if payload.action == "reject" else detail["state"],
                "scope": "CANDIDATE_CONSENT_ONLY", "paid_requests_submitted": 0, "quality_approved": False, "publish_authorized": False}
            history.append(record)
            task["context"]["collector_candidate_decision"] = record
            task.update(updated_at=now())
            if payload.action == "reject":
                task.update(status="REJECTED")
                task_stage(state, task_id, "collector").update(status="skipped", error="USER_REJECTED_CANDIDATE")
                for row in state["collector"]:
                    if row.get("last_task_id") == task_id:
                        row.update(status="REJECTED")
                for batch in state["batches"]:
                    for item in batch["items"]:
                        if item["task_id"] == task_id:
                            item.update(status="REJECTED", error="USER_REJECTED_CANDIDATE")
            audit(state, "collector.candidate." + payload.action, record["id"], payload.reason + "；仅候选意向，0请求，不改变旧分析结果", task_id)
            return {"record": record, "idempotent": False}
        saved = self.repository.mutate(change)
        detail = self.detail(task_id)
        if not saved["idempotent"]:
            await self.collector.hub.broadcast(ProductionEvent(type="TASK_DECISION", task_id=task_id, stage="collector",
                status=detail["state"], message="候选决定已保存；" + detail["next_step"], data={"scope": "CANDIDATE_CONSENT_ONLY", "decision": saved["record"]["decision"]}))
        return {**saved, "candidate": detail, "message": "候选决定已持久化；未启动分析、下载或公开发布。"}


def create_collector_review_router(collector):
    router = APIRouter(tags=["collector-review"])
    service = CollectorCandidateReview(collector)

    @router.get("/candidates/{task_id}")
    def detail(task_id: str):
        return service.detail(task_id)

    @router.post("/candidates/{task_id}/decision")
    async def decide(task_id: str, payload: CandidateDecision):
        return await service.decide(task_id, payload)

    @router.post("/candidates/decisions")
    async def decide_batch(payload: CandidateBatch):
        results = []
        for task_id, signature in payload.candidates.items():
            try:
                result = await service.decide(task_id, CandidateDecision(action=payload.action, reason=payload.reason, fingerprint=signature, confirmed=True))
                results.append({"task_id": task_id, "status": "SAVED", **result})
            except HTTPException as exc:
                results.append({"task_id": task_id, "status": "FAILED", "error": str(exc.detail)})
            except ValueError:
                results.append({"task_id": task_id, "status": "FAILED", "error": "CANDIDATE_INPUT_INVALID"})
        return {"results": results, "saved": sum(row["status"] == "SAVED" for row in results), "paid_requests_submitted": 0}

    return router
