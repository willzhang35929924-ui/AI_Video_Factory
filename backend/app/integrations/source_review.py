"""Human watermark attestation for an otherwise eligible, verified original video."""
import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException

from app.domain import audit, find, new_id, now, task_stage
from app.integrations.asset_registry import file_hash
from app.integrations.contracts import TaskContext
from app.integrations.legacy.vidbee_adapter import VidbeeDownloaderAdapter
from app.integrations.production_rules import verified_factory_rules

WATERMARK_UNVERIFIED_MESSAGE = "VidBee completed the download but did not provide a verifiable watermark-free attestation"

def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class SourceWatermarkReview:
    def __init__(self, repository, settings, collector, vidbee=None):
        self.repository, self.settings, self.collector = repository, settings, collector
        self.vidbee = vidbee or VidbeeDownloaderAdapter(settings)

    def evidence(self, task_id, *, allow_active=False):
        try:
            return self._evidence(task_id, allow_active=allow_active)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            raise HTTPException(409, "SOURCE_REVIEW_EVIDENCE_INVALID：" + type(exc).__name__) from None

    def _evidence(self, task_id, *, allow_active=False):
        state = self.repository.state()
        if any(run["task_id"] == task_id and run.get("error_code") == "UNKNOWN_EXTERNAL_REQUEST" for run in state["runs"]):
            raise HTTPException(409, "UNKNOWN_EXTERNAL_REQUEST：不能以水印审核放行未确认外部请求")
        task, context, source, origin = self.collector._verified_source_origin(task_id, allow_active=allow_active)
        result = task["collector_result"]
        origin_result = json.loads(Path(origin["result_path"]).read_text())
        if any(origin_result.get(key) != result.get(key) for key in ("video_id", "video_url", "views", "likes", "metric_source", "metric_evidence",
                "reference_product_match", "product_match", "product_match_confidence", "product_match_reason", "ai_origin", "cutout_suitability",
                "overlay_detected", "watermark_detected", "decision", "decision_reason", "legacy_vidbee_task_id", "download_status")):
            raise HTTPException(409, "SOURCE_REVIEW_RESULT_CHANGED：当前结果与真实 Collector Run 不一致")
        if (result.get("error_code") or result.get("decision") != "manual_review" or result.get("decision_reason") != "watermark_unverified"
                or result.get("download_status") != "completed" or result.get("reference_product_match") != "pass"
                or result.get("product_match") != "pass" or result.get("ai_origin") != "likely_real" or result.get("watermark_detected") is True
                or any(result.get(key) != context[key] for key in context)):
            raise HTTPException(409, "SOURCE_REVIEW_NOT_ELIGIBLE：仅水印未核验且其余真实分析通过的原片可审核")
        rules = task["context"]["production_rules"]["rules"]
        if context.get("factory_rules"):
            rules = {**rules, **verified_factory_rules(context["factory_rules"])}
        if rules.get("vidbee_require_watermark_review") is not True or rules.get("accepted_product_match") != "pass" or rules.get("accepted_ai_origin") != "likely_real":
            raise HTTPException(409, "SOURCE_REVIEW_RULES_MISMATCH")
        if (result.get("views") is None or result.get("likes") is None or result["views"] < rules["min_views"]
                or result["likes"] < (rules.get("min_likes") or 0)
                or (result["likes"] / result["views"] if result["views"] else 0) < rules["min_like_view_ratio"]
                or result.get("product_match_confidence") is None or result["product_match_confidence"] < rules["product_match_min_confidence"]
                or rules.get("accepted_require_cutout_suitability", True) and result.get("cutout_suitability") != rules["accepted_cutout_suitability"]):
            raise HTTPException(409, "SOURCE_REVIEW_GATE_FAILED")
        directory = self.settings.storage_root / "projects" / task["project_id"] / "tasks" / task_id / "metadata"
        checkpoint_path = directory / "collector-state.json"
        checkpoint = json.loads(checkpoint_path.read_text())
        row = checkpoint["row"]
        report_path = Path(row.get("analysis_report", "")).resolve()
        if (checkpoint.get("status") != "finished" or row.get("decision") != "manual_review" or row.get("reason") != "watermark_unverified"
                or row.get("error") not in {None, "", WATERMARK_UNVERIFIED_MESSAGE} or row.get("analysis_status") != "completed" or row.get("reference_product_match") != "pass"
                or float(row.get("reference_product_confidence") or 0) < rules["product_match_min_confidence"]
                or not report_path.is_relative_to(directory / "legacy-work/data/semantic_analysis") or not report_path.is_file()):
            raise HTTPException(409, "SOURCE_REVIEW_ANALYSIS_NOT_CONFIRMED")
        report = json.loads(report_path.read_text())
        response_path, state_path = report_path.parent / "attempt-001/response.json", report_path.parent / "state.json"
        response, semantic_state = json.loads(response_path.read_text()), json.loads(state_path.read_text())
        if (report.get("analysis_status") != "completed" or report.get("analysis_identity") != row.get("analysis_identity")
                or response.get("status") != "completed" or response.get("result") != report
                or semantic_state.get("status") != "completed" or semantic_state.get("identity") != row.get("analysis_identity") or semantic_state.get("result") != report
                or any(report.get(key) != result.get(key) or row.get(key) != result.get(key) for key in ("product_match", "ai_origin", "cutout_suitability"))):
            raise HTTPException(409, "SOURCE_REVIEW_ANALYSIS_NOT_CONFIRMED")
        return {"scope": "SOURCE_WATERMARK_ONLY", "task_context": context, "source_asset_id": source["id"], "source_video_id": task["source_video_id"],
            "source_sha256": source["sha256"], "source_path": str(Path(source.get("external_path") or source["storage_path"]).resolve()),
            "source_size": source["size"], "source_mtime_ns": source["probe_metadata"]["mtime_ns"], "full_decode_verified": True,
            "legacy_video_id": result["video_id"], "legacy_video_url": result["video_url"], "legacy_vidbee_task_id": result["legacy_vidbee_task_id"],
            "production_rules_sha256": task["context"]["production_rules"]["sha256"],
            "product_image_hashes": [{"path": path, "sha256": file_hash(path)} for path in context["product_images"]],
            "origin_collector_run_id": origin["run"]["id"], "origin_collector_result_path": origin["result_path"], "origin_collector_result_sha256": origin["result_sha256"],
            "collector_result_sha256": fingerprint(result), "analysis_report_path": str(report_path), "analysis_report_sha256": file_hash(report_path),
            "analysis_response_sha256": file_hash(response_path), "analysis_state_sha256": file_hash(state_path)}

    def register_pending(self, task_id, *, allow_active=False):
        task = find(self.repository.state(), "tasks", task_id)
        if task["collector_result"].get("decision_reason") != "watermark_unverified":
            return None
        evidence = self.evidence(task_id, allow_active=allow_active)
        signature = fingerprint(evidence)
        review_id = new_id("review")
        def change(state):
            current = find(state, "tasks", task_id)
            records = current["context"].setdefault("source_watermark_reviews", [])
            existing = next((item for item in records if item["fingerprint"] == signature), None)
            if existing:
                return existing["review_id"]
            if self.evidence(task_id, allow_active=allow_active) != evidence:
                raise HTTPException(409, "SOURCE_REVIEW_EVIDENCE_CHANGED")
            state["reviews"].append({"id": review_id, "task_id": task_id, "asset_id": evidence["source_asset_id"], "status": "pending", "issues": ["WATERMARK_UNVERIFIED"],
                "note": "原片水印待人工核验。须完整播放并确认全程无水印；仅原片准入，不代表 AI accepted 或最终成片。"})
            records.append({"review_id": review_id, "asset_id": evidence["source_asset_id"], "status": "pending", "fingerprint": signature,
                "evidence": evidence, "created_at": now()})
            task_stage(state, task_id, "review").update(status="waiting_review", error="WATERMARK_UNVERIFIED")
            current.update(status="WAITING_REVIEW", current_stage="review", updated_at=now())
            audit(state, "source.watermark_review_created", review_id, "仅原片水印审核；旧采集决策保持 manual_review", task_id)
            return review_id
        self.repository.mutate(change)
        saved = find(self.repository.state(), "tasks", task_id)["context"]["source_watermark_reviews"]
        return next(item["review_id"] for item in saved if item["fingerprint"] == signature)

    def approval_record(self, task_id):
        state = self.repository.state()
        task = find(state, "tasks", task_id)
        return next((item for item in reversed(task["context"].get("source_watermark_reviews", [])) if item["status"] == "approved"
            and item.get("confirmed_full_video_watermark_free") is True
            and any(review["id"] == item["review_id"] and review["asset_id"] == item["asset_id"] and review["status"] == "approved"
                    and review["issues"] == ["WATERMARK_UNVERIFIED"] for review in state["reviews"])), None)

    async def verify_external(self, evidence, *, allow_active=False):
        context = evidence["task_context"]
        task_id = context["task_id"]
        directory = self.settings.storage_root / "projects" / context["project_id"] / "tasks" / task_id / "metadata/source-review-verifications" / new_id("check")
        row = {"vidbee_task_id": evidence["legacy_vidbee_task_id"], "video_id": evidence["legacy_video_id"], "url": evidence["legacy_video_url"],
            "context_id": context["context_id"], "download_path": evidence["source_path"], "download_source_sha256": evidence["source_sha256"], "download_source_type": "vidbee_automation"}
        checked = await self.vidbee.verify_completed_once(context, row, directory)
        process = checked.pop("_process", {})
        metadata = checked.get("asset", {})
        ready = (checked.get("status") == "READY" and process.get("pid") and process.get("exit_code") == 0
            and metadata.get("sha256") == evidence["source_sha256"] and Path(metadata.get("path", "")).resolve() == Path(evidence["source_path"])
            and metadata.get("download_verification", {}).get("association") == "same_file"
            and metadata.get("download_verification", {}).get("task_id") == evidence["legacy_vidbee_task_id"])
        run_id = new_id("run")
        proof_path = directory / "source-review-verification.json"
        proof_path.write_text(json.dumps({"operation": "verify_existing_source", "evidence": evidence, "result": checked, "process": process,
            "run_id": run_id, "download_submission_count": 0, "search_count": 0, "paid_requests": 0}, ensure_ascii=False, indent=2))
        self.repository.mutate(lambda state: state["runs"].append({"id": run_id, "task_id": task_id, "stage": "download", "provider": "VidbeeDownloaderAdapter",
            "status": "success" if ready else "failed", "progress": 100 if ready else 0, "created_at": now(),
            "started_at": datetime.fromtimestamp(process["started_at"], timezone.utc).isoformat() if process.get("started_at") else None,
            "finished_at": datetime.fromtimestamp(process["finished_at"], timezone.utc).isoformat() if process.get("finished_at") else None,
            **{key: process.get(key) for key in ("pid", "exit_code", "duration_ms", "cwd", "stdout_log_path", "stderr_log_path")}, "command": process.get("command") or [],
            "logs": ["[REAL] verify_existing_source；水印审核/生产恢复核验，0 新下载/搜索/付费；evidence=" + str(proof_path)],
            "error_code": None if ready else "SOURCE_REVIEW_DOWNLOAD_NOT_VERIFIED", "error_message": None if ready else "原 VidBee 同文件 completed 证据未确认"}))
        if not ready or await asyncio.to_thread(self.evidence, task_id, allow_active=allow_active) != evidence:
            raise HTTPException(409, "SOURCE_REVIEW_EVIDENCE_CHANGED：未放行原片")
        return {"verification_run_id": run_id, "path": str(proof_path), "sha256": file_hash(proof_path), "verified_at": now()}

    async def validate_approval(self, task_id, *, allow_active=False):
        record = self.approval_record(task_id)
        if not record:
            return False
        evidence = await asyncio.to_thread(self.evidence, task_id, allow_active=allow_active)
        if record["fingerprint"] != fingerprint(evidence) or record["evidence"] != evidence:
            raise HTTPException(409, "SOURCE_REVIEW_APPROVAL_EXPIRED：原片或上下文/配置/分析已变化")
        await self.verify_external(evidence, allow_active=allow_active)
        if self.approval_record(task_id) != record:
            raise HTTPException(409, "SOURCE_REVIEW_APPROVAL_CHANGED")
        return True

    async def action(self, action, review, payload):
        if action not in {"approve", "reject"}:
            raise HTTPException(409, "SOURCE_REVIEW_ONLY：原片仅支持水印通过/拒绝，不能按成片抠像修复或重试")
        if review["issues"] != ["WATERMARK_UNVERIFIED"] or review["status"] not in {"pending", "manual"}:
            raise HTTPException(409, "SOURCE_REVIEW_ALREADY_DECIDED")
        task_id = review["task_id"]
        if action == "approve" and payload.get("confirmed_full_video_watermark_free") is not True:
            raise HTTPException(409, "FULL_VIDEO_WATERMARK_CONFIRMATION_REQUIRED：请完整播放原片并确认全程无水印")
        evidence = await asyncio.to_thread(self.evidence, task_id)
        task = find(self.repository.state(), "tasks", task_id)
        record = next((item for item in task["context"].get("source_watermark_reviews", []) if item["review_id"] == review["id"]), None)
        if not record or record["evidence"] != evidence or record["fingerprint"] != fingerprint(evidence) or record["asset_id"] != review["asset_id"]:
            raise HTTPException(409, "SOURCE_REVIEW_APPROVAL_EXPIRED")
        verification = await self.verify_external(evidence) if action == "approve" else None
        def decide(state):
            current = find(state, "tasks", task_id)
            if self.evidence(task_id) != evidence:
                raise HTTPException(409, "SOURCE_REVIEW_EVIDENCE_CHANGED")
            current_review = find(state, "reviews", review["id"])
            saved = next(item for item in current["context"]["source_watermark_reviews"] if item["review_id"] == review["id"])
            if current_review["status"] not in {"pending", "manual"} or saved != record:
                raise HTTPException(409, "SOURCE_REVIEW_ALREADY_DECIDED")
            status = "approved" if action == "approve" else "rejected"
            current_review.update(status=status, note=payload.get("note", ""))
            saved.update(status=status, confirmed_full_video_watermark_free=action == "approve", decided_at=now(),
                operator="review_ui_confirmation", verification=verification)
            current.update(status="COLLECTED" if action == "approve" else "REJECTED", current_stage="download" if action == "approve" else "review", updated_at=now())
            if action == "approve":
                task_stage(state, task_id, "review").update(status="pending", error=None)
            # This does not complete the final-video review stage or modify the legacy AI decision.
            audit(state, "source.watermark." + action, review["id"], "人工原片水印确认；旧 Decision=manual_review 保持不变；没有启动生产或登记 FINAL", task_id)
        self.repository.mutate(decide)
        return {"message": "原片水印审核已保存；不是成片完成。请回到任务页显式开始生产。", "task_id": task_id, "review_id": review["id"], "scope": "SOURCE_WATERMARK_ONLY"}
