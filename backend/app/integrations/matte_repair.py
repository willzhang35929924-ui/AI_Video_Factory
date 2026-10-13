"""Immutable local edits to a registered RGB/Alpha pair; no acquisition or paid API."""
import asyncio
import ast
import copy
import hashlib
import json
import struct
import subprocess
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import Response, FileResponse
from pydantic import BaseModel, Field, model_validator

from app.domain import audit, find, new_id, now
from app.integrations.asset_registry import file_hash, probe_media
from app.integrations.process_runner import ProcessRunner
from app.integrations.quality_adapter import CutoutQualityAdapter


class RepairKeyframe(BaseModel):
    frame_index: int = Field(ge=0)
    object_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    coordinate_space: Literal["normalized"] = "normalized"
    points: list[dict] = Field(default_factory=list, max_length=200)
    box: list[float] | None = None
    strokes: list[dict] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def coordinates(self):
        for point in self.points:
            if point.get("label") not in (0, 1) or not all(isinstance(point.get(k), (int, float)) and 0 <= point[k] <= 1 for k in ("x", "y")):
                raise ValueError("点选必须使用归一化坐标及正/负标签")
        if self.box is not None and (len(self.box) != 4 or not all(0 <= value <= 1 for value in self.box) or self.box[0] >= self.box[2] or self.box[1] >= self.box[3]):
            raise ValueError("框选坐标必须位于原画布内")
        for stroke in self.strokes:
            radius, points = stroke.get("radius"), stroke.get("points", [])
            if (stroke.get("label") not in (0, 1) or not isinstance(radius, (int, float))
                    or not 1 <= radius <= 200 or not points or len(points) > 4000):
                raise ValueError("画笔半径或轨迹不正确")
            if any(not all(isinstance(p.get(k), (int, float)) and 0 <= p[k] <= 1 for k in ("x", "y")) for p in stroke.get("points", [])):
                raise ValueError("画笔坐标必须位于原画布内")
        if not (self.points or self.box or self.strokes):
            raise ValueError("每个关键帧至少提供点、框或画笔")
        if self.strokes and not any(stroke["label"] == 1 for stroke in self.strokes):
            raise ValueError("橡皮用于排除已有画笔区域，请先用绿色画笔选中目标；点选排除请使用负点")
        return self


class RepairRequest(BaseModel):
    person_asset_id: str
    start_frame: int = Field(ge=0)
    end_frame: int = Field(gt=0)
    mode: Literal["retain", "remove"] = "retain"
    direction: Literal["forward", "backward", "both", "keyframes_only"] = "both"
    keyframes: list[RepairKeyframe] = Field(min_length=1, max_length=300)
    idempotency_key: str = Field(min_length=8, max_length=128)

    @model_validator(mode="after")
    def interval(self):
        if self.end_frame <= self.start_frame or any(not self.start_frame <= item.frame_index < self.end_frame for item in self.keyframes):
            raise ValueError("使用半开区间 [开始帧, 结束帧)，关键帧必须位于区间内")
        identities = [(item.frame_index, item.object_id) for item in self.keyframes]
        if len(identities) != len(set(identities)):
            raise ValueError("同一帧的同一对象只能保存一个关键帧，请合并点、框和画笔")
        objects = {item.object_id for item in self.keyframes}
        if self.direction == "forward" and any(not any(item.object_id == target and item.frame_index == self.start_frame for item in self.keyframes) for target in objects):
            raise ValueError("向后传播需要每个对象在开始帧有标注")
        if self.direction == "backward" and any(not any(item.object_id == target and item.frame_index == self.end_frame - 1 for item in self.keyframes) for target in objects):
            raise ValueError("向前传播需要每个对象在结束帧的前一帧有标注")
        if self.direction == "keyframes_only" and any(not item.strokes or not any(stroke["label"] for stroke in item.strokes) for item in self.keyframes):
            raise ValueError("只应用关键帧时需要正画笔区域；点和框请使用模型传播")
        return self


class TechnicalRepairImportRequest(BaseModel):
    person_asset_id: str
    directory: str
    idempotency_key: str = Field(min_length=8, max_length=128)


class RestorePersonVersionRequest(BaseModel):
    person_asset_id: str


class MatteRepairService:
    def __init__(self, production):
        self.production = production
        self.repository = production.repository
        self.jobs = {}
        self.cancels = {}
        self.lock = asyncio.Lock()
        self.closing = False

    def records(self, task_id):
        return find(self.repository.state(), "tasks", task_id)["context"].get("matte_repairs", [])

    @staticmethod
    def ensure_idle(state, task_id):
        if any(run["task_id"] == task_id and run["status"] in {"running", "queued"} for run in state["runs"]) or any(
            item["task_id"] == task_id and item["status"] in {"QUEUED", "RUNNING"}
            for batch in state.get("batches", []) if batch["status"] in {"QUEUED", "RUNNING", "PAUSED"} for item in batch["items"]
        ):
            raise HTTPException(409, "TASK_BUSY")

    def pair(self, task_id, person_id, verify=True):
        state = self.repository.state()
        task = find(state, "tasks", task_id)
        person = find(state, "assets", person_id)
        if task["execution_mode"] != "integration" or person["task_id"] != task_id or person["type"] != "person":
            raise HTTPException(409, "REGISTERED_PERSON_REQUIRED")
        rgb, _ = self.production.registry.content(person_id)
        reference = person["legacy_reference"]
        if reference.get("alpha_mode") != "straight" or not reference.get("manifest_path"):
            raise HTTPException(409, "RGB_ALPHA_MANIFEST_REQUIRED：旧成片不能冒充人像母资产")
        try:
            alpha = self.production.registry.read_path(reference["alpha_path"])
            manifest_path = self.production.registry.read_path(reference["manifest_path"])
        except ValueError as exc:
            raise HTTPException(409, "MOTHER_INPUT_PATH_NOT_AUTHORIZED") from exc
        if not manifest_path.is_file() or file_hash(manifest_path) != reference.get("manifest_sha256"):
            raise HTTPException(409, "MOTHER_MANIFEST_CHANGED")
        if not alpha.is_file() or not manifest_path.is_file() or verify and (file_hash(alpha) != reference.get("alpha_sha256") or file_hash(rgb) != person["sha256"]):
            raise HTTPException(409, "MOTHER_ASSET_CHANGED")
        manifest = json.loads(manifest_path.read_text())
        if not manifest.get("full_decode_verified"):
            raise HTTPException(409, "MOTHER_FULL_DECODE_REQUIRED")
        return task, person, rgb, alpha, manifest

    async def prepare(self, task_id, request):
        async with self.lock, self.production.start_lock:
            task, person, rgb, alpha, manifest = await asyncio.to_thread(self.pair, task_id, request.person_asset_id)
            helper = Path(__file__).with_name("legacy")
            fingerprint = hashlib.sha256(json.dumps({"request": request.model_dump(exclude={"idempotency_key"}),
                "parent_rgb": person["sha256"], "parent_alpha": person["legacy_reference"]["alpha_sha256"],
                "repair_worker": file_hash(helper / "matte_repair_worker.py"), "tracking_worker": file_hash(helper / "object_tracking_worker.py")},
                sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            previous = next((row for row in self.records(task_id) if row["idempotency_key"] == request.idempotency_key), None)
            if previous:
                values = request.model_dump(exclude={"idempotency_key"})
                if ({key: previous.get(key) for key in values} != values
                        or previous.get("parent_rgb_sha256") != person["sha256"]
                        or previous.get("parent_alpha_sha256") != person["legacy_reference"]["alpha_sha256"]):
                    raise HTTPException(409, "IDEMPOTENCY_CONFLICT")
                return previous, False
            previous = next((row for row in self.records(task_id) if row.get("request_fingerprint") == fingerprint
                             and row["status"] not in {"FAILED", "INTERRUPTED"}), None)
            if previous:
                return previous, False
            if request.end_frame > manifest["media"]["frame_count"]:
                raise HTTPException(422, "REPAIR_INTERVAL_OUTSIDE_SOURCE")
            selection = "technical_validation" if person["legacy_reference"].get("execution_scope") == "TECHNICAL_VALIDATION" else "production"
            current_person = task["context"].get(selection, {}).get("current_person_asset_id")
            if current_person and current_person != person["id"]:
                raise HTTPException(409, "PARENT_VERSION_CHANGED：下拉框只切换查看，请先恢复此版本或选择当前母资产再修复")
            self.ensure_idle(self.repository.state(), task_id)
            run_id, repair_id = new_id("run"), new_id("repair")
            directory = self.production.settings.storage_root / "projects" / task["project_id"] / "tasks" / task_id / "person" / "repairs" / repair_id
            record = {**request.model_dump(), "request_fingerprint": fingerprint, "id": repair_id, "run_id": run_id, "task_id": task_id,
                "parent_version": person["legacy_reference"]["version"], "parent_rgb_sha256": person["sha256"],
                "parent_alpha_sha256": person["legacy_reference"]["alpha_sha256"], "source_sha256": manifest["source_sha256"],
                "source_time_mapping": person["legacy_reference"].get("time_mapping_path"), "coordinate_space": "normalized source canvas",
                "source_width": manifest["media"]["width"], "source_height": manifest["media"]["height"],
                "operator": "factory_review_ui", "created_at": now(), "status": "QUEUED", "progress": 0, "directory": str(directory),
                "asset_id": None, "error_code": None}
            record["execution_scope"] = person["legacy_reference"].get("execution_scope", "PRODUCTION")
            def begin(state):
                self.ensure_idle(state, task_id)
                find(state, "tasks", task_id)["context"].setdefault("matte_repairs", []).append(record)
                state["runs"].append({"id": run_id, "task_id": task_id, "stage": "person", "provider": "FactoryMatteRepair",
                    "status": "queued", "progress": 0, "logs": ["局部修复已排队；母资产保持原样"], "created_at": now(),
                    "idempotency_key": request.idempotency_key})
                audit(state, "repair.queued", repair_id, f"{person['id']} [{request.start_frame},{request.end_frame}) {request.mode}", task_id)
            self.repository.mutate(begin)
            return record, True

    def update(self, task_id, repair_id, **values):
        def change(state):
            record = next(row for row in find(state, "tasks", task_id)["context"]["matte_repairs"] if row["id"] == repair_id)
            record.update(values)
        self.repository.mutate(change)

    def technical_path(self, value, expected=None):
        path = self.production.registry.allowed_path(value)
        root = (self.production.settings.storage_root / "technical_validation").resolve()
        if not path.is_relative_to(root) or expected is not None and path != Path(expected).resolve():
            raise ValueError("TECHNICAL_REPAIR_PATH_MISMATCH")
        return path

    @staticmethod
    def imported_annotations(request, width, height):
        keyframes = copy.deepcopy(request["keyframes"])
        for item in keyframes:
            if item.get("coordinate_space", "normalized") == "source_pixels":
                for point in item.get("points", []):
                    point["x"], point["y"] = point["x"] / width, point["y"] / height
                if item.get("box"):
                    item["box"] = [value / (width if index % 2 == 0 else height) for index, value in enumerate(item["box"])]
                for stroke in item.get("strokes", []):
                    for point in stroke.get("points", []):
                        point["x"], point["y"] = point["x"] / width, point["y"] / height
                item["coordinate_space"] = "normalized"
        return keyframes

    async def prepare_import(self, task_id, request):
        async with self.lock, self.production.start_lock:
            task, person, _, _, manifest = await asyncio.to_thread(self.pair, task_id, request.person_asset_id, False)
            if person["legacy_reference"].get("execution_scope") != "TECHNICAL_VALIDATION":
                raise HTTPException(409, "TECHNICAL_PERSON_REQUIRED")
            try:
                directory = self.technical_path(request.directory)
                paths = {name: self.technical_path(directory / f"{name}.json", directory / f"{name}.json") for name in ("request", "result")}
                if not all(path.is_file() for path in paths.values()):
                    raise ValueError("TECHNICAL_REPAIR_ARTIFACT_NOT_READY")
                original, result = [json.loads(paths[name].read_text()) for name in ("request", "result")]
                if result.get("status") != "PREVIEW_READY":
                    raise ValueError("TECHNICAL_REPAIR_ARTIFACT_NOT_READY")
                hashes = {name: file_hash(path) for name, path in paths.items()}
                normalized = RepairRequest.model_validate({"person_asset_id": person["id"], "idempotency_key": request.idempotency_key,
                    **{key: original[key] for key in ("start_frame", "end_frame", "mode", "direction")},
                    "keyframes": self.imported_annotations(original, manifest["media"]["width"], manifest["media"]["height"])})
                if normalized.end_frame > manifest["media"]["frame_count"]:
                    raise ValueError("REPAIR_INTERVAL_OUTSIDE_SOURCE")
            except (ValueError, OSError, KeyError, TypeError) as exc:
                raise HTTPException(409, str(exc)) from exc
            fingerprint = hashlib.sha256((person["id"] + hashes["request"] + hashes["result"]).encode()).hexdigest()
            previous = next((row for row in self.records(task_id) if row["idempotency_key"] == request.idempotency_key), None)
            if previous:
                if previous.get("import_fingerprint") != fingerprint or previous.get("directory") != str(directory):
                    raise HTTPException(409, "IDEMPOTENCY_CONFLICT")
                return previous, False
            previous = next((row for row in self.records(task_id) if row.get("import_fingerprint") == fingerprint
                             and row["status"] not in {"FAILED", "INTERRUPTED"}), None)
            if previous:
                return previous, False
            self.ensure_idle(self.repository.state(), task_id)
            if task["context"].get("technical_validation", {}).get("current_person_asset_id") != person["id"]:
                raise HTTPException(409, "PARENT_VERSION_CHANGED")
            run_id, repair_id = new_id("run"), new_id("repair")
            record = {**normalized.model_dump(), "id": repair_id, "run_id": run_id, "task_id": task_id,
                "parent_version": person["legacy_reference"]["version"], "parent_rgb_sha256": person["sha256"],
                "parent_alpha_sha256": person["legacy_reference"]["alpha_sha256"], "source_sha256": manifest["source_sha256"],
                "source_width": manifest["media"]["width"], "source_height": manifest["media"]["height"],
                "source_time_mapping": person["legacy_reference"].get("time_mapping_path"),
                "coordinate_space": "normalized source canvas", "origin_keyframes": original["keyframes"],
                "operator": "technical_artifact_registration", "created_at": now(), "status": "QUEUED", "progress": 0, "directory": str(directory),
                "asset_id": None, "error_code": None, "execution_scope": "TECHNICAL_VALIDATION", "imported": True,
                "import_fingerprint": fingerprint, "origin_request_sha256": hashes["request"], "origin_result_sha256": hashes["result"],
                "origin_task_id": original.get("task_id"), "origin_run_id": original.get("run_id"), "origin_repair_id": original.get("id"),
                "origin_person_asset_id": original.get("person_asset_id")}
            def begin(state):
                self.ensure_idle(state, task_id)
                current = find(state, "tasks", task_id)
                if current["context"].get("technical_validation", {}).get("current_person_asset_id") != person["id"]:
                    raise HTTPException(409, "PARENT_VERSION_CHANGED")
                current["context"].setdefault("matte_repairs", []).append(record)
                state["runs"].append({"id": run_id, "task_id": task_id, "stage": "person", "provider": "FactoryTechnicalRepairRegistration",
                    "status": "queued", "progress": 0, "created_at": now(), "idempotency_key": request.idempotency_key,
                    "logs": ["只读核验已执行的技术修复；不启动 SAM、ComfyUI 或外部付费请求，登记耗时不是原推理耗时"]})
                audit(state, "technical.repair.import.queued", repair_id, f"原修复 {record['origin_repair_id']}；母资产与业务状态保持原样", task_id)
            self.repository.mutate(begin)
            return record, True

    async def launch_import(self, task_id, repair_id):
        if repair_id in self.jobs:
            return
        self.cancels[repair_id] = threading.Event()
        self.jobs[repair_id] = asyncio.create_task(self.run_import(task_id, repair_id))
        self.jobs[repair_id].add_done_callback(lambda _: self.jobs.pop(repair_id, None))

    def tracking_fingerprint(self):
        helper = Path(__file__).with_name("legacy") / "object_tracking_worker.py"
        values = {}
        for node in ast.parse(helper.read_text()).body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and node.targets[0].id in {"SAM2_COMMIT", "SAM2_SHA256"}:
                values[node.targets[0].id] = ast.literal_eval(node.value)
        storage = self.production.settings.storage_root
        if file_hash(storage / "models/sam2.1_hiera_tiny.pt") != values["SAM2_SHA256"]:
            raise ValueError("TECHNICAL_TRACKING_MODEL_CHANGED")
        commit = subprocess.run(["git", "-C", str(storage / "vendor/sam2"), "rev-parse", "HEAD"], capture_output=True, text=True, check=True, timeout=10).stdout.strip()
        if commit != values["SAM2_COMMIT"]:
            raise ValueError("TECHNICAL_TRACKING_VENDOR_CHANGED")
        return {"model_sha256": values["SAM2_SHA256"], "code_commit": commit, "tracking_worker_sha256": file_hash(helper)}

    def decoded_hashes(self, path, pixel_format, cancel):
        arguments = ["ffmpeg", "-v", "error", "-xerror", "-i", str(path), "-map", "0:v:0", "-an", "-fps_mode", "passthrough",
                     "-pix_fmt", pixel_format, "-f", "framehash", "-hash", "sha256", "-"]
        process = subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, shell=False)
        started = time.monotonic()
        try:
            while True:
                if cancel.is_set() or time.monotonic() - started > 600:
                    raise ValueError("TECHNICAL_REPAIR_VALIDATION_INTERRUPTED" if cancel.is_set() else "TECHNICAL_REPAIR_DECODE_TIMEOUT")
                try:
                    stdout, _ = process.communicate(timeout=.5)
                    break
                except subprocess.TimeoutExpired:
                    continue
            if process.returncode:
                raise ValueError("TECHNICAL_REPAIR_FULL_DECODE_FAILED")
            hashes = [line.split(",")[-1].strip() for line in stdout.splitlines() if line and not line.startswith("#")]
            if not hashes or any(len(value) != 64 for value in hashes):
                raise ValueError("TECHNICAL_REPAIR_FRAME_HASH_INVALID")
            return hashes
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def verify_import(self, task_id, record, cancel):
        directory = self.technical_path(record["directory"])
        request_path, result_path = directory / "request.json", directory / "result.json"
        if file_hash(request_path) != record["origin_request_sha256"] or file_hash(result_path) != record["origin_result_sha256"]:
            raise ValueError("TECHNICAL_REPAIR_ARTIFACT_CHANGED")
        request, result = [json.loads(path.read_text()) for path in (request_path, result_path)]
        task, parent, rgb, parent_alpha, manifest = self.pair(task_id, record["person_asset_id"])
        parent_ref = parent["legacy_reference"]
        if (task["context"].get("technical_validation", {}).get("current_person_asset_id") != parent["id"]
                or parent_ref["version"] != record["parent_version"]):
            raise ValueError("PARENT_VERSION_CHANGED")
        if (request.get("execution_scope") != parent_ref["origin_execution_scope"] or request.get("task_id") != parent_ref["origin_task_id"]
                or request.get("parent_manifest") != manifest or request.get("parent_rgb_sha256") != parent["sha256"]
                or request.get("parent_alpha_sha256") != parent_ref["alpha_sha256"] or request.get("parent_reference", {}).get("source_sha256") != manifest["source_sha256"]
                or Path(request["rgb_path"]).resolve() != rgb or Path(request["alpha_path"]).resolve() != parent_alpha
                or request.get("source_width") != manifest["media"]["width"] or request.get("source_height") != manifest["media"]["height"]):
            raise ValueError("TECHNICAL_REPAIR_PARENT_MISMATCH")
        if (any(request["parent_reference"].get(key) != parent_ref.get(key) for key in ("alpha_mode", "source_sha256", "workflow_sha256", "alpha_sha256"))
                or any(Path(request["parent_reference"].get(key, "")).resolve() != Path(parent_ref.get(key, "")).resolve()
                       for key in ("manifest_path", "frame_hashes_path", "time_mapping_path", "source_binding_path"))):
            raise ValueError("TECHNICAL_REPAIR_PARENT_REFERENCE_MISMATCH")
        if any(request.get(key) != record["origin_" + suffix] for key, suffix in (("task_id", "task_id"), ("run_id", "run_id"), ("id", "repair_id"), ("person_asset_id", "person_asset_id"))):
            raise ValueError("TECHNICAL_REPAIR_ORIGIN_MISMATCH")
        if result.get("status") != "PREVIEW_READY" or result["asset"].get("sha256") != parent["sha256"] or Path(result["asset"]["path"]).resolve() != rgb:
            raise ValueError("TECHNICAL_REPAIR_RESULT_MISMATCH")
        reference = result["asset"]["legacy_reference"]
        alpha = self.technical_path(reference["alpha_path"], directory / "Alpha.v2.mkv")
        manifest_path = self.technical_path(reference["manifest_path"], directory / "manifest.json")
        evidence_path = self.technical_path(result["frame_evidence_path"], directory / "frame_evidence.json")
        hashes_path = self.technical_path(reference["frame_hashes_path"], directory / "frame_hashes.json")
        tracking_path = self.technical_path(result["tracking_summary_path"], directory / "tracking_summary.json")
        repair_manifest, evidence, hashes, tracking = [json.loads(path.read_text()) for path in (manifest_path, evidence_path, hashes_path, tracking_path)]
        if (repair_manifest.get("schema") != "factory.foreground.repair.v1" or repair_manifest.get("repair_id") != request["id"]
                or repair_manifest.get("processing_run_id") != request["run_id"] or repair_manifest.get("parent_alpha_sha256") != parent_ref["alpha_sha256"]
                or repair_manifest.get("repair") != evidence or repair_manifest.get("semantic_review") != "PENDING"
                or reference.get("parent_person_asset_id") != request["person_asset_id"] or reference.get("repair_id") != request["id"]
                or reference.get("alpha_sha256") != file_hash(alpha) or file_hash(tracking_path) != result["tracking_summary_sha256"]):
            raise ValueError("TECHNICAL_REPAIR_MANIFEST_MISMATCH")
        if not {"black", "white", "bright", "source", "alpha_overlay"}.issubset({row["background"] for row in repair_manifest.get("previews", [])}):
            raise ValueError("TECHNICAL_REPAIR_DIAGNOSTIC_PREVIEWS_REQUIRED")
        tracking_task_id = tracking.get("task_id")
        original_task_binding = (tracking_task_id in (None, "") and record.get("execution_scope") == "TECHNICAL_VALIDATION"
            and parent_ref.get("execution_scope") == "TECHNICAL_VALIDATION"
            and request.get("execution_scope") == "TECHNICAL_VALIDATION_NO_BUSINESS_TASK")
        if (tracking.get("status") != "READY" or tracking.get("provider") != "SAM2.1"
                or tracking_task_id != request["task_id"] and not original_task_binding
                or tracking.get("source_sha256") != parent["sha256"] or Path(tracking.get("source_rgb", "")).resolve() != rgb
                or any(tracking.get(key) != value for key, value in self.tracking_fingerprint().items())
                or any(tracking.get(key) != request[key] for key in ("start_frame", "end_frame", "direction"))
                or tracking.get("interval") != "half_open" or tracking.get("width") != record["source_width"] or tracking.get("height") != record["source_height"]):
            raise ValueError("TECHNICAL_TRACKING_IDENTITY_MISMATCH")
        annotations = [{key: value for key, value in item.items() if key != "strokes"} for item in request["keyframes"]]
        for index, item in enumerate(annotations):
            if request["keyframes"][index].get("strokes"):
                item["mask_path"] = str(self.technical_path(directory / f"keyframe_{index:03d}.png"))
        if tracking.get("keyframes") != annotations:
            raise ValueError("TECHNICAL_TRACKING_ANNOTATIONS_MISMATCH")
        objects = sorted({item["object_id"] for item in annotations})
        indexes = list(range(record["start_frame"], record["end_frame"]))
        if tracking.get("object_ids") != objects or tracking.get("frame_count") != len(indexes) or [row["frame_index"] for row in tracking["frames"]] != indexes:
            raise ValueError("TECHNICAL_TRACKING_FRAME_OR_OBJECT_MISSING")
        for row in tracking["frames"]:
            if set(row["objects"]) != set(objects):
                raise ValueError("TECHNICAL_TRACKING_FRAME_OR_OBJECT_MISSING")
            for item in row["objects"].values():
                if cancel.is_set():
                    raise ValueError("TECHNICAL_REPAIR_VALIDATION_INTERRUPTED")
                path = self.technical_path(item["mask_path"])
                header = path.read_bytes()[:26]
                if (file_hash(path) != item["sha256"] or header[:8] != b"\x89PNG\r\n\x1a\n" or len(header) < 26
                        or struct.unpack(">II", header[16:24]) != (record["source_width"], record["source_height"]) or header[24:26] != b"\x08\x00"):
                    raise ValueError("TECHNICAL_TRACKING_MASK_CHANGED")
        before = self.decoded_hashes(parent_alpha, "gray16le", cancel)
        after = self.decoded_hashes(alpha, "gray16le", cancel)
        actual_rgb = self.decoded_hashes(rgb, "rgb48le", cancel)
        parent_hashes_path = self.production.registry.allowed_path(parent_ref["frame_hashes_path"])
        parent_hashes = json.loads(parent_hashes_path.read_text())
        count = manifest["media"]["frame_count"]
        changed = [index for index in range(count) if index < len(before) and index < len(after) and before[index] != after[index]]
        outside = [index for index in range(count) if not record["start_frame"] <= index < record["end_frame"]]
        if (any(len(value) != count for value in (before, after, actual_rgb)) or actual_rgb != parent_hashes.get("rgb") or actual_rgb != hashes.get("rgb")
                or before != parent_hashes.get("alpha") or before != evidence.get("alpha_before") or after != hashes.get("alpha") or after != evidence.get("alpha_after")):
            raise ValueError("TECHNICAL_REPAIR_LOSSLESS_READBACK_MISMATCH")
        if (not changed or changed != evidence.get("changed_frames") or result.get("changed_frame_count") != len(changed)
                or any(before[index] != after[index] for index in outside) or evidence.get("outside_frame_count") != len(outside)
                or evidence.get("interval") != "half_open" or evidence.get("outside_alpha_hashes_unchanged") is not True
                or evidence.get("rgb_reused_unchanged") is not True or evidence.get("automatic_semantic_pass") is not False
                or any(evidence.get(key) != record[key] for key in ("start_frame", "end_frame"))):
            raise ValueError("TECHNICAL_REPAIR_OUTSIDE_INTERVAL_OR_CHANGE_MISMATCH")
        candidate = copy.deepcopy(result["asset"])
        inherited = ("execution_scope", "origin_execution_scope", "origin_task_id", "origin_context_id", "origin_processing_run_id", "origin_task_context",
                     "origin_manifest_path", "origin_manifest_sha256", "source_binding_path", "source_binding_sha256", "technical_binding")
        candidate["legacy_reference"].update({key: copy.deepcopy(parent_ref[key]) for key in inherited})
        candidate["legacy_reference"].update(parent_person_asset_id=parent["id"], origin_person_asset_id=request["person_asset_id"],
            origin_repair_id=request["id"], origin_repair_run_id=request["run_id"], origin_request_sha256=record["origin_request_sha256"],
            origin_result_sha256=record["origin_result_sha256"], tracking_summary_sha256=result["tracking_summary_sha256"])
        provenance = {"task_binding_source": "origin_request" if original_task_binding else "tracking_summary",
            "tracking_task_id": tracking_task_id, "tracking_task_id_present": "task_id" in tracking,
            "origin_request_task_id": request["task_id"], "origin_request_sha256": record["origin_request_sha256"],
            "tracking_summary_sha256": result["tracking_summary_sha256"]}
        candidate["legacy_reference"]["tracking_task_binding"] = provenance
        candidate = self.production.registry._person_pair(task, candidate, rgb, probe_media(rgb, count_frames=True), file_hash(rgb))
        if result.get("preview_path") != candidate["legacy_reference"]["preview_path"] or result.get("preview_sha256") != candidate["legacy_reference"]["preview_sha256"]:
            raise ValueError("TECHNICAL_REPAIR_PREVIEW_MISMATCH")
        for row in repair_manifest["previews"]:
            if len(self.decoded_hashes(self.technical_path(directory / row["filename"]), "rgb24", cancel)) != count:
                raise ValueError("TECHNICAL_REPAIR_PREVIEW_FULL_DECODE_MISMATCH")
        if file_hash(request_path) != record["origin_request_sha256"] or file_hash(result_path) != record["origin_result_sha256"]:
            raise ValueError("TECHNICAL_REPAIR_ARTIFACT_CHANGED")
        return {**result, "asset": candidate, "original_execution": {"task_id": request["task_id"], "run_id": request["run_id"],
            "repair_id": request["id"], "person_asset_id": request["person_asset_id"], "execution_scope": request["execution_scope"],
            "request_path": str(request_path), "request_sha256": record["origin_request_sha256"], "result_path": str(result_path),
            "result_sha256": record["origin_result_sha256"]}, "artifact_hashes": {
                str(path): file_hash(path) for path in (request_path, result_path, manifest_path, evidence_path, hashes_path, tracking_path)},
            "provenance": provenance, "registration_verified": True, "automatic_semantic_pass": False}

    async def run_import(self, task_id, repair_id):
        record = next(row for row in self.records(task_id) if row["id"] == repair_id)
        run_id, started = record["run_id"], time.monotonic()
        cancel = self.cancels[repair_id]
        self.update(task_id, repair_id, status="VERIFYING", progress=5)
        self.repository.mutate(lambda state: find(state, "runs", run_id).update(status="running", started_at=now(), progress=5))
        try:
            await self.production.event(task_id, "person", "只读核验技术修复：父版本、跟踪模型、实际逐帧读回与区间外哈希", "TECHNICAL_VALIDATION", run_id)
            evidence = await asyncio.to_thread(self.verify_import, task_id, record, cancel)
            if cancel.is_set():
                raise ValueError("TECHNICAL_REPAIR_VALIDATION_INTERRUPTED")
            def complete(state):
                task = find(state, "tasks", task_id)
                current = next(row for row in task["context"]["matte_repairs"] if row["id"] == repair_id)
                if current["status"] != "VERIFYING" or task["context"]["technical_validation"]["current_person_asset_id"] != record["person_asset_id"]:
                    raise ValueError("PARENT_VERSION_CHANGED")
                current.update(status="PREVIEW_READY", progress=100, evidence=evidence, provenance=evidence["provenance"], verified_at=now())
                find(state, "runs", run_id).update(status="success", progress=100, finished_at=now(), duration_ms=int((time.monotonic()-started)*1000))
                audit(state, "technical.repair.import.verified", repair_id, "只读技术修复核验通过；等待应用，未认定画面质量通过，业务状态不变", task_id)
            self.repository.mutate(complete)
            await self.production.event(task_id, "person", "技术修复预览已核验；请预览、应用新版本或保留父版本", "TECHNICAL_VALIDATION", run_id)
        except (ValueError, OSError, KeyError, TypeError, HTTPException, subprocess.SubprocessError) as exc:
            code = str(exc) if isinstance(exc, ValueError) else "TECHNICAL_REPAIR_" + type(exc).__name__.upper()
            self.update(task_id, repair_id, status="FAILED", error_code=code)
            self.repository.mutate(lambda state: find(state, "runs", run_id).update(status="failed", error_code=code,
                error_message="技术修复登记未通过；原证据、母资产、推理历史及业务状态保留", finished_at=now(), duration_ms=int((time.monotonic()-started)*1000)))
            await self.production.event(task_id, "person", code, "FAILED", run_id)
        finally:
            self.cancels.pop(repair_id, None)

    async def launch(self, task_id, repair_id):
        if repair_id in self.jobs:
            return
        self.cancels[repair_id] = threading.Event()
        self.jobs[repair_id] = asyncio.create_task(self.run(task_id, repair_id))
        self.jobs[repair_id].add_done_callback(lambda _: self.jobs.pop(repair_id, None))

    async def run(self, task_id, repair_id):
        record = next(row for row in self.records(task_id) if row["id"] == repair_id)
        run_id = record["run_id"]
        directory = Path(record["directory"])
        loop = asyncio.get_running_loop()
        event_lock, event_futures, process_info = asyncio.Lock(), [], {}
        pending_tracking, tracking_at, tracking_direction = None, 0, None
        async def persist_event(event):
            def change(state):
                run = find(state, "runs", run_id)
                run["last_heartbeat"] = now()
                if event.get("type") == "PROCESS_STARTED":
                    run.update(**{key: event[key] for key in ("pid", "command", "cwd", "stdout_log_path", "stderr_log_path")})
                if event.get("type") == "process_finished":
                    run.update(exit_code=event.get("exit_code"), termination_signal=event.get("termination_signal"),
                               timed_out=event.get("timed_out", False), resource_info=event.get("resource_info", {}))
                if event.get("message"):
                    run["logs"].append(event["message"])
                if event.get("progress") is not None:
                    run["progress"] = event["progress"]
                    repair = next(row for row in find(state, "tasks", task_id)["context"]["matte_repairs"] if row["id"] == repair_id)
                    repair["progress"] = event["progress"]
                if event.get("tracking_stage") == "PROPAGATING" and event.get("frame_index") is not None:
                    repair = next(row for row in find(state, "tasks", task_id)["context"]["matte_repairs"] if row["id"] == repair_id)
                    repair["tracking_progress"] = {key: event[key] for key in ("frame_index", "total", "direction") if key in event}
                    repair["tracking_progress"]["interval_start"] = record["start_frame"]
                    repair["tracking_progress"]["interval_end"] = record["end_frame"]
            async with event_lock:
                await asyncio.to_thread(self.repository.mutate, change)
                if event.get("type") != "PROCESS_HEARTBEAT":
                    await self.production.event(task_id, "person", event.get("message", "局部修复处理中"),
                        "RUNNING", run_id, progress=event.get("progress"))
        def schedule(event):
            event_futures.append(asyncio.run_coroutine_threadsafe(persist_event(dict(event)), loop))
        def progress(event):
            nonlocal pending_tracking, tracking_at, tracking_direction
            if event.get("type") == "output":
                return  # Complete raw stdout/stderr remains in ProcessRunner's files.
            if event.get("type") in {"PROCESS_STARTED", "process_finished"}:
                process_info.update({key: event[key] for key in ("pid", "command", "cwd", "stdout_log_path", "stderr_log_path",
                    "exit_code", "termination_signal", "timed_out", "resource_info", "duration_ms") if key in event})
            if event.get("tracking_stage") == "PROPAGATING" and event.get("frame_index") is not None:
                current = time.monotonic()
                direction = event.get("direction")
                if direction == tracking_direction and current - tracking_at < 1:
                    pending_tracking = dict(event)
                    return
                if pending_tracking is not None and direction != tracking_direction:
                    schedule(pending_tracking)
                pending_tracking = None
                tracking_at, tracking_direction = current, direction
            elif pending_tracking is not None:
                schedule(pending_tracking)
                pending_tracking = None
            schedule(event)
        async def drain_events():
            nonlocal pending_tracking
            if pending_tracking is not None:
                schedule(pending_tracking)
                pending_tracking = None
            completed = await asyncio.gather(*(asyncio.wrap_future(value) for value in event_futures), return_exceptions=True)
            return [value for value in completed if isinstance(value, BaseException)]
        event_errors = []
        try:
            task, person, rgb, alpha, manifest = await asyncio.to_thread(self.pair, task_id, record["person_asset_id"])
            directory.mkdir(parents=True, exist_ok=False)
            input_path, result_path = directory / "request.json", directory / "result.json"
            payload = {**record, "rgb_path": str(rgb), "alpha_path": str(alpha), "parent_manifest": manifest,
                "parent_reference": person["legacy_reference"]}
            objects = {item["object_id"] for item in record["keyframes"]}
            for previous in reversed(self.records(task_id)):
                tracking = previous.get("evidence", {}).get("tracking_summary_path")
                if not tracking or previous["id"] == repair_id or previous.get("parent_rgb_sha256") != record["parent_rgb_sha256"]:
                    continue
                if (any(previous[key] != record[key] for key in ("start_frame", "end_frame", "direction"))
                    or [item for item in previous["keyframes"] if item["object_id"] in objects] != record["keyframes"]):
                    continue
                request_path = self.production.registry.allowed_path(Path(previous["directory"]) / "request.json")
                tracking_path = self.production.registry.allowed_path(tracking)
                payload["cached_tracking"] = {"path": str(tracking_path), "sha256": previous["evidence"]["tracking_summary_sha256"],
                    "request_path": str(request_path), "request_sha256": file_hash(request_path)}
                break
            input_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
            self.update(task_id, repair_id, status="PROPAGATING")
            self.repository.mutate(lambda state: find(state, "runs", run_id).update(status="running", started_at=now()))
            command = [str(self.production.settings.quality_python), "-B", str(Path(__file__).with_name("legacy") / "matte_repair_worker.py"),
                "--request", str(input_path), "--result", str(result_path)]
            process = await asyncio.to_thread(ProcessRunner().run, command, cwd=directory, log_dir=directory / "logs",
                timeout_s=getattr(self.production.settings, "matte_repair_timeout_seconds", 7200), on_event=progress, cancel_event=self.cancels[repair_id])
            process_info.update({key: value for key, value in asdict(process).items() if key in {"pid", "exit_code", "duration_ms",
                "stdout_log_path", "stderr_log_path", "termination_signal", "timed_out", "resource_info"}
                and not (key == "pid" and not value or key == "resource_info" and value is None)})
            event_errors = await drain_events()
            if event_errors:
                raise event_errors[0]
            summary = json.loads(result_path.read_text()) if result_path.is_file() else {"status": "FAILED", "error_code": "REPAIR_PROCESS_FAILED"}
            if process.cancelled:
                raise ValueError("PROCESS_CANCELLED")
            if process.timed_out:
                raise ValueError("PROCESS_TIMEOUT")
            if process.exit_code != 0 or summary["status"] != "PREVIEW_READY":
                raise ValueError(f"WORKER_SIGNAL_{-process.exit_code}" if process.exit_code < 0 else summary.get("error_code") or "REPAIR_PROCESS_FAILED")
            self.update(task_id, repair_id, status="PREVIEW_READY", progress=100, evidence=summary)
            self.repository.mutate(lambda state: find(state, "runs", run_id).update(status="success", progress=100, finished_at=now(),
                **process_info))
            await self.production.event(task_id, "person", "修复预览已生成；区间外解码帧保持不变，等待应用新版本", "WAITING_REVIEW", run_id)
        except Exception as exc:
            if not event_errors:
                event_errors = await drain_events()
            code = "REPAIR_EVENT_PERSISTENCE_FAILED" if event_errors else str(exc) if isinstance(exc, ValueError) else "REPAIR_" + type(exc).__name__.upper()
            self.update(task_id, repair_id, status="FAILED", error_code=code)
            self.repository.mutate(lambda state: find(state, "runs", run_id).update(status="failed", error_code=code,
                error_message=f"局部修复事件保存失败（{type(event_errors[0]).__name__}）；真实进程结果与母资产保留" if event_errors else "局部修复未完成；母资产与所有旧版本保留",
                finished_at=now(), **process_info))
            await self.production.event(task_id, "person", code, "FAILED", run_id)
        finally:
            self.cancels.pop(repair_id, None)

            if not self.closing and record.get("execution_scope", "PRODUCTION") == "PRODUCTION":
                await self.production.consume_deferred_continue(task_id)

    async def apply(self, task_id, repair_id):
        async with self.lock, self.production.start_lock:
            record = next((row for row in self.records(task_id) if row["id"] == repair_id), None)
            if not record or record["status"] not in {"PREVIEW_READY", "APPLIED"}:
                raise HTTPException(409, "REPAIR_PREVIEW_REQUIRED")
            if record["status"] == "APPLIED":
                return record
            self.ensure_idle(self.repository.state(), task_id)
            task, parent, rgb, _, _ = await asyncio.to_thread(self.pair, task_id, record["person_asset_id"])
            technical = record.get("execution_scope") == "TECHNICAL_VALIDATION"
            selection = "technical_validation" if technical else "production"
            current = task["context"].get(selection, {}).get("current_person_asset_id")
            if current and current != parent["id"]:
                raise HTTPException(409, "PARENT_VERSION_CHANGED：请选择最新母资产重新修复")
            evidence = record["evidence"]
            if record.get("imported"):
                if not evidence.get("registration_verified") or not evidence.get("artifact_hashes"):
                    raise HTTPException(409, "TECHNICAL_REPAIR_VERIFICATION_REQUIRED")
                if any(file_hash(self.technical_path(path)) != digest for path, digest in evidence["artifact_hashes"].items()):
                    raise HTTPException(409, "TECHNICAL_REPAIR_ARTIFACT_CHANGED")
            new_asset = await asyncio.to_thread(self.production.registry.register, task, evidence["asset"], "person", record["run_id"])
            def change(state):
                task = find(state, "tasks", task_id)
                row = next(item for item in task["context"]["matte_repairs"] if item["id"] == repair_id)
                if row["status"] == "APPLIED":
                    return
                if task["context"].get(selection, {}).get("current_person_asset_id") not in (None, parent["id"]):
                    raise HTTPException(409, "PARENT_VERSION_CHANGED")
                task["context"].setdefault(selection, {}).update(current_person_asset_id=new_asset["id"], review_approved=False)
                if not technical:
                    task.update(status="WAITING_REVIEW", current_stage="review", updated_at=now())
                state["reviews"].append({"id": new_id("rev"), "task_id": task_id, "asset_id": new_asset["id"], "status": "pending",
                    "issues": ["TECHNICAL_VALIDATION" if technical else "LOCAL_REPAIR_REQUIRES_REVIEW"], "note": "局部修复新版本；旧成片和背景保留，尚未继承人工通过"})
                for asset in state["assets"]:
                    if asset["task_id"] == task_id and asset["type"] == "compose" and asset["legacy_reference"].get("person_asset_id") == parent["id"]:
                        asset["legacy_reference"]["superseded_by_person_asset_id"] = new_asset["id"]
                audit(state, "repair.applied", repair_id, f"{parent['id']} → {new_asset['id']}；区间外帧证据通过", task_id)
                row.update(status="APPLIED", asset_id=new_asset["id"], applied_at=now())
            self.repository.mutate(change)
            return next(row for row in self.records(task_id) if row["id"] == repair_id)

    def select_restored(self, state, task_id, person, departing_id, technical):
        task = find(state, "tasks", task_id)
        selection = "technical_validation" if technical else "production"
        production = task["context"].setdefault(selection, {})
        reviews = [row for row in state["reviews"] if row["task_id"] == task_id and row["asset_id"] == person["id"]]
        def order(pair):
            parts = pair[1].get("id", "").split("-")
            timestamp = parts[1] if len(parts) > 1 and parts[1].isdigit() else ""
            created = "".join(character for character in str(pair[1].get("created_at") or "") if character.isdigit())[:20]
            return created or timestamp, pair[0]
        latest = max(enumerate(reviews), key=order)[1] if reviews else None
        approved = bool(not technical and person.get("status") == "approved" and latest and latest["status"] == "approved"
                        and any(row.get("action") == "review.approve" and row.get("entity_id") == latest["id"]
                                and row.get("task_id") == task_id for row in state["audits"]))
        production.update(current_person_asset_id=person["id"], review_approved=approved)
        production.pop("requested_person_asset_id", None)
        production.pop("waiting", None)
        if not technical:
            task.update(status="COLLECTED" if approved else "WAITING_REVIEW", current_stage="prompt" if approved else "review", updated_at=now())
            rejection = production.get("rejected_review", {})
            if approved and rejection.get("asset_id") == person["id"] and rejection.get("review_id") != latest["id"]:
                production.setdefault("rejected_review_history", []).append(production.pop("rejected_review"))
        if not approved and (latest is None or latest["status"] not in {"pending", "manual"}):
            state["reviews"].append({"id": new_id("rev"), "task_id": task_id, "asset_id": person["id"],
                "status": "pending", "issues": ["TECHNICAL_VALIDATION" if technical else "RESTORED_VERSION_REQUIRES_REVIEW"],
                "note": "恢复历史母资产选择；旧审核与所有文件保留，当前版本尚无可复用人工批准"})
        for asset in state["assets"]:
            reference = asset.get("legacy_reference", {})
            if (asset["task_id"] != task_id or asset["type"] != "compose"
                    or (reference.get("execution_scope") == "TECHNICAL_VALIDATION") != technical):
                continue
            if departing_id and reference.get("person_asset_id") == person["id"] and reference.get("superseded_by_person_asset_id") == departing_id:
                reference.pop("superseded_by_person_asset_id")
            elif departing_id and reference.get("person_asset_id") == departing_id and not reference.get("superseded_by_person_asset_id"):
                reference["superseded_by_person_asset_id"] = person["id"]
        return approved, latest["id"] if approved else None

    async def undo(self, task_id, repair_id):
        async with self.lock, self.production.start_lock:
            record = next((row for row in self.records(task_id) if row["id"] == repair_id), None)
            if not record or record["status"] != "APPLIED":
                raise HTTPException(409, "APPLIED_REPAIR_REQUIRED")
            self.ensure_idle(self.repository.state(), task_id)
            _, parent, _, _, _ = await asyncio.to_thread(self.pair, task_id, record["person_asset_id"])
            technical = parent["legacy_reference"].get("execution_scope") == "TECHNICAL_VALIDATION"
            if technical != (record.get("execution_scope") == "TECHNICAL_VALIDATION"):
                raise HTTPException(409, "PERSON_SCOPE_MISMATCH：技术验收资产不能替代正常生产资产")
            if (record.get("parent_rgb_sha256", parent["sha256"]) != parent["sha256"]
                    or record.get("parent_alpha_sha256", parent["legacy_reference"]["alpha_sha256"]) != parent["legacy_reference"]["alpha_sha256"]):
                raise HTTPException(409, "PARENT_IDENTITY_CHANGED")
            approved = False
            review_id = None
            def change(state):
                nonlocal approved, review_id
                self.ensure_idle(state, task_id)
                task = find(state, "tasks", task_id)
                selection = "technical_validation" if technical else "production"
                if task["context"][selection].get("current_person_asset_id") != record["asset_id"]:
                    raise HTTPException(409, "CURRENT_VERSION_CHANGED")
                departing = find(state, "assets", record["asset_id"])
                if (departing["task_id"] != task_id or departing["type"] != "person"
                        or (departing["legacy_reference"].get("execution_scope") == "TECHNICAL_VALIDATION") != technical):
                    raise HTTPException(409, "PERSON_SCOPE_MISMATCH")
                approved, review_id = self.select_restored(state, task_id, parent, departing["id"], technical)
                row = next(item for item in task["context"]["matte_repairs"] if item["id"] == repair_id)
                row.update(status="UNDONE", undone_at=now())
                audit(state, "repair.undo", repair_id, "恢复父版本选择；所有媒体、修复证据及审核历史保留", task_id)
            self.repository.mutate(change)
            return {**next(row for row in self.records(task_id) if row["id"] == repair_id),
                    "review_reused": approved, "review_id": review_id,
                    "message": "父版本已恢复；文件与原人工批准身份一致，沿用该审核，按自动续跑设置继续。" if approved else "父版本已恢复；修复新版本未删除，当前版本仍需审核。"}

    async def restore_version(self, task_id, person_asset_id):
        async with self.lock, self.production.start_lock:
            self.ensure_idle(self.repository.state(), task_id)
            task, person, _, _, _ = await asyncio.to_thread(self.pair, task_id, person_asset_id)
            technical = person["legacy_reference"].get("execution_scope") == "TECHNICAL_VALIDATION"
            selection = "technical_validation" if technical else "production"
            current = task["context"].get(selection, {}).get("current_person_asset_id")
            if current == person_asset_id:
                return {"task_id": task_id, "person_asset_id": person_asset_id, "changed": False,
                        "message": "已是当前母资产版本；没有重复执行抠像或修复"}
            if current:
                previous = find(self.repository.state(), "assets", current)
                if (previous["legacy_reference"].get("execution_scope") == "TECHNICAL_VALIDATION") != technical:
                    raise HTTPException(409, "PERSON_SCOPE_MISMATCH：技术验收资产不能替代正常生产资产")
            approved = False
            review_id = None
            def change(state):
                nonlocal approved, review_id
                self.ensure_idle(state, task_id)
                current_task = find(state, "tasks", task_id)
                production = current_task["context"].setdefault(selection, {})
                if production.get("current_person_asset_id") != current:
                    raise HTTPException(409, "CURRENT_VERSION_CHANGED")
                approved, review_id = self.select_restored(state, task_id, person, current, technical)
                audit(state, "person.version.restore", person_asset_id, f"{current or 'NONE'} → {person_asset_id}；没有复制文件或重新抠像", task_id)
            self.repository.mutate(change)
            status = "COLLECTED" if approved else "WAITING_REVIEW"
            message = "已恢复相同母资产并沿用原人工批准；没有复制文件或重新抠像。" if approved else "已恢复历史版本；文件与历史保留，请审核当前版本。"
            await self.production.event(task_id, "person", message, status)
            return {"task_id": task_id, "person_asset_id": person_asset_id, "changed": True,
                    "status": status, "review_reused": approved, "review_id": review_id, "message": message}

    async def continue_after_version_change(self, task_id):
        task = find(self.repository.state(), "tasks", task_id)
        person_id = task["context"].get("production", {}).get("current_person_asset_id")
        if not person_id:
            return
        person = find(self.repository.state(), "assets", person_id)
        if person["legacy_reference"].get("execution_scope") == "TECHNICAL_VALIDATION":
            return
        try:
            queued = await self.production.continue_after_repair(task_id)
            if queued.get("accepted"):
                await self.production.launch(queued["batch_id"])
        except HTTPException as exc:
            await self.production.event(task_id, "quality", "母资产版本已保存；自动质检等待：" + str(exc.detail), "WAITING_MANUAL")

    async def shutdown(self):
        self.closing = True
        for cancel in self.cancels.values():
            cancel.set()
        if self.jobs:
            await asyncio.gather(*list(self.jobs.values()), return_exceptions=True)

    async def register_technical(self, task_id, manifest_path):
        async with self.lock, self.production.start_lock:
            task = find(self.repository.state(), "tasks", task_id)
            self.ensure_idle(self.repository.state(), task_id)
            run_id = new_id("run")
            self.repository.mutate(lambda state: state["runs"].append({"id": run_id, "task_id": task_id, "stage": "person",
                "provider": "FactoryTechnicalRegistration", "status": "running", "progress": 0, "created_at": now(), "started_at": now(),
                "logs": ["登记真实技术验收母资产；不改变采集决策，不计入生产成功"]}))
            try:
                asset = await asyncio.to_thread(self.production.registry.register_technical_pair, task, Path(manifest_path), run_id)
                def complete(state):
                    current = find(state, "tasks", task_id)
                    current["context"].setdefault("technical_validation", {}).update(current_person_asset_id=asset["id"])
                    if not any(review["asset_id"] == asset["id"] for review in state["reviews"]):
                        state["reviews"].append({"id": new_id("rev"), "task_id": task_id, "asset_id": asset["id"], "status": "pending",
                            "issues": ["TECHNICAL_VALIDATION"], "note": "真实问题原片的技术验收；不是本轮新 accepted，不改变原业务决策"})
                    find(state, "runs", run_id).update(status="success", progress=100, finished_at=now())
                    audit(state, "technical.register", asset["id"], "原母资产身份与任务关联分别保留；生产计数不变", task_id)
                self.repository.mutate(complete)
                await self.production.event(task_id, "person", "技术母资产已登记，业务任务和采集决策不变", "TECHNICAL_VALIDATION", run_id)
            except (ValueError, OSError, subprocess.SubprocessError) as exc:
                self.repository.mutate(lambda state: find(state, "runs", run_id).update(status="failed", error_code=str(exc), finished_at=now()))
                await self.production.event(task_id, "person", "技术母资产登记失败；原资产和业务状态保留", "FAILED", run_id)
                return None
            return asset

    async def technical_background(self, task_id, image_path):
        async with self.lock, self.production.start_lock:
            task = find(self.repository.state(), "tasks", task_id)
            self.ensure_idle(self.repository.state(), task_id)
            if not task["context"].get("technical_validation", {}).get("current_person_asset_id"):
                raise HTTPException(409, "TECHNICAL_PERSON_REQUIRED")
            path = self.production.registry.allowed_path(image_path)
            run_id = new_id("run")
            self.repository.mutate(lambda state: state["runs"].append({"id": run_id, "task_id": task_id, "stage": "background",
                "provider": "FactoryTechnicalBackgroundRegistration", "status": "running", "progress": 0, "created_at": now(), "started_at": now(),
                "logs": ["只读登记已有技术验收背景；未调用背景生成服务"]}))
            try:
                asset = await asyncio.to_thread(self.production.registry.register, task, {"path": str(path), "sha256": file_hash(path),
                    "source_system": "ExistingBackgroundTechnicalValidation", "legacy_reference": {"execution_scope": "TECHNICAL_VALIDATION",
                        "background_origin": "existing actual image; no new generation"}}, "background", run_id)
            except (ValueError, OSError, subprocess.SubprocessError) as exc:
                self.repository.mutate(lambda state: find(state, "runs", run_id).update(status="failed", error_code=str(exc), finished_at=now()))
                raise HTTPException(409, str(exc)) from exc
            self.repository.mutate(lambda state: find(state, "runs", run_id).update(status="success", progress=100, finished_at=now()))
            self.repository.mutate(lambda state: audit(state, "technical.background", asset["id"], "只读复用已有背景，不计新生产背景", task_id))
            return asset

    async def technical_compose(self, task_id):
        async with self.lock, self.production.start_lock:
            state = self.repository.state()
            self.ensure_idle(state, task_id)
            task = find(state, "tasks", task_id)
            person_id = task["context"].get("technical_validation", {}).get("current_person_asset_id")
            _, person, _, _, manifest = await asyncio.to_thread(self.pair, task_id, person_id)
            background = next((row for row in reversed(state["assets"]) if row["task_id"] == task_id and row["type"] == "background"
                and row["legacy_reference"].get("execution_scope") == "TECHNICAL_VALIDATION" and row["registry_status"] == "registered"), None)
            if not background:
                raise HTTPException(409, "EXISTING_TECHNICAL_BACKGROUND_REQUIRED")
            self.production.registry.content(background["id"])
            composition = next((row for row in reversed(state["assets"]) if row["task_id"] == task_id and row["type"] == "compose"
                and row["registry_status"] == "registered" and row["legacy_reference"].get("execution_scope") == "TECHNICAL_VALIDATION"
                and row["legacy_reference"].get("person_asset_id") == person["id"]
                and row["legacy_reference"].get("background_asset_id") == background["id"]), None)
            if composition:
                self.production.registry.content(composition["id"])
            run_id = new_id("run")
            directory = self.production.settings.storage_root / "technical_validation" / "registered" / task_id
            self.repository.mutate(lambda current: current["runs"].append({"id": run_id, "task_id": task_id, "stage": "compose",
                "provider": "FactoryTechnicalComposition", "status": "queued", "progress": 0, "created_at": now(),
                "logs": ["TECHNICAL_VALIDATION：复用已有 RGB/Alpha 与背景；不执行采集、抠像、Prompt 或 RPA"]}))
            payload = {**self.production.collector.context(task_id), "run_id": run_id, "execution_scope": "TECHNICAL_VALIDATION",
                "source_video": manifest["source_path"], "video_id": person["legacy_reference"]["technical_binding"]["video_id"],
                "person_asset": person, "background_asset": background, "composition_asset": composition,
                "job": {"id": "TECH-BG01", "prompt": "", "negative_prompt": ""}}
            return {"task_id": task_id, "run_id": run_id, "status": "QUEUED", "execution_scope": "TECHNICAL_VALIDATION"}, payload, directory

    async def launch_technical_compose(self, task_id, run_id, payload, directory):
        if run_id in self.jobs:
            return
        self.jobs[run_id] = asyncio.create_task(self.run_technical_compose(task_id, run_id, payload, directory))
        self.jobs[run_id].add_done_callback(lambda _: self.jobs.pop(run_id, None))

    async def run_technical_compose(self, task_id, run_id, payload, directory):
        cancel = threading.Event()
        self.cancels[run_id] = cancel
        self.repository.mutate(lambda state: find(state, "runs", run_id).update(status="running", started_at=now()))
        loop = asyncio.get_running_loop()
        def progress(event):
            def change(state):
                run = find(state, "runs", run_id)
                run.update(**{key: event[key] for key in ("pid", "command", "cwd", "progress", "duration_ms", "stdout_log_path", "stderr_log_path") if key in event})
                if event.get("message"):
                    run["logs"].append(event["message"])
            self.repository.mutate(change)
            asyncio.run_coroutine_threadsafe(self.production.event(task_id, "compose", event.get("message", "母资产合成处理中"), "TECHNICAL_VALIDATION", run_id, progress=event.get("progress")), loop)
        try:
            result = await self.production.providers["compose"].execute("compose", payload, directory, progress, cancel)
            if result["status"] != "READY":
                raise ValueError(result.get("error_code") or "TECHNICAL_COMPOSE_FAILED")
            self.production.save_snapshot(task_id, run_id, result.get("metadata", {}).get("workflow_snapshot"))
            if not result.get("assets"):
                raise ValueError("TECHNICAL_COMPOSE_OUTPUT_MISSING")
            task = find(self.repository.state(), "tasks", task_id)
            for evidence in result["assets"]:
                reference = evidence["legacy_reference"]
                fingerprint = reference.get("input_fingerprint", "")
                if len(fingerprint) != 64 or any(character not in "0123456789abcdef" for character in fingerprint):
                    raise ValueError("TECHNICAL_COMPOSE_INPUT_FINGERPRINT_REQUIRED")
                reference.update(execution_scope="TECHNICAL_VALIDATION", factory_fingerprint="technical-compose:" + fingerprint)
                asset = await asyncio.to_thread(self.production.registry.register, task, evidence, "compose", run_id,
                    person=payload["person_asset"], background=payload["background_asset"])
                def review(state):
                    if not any(row["asset_id"] == asset["id"] for row in state["reviews"]):
                        state["reviews"].append({"id": new_id("rev"), "task_id": task_id, "asset_id": asset["id"],
                            "status": "pending", "issues": ["TECHNICAL_VALIDATION"], "note": "母资产复用技术成片，尚未人工审核；不计本轮生产成功"})
                self.repository.mutate(review)
            if result.get("metadata", {}).get("cache_hit"):
                self.repository.mutate(lambda state: find(state, "runs", run_id)["logs"].append("已校验并复用同输入成片缓存；本次未提交 ComfyUI，原执行与资产历史保留"))
            process = {key: value for key, value in result.get("_process", {}).items() if key in {"pid", "command", "cwd", "duration_ms", "exit_code", "stdout_log_path", "stderr_log_path"}}
            self.repository.mutate(lambda state: find(state, "runs", run_id).update(status="success", progress=100, finished_at=now(), **process))
            await self.assess_technical_person(task_id, payload["person_asset"], cancel)
            await self.production.event(task_id, "compose", "真实技术成片已登记，等待人工检查；业务任务状态不变", "TECHNICAL_VALIDATION", run_id, progress=100)
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            self.repository.mutate(lambda state: find(state, "runs", run_id).update(status="failed", error_code=str(exc), finished_at=now()))
        finally:
            self.cancels.pop(run_id, None)

    async def assess_technical_person(self, task_id, person, cancel):
        run_id = new_id("run")
        task = find(self.repository.state(), "tasks", task_id)
        directory = self.production.settings.storage_root / "projects" / task["project_id"] / "tasks" / task_id / "person/quality" / run_id
        self.repository.mutate(lambda state: state["runs"].append({"id": run_id, "task_id": task_id, "stage": "quality",
            "provider": "OpenCVCutoutQualityAnalyzer", "status": "running", "progress": 0, "created_at": now(), "started_at": now(),
            "logs": ["TECHNICAL_VALIDATION：检查已登记母 Alpha；不改变采集决策，不自动通过审核"]}))
        try:
            result = await CutoutQualityAdapter(self.production.settings).analyze(person, directory, cancel_event=cancel)
            process = {key: value for key, value in result.get("_process", {}).items() if key in {
                "pid", "command", "cwd", "duration_ms", "exit_code", "stdout_log_path", "stderr_log_path"}}
            def save(state):
                run = find(state, "runs", run_id)
                run.update(status="success" if result["status"] in {"PASS", "FAIL"} else "failed", finished_at=now(), progress=100,
                    error_code=result.get("error_code"), **process)
                run["logs"].append(("已验证并复用真实检测缓存；本次未启动检测进程。" if result.get("cache_hit") else "真实 Alpha 检测完成。") +
                    "语义完整性 UNKNOWN；报告=" + str(result.get("report_path", "UNKNOWN")))
                if not any(row["asset_id"] == person["id"] and row["fingerprint"] == result.get("fingerprint") for row in state["quality_assessments"]):
                    state["quality_assessments"].append({"id": new_id("quality"), "task_id": task_id, "asset_id": person["id"],
                        "processing_run_id": run_id, "status": result["status"], "quality_score": result.get("quality_score"),
                        "metrics": {**result.get("metrics", {}), "geometry_status": result.get("geometry_status", "UNKNOWN"),
                            "semantic_status": "UNKNOWN", "report_path": result.get("report_path"), "cache_hit": result.get("cache_hit", False)},
                        "issue_segments": result.get("issue_segments", []), "fingerprint": result.get("fingerprint", ""), "created_at": now()})
                for review in state["reviews"]:
                    asset = find(state, "assets", review["asset_id"])
                    if review["status"] == "pending" and asset["legacy_reference"].get("person_asset_id") == person["id"]:
                        review["issues"] = list(dict.fromkeys(review["issues"] + [item["issue"] for item in result.get("issue_segments", [])]))
                audit(state, "technical.quality_recorded", person["id"], "仅几何/已标记对象检测；业务状态、人工审核和 FINAL 不变", task_id)
            self.repository.mutate(save)
            await self.production.event(task_id, "quality", "母 Alpha 几何检测 " + result["status"] + "；语义待人工审核", "TECHNICAL_VALIDATION", run_id, progress=100)
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            self.repository.mutate(lambda state: find(state, "runs", run_id).update(status="failed", finished_at=now(),
                error_code="QUALITY_ANALYSIS_FAILED", error_message=type(exc).__name__))

    async def recover(self):
        for task in self.repository.state()["tasks"]:
            for record in task["context"].get("matte_repairs", []):
                if record["status"] in {"QUEUED", "PROPAGATING", "VERIFYING"}:
                    self.update(task["id"], record["id"], status="INTERRUPTED", error_code="LOCAL_EXECUTION_INTERRUPTED")
                    self.repository.mutate(lambda state: find(state, "runs", record["run_id"]).update(status="failed", finished_at=now(),
                        error_code="LOCAL_EXECUTION_INTERRUPTED", error_message="本地修复中断；已保留标注、父版本和半成品，未自动重复执行"))
                    await self.production.event(task["id"], "person", "局部修复中断，标注/半成品/父版本已保留；显式重试，不自动修改母资产", "WAITING_REVIEW", record["run_id"])


def create_repair_router(service):
    router = APIRouter(tags=["matte-repair"])
    frame_slots = asyncio.Semaphore(2)

    @router.post("/api/tasks/{task_id}/technical-validation/register", status_code=202)
    async def register_technical(task_id: str, payload: dict, background_tasks: BackgroundTasks):
        path = service.production.registry.allowed_path(payload.get("manifest_path", ""))
        if not path.is_relative_to((service.production.settings.storage_root / "technical_validation").resolve()):
            raise HTTPException(422, "TECHNICAL_VALIDATION_PATH_REQUIRED")
        background_tasks.add_task(service.register_technical, task_id, path)
        return {"task_id": task_id, "status": "QUEUED", "execution_scope": "TECHNICAL_VALIDATION", "business_executed": False}

    @router.post("/api/tasks/{task_id}/technical-validation/background")
    async def technical_background(task_id: str, payload: dict):
        return await service.technical_background(task_id, payload.get("image_path", ""))

    @router.post("/api/tasks/{task_id}/technical-validation/repairs/import", status_code=202)
    async def import_repair(task_id: str, request: TechnicalRepairImportRequest, background_tasks: BackgroundTasks):
        record, created = await service.prepare_import(task_id, request)
        if created:
            background_tasks.add_task(service.launch_import, task_id, record["id"])
        return record

    @router.post("/api/tasks/{task_id}/technical-validation/compose", status_code=202)
    async def technical_compose(task_id: str, background_tasks: BackgroundTasks):
        queued, payload, directory = await service.technical_compose(task_id)
        background_tasks.add_task(service.launch_technical_compose, task_id, queued["run_id"], payload, directory)
        return queued

    @router.get("/api/tasks/{task_id}/repairs")
    def records(task_id: str):
        return service.records(task_id)

    @router.get("/api/tasks/{task_id}/person/{person_id}/info")
    async def info(task_id: str, person_id: str):
        _, _, _, _, manifest = await asyncio.to_thread(service.pair, task_id, person_id, False)
        return {"media": manifest["media"], "precision": manifest.get("precision"),
                "alpha_mode": manifest["alpha_mode"], "source_sha256": manifest["source_sha256"],
                "full_decode_verified": manifest["full_decode_verified"]}

    @router.post("/api/tasks/{task_id}/repairs", status_code=202)
    async def create(task_id: str, request: RepairRequest, background_tasks: BackgroundTasks):
        record, created = await service.prepare(task_id, request)
        if created:
            background_tasks.add_task(service.launch, task_id, record["id"])
        return record

    @router.post("/api/tasks/{task_id}/repairs/{repair_id}/apply")
    async def apply(task_id: str, repair_id: str, background_tasks: BackgroundTasks):
        previous = next((row for row in service.records(task_id) if row["id"] == repair_id), None)
        record = await service.apply(task_id, repair_id)
        if previous and previous["status"] != "APPLIED" and record.get("execution_scope") != "TECHNICAL_VALIDATION":
            background_tasks.add_task(service.continue_after_version_change, task_id)
        return record

    @router.post("/api/tasks/{task_id}/repairs/{repair_id}/undo")
    async def undo(task_id: str, repair_id: str, background_tasks: BackgroundTasks):
        record = await service.undo(task_id, repair_id)
        if record.get("execution_scope") != "TECHNICAL_VALIDATION":
            background_tasks.add_task(service.continue_after_version_change, task_id)
        return record

    @router.post("/api/tasks/{task_id}/person/restore")
    async def restore_version(task_id: str, request: RestorePersonVersionRequest, background_tasks: BackgroundTasks):
        result = await service.restore_version(task_id, request.person_asset_id)
        if result["changed"]:
            background_tasks.add_task(service.continue_after_version_change, task_id)
        return result

    @router.get("/api/tasks/{task_id}/repairs/{repair_id}/preview")
    def preview(task_id: str, repair_id: str):
        record = next((row for row in service.records(task_id) if row["id"] == repair_id), None)
        if not record or record["status"] not in {"PREVIEW_READY", "APPLIED", "UNDONE"}:
            raise HTTPException(404, "修复预览尚未生成")
        try:
            path = service.production.registry.read_path(record["evidence"]["preview_path"])
        except ValueError as exc:
            raise HTTPException(409, "PREVIEW_PATH_NOT_AUTHORIZED") from exc
        if file_hash(path) != record["evidence"]["preview_sha256"]:
            raise HTTPException(409, "PREVIEW_CHANGED")
        return FileResponse(path, media_type="video/mp4")

    @router.get("/api/tasks/{task_id}/person/{person_id}/frame")
    async def frame(task_id: str, person_id: str, request: Request, frame: int = 0, channel: Literal["rgb", "alpha", "rgba"] = "rgb"):
        async with frame_slots:
            if await request.is_disconnected():
                return Response(status_code=499)
            _, _, rgb, alpha, manifest = await asyncio.to_thread(service.pair, task_id, person_id, False)
            if frame < 0 or frame >= manifest["media"]["frame_count"]:
                raise HTTPException(422, "帧号超出母资产范围")
            media = manifest["media"]
            fps = media.get("fps", 0)
            paired_media = [manifest.get("rgb_media", {}), manifest.get("alpha_media", {})]
            # The verified CFR clock allows seeking between frames, avoiding both a
            # full FFV1 decode and rounding n/fps just past the requested frame.
            seek = (manifest.get("presentation_timestamps_verified") and fps > 0 and .5 / fps > .001501
                    and all(item.get("codec") == "ffv1" and float(item.get("video_start_time") or 0) == 0
                            and item.get("frame_count") == media["frame_count"] and item.get("fps") == fps
                            for item in paired_media))
            input_seek = ["-ss", f"{max(0, (frame - .5) / fps):.9f}"] if seek else []
            selected_frame = 0 if seek else frame
            arguments = ["ffmpeg", "-v", "error", "-threads", "2", *input_seek, "-i", str(rgb if channel == "rgb" else alpha),
                "-vf", f"select=eq(n\\,{selected_frame}),scale=720:-1", "-frames:v", "1", "-threads", "2", "-f", "image2pipe", "-vcodec", "png", "-"]
            if channel == "rgba":
                arguments = ["ffmpeg", "-v", "error", "-filter_complex_threads", "1", "-threads", "2", *input_seek, "-i", str(rgb),
                    "-threads", "2", *input_seek, "-i", str(alpha), "-filter_complex",
                    f"[0:v][1:v]alphamerge,select=eq(n\\,{selected_frame}),scale=720:-1,format=rgba[out]", "-map", "[out]",
                    "-frames:v", "1", "-threads", "2", "-f", "image2pipe", "-vcodec", "png", "-"]
            try:
                result = await asyncio.to_thread(subprocess.run, arguments, capture_output=True, check=True, timeout=30)
            except subprocess.SubprocessError:
                raise HTTPException(422, "FRAME_DECODE_FAILED") from None
        return Response(result.stdout, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})

    return router
