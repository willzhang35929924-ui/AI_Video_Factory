import asyncio
import json
import subprocess

from fastapi import HTTPException
from pydantic import ValidationError

from app.action_schemas import ReviewNote
from app.domain import audit, find, new_id, now, task_stage
from app.integrations.asset_registry import file_hash
from app.integrations.process_probe import probe_process


class RealReviewService:
    def __init__(self, production):
        self.production = production
        self.repository = production.repository

    @staticmethod
    def current_lineage(state, task, asset):
        reference = asset.get("legacy_reference", {})
        if reference.get("superseded_by_person_asset_id"):
            return False
        selected = task["context"].get("production", {}).get("current_person_asset_id")
        if not selected:
            return True
        if asset["type"] == "person":
            return asset["id"] == selected
        if asset["type"] == "final" and reference.get("composition_asset_id"):
            composition = next((row for row in state["assets"] if row["id"] == reference["composition_asset_id"] and row["task_id"] == task["id"]), None)
            if not composition or composition.get("legacy_reference", {}).get("superseded_by_person_asset_id"):
                return False
            reference = composition["legacy_reference"]
        return reference.get("person_asset_id") == selected

    def historical_rejection_evidence(self, state, task):
        production = task["context"].get("production", {})
        rejected = production.get("rejected_review", {})
        assets = {row["id"]: row for row in state["assets"]}
        old = assets.get(rejected.get("asset_id"))
        review = next((row for row in state["reviews"] if row["id"] == rejected.get("review_id")
                       and row["task_id"] == task["id"] and row["asset_id"] == rejected.get("asset_id") and row["status"] == "rejected"), None)
        if (not old or not review or old["type"] != "person" or old["task_id"] != task["id"] or old["status"] != "rejected"
                or old["registry_status"] != "registered" or old.get("legacy_reference", {}).get("execution_scope", "PRODUCTION") != "PRODUCTION"):
            return None
        selected = production.get("current_person_asset_id")
        visited, current = set(), assets.get(selected)
        while current:
            if (current["id"] in visited or current["id"] == old["id"] or current["task_id"] != task["id"]
                    or current.get("legacy_reference", {}).get("execution_scope", "PRODUCTION") != "PRODUCTION"):
                return None
            visited.add(current["id"])
            parent_id = current.get("legacy_reference", {}).get("parent_person_asset_id")
            if not parent_id:
                break
            current = assets.get(parent_id)
            if current is None:
                return None
        def audited(action, entity_id):
            return any(row.get("task_id") == task["id"] and row.get("action") == action and row.get("entity_id") == entity_id for row in state["audits"])
        repairs = task["context"].get("matte_repairs", [])
        old_reference = old["legacy_reference"]
        undone = next((row for row in repairs if row.get("asset_id") == old["id"] and row.get("id") == old_reference.get("repair_id")
                       and row.get("run_id") == old_reference.get("processing_run_id") and row.get("person_asset_id") == old_reference.get("parent_person_asset_id")
                       and row.get("status") == "UNDONE"
                       and row.get("task_id") == task["id"] and row.get("execution_scope", "PRODUCTION") == "PRODUCTION"), None)
        if (not undone or not undone.get("applied_at") or not audited("review.reject", review["id"])
                or not audited("repair.applied", undone["id"]) or not audited("repair.undo", undone["id"])):
            return None
        if any(row["task_id"] == task["id"] and row["status"] == "rejected" and (assets.get(row["asset_id"], {}).get("type") == "source"
                or row["asset_id"] in assets and self.current_lineage(state, task, assets[row["asset_id"]])) for row in state["reviews"]):
            return None
        try:
            person = self.production._asset(task["id"], "person")
            if not person or person["id"] != selected or person["status"] != "approved":
                return None
            reviews = [row for row in state["reviews"] if row["task_id"] == task["id"] and row["asset_id"] == selected]
            def review_order(pair):
                parts = pair[1].get("id", "").split("-")
                timestamp = parts[1] if len(parts) > 1 and parts[1].isdigit() else ""
                created = "".join(character for character in str(pair[1].get("created_at") or "") if character.isdigit())[:20]
                return created or timestamp, pair[0]
            approval = max(enumerate(reviews), key=review_order)[1] if reviews else None
            if not approval or approval["status"] != "approved" or not audited("review.approve", approval["id"]):
                return None
            reference = person["legacy_reference"]
            applied = next((row for row in repairs if row.get("asset_id") == selected and row.get("id") == reference.get("repair_id")
                            and row.get("run_id") == reference.get("processing_run_id") and row.get("task_id") == task["id"]
                            and row.get("person_asset_id") == reference.get("parent_person_asset_id")
                            and row.get("status") == "APPLIED" and row.get("execution_scope", "PRODUCTION") == "PRODUCTION"), None)
            run = next((row for row in state["runs"] if applied and row["id"] == applied["run_id"] and row["task_id"] == task["id"]
                        and row["stage"] == "person" and row["provider"] == "FactoryMatteRepair" and row["status"] == "success"), None)
            if (not approval or not applied or not applied.get("applied_at") or not audited("repair.applied", applied["id"])
                    or not run or not isinstance(run.get("pid"), int) or run["pid"] <= 0 or run.get("exit_code") != 0):
                return None
            manifest = json.loads(self.production.registry.allowed_path(reference["manifest_path"]).read_text())
            parent = assets.get(applied["person_asset_id"])
            if (not parent or parent["task_id"] != task["id"] or parent["type"] != "person" or parent["registry_status"] != "registered"
                    or parent.get("legacy_reference", {}).get("execution_scope", "PRODUCTION") != "PRODUCTION"):
                return None
            if (manifest.get("processing_run_id") != run["id"] or manifest.get("repair_id") != applied["id"]
                    or manifest.get("parent_alpha_sha256") != applied.get("parent_alpha_sha256")
                    or applied.get("parent_rgb_sha256") != person["sha256"] or parent["sha256"] != person["sha256"]
                    or applied.get("parent_alpha_sha256") != parent["legacy_reference"].get("alpha_sha256")
                    or self.production.registry.allowed_path(manifest["parent_rgb_path"]) != self.production.registry.allowed_path(parent["storage_path"])
                    or self.production.registry.allowed_path(manifest["parent_alpha_path"]) != self.production.registry.allowed_path(parent["legacy_reference"]["alpha_path"])
                    or file_hash(self.production.registry.allowed_path(parent["legacy_reference"]["alpha_path"])) != applied["parent_alpha_sha256"]
                    or file_hash(self.production.registry.content(selected)[0]) != person["sha256"]):
                return None
        except (HTTPException, OSError, ValueError, KeyError):
            return None
        return {"person_asset_id": selected, "person_rgb_sha256": person["sha256"], "person_alpha_sha256": reference["alpha_sha256"],
                "person_review_id": approval["id"], "person_run_id": run["id"], "rejected_review": rejected}

    async def reconcile_historical_rejections(self):
        """Repair only an audited UNDONE sibling rejection that wrongly stopped an approved current mother."""
        recovered = []
        if self.production.settings.app_mode != "integration":
            return recovered
        policy = self.production.policy()
        if not policy["auto_resume"] or not policy["auto_advance"]:
            return recovered
        snapshot = self.repository.state()
        task_ids = [task["id"] for task in snapshot["tasks"]]
        for task_id in task_ids:
            task = find(snapshot, "tasks", task_id)
            production = task["context"].get("production", {})
            if task["execution_mode"] != "integration" or task["status"] != "REJECTED" or not production.get("automatic"):
                continue
            origin = next((row for row in snapshot["batches"] if row["id"] == production.get("batch_id") and task["id"] in row["task_ids"]), None)
            if not origin or origin["status"] in {"PAUSED", "CANCELLED"}:
                continue
            runs = [row for row in snapshot["runs"] if row["task_id"] == task["id"]]
            if any(row["status"] in {"queued", "running"} or row.get("error_code") == "UNKNOWN_EXTERNAL_REQUEST" for row in runs):
                continue
            blocked = False
            for run in runs:
                if run["id"] not in self.production.recovered_run_ids or not run.get("pid"):
                    continue
                try:
                    probe_process(run["pid"])
                except ProcessLookupError:
                    continue
                except PermissionError:
                    blocked = True
                else:
                    blocked = True
                if blocked:
                    break
            if blocked:
                continue
            evidence = await asyncio.to_thread(self.historical_rejection_evidence, snapshot, task)
            if not evidence:
                continue
            def reconcile(state):
                selected = find(state, "tasks", task["id"])
                context = selected["context"]["production"]
                if selected["status"] != "REJECTED" or context.get("current_person_asset_id") != evidence["person_asset_id"] or context.get("rejected_review") != evidence["rejected_review"]:
                    raise HTTPException(409, "CURRENT_VERSION_CHANGED")
                context.setdefault("rejected_review_history", []).append({**context.pop("rejected_review"), "reconciled_at": now(), "scope": "HISTORICAL_VERSION"})
                context["review_approved"] = True
                if context.get("deferred_continue", {}).get("status") == "PENDING" and context["deferred_continue"].get("person_asset_id") != evidence["person_asset_id"]:
                    context["deferred_continue"].update(status="SUPERSEDED", reason="CURRENT_PERSON_CHANGED", checked_at=now())
                selected.update(status="COLLECTED", current_stage="quality", updated_at=now())
                audit(state, "production.historical_rejection_reconciled", evidence["rejected_review"]["review_id"],
                      f"UNDONE旧分支拒绝保留；当前 {evidence['person_asset_id']} 真人批准、RGB/Alpha hash与成功run已核实；正式恢复当前版本质检，不继承旧成片批准", task["id"])
            self.repository.mutate(reconcile)
            outcome = await self.production.continue_task(task["id"], stage="quality")
            recovered.append({**outcome, **evidence, "recovered": True})
            if outcome.get("accepted"):
                await self.production.launch(outcome["batch_id"])
            snapshot = self.repository.state()
        return recovered

    async def action(self, action, entity_id, payload):
        try:
            payload = ReviewNote.model_validate(payload).model_dump()
        except ValidationError:
            raise HTTPException(422, "INVALID_REVIEW_PAYLOAD") from None
        state = self.repository.state()
        review = next((row for row in state["reviews"] if row["id"] == entity_id), None)
        task_id = review["task_id"] if review else entity_id
        task = find(state, "tasks", task_id)
        if task["execution_mode"] != "integration":
            raise HTTPException(409, "REAL_REVIEW_ONLY")
        if action in {"interval", "fix_interval"}:
            raise HTTPException(409, "USE_MATTE_REPAIR：请从局部修复入口选择母资产、区间和标注")
        asset = next((row for row in state["assets"] if review and row["id"] == review["asset_id"]), None)
        if asset and asset["type"] == "source":
            if asset["registry_status"] != "registered" or asset.get("legacy_reference", {}).get("execution_scope") == "TECHNICAL_VALIDATION":
                raise HTTPException(409, "SOURCE_REVIEW_NOT_ELIGIBLE")
            result = await self.production.source_review.action(action, review, payload)
            await self.production.event(task_id, "review", result["message"])
            if action == "approve":
                return {**result, **await self.production.continue_task(task_id)}
            return result
        if asset and asset["legacy_reference"].get("execution_scope") == "TECHNICAL_VALIDATION":
            if action not in {"approve", "reject", "fix"}:
                raise HTTPException(409, "TECHNICAL_VALIDATION：请从局部修复入口使用已保存母资产和背景；不能启动生产重试")
            if review["status"] in {"approved", "rejected"}:
                raise HTTPException(409, "REVIEW_ALREADY_DECIDED")
            self.production.registry.content(asset["id"])
            def decide_technical(current):
                find(current, "reviews", review["id"]).update(status={"approve": "approved", "reject": "rejected", "fix": "manual"}[action], note=str(payload.get("note", ""))[:2000])
                audit(current, "technical.review." + action, asset["id"], "仅技术媒体验收；不改变采集决策、不登记 FINAL", task_id)
            self.repository.mutate(decide_technical)
            return {"message": "技术审核已保存；生产任务与采集决策不变，未登记 FINAL", "task_id": task_id}
        if action in {"retry", "reprocess"}:
            if asset and not self.current_lineage(state, task, asset):
                raise HTTPException(409, "HISTORICAL_VERSION_RESTORE_REQUIRED：这是历史版本，请先明确恢复该母资产；不会重跑或停止当前版本")
            stage = "cutout" if action == "reprocess" else "quality" if asset and asset["type"] == "person" else "compose"
            return await self.production.start([task_id], retry=True, stage=stage)
        if not review:
            raise HTTPException(409, "该任务尚无可审核的真实成片资产")
        asset = find(state, "assets", review["asset_id"])
        if asset["task_id"] != task_id or asset["registry_status"] != "registered" or asset["type"] not in {"person", "compose", "final"}:
            raise HTTPException(409, "尚无真实人物或成片文件，不能人工放行为成片")
        if action not in {"approve", "reject", "fix"}:
            raise HTTPException(422, "UNKNOWN_REVIEW_ACTION")
        if review["status"] in {"approved", "rejected"}:
            raise HTTPException(409, "REVIEW_ALREADY_DECIDED：历史结果保留，请重新处理创建新版本")
        selected_person = task["context"].get("production", {}).get("current_person_asset_id")
        current_lineage = self.current_lineage(state, task, asset)
        if action == "approve" and not current_lineage:
            raise HTTPException(409, "SUPERSEDED_VERSION：母资产已更新，请审核重新合成的新版本；旧结果保留")
        # Confirm the registered file is still available before accepting it.
        self.production.registry.content(asset["id"])
        note = str(payload.get("note", ""))[:2000]
        if not current_lineage:
            def historical(current):
                if self.current_lineage(current, find(current, "tasks", task_id), find(current, "assets", asset["id"])):
                    raise HTTPException(409, "CURRENT_VERSION_CHANGED：所选历史版本已成为当前版本，请刷新后审核")
                row = find(current, "reviews", review["id"])
                row.update(status="rejected" if action == "reject" else "manual", note=note)
                if action == "reject":
                    find(current, "assets", asset["id"])["status"] = "rejected"
                audit(current, "review." + action, review["id"], f"历史版本 {asset['id']}；不改变当前母资产、任务或生产队列；{note}", task_id)
            self.repository.mutate(historical)
            await self.production.event(task_id, "review", "历史版本审核已保存；当前母资产与生产队列不变")
            return {"message": "历史版本审核已保存；当前版本继续按其真实审核结果生产", "task_id": task_id,
                    "review_id": review["id"], "scope": "HISTORICAL_VERSION"}
        if action == "approve" and asset["type"] == "compose":
            reference = asset["legacy_reference"]
            person = next((row for row in state["assets"] if row["id"] == reference.get("person_asset_id")), None)
            background = next((row for row in state["assets"] if row["id"] == reference.get("background_asset_id")), None)
            # Verify/register the final file before persisting an approved decision.
            run_id = new_id("run")
            self.repository.mutate(lambda current: current["runs"].append({"id": run_id, "task_id": task_id, "stage": "review",
                "provider": "HumanReview", "status": "running", "progress": 0, "logs": ["确认已登记成片"], "created_at": now(), "started_at": now()}))
            try:
                self.production.registry.register(task, {"path": asset["storage_path"], "sha256": asset["sha256"],
                    "source_system": "HumanReview", "legacy_reference": {"review_id": review["id"], "composition_asset_id": asset["id"]}},
                    "final", run_id, prompt_id=reference.get("prompt_id"), person=person, background=background)
            except (ValueError, OSError, subprocess.SubprocessError):
                self.repository.mutate(lambda current: find(current, "runs", run_id).update(status="failed", error_code="FINAL_ASSET_NOT_VERIFIED", finished_at=now()))
                raise
            self.repository.mutate(lambda current: find(current, "runs", run_id).update(status="success", progress=100, finished_at=now()))
        def change(current):
            if not self.current_lineage(current, find(current, "tasks", task_id), find(current, "assets", asset["id"])):
                raise HTTPException(409, "CURRENT_VERSION_CHANGED：母资产已变化，未把此次审核应用到新版本")
            row = find(current, "reviews", review["id"])
            row.update(status={"approve": "approved", "reject": "rejected", "fix": "manual"}[action], note=note)
            if action != "fix":
                find(current, "assets", asset["id"])["status"] = "approved" if action == "approve" else "rejected"
            business_asset_ids = {item["id"] for item in current["assets"] if item["task_id"] == task_id and self.production.business_asset(item)
                and not item.get("legacy_reference", {}).get("superseded_by_person_asset_id")
                and (item["type"] not in {"person", "compose", "final"} or self.current_lineage(current, find(current, "tasks", task_id), item))}
            remaining = [item for item in current["reviews"] if item["task_id"] == task_id and item["asset_id"] in business_asset_ids and item["status"] in {"pending", "manual"}]
            task = find(current, "tasks", task_id)
            production = task["context"].setdefault("production", {})
            production["review_approved"] = action == "approve"
            if action == "reject":
                production["rejected_review"] = {"review_id": review["id"], "asset_id": asset["id"], "decided_at": now()}
            elif action == "approve" and production.get("rejected_review"):
                previous = production["rejected_review"]
                if previous.get("asset_id") != asset["id"]:
                    production.setdefault("rejected_review_history", []).append(production.pop("rejected_review"))
            checkpoint = production.get("first_composition", {})
            if checkpoint.get("composition_asset_id") == asset["id"] and action == "approve":
                production["first_composition"] = {**checkpoint, "human_review": "APPROVED", "review_id": review["id"], "approved_at": now()}
            latest_prompts = {item.get("ordinal") or item["id"]: item["id"] for item in current["prompts"]
                              if item["task_id"] == task_id and item["status"] != "MOCK"}
            approved_assets = {item["asset_id"] for item in current["reviews"]
                               if item["task_id"] == task_id and item["status"] == "approved"}
            assets_by_id = {item["id"]: item for item in current["assets"]}
            approved_prompts = set()
            for item in current["assets"]:
                if item["id"] not in approved_assets or item["type"] not in {"compose", "final"}:
                    continue
                composition = assets_by_id.get(item["legacy_reference"].get("composition_asset_id"), item)
                reference = composition["legacy_reference"]
                if reference.get("superseded_by_person_asset_id") or selected_person and reference.get("person_asset_id") != selected_person:
                    continue
                if reference.get("factory_fingerprint") and reference["factory_fingerprint"] != self.production._fingerprint(
                    task, "compose", reference.get("prompt_id"), assets_by_id.get(reference.get("person_asset_id")),
                    assets_by_id.get(reference.get("background_asset_id")),
                ):
                    continue
                approved_prompts.add(reference.get("prompt_id"))
            incomplete = sorted(set(latest_prompts.values()) - approved_prompts) if asset["type"] != "person" else []
            task["context"]["production"]["incomplete_prompt_ids"] = incomplete
            awaiting = bool(remaining or (incomplete and action == "approve"))
            accepted_status = "COLLECTED" if asset["type"] == "person" else "COMPLETED"
            active_run = next((item for item in current["runs"] if item["id"] == production.get("active_run_id")
                and item["task_id"] == task_id and item["stage"] == production.get("active_stage")
                and item["status"] in {"queued", "running"} and item["provider"] not in {"mock", "HumanReview", "FactoryMatteRepair"}), None)
            active_batch = next((item for item in current["batches"] if item["id"] == production.get("batch_id")
                and item.get("active_task_id") == task_id and any(row["task_id"] == task_id and row["status"] == "RUNNING" for row in item["items"])), None)
            preserve_execution = action == "approve" and (active_run and active_batch
                or task["status"] in {"CANCELLED", "REJECTED", "SKIPPED", "PAUSED"})
            if not preserve_execution:
                task.update(status="REJECTED" if action == "reject" else "WAITING_REVIEW" if awaiting else accepted_status if action == "approve" else "WAITING_REVIEW",
                            current_stage="prompt" if action == "approve" and asset["type"] == "person" else "review")
            task["updated_at"] = now()
            stage = next((item for item in current["stages"] if item["task_id"] == task_id and item["name"] == "review"), None)
            if stage:
                stage.update(status="waiting_review" if awaiting else "success" if action == "approve" else "failed", end_time=now())
            audit(current, "review." + action, review["id"], f"真实资产 {asset['id']}；{note}", task_id)
        self.repository.mutate(change)
        if action == "approve" and asset["type"] == "compose":
            self.repository.mutate(lambda current: task_stage(current, task_id, "final").update(status="success", end_time=now()))
        await self.production.event(task_id, "review", "审核结果已保存；保留原资产和历史版本")
        if action == "approve" and asset["type"] in {"person", "compose"} and find(self.repository.state(), "tasks", task_id)["status"] != "COMPLETED":
            return {"review_id": review["id"], **await self.production.continue_task(task_id)}
        return {"message": "审核结果已保存", "task_id": task_id, "review_id": review["id"]}
