import csv
import hashlib
import json
import math
import mimetypes
import os
import shutil
import subprocess
import threading
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import models as m
from app.domain import new_id, now
from app.integrations.process_runner import ProcessRunner


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def probe_media(path, *, count_frames=False):
    path = Path(path)
    if not path.is_file() or not path.stat().st_size:
        raise ValueError("OUTPUT_NOT_FOUND")
    process = subprocess.run(["ffprobe", "-v", "error", *(["-count_frames"] if count_frames else []), "-show_streams", "-show_format", "-of", "json", str(path)],
                             shell=False, capture_output=True, text=True, check=True, timeout=600 if count_frames else 30)
    if count_frames and process.stderr.strip():
        raise ValueError("MEDIA_FULL_DECODE_FAILED")
    data = json.loads(process.stdout)
    stream = next((row for row in data["streams"] if row["codec_type"] == "video"), None)
    if not stream or not stream.get("width") or not stream.get("height"):
        raise ValueError("MEDIA_STREAM_INVALID")
    rate = stream.get("avg_frame_rate", "0/0")
    if rate == "0/0":
        rate = stream.get("r_frame_rate", "0/0")
    fps = float(Fraction(rate)) if rate != "0/0" else 0
    frame_count = int(stream.get("nb_read_frames") or stream.get("nb_frames")) if str(stream.get("nb_read_frames") or stream.get("nb_frames", "")).isdigit() else None
    container_duration = float(data.get("format", {}).get("duration") or 0)
    duration = float(stream.get("duration") or (frame_count / fps if count_frames and frame_count and fps else container_duration))
    return {"width": stream["width"], "height": stream["height"], "fps": fps,
            "r_frame_rate": stream.get("r_frame_rate"), "avg_frame_rate": stream.get("avg_frame_rate"),
            "duration": duration, "container_duration": container_duration, "video_start_time": stream.get("start_time"),
            "codec": stream.get("codec_name", "UNKNOWN"), "frame_count": frame_count,
            "pix_fmt": stream.get("pix_fmt"), "bits_per_raw_sample": stream.get("bits_per_raw_sample"),
            "audio": [{key: row.get(key) for key in ("codec_name", "sample_rate", "channels", "start_time", "duration")} for row in data["streams"] if row["codec_type"] == "audio"]}


class AssetRegistry:
    def __init__(self, repository, settings):
        self.repository, self.settings = repository, settings
        self.alpha_preview_lock = threading.RLock()

    def storage_usage(self):
        root = self.settings.storage_root.resolve()
        logical_bytes, allocated_bytes, file_count, errors, inodes = 0, 0, 0, [], set()
        for path in root.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                stat = path.stat()
                logical_bytes += stat.st_size
                file_count += 1
                identity = (stat.st_dev, stat.st_ino)
                if identity not in inodes:
                    allocated_bytes += stat.st_blocks * 512
                    inodes.add(identity)
            except OSError as exc:
                errors.append({"path": str(path), "error": exc.strerror or str(exc)})
        disk = shutil.disk_usage(root)
        return {"root": str(root), "status": "PARTIAL" if errors else "PASS", "logical_bytes": logical_bytes,
                "allocated_bytes": allocated_bytes, "file_count": file_count, "disk_free_bytes": disk.free,
                "disk_total_bytes": disk.total, "errors": errors[:10], "checked_at": now()}

    def deletion_statuses(self, *, scope="production", asset_ids=None):
        visible = self.repository.state(scope=scope)["assets"]
        if asset_ids is not None:
            selected = set(asset_ids)
            visible = [asset for asset in visible if asset["id"] in selected]
        state = self.repository.state()
        with self.repository.lock, Session(self.repository.engine) as session:
            return [self._deletion_plan(asset, state, session) for asset in visible]

    def deletion_status(self, asset_id, *, scope="production"):
        visible = self.repository.state(scope=scope)["assets"]
        asset = next((row for row in visible if row["id"] == asset_id), None)
        if not asset:
            raise HTTPException(404, "资产不存在或不属于当前素材视图")
        with self.repository.lock, Session(self.repository.engine) as session:
            return self._deletion_plan(asset, self.repository.state(), session)

    @staticmethod
    def _contains_identifier(value, identifier):
        if isinstance(value, dict):
            return any(AssetRegistry._contains_identifier(item, identifier) for item in value.values())
        if isinstance(value, list):
            return any(AssetRegistry._contains_identifier(item, identifier) for item in value)
        return value == identifier

    def _asset_files(self, asset):
        reference = asset.get("legacy_reference", {})
        values = [asset.get("external_path") or asset.get("storage_path")]
        values.extend(reference[key] for key in ("alpha_path", "manifest_path", "preview_path", "thumbnail_path",
            "frame_hashes_path", "time_mapping_path", "source_binding_path") if isinstance(reference.get(key), str))
        paths = {Path(value).absolute() for value in values if value}
        manifest = reference.get("manifest_path")
        if manifest and Path(manifest).is_file():
            manifest_path = Path(manifest).absolute()
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            for row in data.get("previews", []):
                filename = row.get("filename")
                if filename:
                    path = (manifest_path.parent / filename).absolute()
                    if not path.resolve().is_relative_to(manifest_path.parent.resolve()):
                        raise ValueError("ASSET_PREVIEW_PATH_OUTSIDE_MANIFEST")
                    paths.add(path)
        cache = self.settings.storage_root / "diagnostic_previews"
        if cache.is_dir():
            for result in cache.glob("*/preview.json"):
                data = json.loads(result.read_text(encoding="utf-8"))
                if data.get("inputs", {}).get("asset_id") == asset["id"]:
                    paths.update(path.absolute() for path in result.parent.rglob("*") if path.is_file())
        return paths

    @staticmethod
    def _file_writers(paths):
        if os.name == "nt" and paths:
            raise ValueError("WINDOWS_WRITER_PROBE_NOT_AVAILABLE")
        executable = shutil.which("lsof")
        if not executable or not paths:
            return []
        targets, identities = set(), set()
        for path in set(map(Path, paths)):
            targets.update((str(path.absolute()), str(path.resolve())))
            stat = path.stat()
            identities.add((stat.st_dev, stat.st_ino))
        result = subprocess.run([executable, "-nP", "-w", "-F0pafnDi"], shell=False,
                                capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            raise ValueError("FILE_LOCK_CHECK_FAILED")
        writers, pid, descriptor = set(), None, {}

        def observe():
            if (pid and descriptor.get("access") in {"w", "u"}
                    and (descriptor.get("name") in targets or (descriptor.get("device"), descriptor.get("inode")) in identities)):
                writers.add(pid)

        for field in result.stdout.split("\0"):
            field = field.lstrip("\n")
            if not field:
                continue
            key, value = field[0], field[1:]
            if key in {"p", "f"}:
                observe()
                descriptor = {}
                if key == "p":
                    pid = value
            elif key == "a":
                descriptor["access"] = value
            elif key == "n":
                descriptor["name"] = value
            elif key == "D":
                descriptor["device"] = int(value, 0)
            elif key == "i":
                descriptor["inode"] = int(value)
        observe()
        return sorted(writers)

    def _deletion_plan(self, asset, state, session, *, check_writers=True, file_paths=None, path_owners=None, path_errors=None, family=False):
        reasons, warnings, references = [], [], []
        retained_paths = set()
        root = self.settings.storage_root.resolve()
        declared_root = self.settings.storage_root.absolute()
        task = next((row for row in state["tasks"] if row["id"] == asset["task_id"]), None)

        def protect(code, kind, identifier):
            (reasons if code in {"RUNNING", "FILE_IN_USE", "FILE_LOCK_CHECK_FAILED", "WINDOWS_WRITER_PROBE_NOT_AVAILABLE", "EXTERNAL_LEGACY_FILE", "SYMLINK_FILE_PROTECTED", "INVALID_FILE_TARGET", "REFERENCE_FILES_UNVERIFIED", "FILE_PLAN_UNVERIFIED", "FILE_ACCESS_ERROR", "MEDIA_DELETED"} else warnings).append(code)
            references.append({"type": kind, "id": identifier, "reason": code})

        if asset.get("status") == "MEDIA_DELETED":
            protect("MEDIA_DELETED", "asset", asset["id"])

        active_runs = [row for row in state["runs"] if row["task_id"] == asset["task_id"] and row["status"].lower() in {"queued", "running", "verifying"}]
        for run in active_runs:
            protect("RUNNING", "run", run["id"])
        for batch in state["batches"]:
            if asset["task_id"] in batch["task_ids"] and batch["status"].upper() in {"QUEUED", "RUNNING", "PAUSED", "WAITING", "WAITING_MANUAL", "WAITING_REVIEW"}:
                protect("ACTIVE_PRODUCTION", "batch", batch["id"])
        if task and task["status"].upper() in {"COLLECTING", "RUNNING"}:
            protect("ACTIVE_PRODUCTION", "task", task["id"])
        peers = [row for row in state["assets"] if row["task_id"] == asset["task_id"] and row["type"] == asset["type"] and row["registry_status"] == "registered" and row.get("legacy_reference", {}).get("execution_scope") == asset.get("legacy_reference", {}).get("execution_scope")]
        if asset["type"] in {"background", "compose", "final"}:
            peers = [row for row in peers if row.get("legacy_reference", {}).get("prompt_id") == asset.get("legacy_reference", {}).get("prompt_id")]
        latest = max(peers, key=lambda row: (row.get("legacy_reference", {}).get("version", 0), row["created_at"], row["id"]), default=None)
        history = bool(latest and latest["id"] != asset["id"] and asset["type"] in {"person", "compose", "final", "background"})
        if asset.get("legacy_reference", {}).get("execution_scope") == "TECHNICAL_VALIDATION":
            protect("PROTECTED_TECHNICAL_ASSET", "asset", asset["id"])
        if asset["registry_status"] == "mock" or not task or task["execution_mode"] != "integration":
            protect("PROTECTED_HISTORY_RECORD", "asset", asset["id"])
        if history:
            protect("HISTORY_VERSION", "asset", asset["id"])
        if latest and latest["id"] == asset["id"] and asset["type"] in {"person", "compose", "final"}:
            protect("CURRENT_PERSON_ASSET" if asset["type"] == "person" else "CURRENT_COMPOSITION_ASSET", "task", asset["task_id"])
        for other in state["assets"]:
            if other["id"] != asset["id"] and self._contains_identifier(other.get("legacy_reference", {}), asset["id"]):
                protect("REFERENCED", "asset", other["id"])
            if asset["type"] == "source" and asset.get("sha256") and other["type"] == "person" and other.get("legacy_reference", {}).get("source_sha256") == asset["sha256"]:
                protect("REFERENCED", "asset", other["id"])
        for other_task in state["tasks"]:
            if self._contains_identifier(other_task.get("context", {}), asset["id"]):
                protect("REFERENCED", "task", other_task["id"])
        for key in ("reviews", "quality_assessments", "publishing"):
            for row in state[key]:
                if row.get("asset_id") == asset["id"] or row.get("video_id") == asset["id"]:
                    protect("REFERENCED", key, row["id"])
        background_ids = list(session.scalars(select(m.BackgroundAsset.id).where(m.BackgroundAsset.asset_id == asset["id"])))
        for row in session.scalars(select(m.CompositionAsset)):
            if row.asset_id != asset["id"] and (row.person_asset_id == asset["id"] or row.background_id in background_ids):
                protect("REFERENCED", "composition", row.id)
                protect("REFERENCED", "task", row.task_id)
        if asset["type"] == "source":
            for source in session.scalars(select(m.SourceVideo)):
                if source.sha256 == asset.get("sha256") and session.scalar(select(m.Task.id).where(m.Task.source_video_id == source.id)):
                    protect("REFERENCED", "source_video", source.id)
        try:
            if file_paths is not None and asset["id"] in (path_errors or {}):
                raise ValueError("FILE_PLAN_UNVERIFIED")
            paths = (file_paths[asset["id"]] if file_paths is not None else
                set() if asset["type"] == "source_candidate" or asset["registry_status"] == "mock" else self._asset_files(asset))
            for path in paths:
                boundaries = (declared_root, self.settings.legacy_tiktok_auto_root.absolute(), self.settings.legacy_video_auto_root.absolute(),
                    (Path.home() / "Downloads").absolute(), *(candidate.absolute() for candidate in self.settings.readonly_asset_roots)) if family else (root, declared_root)
                if any(item.is_symlink() for item in [path, *path.parents] if any(item.is_relative_to(boundary) for boundary in boundaries)):
                    protect("SYMLINK_FILE_PROTECTED", "path", str(path))
                if not path.resolve().is_relative_to(root) or path.resolve() == root:
                    if family:
                        error = self._family_external_file_error(asset, path)
                        if error:
                            reasons.append(error)
                            references.append({"type": "path", "id": str(path), "reason": error})
                    else:
                        protect("EXTERNAL_LEGACY_FILE", "path", str(path))
                if path.exists() and not path.is_file():
                    protect("INVALID_FILE_TARGET", "path", str(path))
            if path_owners is not None:
                registered = {row["id"] for row in state["assets"] if row["registry_status"] == "registered" and row["id"] != asset["id"]}
                for identifier in sorted(registered & (path_errors or {}).keys()):
                    protect("REFERENCE_FILES_UNVERIFIED", "asset", identifier)
                shared_by_owner = {}
                for path in paths:
                    for identifier in path_owners.get(("path", str(path.resolve())), set()) & registered:
                        shared_by_owner.setdefault(identifier, set()).add(path)
                for identifier, shared in sorted(shared_by_owner.items()):
                    protect("SHARED_FILE", "asset", identifier)
                    retained_paths.update(shared)
            else:
                for other in state["assets"]:
                    if other["id"] == asset["id"] or other["registry_status"] != "registered":
                        continue
                    try:
                        other_paths = {path.resolve() for path in self._asset_files(other)}
                        shared = {path for path in paths if path.resolve() in other_paths}
                    except (OSError, ValueError, TypeError):
                        protect("REFERENCE_FILES_UNVERIFIED", "asset", other["id"])
                        continue
                    if shared:
                        protect("SHARED_FILE", "asset", other["id"])
                        retained_paths.update(shared)
        except (OSError, ValueError, TypeError):
            paths = set()
            protect("FILE_PLAN_UNVERIFIED", "asset", asset["id"])
        files = []
        for path in sorted(paths):
            try:
                stat = path.stat() if path.is_file() else None
                files.append({"path": str(path), "exists": stat is not None, "size": stat.st_size if stat else 0,
                              "mtime_ns": stat.st_mtime_ns if stat else None,
                              "allocated_bytes": stat.st_blocks * 512 if stat else 0, "link_count": stat.st_nlink if stat else 0,
                              "shared": path in retained_paths, "action": "KEEP_SHARED" if path in retained_paths else "DELETE"})
            except OSError:
                protect("FILE_ACCESS_ERROR", "path", str(path))
        for path in paths:
            for product in state["products"]:
                if product.get("image") and Path(product["image"]).is_absolute() and Path(product["image"]).resolve() == path.resolve():
                    protect("REFERENCED", "product", product["id"])
                    retained_paths.add(path)
            for other_task in state["tasks"]:
                if self._contains_identifier(other_task.get("context", {}), str(path)):
                    protect("REFERENCED", "task", other_task["id"])
        affected_task_ids = {asset["task_id"]}
        affected_task_ids.update(row["id"] for row in references if row["type"] == "task")
        affected_task_ids.update(other["task_id"] for other in state["assets"] if any(row["id"] == other["id"] and row["type"] == "asset" for row in references))
        affected_task_ids.update(row["task_id"] for row in state["runs"] if any(ref["type"] == "run" and ref["id"] == row["id"] for ref in references))
        for key in ("reviews", "quality_assessments", "publishing"):
            affected_task_ids.update(row["task_id"] for row in state[key] if any(ref["type"] == key and ref["id"] == row["id"] for ref in references))
        for run in state["runs"]:
            if run["task_id"] in affected_task_ids and run["status"].lower() in {"queued", "running", "verifying"}:
                protect("RUNNING", "run", run["id"])
        for item in files:
            if Path(item["path"]) in retained_paths:
                item.update(shared=True, action="KEEP_SHARED")
        if not reasons and check_writers:
            try:
                for pid in self._file_writers([Path(item["path"]) for item in files if item["exists"] and item["action"] == "DELETE"]):
                    protect("FILE_IN_USE", "process", pid)
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                code = "WINDOWS_WRITER_PROBE_NOT_AVAILABLE" if str(exc) == "WINDOWS_WRITER_PROBE_NOT_AVAILABLE" else "FILE_LOCK_CHECK_FAILED"
                protect(code, "asset", asset["id"])
        reasons = list(dict.fromkeys(reasons))
        warnings = list(dict.fromkeys(warnings))
        references = list({(row["type"], row["id"], row["reason"]): row for row in references}.values())
        fingerprint = hashlib.sha256(json.dumps({"asset": asset, "files": files, "reasons": reasons, "warnings": warnings, "references": references}, sort_keys=True).encode()).hexdigest()
        return {"asset_id": asset["id"], "can_delete": not reasons,
                "can_delete_history": not reasons, "history_version": history,
                "status": reasons[0] if reasons else warnings[0] if warnings else "UNREFERENCED",
                "reasons": reasons, "warnings": warnings, "references": references, "files": files,
                "requires_impact_confirmation": bool(warnings), "affected_task_ids": sorted(affected_task_ids),
                "bytes_to_release": sum(row["size"] for row in files if row["action"] == "DELETE"),
                "retained_shared_bytes": sum(row["size"] for row in files if row["action"] == "KEEP_SHARED"),
                "allocated_bytes_reclaim_estimate": sum(row["allocated_bytes"] for row in files if row["link_count"] == 1 and row["action"] == "DELETE"), "fingerprint": fingerprint}

    def delete_asset(self, asset_id, fingerprint, *, confirmed=False, impact_confirmed=False):
        try:
            return self._delete_asset(asset_id, fingerprint, confirmed=confirmed, impact_confirmed=impact_confirmed)
        except HTTPException as exc:
            if not self._record_deletion_failure(asset_id, exc.detail) and isinstance(exc.detail, dict):
                exc.detail["audit_error"] = "DELETE_AUDIT_UNAVAILABLE"
            raise

    def _record_deletion_failure(self, asset_id, detail):
        try:
            with self.repository.lock, Session(self.repository.engine) as session:
                asset = session.get(m.VideoAsset, asset_id)
                identity = session.get(m.AssetIdentity, asset_id)
                session.add(m.AuditLog(id=new_id("audit"), task_id=asset.task_id if asset else identity.task_id if identity else None,
                    action="asset.delete_failed", entity_id=asset_id, message=json.dumps({"status": "FAILED", "error": detail}, ensure_ascii=False),
                    created_at=datetime.fromisoformat(now())))
                session.commit()
            return True
        except (SQLAlchemyError, RuntimeError):
            return False

    def _delete_asset(self, asset_id, fingerprint, *, confirmed=False, impact_confirmed=False):
        if not confirmed:
            raise HTTPException(422, {"code": "DELETE_CONFIRMATION_REQUIRED", "message": "请明确确认删除素材记录和 Factory 留存媒体；任务、审核与删除审计保留"})
        moved = []
        with self.alpha_preview_lock, self.repository.lock, Session(self.repository.engine) as session:
            state = self.repository._read(session)
            asset = next((row for row in state["assets"] if row["id"] == asset_id), None)
            if not asset:
                raise HTTPException(404, "资产不存在或已删除")
            plan = self._deletion_plan(asset, state, session)
            if not plan["can_delete"]:
                raise HTTPException(409, {"code": plan["status"], "message": "相关运行尚未结束或文件不在可安全删除范围；请等待运行完成后重新检查。旧系统/外部文件继续只读。", "references": plan["references"]})
            if plan["fingerprint"] != fingerprint:
                raise HTTPException(409, {"code": "DELETE_PLAN_CHANGED", "message": "文件或引用已变化，请刷新后重新确认"})
            if plan["requires_impact_confirmation"] and not impact_confirmed:
                raise HTTPException(422, {"code": "DELETE_IMPACT_CONFIRMATION_REQUIRED", "message": "请确认历史引用可能失去该素材；共享文件会保留，正在运行的文件不会删除", "references": plan["references"], "affected_task_ids": plan["affected_task_ids"]})
            disk_free_before = shutil.disk_usage(self.settings.storage_root).free
            quarantine = self.settings.storage_root / ".asset-delete" / uuid4().hex
            recovery_manifest = quarantine / "deletion.json"
            pending_manifest = quarantine / "deletion.pending.json"
            try:
                for index, item in enumerate(plan["files"]):
                    source = Path(item["path"])
                    if item["exists"] and item["action"] == "DELETE":
                        quarantine.mkdir(parents=True, exist_ok=True)
                        target = quarantine / str(index)
                        pending_manifest.write_text(json.dumps({"asset_id": asset_id, "fingerprint": fingerprint,
                            "state": "STAGING", "moves": [{"source": str(original), "target": str(staged)} for original, staged in [*moved, (source, target)]]}), encoding="utf-8")
                        os.replace(pending_manifest, recovery_manifest)
                        os.replace(source, target)
                        moved.append((source, target))
                deleted_at = now()
                identity = session.get(m.AssetIdentity, asset_id)
                identity.deleted_at = datetime.fromisoformat(deleted_at)
                identity.deletion_snapshot = {"asset": asset, "fingerprint": fingerprint, "files": plan["files"],
                    "references": plan["references"], "impact_confirmed": impact_confirmed, "status": "CLEANUP_PENDING"}
                session.delete(session.get(m.VideoAsset, asset_id))
                audit_id = new_id("audit")
                session.add(m.AuditLog(id=audit_id, task_id=asset["task_id"], action="asset.delete", entity_id=asset_id,
                    message=json.dumps({"name": asset["name"], "sha256": asset.get("sha256"), "files": plan["files"], "bytes": plan["bytes_to_release"], "references": plan["references"], "affected_task_ids": plan["affected_task_ids"], "impact_confirmed": impact_confirmed, "record_deleted": True, "status": "CLEANUP_PENDING"}, ensure_ascii=False), created_at=datetime.fromisoformat(deleted_at)))
                session.commit()
            except Exception as exc:
                session.rollback()
                restore_errors = []
                for source, target in reversed(moved):
                    try:
                        if target.exists():
                            os.replace(target, source)
                    except OSError as restore_error:
                        restore_errors.append({"source": str(source), "quarantined_path": str(target), "error": restore_error.strerror or str(restore_error)})
                pending_manifest.unlink(missing_ok=True)
                if not restore_errors:
                    recovery_manifest.unlink(missing_ok=True)
                    if quarantine.exists():
                        quarantine.rmdir()
                failure = {"code": "ASSET_DELETE_FAILED", "message": f"删除未完成；素材记录已保留。{type(exc).__name__}",
                    "restore_errors": restore_errors, "recovery_manifest": str(recovery_manifest) if restore_errors else None}
                raise HTTPException(500, failure) from None
        cleanup_errors = []
        released = 0
        deleted_allocated = 0
        for source, target in moved:
            try:
                stat = target.stat()
                target.unlink()
                released += stat.st_size
                if stat.st_nlink == 1:
                    deleted_allocated += stat.st_blocks * 512
            except OSError as exc:
                cleanup_errors.append({"path": str(target), "error": exc.strerror or str(exc)})
        if quarantine.exists() and not cleanup_errors:
            recovery_manifest.unlink(missing_ok=True)
            quarantine.rmdir()
        result = {"asset_id": asset_id, "status": "DELETED" if not cleanup_errors else "DELETED_WITH_CLEANUP_ERROR",
                "record_deleted": True, "registry_status": "DELETED", "original_files_absent": all(not Path(row["path"]).exists() for row in plan["files"] if row["action"] == "DELETE"),
                "shared_files_preserved": all(Path(row["path"]).is_file() for row in plan["files"] if row["action"] == "KEEP_SHARED" and row["exists"]),
                "retained_shared_bytes": plan["retained_shared_bytes"],
                "released_bytes": released, "cleanup_errors": cleanup_errors, "audit_preserved": True,
                "deleted_allocated_bytes": deleted_allocated, "disk_free_before": disk_free_before,
                "disk_free_after": shutil.disk_usage(self.settings.storage_root).free,
                "affected_task_ids": plan["affected_task_ids"], "affected_asset_ids": [asset_id]}
        with self.repository.lock, Session(self.repository.engine) as session:
            identity = session.get(m.AssetIdentity, asset_id)
            identity.deletion_snapshot = {**identity.deletion_snapshot, "result": result, "status": result["status"]}
            audit = session.get(m.AuditLog, audit_id)
            audit.message = json.dumps({**json.loads(audit.message), **result}, ensure_ascii=False)
            session.commit()
        return result

    def bulk_delete(self, items):
        results, validated = [], []
        with self.alpha_preview_lock, self.repository.lock, Session(self.repository.engine) as session:
            state = self.repository._read(session)
            assets = {asset["id"]: asset for asset in state["assets"]}
            for item in items:
                asset = assets.get(item.asset_id)
                plan = self._deletion_plan(asset, state, session) if asset else None
                if not item.confirmed or plan and plan["requires_impact_confirmation"] and not item.impact_confirmed:
                    results.append({"asset_id": item.asset_id, "status": "FAILED", "code": "DELETE_CONFIRMATION_REQUIRED", "message": "请明确确认媒体删除及引用影响"})
                elif not plan or not plan["can_delete"] or plan["fingerprint"] != item.fingerprint:
                    results.append({"asset_id": item.asset_id, "status": "FAILED", "code": plan["status"] if plan and not plan["can_delete"] else "DELETE_PLAN_CHANGED", "message": "文件或引用已变化，或文件不在可删除范围；请重新检查"})
                else:
                    validated.append(item)
            for failure in results:
                self._record_deletion_failure(failure["asset_id"], failure)
            for item in validated:
                current = next((asset for asset in self.repository.state()["assets"] if asset["id"] == item.asset_id), None)
                if current is None:
                    results.append({"asset_id": item.asset_id, "status": "FAILED", "code": "ASSET_ALREADY_DELETED", "released_bytes": 0})
                    continue
                try:
                    plan = self._deletion_plan(current, self.repository.state(), session)
                    results.append(self.delete_asset(item.asset_id, plan["fingerprint"], confirmed=True, impact_confirmed=item.impact_confirmed))
                except HTTPException as exc:
                    detail = exc.detail if isinstance(exc.detail, dict) else {"message": exc.detail}
                    results.append({"asset_id": item.asset_id, "status": "FAILED", "http_status": exc.status_code, **detail})
        return {"results": results, "deleted": sum(row["status"] == "DELETED" for row in results),
                "failed": sum(row["status"] != "DELETED" for row in results)}

    @staticmethod
    def _record_snapshot(row):
        return {column.name: value.isoformat() if isinstance(value, datetime) else value
                for column in row.__table__.columns for value in [getattr(row, column.name)]}

    @staticmethod
    def _family_ids(value):
        keys = {"asset_id", "output_asset_id", "parent_asset_id", "parent_person_asset_id", "origin_person_asset_id",
                "person_asset_id", "background_asset_id", "composition_asset_id", "source_asset_id",
                "technical_person_asset_id", "superseded_by_person_asset_id"}
        if isinstance(value, dict):
            result = {item for key, item in value.items() if key in keys and isinstance(item, str)}
            for item in value.values():
                result.update(AssetRegistry._family_ids(item))
            return result
        if isinstance(value, list):
            return set().union(*(AssetRegistry._family_ids(item) for item in value)) if value else set()
        return set()

    def _family_external_file_error(self, asset, path):
        primary = Path(asset.get("external_path") or asset.get("storage_path")).absolute()
        media_suffixes = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif", ".tif", ".tiff", ".exr", ".wav", ".mp3"}
        roots = (self.settings.legacy_tiktok_auto_root / "downloads", self.settings.legacy_video_auto_root / "output",
            Path.home() / "Downloads", *self.settings.readonly_asset_roots)
        if (asset.get("registry_status") != "registered" or ".." in path.parts or not path.is_absolute()
                or not any(path.resolve().is_relative_to(candidate.resolve()) and path.resolve() != candidate.resolve()
                    for candidate in roots if candidate.resolve() not in {Path(candidate.anchor), Path.home()})):
            return "EXTERNAL_FILE_OWNERSHIP_UNVERIFIED"
        if primary.suffix.lower() not in media_suffixes:
            return "EXTERNAL_FILE_OWNERSHIP_UNVERIFIED"
        if path == primary:
            if not asset.get("sha256"):
                return "EXTERNAL_FILE_OWNERSHIP_UNVERIFIED"
            if path.is_file() and file_hash(path) != asset["sha256"]:
                return "MEDIA_IDENTITY_MISMATCH"
        else:
            reference = asset.get("legacy_reference", {})
            hashes = [reference.get(key.removesuffix("_path") + "_sha256") for key, value in reference.items()
                if key.endswith("_path") and isinstance(value, str) and Path(value).absolute() == path]
            if (not path.resolve().is_relative_to(primary.parent.resolve()) or not any(hashes)
                    or path.suffix.lower() not in media_suffixes | {".json", ".csv", ".npy", ".npz", ".txt", ".log", ".srt"}):
                return "EXTERNAL_FILE_OWNERSHIP_UNVERIFIED"
            if path.is_file() and file_hash(path) not in hashes:
                return "MEDIA_IDENTITY_MISMATCH"
        return None

    @staticmethod
    def _detach_family(value, identifiers):
        if isinstance(value, dict):
            return {key: AssetRegistry._detach_family(item, identifiers) for key, item in value.items()
                    if not isinstance(item, str) or item not in identifiers}
        if isinstance(value, list):
            return [AssetRegistry._detach_family(item, identifiers) for item in value
                    if not (isinstance(item, str) and item in identifiers)
                    and not (isinstance(item, dict) and any(item.get(key) in identifiers for key in
                        ("asset_id", "output_asset_id", "person_asset_id", "background_asset_id", "composition_asset_id")
                        if isinstance(item.get(key), str)))]
        return value

    @staticmethod
    def _context_records(value):
        if isinstance(value, dict):
            if isinstance(value.get("id"), str) and AssetRegistry._family_ids(value):
                yield value
            for item in value.values():
                yield from AssetRegistry._context_records(item)
        elif isinstance(value, list):
            for item in value:
                yield from AssetRegistry._context_records(item)

    @staticmethod
    def _owned_manifest_directory(asset, manifest, manifest_path, root):
        if asset["type"] not in {"person", "compose"}:
            return None
        schema = manifest.get("schema")
        if schema not in {"factory.foreground.v1", "factory.foreground.repair.v1", "factory.composition.v1", "factory.manual-composition.v1"}:
            return None
        reference = asset.get("legacy_reference", {})
        directory = Path(manifest_path).absolute().parent
        resolved = directory.resolve()
        primary = Path(asset.get("external_path") or asset["storage_path"]).resolve()
        fingerprint = manifest.get("input_fingerprint")
        run_id = manifest.get("processing_run_id")
        bound_name = (isinstance(fingerprint, str) and len(fingerprint) == 64
            and all(char in "0123456789abcdef" for char in fingerprint) and directory.name == fingerprint
            and reference.get("input_fingerprint") == fingerprint)
        bound_name = bound_name or bool(run_id and directory.name == run_id and reference.get("processing_run_id") == run_id)
        if schema == "factory.foreground.repair.v1":
            alpha = Path(reference.get("alpha_path", "")).resolve()
            bound_name = bool(manifest.get("repair_id") and directory.name == manifest["repair_id"]
                and run_id == reference.get("processing_run_id"))
            output_matches = (alpha.parent == resolved and manifest.get("alpha_sha256") == reference.get("alpha_sha256")
                and manifest.get("rgb_sha256") == asset.get("sha256"))
        else:
            digest_key = "rgb_sha256" if schema == "factory.foreground.v1" else "output_sha256"
            output_matches = primary.parent == resolved and bool(asset.get("sha256")) and manifest.get(digest_key) == asset["sha256"]
            if schema == "factory.manual-composition.v1":
                output_matches = output_matches and bool(manifest.get("output_path")) and Path(manifest["output_path"]).resolve() == primary
        if (manifest.get("task_id") != asset["task_id"] or asset["task_id"] not in directory.parts
                or not resolved.is_relative_to(root) or resolved == root or not bound_name or not output_matches):
            raise ValueError("OWNED_MANIFEST_DIRECTORY_UNVERIFIED")
        return directory

    def _family_plan(self, requested, scope, state, session):
        requested = sorted(set(requested))
        visible = self.repository._production_state(json.loads(json.dumps(state))) if scope == "production" and self.repository.app_mode == "integration" else state
        if not requested or any(identifier not in {row["id"] for row in visible["assets"]} for identifier in requested):
            raise HTTPException(404, "所选素材不存在或不属于当前素材视图")
        assets = {row["id"]: row for row in state["assets"]}
        identities = list(session.scalars(select(m.AssetIdentity)))
        for identity in identities:
            old = identity.deletion_snapshot.get("asset", {})
            if identity.id not in assets and old.get("id") == identity.id:
                assets[identity.id] = {**old, "registry_status": "deleted"}
        paths, declared_paths, path_errors, edges, external = {}, {}, {}, set(), []
        owned_directories, asset_directories, directory_errors = {}, {}, {}
        root = self.settings.storage_root.resolve()
        for identifier, asset in assets.items():
            try:
                paths[identifier] = set() if asset["type"] == "source_candidate" or asset["registry_status"] == "mock" else self._asset_files(asset)
                if asset["type"] == "source_candidate":
                    values = [asset.get("storage_path"), asset.get("legacy_reference", {}).get("raw_result_path"),
                        asset.get("legacy_reference", {}).get("analysis_report")]
                    paths[identifier] = {Path(value).absolute() for value in values if value and Path(value).resolve().is_relative_to(root)}
                declared_paths[identifier] = set(paths[identifier])
                parents = self._family_ids(asset.get("legacy_reference", {}))
                source_bindings = [asset.get("legacy_reference", {})]
                manifest_path = asset.get("legacy_reference", {}).get("manifest_path")
                if manifest_path and Path(manifest_path).is_file():
                    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
                    source_bindings.append(manifest)
                    parents.update(self._family_ids(manifest))
                    try:
                        directory = self._owned_manifest_directory(asset, manifest, manifest_path, root)
                    except ValueError:
                        directory_errors[identifier] = str(Path(manifest_path).absolute().parent)
                        directory = None
                    if directory is not None:
                        asset_directories[identifier] = directory
                        if directory not in owned_directories:
                            owned_directories[directory] = {path.absolute() for path in directory.rglob("*") if path.is_file() or path.is_symlink()}
                        paths[identifier].update(owned_directories[directory])
                for binding in source_bindings:
                    source_path, source_sha256 = binding.get("source_path"), binding.get("source_sha256")
                    if source_path and source_sha256:
                        parents.update(candidate["id"] for candidate in assets.values()
                            if candidate["type"] == "source" and candidate.get("sha256") == source_sha256
                            and candidate.get("external_path") and Path(candidate["external_path"]).resolve() == Path(source_path).resolve())
                for parent in parents & assets.keys():
                    if parent != identifier:
                        edges.add((parent, identifier, "EXPLICIT_ASSET_LINK"))
            except (OSError, ValueError, TypeError, KeyError):
                paths[identifier] = set()
                declared_paths[identifier] = set()
                path_errors[identifier] = "FILE_PLAN_UNVERIFIED"
        by_file = {}
        for identifier, asset_paths in paths.items():
            for path in asset_paths:
                # Identical content is not a relationship; only an actual shared path/inode is.
                canonical = ("path", str(path.resolve()))
                owners = by_file.setdefault(canonical, set())
                owners.add(identifier)
                if path.is_file() and not path.is_symlink():
                    stat = path.stat()
                    by_file.setdefault(("inode", stat.st_dev, stat.st_ino), set()).add(identifier)
        for owners in by_file.values():
            ordered = sorted(owners)
            for identifier in ordered[1:]:
                edges.add((ordered[0], identifier, "SHARED_PHYSICAL_FILE"))
        for composition in session.scalars(select(m.CompositionAsset)):
            if composition.asset_id not in assets:
                continue
            for parent in [composition.person_asset_id,
                    session.get(m.BackgroundAsset, composition.background_id).asset_id if composition.background_id and session.get(m.BackgroundAsset, composition.background_id) else None]:
                if parent in assets and parent != composition.asset_id:
                    edges.add((parent, composition.asset_id, "TYPED_COMPOSITION_LINK"))
            source = session.get(m.SourceVideo, composition.source_video_id) if composition.source_video_id else None
            if source and source.external_path:
                for identifier, asset in assets.items():
                    if asset["type"] == "source" and asset.get("external_path") == source.external_path:
                        edges.add((identifier, composition.asset_id, "TYPED_SOURCE_FILE_LINK"))
        for task in state["tasks"]:
            for record in self._context_records(task.get("context", {})):
                identifiers = sorted(self._family_ids(record) & assets.keys())
                for identifier in identifiers[1:]:
                    edges.add((identifiers[0], identifier, "EXPLICIT_VERSION_RECORD"))
        selected = set(requested)
        changed = True
        while changed:
            changed = False
            for left, right, reason in sorted(edges):
                if (left in selected) == (right in selected):
                    continue
                candidate = right if left in selected else left
                asset = assets[candidate]
                selected.add(candidate)
                changed = True
        selected_ids = sorted(selected)
        selected_task_ids = {assets[identifier]["task_id"] for identifier in selected_ids}
        orphan_outputs, orphan_errors = [], []
        selected_sources = [assets[identifier] for identifier in selected_ids if assets[identifier]["type"] == "source"]
        tasks = {row["id"]: row for row in state["tasks"]}
        for audit in session.scalars(select(m.AuditLog).where(m.AuditLog.action == "asset.family_delete")) if selected_sources else []:
            try:
                deleted = json.loads(audit.message)
            except (ValueError, TypeError):
                continue
            if deleted.get("status") not in {"DELETED", "PARTIAL", "DELETED_WITH_CLEANUP_ERROR"}:
                continue
            for old in deleted.get("assets", []):
                if old.get("type") != "person" or old.get("id") in assets or old.get("task_id") not in tasks:
                    continue
                reference = old.get("legacy_reference", {})
                source_path = reference.get("source_path") or reference.get("source_audio_path")
                source = next((row for row in selected_sources if source_path and row.get("sha256") == reference.get("source_sha256")
                    and Path(row.get("external_path") or row["storage_path"]).resolve() == Path(source_path).resolve()), None)
                if source is None:
                    continue
                task = tasks[old["task_id"]]
                compose_root = root / "projects" / task["project_id"] / "tasks" / task["id"] / "compose"
                if not compose_root.resolve().is_relative_to(root):
                    continue
                for manifest_path in compose_root.glob("*/manifest.json"):
                    try:
                        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        continue
                    if not isinstance(manifest, dict) or manifest.get("schema") != "factory.manual-composition.v1" or manifest.get("person_asset_id") != old["id"]:
                        continue
                    try:
                        output = Path(manifest["output_path"]).absolute()
                        owner = {"type": "compose", "task_id": task["id"], "external_path": str(output), "sha256": manifest.get("output_sha256"),
                            "legacy_reference": {"processing_run_id": manifest.get("processing_run_id")}}
                        directory = self._owned_manifest_directory(owner, manifest, manifest_path, root)
                        if (directory is None or manifest.get("status") != "READY" or manifest.get("rgb_sha256") != old.get("sha256")
                                or manifest.get("alpha_sha256") != reference.get("alpha_sha256")
                                or output.is_file() and file_hash(output) != manifest["output_sha256"]):
                            raise ValueError("ORPHAN_OUTPUT_IDENTITY_UNVERIFIED")
                        orphan_outputs.append({"directory": str(directory), "manifest_path": str(manifest_path), "output_path": str(output),
                            "output_sha256": manifest["output_sha256"], "source_asset_id": source["id"], "deleted_person_asset_id": old["id"],
                            "deletion_audit_id": audit.id, "processing_run_id": manifest["processing_run_id"], "task_id": task["id"]})
                    except (OSError, ValueError, TypeError, KeyError):
                        orphan_errors.append(str(manifest_path))
        records, run_ids = [], set()
        snapshots = {}

        def add_record(row, action="DELETE", reason="EXACT_ASSET_OWNERSHIP"):
            identifier = getattr(row, "id", None) or row.resource_key
            key = (row.__tablename__, identifier)
            if key in snapshots:
                return
            snapshots[key] = self._record_snapshot(row)
            records.append({"table": row.__tablename__, "id": identifier, "action": action, "reason": reason})

        for identifier in selected_ids:
            run_id = assets[identifier].get("legacy_reference", {}).get("processing_run_id")
            if run_id:
                run_ids.add(run_id)
        run_ids.update(row["processing_run_id"] for row in orphan_outputs)
        shared_source_paths = set()
        source_paths = {assets[identifier].get("external_path") or assets[identifier].get("storage_path")
                        for identifier in selected_ids if assets[identifier]["type"] == "source"}
        for source in session.scalars(select(m.SourceVideo)):
            if not source.external_path or source.external_path not in source_paths:
                continue
            other_task = session.scalar(select(m.Task.id).where(m.Task.source_video_id == source.id, m.Task.id.not_in(sorted(selected_task_ids))).limit(1))
            if other_task:
                shared_source_paths.add(Path(source.external_path).resolve())
                add_record(source, "KEEP_SHARED", "SOURCE_STILL_OWNED_BY_UNSELECTED_TASK")
            else:
                add_record(source)
        context_records = []
        for task in state["tasks"]:
            for record in self._context_records(task.get("context", {})):
                if self._family_ids(record) & selected:
                    key = ("task_context", task["id"] + ":" + record["id"])
                    snapshots[key] = record
                    records.append({"table": key[0], "id": key[1], "action": "DELETE", "reason": "EXACT_VERSION_INTERVENTION_OR_REPAIR"})
                    context_records.append(record)
                    if record.get("run_id"):
                        run_ids.add(record["run_id"])
        for model in (m.Review, m.QualityAssessment, m.CutoutAsset, m.CompositionAsset, m.BackgroundAsset):
            for row in session.scalars(select(model)):
                if row.asset_id in selected:
                    add_record(row)
                    if isinstance(row, m.QualityAssessment):
                        run_ids.add(row.processing_run_id)
                elif isinstance(row, m.CompositionAsset) and row.person_asset_id in selected:
                    # Closure must include all typed descendants, even if a registry row was removed earlier.
                    add_record(row)
        referenced_workflows = {snapshot["workflow_id"] for (table, _), snapshot in snapshots.items() if table == "cutout_assets"}
        for workflow in session.scalars(select(m.WorkflowSnapshot)):
            if workflow.id in referenced_workflows or workflow.processing_run_id in run_ids:
                other = session.scalar(select(m.CutoutAsset.id).where(m.CutoutAsset.workflow_id == workflow.id, m.CutoutAsset.asset_id.not_in(selected_ids)).limit(1))
                if not other:
                    add_record(workflow)
        published_anchor_ids = set()
        active_publications = []
        for publication in session.scalars(select(m.PublishingTask)):
            if publication.video_id not in selected:
                continue
            has_receipt = publication.submission_state in {"SUBMITTED", "SUBMITTING", "UNKNOWN_EXTERNAL_RESULT", "PUBLISHED_VERIFIED"} or bool(publication.platform_video_id or publication.platform_url)
            active = session.scalar(select(m.PublishRun.id).where(m.PublishRun.publishing_task_id == publication.id,
                m.PublishRun.status.in_(["QUEUED", "RUNNING", "SUBMITTING"])).limit(1))
            if has_receipt or active:
                if active:
                    active_publications.append(active)
                published_anchor_ids.add(publication.video_id)
                add_record(publication, "KEEP_RECEIPT", "PUBLICATION_RECEIPT_OR_UNKNOWN_EXTERNAL_RESULT")
                continue
            for model in (m.StepExecution, m.ResourceLock, m.DescriptionUsage, m.PublishRun):
                for row in session.scalars(select(model).where(model.publishing_task_id == publication.id)):
                    add_record(row)
            add_record(publication)
        for run in session.scalars(select(m.ProcessingRun)):
            if run.id not in run_ids:
                continue
            outputs = {identifier for identifier, asset in assets.items() if asset.get("legacy_reference", {}).get("processing_run_id") == run.id}
            outputs.update(row.asset_id for row in session.scalars(select(m.QualityAssessment).where(m.QualityAssessment.processing_run_id == run.id)))
            outputs.update(identifier for record in context_records if record.get("run_id") == run.id for identifier in self._family_ids(record) & assets.keys())
            outputs.update(row["source_asset_id"] for row in orphan_outputs if row["processing_run_id"] == run.id)
            paid_unknown = "UNKNOWN_EXTERNAL" in (run.error_code or "") or self._contains_identifier(run.resource_info, "UNKNOWN_EXTERNAL_REQUEST")
            prompts = list(session.scalars(select(m.Prompt.id).where(m.Prompt.processing_run_id == run.id)))
            action = "DELETE" if outputs and outputs <= selected and not paid_unknown and not prompts else "DETACH"
            add_record(run, action, "EXCLUSIVE_FINISHED_OUTPUTS" if action == "DELETE" else "OTHER_OUTPUT_OR_EXTERNAL_REQUEST_HISTORY")
        for error in session.scalars(select(m.ErrorLog)):
            if self._family_ids(error.details) & selected or any(self._contains_identifier(error.details, identifier) for identifier in run_ids):
                add_record(error)
        for identifier in selected_ids:
            row = session.get(m.VideoAsset, identifier)
            if row:
                add_record(row)
            identity = session.get(m.AssetIdentity, identifier)
            if identity:
                add_record(identity, "KEEP_RECEIPT_ANCHOR" if identifier in published_anchor_ids else "DELETE",
                    "PUBLISHED_FOREIGN_KEY_REQUIRES_ANCHOR" if identifier in published_anchor_ids else "FAMILY_IDENTITY")
        reasons, warnings, references, files = (["RUNNING"] if active_publications else []), [], [], {}
        if orphan_errors:
            reasons.append("ORPHAN_OUTPUT_IDENTITY_UNVERIFIED")
            references.extend({"type": "path", "id": path, "reason": "ORPHAN_OUTPUT_IDENTITY_UNVERIFIED"} for path in orphan_errors)
        references.extend({"type": "publish_run", "id": identifier, "reason": "RUNNING"} for identifier in active_publications)
        affected_task_ids = {assets[identifier]["task_id"] for identifier in selected_ids}
        affected_task_ids.update(task["id"] for task in state["tasks"] if self._family_ids(task.get("context", {})) & selected)
        for identifier in selected_ids:
            asset = assets[identifier]
            plan = self._deletion_plan(asset, state, session, check_writers=False,
                file_paths=declared_paths, path_owners=by_file, path_errors=path_errors, family=True)
            reasons.extend(plan["reasons"])
            warnings.extend(plan["warnings"])
            references.extend({**item, "asset_id": identifier} for item in plan["references"])
            if identifier in path_errors:
                reasons.append(path_errors[identifier])
            if identifier in directory_errors:
                reasons.append("FILE_PLAN_UNVERIFIED")
                references.append({"type": "path", "id": directory_errors[identifier], "reason": "OWNED_MANIFEST_DIRECTORY_UNVERIFIED", "asset_id": identifier})
            for item in plan["files"]:
                path = Path(item["path"])
                owners = by_file.get(("path", str(path.resolve())), set())
                product_shared = any(product.get("image") and Path(product["image"]).is_absolute() and Path(product["image"]).resolve() == path.resolve() for product in state["products"])
                keep = bool(owners - selected) or product_shared or path.resolve() in shared_source_paths
                files[str(path)] = {**item, "asset_ids": sorted(owners), "shared": keep, "action": "KEEP_SHARED" if keep else "DELETE"}
                if not path.resolve().is_relative_to(root):
                    files[str(path)]["external_registered_media"] = True
                    if path == Path(asset.get("external_path") or asset.get("storage_path")).absolute():
                        files[str(path)]["sha256"] = asset.get("sha256")
        directories = {asset_directories[identifier] for identifier in selected_ids if identifier in asset_directories}
        directories.update(Path(row["directory"]) for row in orphan_outputs)
        affected_task_ids.update(row["task_id"] for row in orphan_outputs)
        for row in records:
            if row["table"] == "processing_runs" and row["action"] == "DELETE":
                run = snapshots[(row["table"], row["id"])]
                for log_key in ("stdout_log_path", "stderr_log_path"):
                    if run.get(log_key):
                        path = Path(run[log_key]).absolute()
                        directory = path.parent.parent if path.parent.name == "logs" else path.parent
                        if row["id"] in directory.parts and directory.resolve().is_relative_to(root):
                            directories.add(directory)
        for record in context_records:
            if record.get("directory") and record["id"] in Path(record["directory"]).parts:
                directory = Path(record["directory"]).absolute()
                if directory.resolve().is_relative_to(root):
                    directories.add(directory)
        listed_paths = {Path(value).resolve() for value in files}
        for directory in sorted(directories):
            if any(parent.is_symlink() for parent in [directory, *directory.parents] if parent.is_relative_to(root)):
                reasons.append("SYMLINK_FILE_PROTECTED")
                continue
            for child in directory.rglob("*"):
                if child.is_symlink():
                    reasons.append("SYMLINK_FILE_PROTECTED")
                    continue
                if not child.is_file() or child.resolve() in listed_paths:
                    continue
                stat = child.stat()
                owners = by_file.get(("path", str(child.resolve())), set())
                keep = bool(owners - selected)
                files[str(child.absolute())] = {"path": str(child.absolute()), "exists": True, "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns, "allocated_bytes": stat.st_blocks * 512, "link_count": stat.st_nlink,
                    "asset_ids": sorted(owners), "shared": keep, "action": "KEEP_SHARED" if keep else "DELETE"}
                listed_paths.add(child.resolve())
        files = [files[key] for key in sorted(files)]
        for run in state["runs"]:
            if run["id"] in run_ids and run["status"].lower() in {"queued", "running", "verifying"}:
                reasons.append("RUNNING")
        if not reasons:
            try:
                writers = self._file_writers([Path(row["path"]) for row in files if row["exists"] and row["action"] == "DELETE"])
                if writers:
                    reasons.append("FILE_IN_USE")
                    references.extend({"type": "process", "id": pid, "reason": "FILE_IN_USE"} for pid in writers)
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                reasons.append("WINDOWS_WRITER_PROBE_NOT_AVAILABLE" if str(exc) == "WINDOWS_WRITER_PROBE_NOT_AVAILABLE" else "FILE_LOCK_CHECK_FAILED")
        counts = {}
        for row in records:
            key = row["table"] + ":" + row["action"]
            counts[key] = counts.get(key, 0) + 1
        plan = {"requested_asset_ids": requested, "expanded_asset_ids": selected_ids,
            "assets": [{**assets[identifier], "delete_protection": [row["reason"] for row in references if row.get("asset_id") == identifier and row["reason"] in reasons]} for identifier in selected_ids],
            "edges": [{"from_asset_id": left, "to_asset_id": right, "reason": reason} for left, right, reason in sorted(edges) if left in selected or right in selected],
            "references": references, "records": records, "record_actions": counts, "files": files, "file_count": sum(row["exists"] and row["action"] == "DELETE" for row in files),
            "affected_task_ids": sorted(affected_task_ids), "remaining_external_media": list({(row["asset_id"], row["path"]): row for row in external}.values()),
            "can_delete": not reasons, "reasons": sorted(set(reasons)), "warnings": sorted(set(warnings)),
            "requires_impact_confirmation": True, "bytes_to_release": sum(row["size"] for row in files if row["action"] == "DELETE"),
            "allocated_bytes_reclaim_estimate": sum(row["allocated_bytes"] for row in files if row["link_count"] == 1 and row["action"] == "DELETE"),
            "retained_shared_bytes": sum(row["size"] for row in files if row["action"] == "KEEP_SHARED"),
            "status": reasons[0] if reasons else "FAMILY_CONFIRMATION_REQUIRED", "published_anchor_ids": sorted(published_anchor_ids),
            "orphan_outputs": orphan_outputs}
        plan["fingerprint"] = hashlib.sha256(json.dumps({"plan": plan, "snapshots": {table+":"+identifier: snapshot for (table, identifier), snapshot in snapshots.items()},
            "task_contexts": {row["id"]: row["context"] for row in state["tasks"] if row["id"] in affected_task_ids}}, sort_keys=True).encode()).hexdigest()
        plan["_snapshots"] = snapshots
        return plan

    def family_deletion_plan(self, asset_ids, *, scope="production"):
        with self.alpha_preview_lock, self.repository.lock, Session(self.repository.engine) as session:
            plan = self._family_plan(asset_ids, scope, self.repository._read(session), session)
            return {key: value for key, value in plan.items() if not key.startswith("_")}

    def delete_family(self, asset_ids, fingerprint, *, scope="production", confirmed=False, impact_confirmed=False):
        moved, audit_id = [], new_id("audit")
        quarantine = self.settings.storage_root / ".asset-delete" / audit_id
        manifest = quarantine / "deletion.json"
        try:
            if not confirmed or not impact_confirmed:
                raise HTTPException(422, {"code": "DELETE_FAMILY_CONFIRMATION_REQUIRED", "message": "请确认删除整个关联家族、所列历史记录及确切媒体路径；包括计划内登记的 Legacy 原片，未列文件不删除"})
            root = self.settings.storage_root.resolve()
            if (not quarantine.resolve().is_relative_to(root)
                    or any(path.is_symlink() for path in [quarantine, *quarantine.parents] if path.is_relative_to(self.settings.storage_root.absolute()))):
                raise HTTPException(409, {"code": "SYMLINK_FILE_PROTECTED", "message": "删除暂存路径不在 Factory 的安全存储范围"})
            with self.alpha_preview_lock, self.repository.lock, Session(self.repository.engine) as session:
                state = self.repository._read(session)
                plan = self._family_plan(asset_ids, scope, state, session)
                if not plan["can_delete"]:
                    raise HTTPException(409, {"code": plan["status"], "message": "运行或路径保护未通过，整个家族均未删除", "reasons": plan["reasons"], "references": plan["references"]})
                if fingerprint != plan["fingerprint"]:
                    raise HTTPException(409, {"code": "DELETE_PLAN_CHANGED", "message": "家族、文件或历史引用已变化，请重新检查并确认"})
                disk_free_before = shutil.disk_usage(self.settings.storage_root).free
                try:
                    for index, item in enumerate(plan["files"]):
                        if item["exists"] and item["action"] == "DELETE":
                            source, target = Path(item["path"]), quarantine / str(index)
                            quarantine.mkdir(parents=True, exist_ok=True)
                            manifest.write_text(json.dumps({"audit_id": audit_id, "asset_ids": plan["expanded_asset_ids"], "state": "STAGING",
                                "moves": [{"source": str(original), "target": str(staged)} for original, staged in [*moved, (source, target)]]}), encoding="utf-8")
                            os.replace(source, target)
                            moved.append((source, target))
                    deleted_ids = set(plan["expanded_asset_ids"])
                    deleted_paths = {row["path"] for row in plan["files"] if row["action"] == "DELETE"}
                    deleted_records = {row["id"] for row in plan["records"] if row["action"] == "DELETE"}
                    tokens = deleted_ids | deleted_paths | deleted_records
                    source_ids = {row["id"] for row in plan["records"] if row["table"] == "source_videos" and row["action"] == "DELETE"}
                    for task in session.scalars(select(m.Task).where(m.Task.source_video_id.in_(sorted(source_ids)))):
                        task.source_video_id = None
                    session.flush()
                    ordered = (m.StepExecution, m.ResourceLock, m.DescriptionUsage, m.PublishRun, m.PublishingTask,
                        m.Review, m.QualityAssessment, m.CompositionAsset, m.CutoutAsset, m.BackgroundAsset,
                        m.WorkflowSnapshot, m.ProcessingRun, m.ErrorLog, m.SourceVideo, m.VideoAsset, m.AssetIdentity)
                    for model in ordered:
                        for item in [row for row in plan["records"] if row["table"] == model.__tablename__]:
                            row = session.get(model, item["id"])
                            if not row:
                                continue
                            if item["action"] == "DELETE":
                                session.delete(row)
                            elif isinstance(row, m.ProcessingRun):
                                row.resource_info = self._detach_family(row.resource_info, tokens)
                                row.logs = self._detach_family(row.logs, tokens)
                            elif isinstance(row, m.AssetIdentity):
                                row.deleted_at = datetime.fromisoformat(now())
                                row.deletion_snapshot = {"status": "PARTIAL_RECEIPT_ANCHOR_RETAINED", "family_delete_audit_id": audit_id}
                        session.flush()
                    for task in session.scalars(select(m.Task)):
                        affected = task.id in plan["affected_task_ids"] or self._family_ids(task.context) & deleted_ids
                        if not affected:
                            continue
                        context = self._detach_family(task.context, tokens)
                        task.collector_result = self._detach_family(task.collector_result, tokens)
                        context.setdefault("asset_family_deletions", []).append({"audit_id": audit_id, "asset_ids": plan["expanded_asset_ids"], "deleted_at": now()})
                        production = context.setdefault("production", {})
                        production.update(automatic=False, review_approved=False,
                            asset_family_deleted={"status": "INVALIDATED", "audit_id": audit_id, "asset_ids": plan["expanded_asset_ids"], "saved_at": now()})
                        production.pop("deferred_continue", None)
                        task.context, task.status = context, "CANCELLED"
                        task.updated_at = datetime.fromisoformat(now())
                    for batch in session.scalars(select(m.ProductionBatch)):
                        if any(item.get("task_id") in plan["affected_task_ids"] for item in batch.items):
                            batch.items = [{**item, "status": "CANCELLED", "error_code": "ASSET_FAMILY_DELETED", "family_delete_audit_id": audit_id}
                                if item.get("task_id") in plan["affected_task_ids"] else item for item in batch.items]
                    session.add(m.AuditLog(id=audit_id, task_id=plan["affected_task_ids"][0], action="asset.family_delete", entity_id=plan["requested_asset_ids"][0],
                        message=json.dumps({**{key: value for key, value in plan.items() if not key.startswith("_")},
                            "record_snapshots": {table+":"+identifier: snapshot for (table, identifier), snapshot in plan["_snapshots"].items()},
                            "status": "CLEANUP_PENDING", "disk_free_before": disk_free_before}, ensure_ascii=False), created_at=datetime.fromisoformat(now())))
                    session.commit()
                except Exception as exc:
                    session.rollback()
                    restore_errors = []
                    for source, target in reversed(moved):
                        try:
                            if target.exists():
                                os.replace(target, source)
                        except OSError as restore_error:
                            restore_errors.append({"path": str(source), "quarantined_path": str(target), "error": str(restore_error)})
                    if not restore_errors:
                        manifest.unlink(missing_ok=True)
                        if quarantine.exists():
                            quarantine.rmdir()
                    raise HTTPException(500, {"code": "ASSET_FAMILY_DELETE_FAILED", "message": f"家族删除未完成，数据库记录保留：{type(exc).__name__}",
                        "restore_errors": restore_errors, "recovery_manifest": str(manifest) if restore_errors else None}) from None
        except HTTPException as exc:
            for identifier in set(asset_ids):
                self._record_deletion_failure(identifier, exc.detail)
            raise
        released, allocated, errors = 0, 0, []
        for source, target in moved:
            try:
                stat = target.stat()
                target.unlink()
                released += stat.st_size
                if stat.st_nlink == 1:
                    allocated += stat.st_blocks * 512
            except OSError as exc:
                errors.append({"path": str(target), "error": exc.strerror or str(exc)})
        if quarantine.exists() and not errors:
            manifest.unlink(missing_ok=True)
            quarantine.rmdir()
        result = {"status": "DELETED_WITH_CLEANUP_ERROR" if errors else "PARTIAL" if plan["published_anchor_ids"] else "DELETED",
            "record_deleted": True, "deleted_asset_ids": plan["expanded_asset_ids"], "affected_asset_ids": plan["expanded_asset_ids"],
            "affected_task_ids": plan["affected_task_ids"], "released_bytes": released, "deleted_allocated_bytes": allocated,
            "retained_shared_bytes": plan["retained_shared_bytes"], "cleanup_errors": errors, "audit_id": audit_id,
            "record_actions": plan["record_actions"], "published_anchor_ids": plan["published_anchor_ids"],
            "original_files_absent": all(not Path(row["path"]).exists() for row in plan["files"] if row["action"] == "DELETE"),
            "shared_files_preserved": all(Path(row["path"]).is_file() for row in plan["files"] if row["exists"] and row["action"] == "KEEP_SHARED"),
            "disk_free_before": disk_free_before, "disk_free_after": shutil.disk_usage(self.settings.storage_root).free}
        with self.repository.lock, Session(self.repository.engine) as session:
            audit = session.get(m.AuditLog, audit_id)
            audit.message = json.dumps({**json.loads(audit.message), "result": result, "status": result["status"]}, ensure_ascii=False)
            session.commit()
        return result

    def media_status(self, asset):
        result = {"asset_id": asset["id"], "available": False, "preview_available": False,
                  "status": "NOT_REGISTERED", "exists": False, "path": asset.get("external_path") or asset["storage_path"],
                  "size": None, "recorded_size": asset.get("size"), "hash_check": "NOT_RECHECKED"}
        if asset.get("status") == "MEDIA_DELETED":
            return {**result, "status": "MEDIA_DELETED", "size": 0, "deletion": asset.get("legacy_reference", {}).get("media_deletion")}
        if asset["type"] == "source_candidate":
            return {**result, "status": "SOURCE_CANDIDATE"}
        if asset["registry_status"] != "registered":
            return result
        try:
            path = self.read_path(result["path"])
            result["path"] = str(path)
            result["exists"] = path.is_file()
            if not result["exists"]:
                return {**result, "status": "OUTPUT_NOT_FOUND"}
            result["size"] = path.stat().st_size
            if not result["size"] or result["size"] != asset.get("size"):
                return {**result, "status": "SIZE_MISMATCH"}
            result.update(available=True, status="FILE_PRESENT", preview_available=True)
            if asset["type"] == "person":
                reference = asset["legacy_reference"]
                alpha = self.read_path(reference["alpha_path"])
                result["alpha"] = {"path": str(alpha), "exists": alpha.is_file(),
                                   "sha256": reference.get("alpha_sha256"), "hash_check": "NOT_RECHECKED"}
                preview = self.read_path(reference["preview_path"])
                result["preview_available"] = alpha.is_file() and preview.is_file()
            return result
        except (OSError, ValueError, KeyError):
            return {**result, "preview_available": False,
                    "status": "PREVIEW_NOT_AVAILABLE" if result["available"] else "PATH_NOT_ALLOWED"}

    def details(self, asset_id, *, scope="production"):
        state = self.repository.read_asset_context(asset_id, scope=scope)
        asset = next((row for row in state["assets"] if row["id"] == asset_id), None)
        if not asset:
            raise HTTPException(404, "资产不存在或不属于当前素材视图")
        task = next(row for row in state["tasks"] if row["id"] == asset["task_id"])
        assets = {row["id"]: row for row in state["assets"] if row["task_id"] == task["id"]}
        parent_keys = ("parent_asset_id", "parent_person_asset_id", "origin_person_asset_id", "person_asset_id", "background_asset_id", "composition_asset_id")

        def parent_ids(row):
            reference = row["legacy_reference"]
            identifiers = [reference[key] for key in parent_keys if reference.get(key)]
            if row["type"] == "person" and reference.get("source_sha256"):
                identifiers.extend(item["id"] for item in assets.values() if item["type"] == "source" and item.get("sha256") == reference["source_sha256"])
            return list(dict.fromkeys(value for value in identifiers if value != row["id"]))

        ancestor_ids, edges, pending = set(), [], [asset_id]
        while pending:
            identifier = pending.pop()
            if identifier in ancestor_ids:
                continue
            ancestor_ids.add(identifier)
            for parent in parent_ids(assets[identifier]):
                edges.append({"from_asset_id": parent, "to_asset_id": identifier, "resolved": parent in assets})
                if parent in assets:
                    pending.append(parent)
        descendants = [row for row in assets.values() if asset_id in parent_ids(row)]
        reference = asset["legacy_reference"]
        run_ids = {assets[key]["legacy_reference"]["processing_run_id"] for key in ancestor_ids if assets[key]["legacy_reference"].get("processing_run_id")}
        task_context = task["context"]
        checkpoints = [*task_context.get("download_checkpoints", []), task_context.get("download_checkpoint", {})]
        for key in ancestor_ids:
            source = assets[key]
            if source["type"] != "source" or not source.get("sha256"):
                continue
            for checkpoint in checkpoints:
                if checkpoint.get("source_asset_id") == key and checkpoint.get("sha256") == source["sha256"]:
                    run_ids.add(checkpoint.get("verification_run_id"))
            for review in task_context.get("source_watermark_reviews", []):
                evidence = review.get("evidence", {})
                if review.get("asset_id") == key and evidence.get("source_asset_id") == key and evidence.get("source_sha256") == source["sha256"]:
                    run_ids.add(evidence.get("origin_collector_run_id"))
                    run_ids.add((review.get("verification") or {}).get("verification_run_id"))
        run_ids.intersection_update(row["id"] for row in state["runs"] if row["task_id"] == task["id"])
        snapshots = [row for row in self.repository.workflows(task["id"]) if row.get("processing_run_id") in run_ids]
        context = task["context"].get("task_context", {})
        context = {key: context[key] for key in ("task_id", "project_id", "country", "keyword", "product_id", "product_images", "source_document", "source_row", "context_id") if key in context}
        return {"asset": asset, "media": self.media_status(asset), "context": context,
                "context_id": asset.get("legacy_context_id") or context.get("context_id"),
                "parent_asset_id": reference.get("parent_asset_id") or reference.get("parent_person_asset_id"),
                "version": reference.get("version"), "execution_scope": reference.get("execution_scope", "PRODUCTION" if task["execution_mode"] == "integration" else "MOCK"),
                "lineage": {"assets": [assets[key] for key in sorted(ancestor_ids)], "edges": edges, "children": descendants},
                "workflows": snapshots, "runs": [row for row in state["runs"] if row["id"] in run_ids],
                "quality": [row for row in state["quality_assessments"] if row["asset_id"] in ancestor_ids],
                "reviews": [row for row in state["reviews"] if row["asset_id"] == asset_id]}

    def allowed_path(self, value):
        path = Path(value).resolve()
        roots = (self.settings.storage_root, self.settings.legacy_tiktok_auto_root / "downloads", self.settings.legacy_video_auto_root / "output")
        if not any(path.is_relative_to(root.resolve()) for root in roots):
            raise ValueError("ASSET_PATH_OUTSIDE_AUTHORIZED_STORAGE")
        return path

    def read_path(self, value):
        path = Path(value).resolve()
        if any(path.is_relative_to(root.resolve()) for root in self.settings.readonly_asset_roots):
            return path
        return self.allowed_path(path)

    def _product_image_hash(self, value):
        path = Path(value).resolve()
        roots = (self.settings.storage_root.resolve(), (self.settings.legacy_tiktok_auto_root / "data/product_references").resolve())
        if not any(path.is_relative_to(root) for root in roots):
            raise ValueError("TECHNICAL_PRODUCT_IMAGE_OUTSIDE_STORAGE")
        return file_hash(path)

    def _technical_binding(self, task, reference):
        origin_path = self.allowed_path(reference["origin_manifest_path"])
        technical_root = (self.settings.storage_root / "technical_validation").resolve()
        if not origin_path.is_relative_to(technical_root) or file_hash(origin_path) != reference.get("origin_manifest_sha256"):
            raise ValueError("TECHNICAL_ORIGIN_MANIFEST_MISMATCH")
        origin = json.loads(origin_path.read_text())
        if (origin.get("execution_scope") != "TECHNICAL_VALIDATION_NO_BUSINESS_TASK" or origin.get("schema") != "factory.foreground.v1"
                or origin.get("status") != "READY" or origin.get("full_decode_verified") is not True):
            raise ValueError("TECHNICAL_ORIGIN_SCOPE_REQUIRED")
        if (reference.get("origin_task_id") != origin.get("task_id") or reference.get("origin_context_id") != origin.get("context_id")):
            raise ValueError("TECHNICAL_ORIGIN_IDENTITY_MISMATCH")
        if reference.get("origin_processing_run_id") != origin.get("processing_run_id") or reference.get("origin_task_context") != origin.get("task_context", {}):
            raise ValueError("TECHNICAL_ORIGIN_IDENTITY_MISMATCH")
        binding_path = self.allowed_path(reference["source_binding_path"])
        if binding_path != origin_path.parent / "source_binding.json" or file_hash(binding_path) != reference.get("source_binding_sha256"):
            raise ValueError("TECHNICAL_SOURCE_BINDING_MISMATCH")
        binding = json.loads(binding_path.read_text())
        if binding != origin.get("resolved_source") or binding.get("not_factory_accepted_task") is not True:
            raise ValueError("TECHNICAL_SOURCE_BINDING_MISMATCH")
        context = task["context"]["task_context"]
        if any(context.get(key) != task[key] for key in ("project_id", "country", "keyword", "product_id")) or context.get("task_id") != task["id"]:
            raise ValueError("TECHNICAL_FACTORY_CONTEXT_MISMATCH")
        expected = {"country": task["country"], "keyword": task["keyword"], "source_document": context.get("source_document"),
                    "source_row": context.get("source_row"), "legacy_context_id": context["context_id"]}
        if any(binding.get(key) != value or value is None for key, value in expected.items()):
            raise ValueError("TECHNICAL_FACTORY_CONTEXT_MISMATCH")
        source = self.allowed_path(binding["file_path"])
        if (source != Path(origin["source_path"]).resolve() or binding.get("sha256") != origin.get("source_sha256")
                or file_hash(source) != binding["sha256"] or source.stat().st_size != binding.get("bytes")
                or binding.get("vidbee_status") != "completed" or not binding.get("vidbee_task_id") or not binding.get("video_id")):
            raise ValueError("TECHNICAL_SOURCE_FILE_MISMATCH")
        legacy_csv = self.settings.legacy_tiktok_auto_root / "output" / "candidates.csv"
        with legacy_csv.open(newline="", encoding="utf-8-sig") as handle:
            rows = [row for row in csv.DictReader(handle) if row.get("video_id") == binding["video_id"] and row.get("context_id") == binding["legacy_context_id"]]
        if len(rows) != 1:
            raise ValueError("TECHNICAL_LEGACY_ROW_NOT_UNIQUE")
        row = rows[0]
        if (any(row.get(key) != str(value) for key, value in expected.items() if key != "legacy_context_id")
                or row.get("vidbee_task_id") != binding["vidbee_task_id"] or row.get("url") != binding.get("video_url")):
            raise ValueError("TECHNICAL_LEGACY_ROW_MISMATCH")
        image_paths = context.get("product_images", [])
        hashes = [self._product_image_hash(value) for value in image_paths]
        legacy_images = row.get("reference_images", "").split("|")
        if not hashes or hashes != [self._product_image_hash(value) for value in legacy_images] or self._product_image_hash(binding["product_image"]) != hashes[0]:
            raise ValueError("TECHNICAL_PRODUCT_IMAGES_MISMATCH")
        snapshot = {"task_id": task["id"], "project_id": task["project_id"], "product_id": task["product_id"],
                    "context_id": context["context_id"], **{key: expected[key] for key in ("country", "keyword", "source_document", "source_row")},
                    "product_images": image_paths, "product_image_sha256": hashes, "video_id": binding["video_id"],
                    "legacy_vidbee_task_id": binding["vidbee_task_id"], "legacy_decision": binding.get("legacy_decision", "UNKNOWN")}
        if reference.get("technical_binding") != snapshot:
            raise ValueError("TECHNICAL_BINDING_SNAPSHOT_MISMATCH")
        return origin, binding

    def register_technical_pair(self, task, manifest_path, run_id):
        manifest_path = self.allowed_path(manifest_path)
        if not manifest_path.is_relative_to((self.settings.storage_root / "technical_validation").resolve()):
            raise ValueError("TECHNICAL_ORIGIN_OUTSIDE_VALIDATION_STORAGE")
        manifest = json.loads(manifest_path.read_text())
        binding_path = manifest_path.parent / "source_binding.json"
        binding = json.loads(binding_path.read_text())
        context = task["context"]["task_context"]
        reference = {"execution_scope": "TECHNICAL_VALIDATION", "origin_execution_scope": manifest.get("execution_scope"),
            "origin_task_id": manifest["task_id"], "origin_context_id": manifest["context_id"],
            "origin_processing_run_id": manifest.get("processing_run_id"), "origin_task_context": manifest.get("task_context", {}),
            "origin_manifest_path": str(manifest_path), "origin_manifest_sha256": file_hash(manifest_path),
            "source_binding_path": str(binding_path), "source_binding_sha256": file_hash(binding_path),
            "technical_binding": {"task_id": task["id"], "project_id": task["project_id"], "product_id": task["product_id"],
                "context_id": context["context_id"], "country": task["country"], "keyword": task["keyword"],
                "source_document": context.get("source_document"), "source_row": context.get("source_row"),
                "product_images": context.get("product_images", []), "product_image_sha256": [self._product_image_hash(value) for value in context.get("product_images", [])],
                "video_id": binding["video_id"], "legacy_vidbee_task_id": binding["vidbee_task_id"], "legacy_decision": binding.get("legacy_decision", "UNKNOWN")},
            "alpha_path": str(manifest_path.parent / "Alpha.mkv"), "alpha_sha256": manifest["alpha_sha256"],
            "manifest_path": str(manifest_path), "alpha_mode": manifest["alpha_mode"],
            "source_sha256": manifest["source_sha256"], "workflow_sha256": manifest["workflow_sha256"],
            "source_audio_path": manifest.get("source_audio_path"), "input_fingerprint": manifest.get("input_fingerprint"),
            "frame_hashes_path": str(manifest_path.parent / "frame_hashes.json"), "time_mapping_path": str(manifest_path.parent / "time_mapping.json"),
            "full_cutout_runs": manifest.get("full_cutout_runs"), "object_strategy": manifest.get("object_strategy", {}),
            "preview_path": str(manifest_path.parent / manifest["previews"][0]["filename"]), "preview_sha256": manifest["previews"][0]["sha256"]}
        self._technical_binding(task, reference)
        return self.register(task, {"path": str(manifest_path.parent / "RGB.mkv"), "sha256": manifest["rgb_sha256"],
            "source_system": "FactoryTechnicalValidation", "legacy_reference": reference}, "person", run_id)

    def _verified_media(self, path, recorded):
        if not isinstance(recorded, dict):
            raise ValueError("PERSON_VERIFIED_TIMELINE_REQUIRED")
        frames, fps, duration = recorded.get("frame_count"), recorded.get("fps", 0), recorded.get("duration", 0)
        if (not isinstance(frames, int) or isinstance(frames, bool) or frames <= 0
                or not isinstance(fps, (int, float)) or not isinstance(duration, (int, float))
                or not math.isfinite(fps) or not math.isfinite(duration) or fps <= 0 or duration <= 0 or abs(duration - frames / fps) > 1 / fps):
            raise ValueError("PERSON_VERIFIED_TIMELINE_REQUIRED")
        checked = probe_media(path)
        if (any(checked[key] != recorded[key] for key in ("width", "height", "codec"))
                or abs(checked["fps"] - fps) > .001
                or recorded.get("pix_fmt") and checked.get("pix_fmt") != recorded["pix_fmt"]
                or checked.get("frame_count") is not None and checked["frame_count"] != frames):
            raise ValueError("PERSON_VERIFIED_STREAM_MISMATCH")
        for key in ("container_duration", "video_start_time"):
            if checked.get(key) is not None and recorded.get(key) is not None and abs(float(checked[key]) - float(recorded[key])) > 1 / fps:
                raise ValueError("PERSON_VERIFIED_TIMELINE_MISMATCH")
        return {**checked, "frame_count": frames, "duration": duration}

    def _person_pair(self, task, evidence, path, media, digest, *, verified_asset=None):
        path = self.read_path(path)
        reference = evidence.get("legacy_reference", {})
        if verified_asset is not None:
            registered = next((row for row in self.repository.state()["assets"] if row["id"] == verified_asset.get("id")), None)
            if (registered != verified_asset or verified_asset["registry_status"] != "registered" or verified_asset["type"] != "person"
                    or verified_asset["task_id"] != task["id"] or verified_asset["legacy_reference"] != reference
                    or reference.get("full_decode_verified") is not True or verified_asset["sha256"] != digest
                    or self.read_path(verified_asset["external_path"] or verified_asset["storage_path"]) != path
                    or file_hash(self.read_path(reference["manifest_path"])) != reference.get("manifest_sha256")):
                raise ValueError("PERSON_VERIFIED_REGISTRATION_REQUIRED")
        if not reference.get("alpha_path"):
            raise ValueError("PERSON_ALPHA_REQUIRED")
        alpha = self.read_path(reference["alpha_path"])
        if alpha == path:
            raise ValueError("PERSON_ALPHA_MUST_BE_INDEPENDENT_MASK")
        alpha_digest = file_hash(alpha)
        if reference.get("alpha_sha256") and reference["alpha_sha256"] != alpha_digest:
            raise ValueError("PERSON_ALPHA_HASH_MISMATCH")
        alpha_media = self._verified_media(alpha, reference["alpha_metadata"]) if verified_asset is not None else probe_media(alpha, count_frames=True)
        if (any(alpha_media[key] != media[key] for key in ("width", "height"))
                or abs(alpha_media["fps"]-media["fps"]) > .001
                or abs(alpha_media["duration"]-media["duration"]) > 1/media["fps"]):
            raise ValueError("PERSON_ALPHA_ALIGNMENT_MISMATCH")
        if any(not isinstance(item.get("frame_count"), int) or item["frame_count"] <= 0 for item in (media, alpha_media)):
            raise ValueError("PERSON_FRAME_COUNT_REQUIRED")
        if alpha_media["frame_count"] != media["frame_count"]:
            raise ValueError("PERSON_ALPHA_ALIGNMENT_MISMATCH")
        if reference.get("alpha_sha256") != alpha_digest:
            raise ValueError("PERSON_ALPHA_HASH_MISMATCH")
        if not reference.get("manifest_path"):
            raise ValueError("PERSON_MANIFEST_REQUIRED")
        manifest_path = self.read_path(reference["manifest_path"])
        roots = (self.settings.storage_root, *self.settings.readonly_asset_roots)
        if not all(any(item.is_relative_to(root.resolve()) for root in roots) for item in (path, alpha, manifest_path)):
            raise ValueError("PERSON_OUTPUT_OUTSIDE_FACTORY")
        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, ValueError) as exc:
            raise ValueError("PERSON_MANIFEST_INVALID") from exc
        if not isinstance(manifest, dict):
            raise ValueError("PERSON_MANIFEST_INVALID")
        if manifest.get("schema") not in {"factory.foreground.v1", "factory.foreground.repair.v1"} or manifest.get("status") != "READY" or manifest.get("full_decode_verified") is not True:
            raise ValueError("PERSON_FULL_DECODE_EVIDENCE_REQUIRED")
        if manifest.get("execution_scope", "PRODUCTION").startswith("TECHNICAL_VALIDATION"):
            if reference.get("execution_scope") != "TECHNICAL_VALIDATION":
                raise ValueError("TECHNICAL_BINDING_REQUIRED")
            origin, _ = self._technical_binding(task, reference)
            if (manifest.get("task_id") != origin["task_id"] or manifest.get("context_id") != origin["context_id"]
                    or manifest.get("execution_scope") != origin["execution_scope"]):
                raise ValueError("TECHNICAL_ORIGIN_IDENTITY_MISMATCH")
        elif reference.get("execution_scope") == "TECHNICAL_VALIDATION" or manifest.get("task_id") != task["id"] or manifest.get("context_id") != task["context"]["task_context"]["context_id"]:
            raise ValueError("PERSON_CONTEXT_MISMATCH")
        if reference.get("alpha_mode") != "straight" or manifest.get("alpha_mode") != "straight":
            raise ValueError("PERSON_ALPHA_MODE_INVALID")
        if not isinstance(manifest.get("precision"), dict) or not all(isinstance(manifest["precision"].get(key), str) and manifest["precision"][key].strip() for key in ("RGB", "Alpha")):
            raise ValueError("PERSON_PRECISION_EVIDENCE_REQUIRED")
        if "uncomposited" not in str(manifest.get("rgb_source", "")):
            raise ValueError("PERSON_UNCOMPOSITED_RGB_REQUIRED")
        if media["codec"] != "ffv1" or not str(media.get("pix_fmt", "")).startswith(("rgb", "bgr", "gbr")) or alpha_media["codec"] != "ffv1" or alpha_media.get("pix_fmt") != "gray16le":
            raise ValueError("PERSON_LOSSLESS_RGB_ALPHA_REQUIRED")
        for recorded, actual in ((manifest.get("media", {}), media), (manifest.get("rgb_media", {}), media), (manifest.get("alpha_media", {}), alpha_media)):
            if not isinstance(recorded, dict) or any(recorded.get(key) != actual[key] for key in ("width", "height", "frame_count")) or abs(float(recorded.get("fps", 0))-actual["fps"]) > .001:
                raise ValueError("PERSON_MANIFEST_ALIGNMENT_MISMATCH")
        if manifest.get("rgb_sha256") != digest or manifest.get("alpha_sha256") != alpha_digest:
            raise ValueError("PERSON_MANIFEST_HASH_MISMATCH")
        if manifest.get("schema") == "factory.foreground.repair.v1":
            if Path(manifest.get("parent_rgb_path", "")).resolve() != path or alpha.parent != manifest_path.parent:
                raise ValueError("PERSON_MANIFEST_PATH_MISMATCH")
        elif path != manifest_path.parent / "RGB.mkv" or alpha != manifest_path.parent / "Alpha.mkv":
            raise ValueError("PERSON_MANIFEST_PATH_MISMATCH")
        if reference.get("source_sha256") != manifest.get("source_sha256") or reference.get("workflow_sha256") != manifest.get("workflow_sha256"):
            raise ValueError("PERSON_INPUT_EVIDENCE_MISMATCH")
        if not reference.get("source_sha256") or not reference.get("workflow_sha256"):
            raise ValueError("PERSON_INPUT_EVIDENCE_REQUIRED")
        source = self.read_path(manifest["source_path"])
        if file_hash(source) != manifest["source_sha256"]:
            raise ValueError("PERSON_SOURCE_HASH_MISMATCH")
        reference = {**reference, "alpha_sha256": alpha_digest, "alpha_metadata": alpha_media,
                     "manifest_sha256": file_hash(manifest_path), "full_decode_verified": True,
                     "precision": manifest["precision"]}
        channels = ["black"]+[channel for channel in ("white", "bright", "source", "alpha_overlay")
                              if any(row.get("background") == channel for row in manifest.get("previews", []))]
        for channel in channels:
            self._person_preview(reference, manifest, manifest_path, channel, media, reuse_verified=verified_asset is not None)
        reference["preview_channels"] = channels
        return {**evidence, "legacy_reference": reference}

    def _person_preview(self, reference, manifest, manifest_path, channel, person_media, *, reuse_verified=False):
        row = next((item for item in manifest.get("previews", []) if item.get("background") == channel), None)
        if row:
            path = self.read_path(manifest_path.parent / row["filename"])
            digest = row.get("sha256")
            if row.get("full_decode_verified") is not True:
                raise ValueError("PERSON_PREVIEW_DECODE_EVIDENCE_REQUIRED")
            if channel == "black" and (Path(reference.get("preview_path", "")).resolve() != path or reference.get("preview_sha256") != digest):
                raise ValueError("PERSON_PREVIEW_REFERENCE_MISMATCH")
        elif channel == "black" and reference.get("preview_path"):
            path, digest = self.read_path(reference["preview_path"]), reference.get("preview_sha256")
        else:
            raise ValueError("PERSON_PREVIEW_CHANNEL_NOT_FOUND")
        if path.parent != manifest_path.parent or path.suffix.lower() != ".mp4" or not digest or file_hash(path) != digest:
            raise ValueError("PERSON_PREVIEW_HASH_OR_PATH_MISMATCH")
        preview_media = row.get("media") if row else None
        expected_preview = preview_media or {**{key: person_media[key] for key in ("width", "height", "fps", "frame_count", "duration")}, "codec": "h264", "pix_fmt": "yuv420p"}
        checked = self._verified_media(path, expected_preview) if reuse_verified else probe_media(path, count_frames=True)
        if checked["codec"] != "h264" or checked["frame_count"] != person_media["frame_count"] or abs(checked["fps"]-person_media["fps"]) > .001:
            raise ValueError("PERSON_PREVIEW_ALIGNMENT_MISMATCH")
        return path, "video/mp4"

    def register(self, task, evidence, kind, run_id, *, prompt_id=None, person=None, background=None):
        reference = evidence.get("legacy_reference", {})
        technical_parent = person if person and person.get("legacy_reference", {}).get("execution_scope") == "TECHNICAL_VALIDATION" else None
        if kind in {"compose", "final"} and reference.get("execution_scope") == "TECHNICAL_VALIDATION" and not technical_parent:
            technical_parent = next((row for row in self.repository.state()["assets"] if row["id"] == reference.get("technical_person_asset_id")), None)
            if not technical_parent:
                raise ValueError("TECHNICAL_PARENT_REQUIRED")
        if technical_parent:
            if (technical_parent["task_id"] != task["id"] or technical_parent.get("type") != "person" or technical_parent.get("registry_status") != "registered"
                    or technical_parent.get("legacy_reference", {}).get("execution_scope") != "TECHNICAL_VALIDATION"):
                raise ValueError("TECHNICAL_PARENT_CONTEXT_MISMATCH")
            original = technical_parent["legacy_reference"]
            self._technical_binding(task, original)
            keys = ("execution_scope", "origin_execution_scope", "origin_task_id", "origin_context_id", "origin_processing_run_id", "origin_task_context", "origin_manifest_path",
                    "origin_manifest_sha256", "source_binding_path", "source_binding_sha256", "technical_binding")
            evidence = {**evidence, "legacy_reference": {**reference, **{key: original[key] for key in keys},
                "technical_person_asset_id": technical_parent["id"]}}
        reference = evidence.get("legacy_reference", {})
        parent = next((row for row in self.repository.state()["assets"] if row["id"] == reference.get("parent_person_asset_id")), None) if kind == "person" and reference.get("parent_person_asset_id") else None
        reused_readonly_rgb = False
        try:
            path = self.allowed_path(evidence["path"])
        except ValueError:
            if (not parent or task["execution_mode"] != "integration" or parent["task_id"] != task["id"]
                    or parent["type"] != "person" or parent["registry_status"] != "registered"
                    or parent["legacy_reference"].get("full_decode_verified") is not True
                    or parent["legacy_reference"].get("execution_scope") not in {None, "PRODUCTION"}
                    or reference.get("execution_scope") not in {None, "PRODUCTION"}):
                raise
            path = self.read_path(evidence["path"])
            if path != self.read_path(parent["storage_path"]) or path.stat().st_size != parent["size"]:
                raise ValueError("PERSON_PARENT_RGB_IDENTITY_MISMATCH")
            for key in ("alpha_path", "manifest_path", "preview_path"):
                output = self.allowed_path(reference[key])
                if not output.is_relative_to(self.settings.storage_root.resolve()):
                    raise ValueError("PERSON_REVISION_OUTPUT_OUTSIDE_FACTORY")
            reused_readonly_rgb = True
        digest = file_hash(path)
        if evidence.get("sha256") and evidence["sha256"] != digest:
            raise ValueError("ASSET_HASH_MISMATCH")
        if reused_readonly_rgb and parent["sha256"] != digest:
            raise ValueError("PERSON_PARENT_RGB_IDENTITY_MISMATCH")
        if (parent and task["execution_mode"] == "integration" and parent["task_id"] == task["id"] and parent["type"] == "person"
                and parent["registry_status"] == "registered" and parent["legacy_reference"].get("full_decode_verified") is True
                and parent["legacy_reference"].get("execution_scope") != "TECHNICAL_VALIDATION" and reference.get("execution_scope") != "TECHNICAL_VALIDATION"
                and Path(parent["storage_path"]).resolve() == path and parent["sha256"] == digest and parent["size"] == path.stat().st_size
                and isinstance(parent["probe_metadata"].get("frame_count"), int) and parent["probe_metadata"]["frame_count"] > 0
                and all(parent["probe_metadata"].get(key) == parent[key] for key in ("width", "height", "fps", "duration", "codec"))):
            media = dict(parent["probe_metadata"])
        else:
            media = probe_media(path, count_frames=kind == "person")
        if kind == "background" and reference.get("execution_scope") == "TECHNICAL_VALIDATION":
            if media["codec"] not in {"png", "mjpeg", "webp"}:
                raise ValueError("TECHNICAL_BACKGROUND_IMAGE_REQUIRED")
            evidence = {**evidence, "legacy_reference": {**reference,
                "factory_fingerprint": "technical-background:" + hashlib.sha256((task["id"] + ":" + digest).encode()).hexdigest()}}
        if kind != "background" and (media["duration"] <= 0 or media["fps"] <= 0):
            raise ValueError("VIDEO_METADATA_INCOMPLETE")
        if kind == "person":
            evidence = self._person_pair(task, evidence, path, media, digest)
        state = self.repository.state()
        reference = evidence.get("legacy_reference", {})
        existing = next((asset for asset in state["assets"] if asset["registry_status"] == "registered" and asset["task_id"] == task["id"] and asset["type"] == kind
                         and asset.get("sha256") == digest and asset.get("legacy_reference", {}).get("prompt_id") == prompt_id
                         and all(asset.get("legacy_reference", {}).get(key) == reference.get(key) for key in ("alpha_sha256", "factory_fingerprint", "manifest_sha256"))
                         and asset.get("legacy_reference", {}).get("person_asset_id") == (person["id"] if person else None)
                         and asset.get("legacy_reference", {}).get("background_asset_id") == (background["id"] if background else None)), None)
        if existing:
            return existing
        identifier = new_id("ast")
        version = self.repository.next_asset_version(task["id"], kind)
        reference = {**evidence.get("legacy_reference", {}), "processing_run_id": run_id, "version": version,
                     "prompt_id": prompt_id, "person_asset_id": person["id"] if person else None,
                     "background_asset_id": background["id"] if background else None}
        asset = {"id": identifier, "task_id": task["id"], "type": kind,
                 "asset_type": {"source": "SOURCE_VIDEO", "person": "PERSON_VIDEO", "background": "BACKGROUND_IMAGE", "compose": "COMPOSITION_VIDEO", "final": "FINAL_VIDEO"}[kind],
                 "name": path.name, "thumbnail": "", **{key: media[key] for key in ("width", "height", "fps", "duration", "codec")},
                 "country": task["country"], "keyword": task["keyword"], "status": "READY", "storage_path": str(path),
                 "external_path": str(path), "created_at": now(), "sha256": digest, "size": path.stat().st_size,
                 "mime_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream", "source_system": evidence.get("source_system", "Factory"),
                 "registry_status": "registered", "legacy_context_id": task["context"]["task_context"]["context_id"],
                 "legacy_video_id": reference.get("technical_binding", {}).get("video_id") or task.get("collector_result", {}).get("video_id"),
                 "legacy_reference": reference, "probe_metadata": media}
        return self.repository.register_real_asset(asset, task)

    def preview(self, asset_id, channel="black"):
        asset = self.repository.read_asset(asset_id)
        if not asset or asset.get("registry_status") != "registered":
            raise HTTPException(404, "该记录没有已验证的预览")
        if asset["type"] != "person":
            return self._content(asset)
        if channel not in {"black", "white", "bright", "source", "alpha_overlay", "alpha"}:
            raise HTTPException(400, "不支持的预览通道")
        try:
            reference = asset["legacy_reference"]
            manifest_path = self.read_path(reference["manifest_path"])
            if file_hash(manifest_path) != reference.get("manifest_sha256"):
                raise ValueError("PERSON_MANIFEST_HASH_MISMATCH")
            manifest = json.loads(manifest_path.read_text())
            if channel == "alpha":
                return self._alpha_preview(asset, reference, manifest, manifest_path)
            return self._person_preview(reference, manifest, manifest_path, channel, asset["probe_metadata"])
        except (KeyError, OSError, ValueError, subprocess.SubprocessError):
            raise HTTPException(404, "PERSON_PREVIEW_NOT_VERIFIED：该通道缺失、文件变化或未通过真实媒体校验") from None

    def _alpha_preview(self, asset, reference, manifest, manifest_path):
        alpha = self.read_path(reference["alpha_path"])
        roots = (self.settings.storage_root, *self.settings.readonly_asset_roots)
        if (not any(alpha.is_relative_to(root.resolve()) for root in roots)
                or reference.get("alpha_sha256") != manifest.get("alpha_sha256")
                or manifest.get("rgb_sha256") != asset["sha256"]
                or file_hash(alpha) != reference["alpha_sha256"]):
            raise ValueError("PERSON_ALPHA_HASH_MISMATCH")
        worker = Path(__file__).with_name("legacy") / "alpha_preview_worker.py"
        helper = worker.with_name("comfy_asset_worker.py")
        media = asset["probe_metadata"]
        inputs = {"asset_id": asset["id"], "alpha_path": str(alpha), "alpha_sha256": reference["alpha_sha256"],
                  "manifest_path": str(manifest_path), "manifest_sha256": reference["manifest_sha256"],
                  "media": {key: media[key] for key in ("width", "height", "fps", "frame_count")},
                  "worker_sha256": file_hash(worker), "helper_sha256": file_hash(helper)}
        identity = hashlib.sha256(json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        directory = self.allowed_path(self.settings.storage_root / "diagnostic_previews" / identity)
        if not directory.is_relative_to(self.settings.storage_root.resolve()):
            raise ValueError("ALPHA_PREVIEW_CACHE_OUTSIDE_FACTORY")
        result_path = self.allowed_path(directory / "preview.json")
        if result_path.parent != directory:
            raise ValueError("ALPHA_PREVIEW_CACHE_OUTSIDE_FACTORY")
        with self.alpha_preview_lock:
            if not result_path.is_file():
                attempt = self.allowed_path(directory / "runs" / uuid4().hex)
                if attempt.parent != directory / "runs":
                    raise ValueError("ALPHA_PREVIEW_CACHE_OUTSIDE_FACTORY")
                attempt.mkdir(parents=True)
                request_path = attempt / "request.json"
                request_path.write_text(json.dumps({"inputs": inputs, "storage_root": str(self.settings.storage_root.resolve()),
                                                   "readonly_asset_roots": [str(root.resolve()) for root in self.settings.readonly_asset_roots],
                                                   "output_path": str(attempt / "Alpha.mp4")}), encoding="utf-8")
                process = ProcessRunner().run([str(self.settings.quality_python), "-B", str(worker),
                    "--request", str(request_path), "--result", str(result_path)],
                    cwd=Path(__file__).resolve().parents[2], log_dir=attempt / "logs", timeout_s=600)
                if process.exit_code or process.timed_out or process.cancelled or not result_path.is_file():
                    raise ValueError("PERSON_ALPHA_PREVIEW_GENERATION_FAILED")
            result = json.loads(result_path.read_text())
            if (not isinstance(result, dict) or result.get("schema") != "factory.alpha-preview.v1" or result.get("inputs") != inputs
                    or result.get("status") != "READY" or result.get("full_decode_verified") is not True
                    or result.get("diagnostic_only") is not True or result.get("cutout_inference_executions") != 0
                    or file_hash(alpha) != inputs["alpha_sha256"] or file_hash(manifest_path) != inputs["manifest_sha256"]):
                raise ValueError("PERSON_ALPHA_PREVIEW_EVIDENCE_MISMATCH")
            path = self.allowed_path(result["path"])
            if not path.is_relative_to(directory) or path.suffix.lower() != ".mp4" or file_hash(path) != result.get("sha256"):
                raise ValueError("PERSON_ALPHA_PREVIEW_HASH_OR_PATH_MISMATCH")
            checked = probe_media(path, count_frames=True)
            if (checked["codec"] != "h264" or any(checked[key] != media[key] for key in ("width", "height", "frame_count"))
                    or abs(checked["fps"] - media["fps"]) > .001):
                raise ValueError("PERSON_ALPHA_PREVIEW_ALIGNMENT_MISMATCH")
            return path, "video/mp4"

    def content(self, asset_id):
        return self._content(self.repository.read_asset(asset_id))

    def _content(self, asset):
        if not asset or asset["registry_status"] != "registered" or asset["type"] == "source_candidate":
            raise HTTPException(404, "该记录没有已验证的媒体文件")
        try:
            path = self.read_path(asset["external_path"] or asset["storage_path"])
            if not path.is_file() or path.stat().st_size != asset["size"]:
                raise ValueError("OUTPUT_NOT_FOUND")
            return path, asset["mime_type"]
        except (OSError, ValueError):
            raise HTTPException(404, "OUTPUT_NOT_FOUND：已登记的原文件缺失或发生变化") from None
