from __future__ import annotations

import asyncio
import hashlib
import json
import math
from dataclasses import asdict
from pathlib import Path

from app.integrations.process_runner import ProcessRunner

QUALITY_VERSION = "alpha-geometry-v2"
QUALITY_THRESHOLDS = {"max_dimension": 384, "alpha_threshold": 127, "min_foreground_area": 0.01,
                      "max_hole_ratio": 0.08, "max_area_change": 0.25, "min_motion_iou": 0.72,
                      "min_component_ratio": 0.65, "max_edge_change": 0.35, "min_issue_duration_s": 0.1,
                      "min_continuity_iou": 0.5, "max_bbox_jump": 0.12, "max_area_anomaly": 0.35,
                      "min_keep_object_coverage": 0.75, "min_object_pixels": 8, "max_foreground_area": 0.9999}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class CutoutQualityAdapter:
    provider = "OpenCVCutoutQualityAnalyzer"

    def __init__(self, settings, runner=None, thresholds=None):
        self.settings = settings
        self.runner = runner or ProcessRunner()
        self.thresholds = {**QUALITY_THRESHOLDS, **(thresholds or {})}
        if set(self.thresholds) != set(QUALITY_THRESHOLDS):
            raise ValueError("unknown quality threshold")
        for key, value in self.thresholds.items():
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError("invalid quality threshold: " + key)
        if not 16 <= self.thresholds["max_dimension"] <= 1920 or not 0 <= self.thresholds["alpha_threshold"] <= 255:
            raise ValueError("invalid mask analysis dimensions or threshold")

    async def analyze(self, person_asset, directory, on_event=None, cancel_event=None):
        return await asyncio.to_thread(self._analyze, person_asset, Path(directory), on_event, cancel_event)

    def _unknown(self, code, *, retryable=True):
        return {"provider": self.provider, "mode": "REAL", "version": QUALITY_VERSION, "status": "UNKNOWN",
                "quality_score": None, "metrics": {}, "issue_segments": [], "error_code": code,
                "retryable": retryable, "cache_hit": False, "_process": {}}

    def _keep_evidence(self, person_asset, storage):
        reference = person_asset.get("legacy_reference") or {}
        if reference.get("object_strategy", {}).get("mode") != "retain":
            return None
        evidence_path = Path(reference.get("repair_evidence_path", "")).resolve()
        if not evidence_path.is_relative_to(storage) or not evidence_path.is_file():
            raise ValueError("QUALITY_KEEP_EVIDENCE_MISSING")
        directory = evidence_path.parent
        tracking_path, request_path = directory / "tracking_summary.json", directory / "request.json"
        if not tracking_path.is_file() or not request_path.is_file():
            raise ValueError("QUALITY_KEEP_EVIDENCE_MISSING")
        tracking, request = json.loads(tracking_path.read_text()), json.loads(request_path.read_text())
        rgb_value = person_asset.get("external_path") or person_asset.get("storage_path")
        if not rgb_value:
            raise ValueError("QUALITY_KEEP_SOURCE_MISSING")
        rgb = Path(rgb_value).resolve()
        if not rgb.is_relative_to(storage) or not rgb.is_file() or sha256_file(rgb) != person_asset.get("sha256"):
            raise ValueError("QUALITY_KEEP_SOURCE_CHANGED")
        objects = reference["object_strategy"].get("object_ids", [])
        start, end = request.get("start_frame"), request.get("end_frame")
        tracking_hash = sha256_file(tracking_path)
        if (not isinstance(start, int) or not isinstance(end, int) or start < 0 or end <= start
                or request.get("mode") != "retain" or request.get("id") != reference.get("repair_id")
                or request.get("parent_rgb_sha256") != person_asset.get("sha256")
                or Path(request.get("rgb_path", "")).resolve() != rgb
                or tracking.get("status") != "READY" or not objects or len(set(objects)) != len(objects)
                or tracking.get("provider") not in {"SAM2.1", "ManualBrush"}
                or reference.get("tracking_summary_sha256", tracking_hash) != tracking_hash):
            raise ValueError("QUALITY_KEEP_EVIDENCE_MISMATCH")
        if tracking["provider"] == "SAM2.1" and (
                tracking.get("source_sha256") != person_asset.get("sha256") or tracking.get("interval") != "half_open"
                or Path(tracking.get("source_rgb", "")).resolve() != rgb
                or tracking.get("start_frame") != start or tracking.get("end_frame") != end
                or tracking.get("object_ids") != objects):
            raise ValueError("QUALITY_KEEP_EVIDENCE_MISMATCH")
        frames = tracking.get("frames", [])
        anchors = request.get("keyframes", [])
        if (not anchors or {item.get("object_id") for item in anchors} != set(objects)
                or any(not isinstance(item.get("frame_index"), int) or not start <= item["frame_index"] < end
                       or not (item.get("points") or item.get("box") or item.get("strokes")) for item in anchors)):
            raise ValueError("QUALITY_KEEP_ANNOTATION_MISSING")
        if tracking["provider"] == "SAM2.1":
            expected_anchors = [{key: value for key, value in item.items() if key != "strokes"} for item in anchors]
            for index, item in enumerate(anchors):
                if item.get("strokes"):
                    expected_anchors[index]["mask_path"] = str(directory / f"keyframe_{index:03d}.png")
            width, height = request.get("source_width"), request.get("source_height")
            def normalized_annotations(items):
                result = []
                for item in items:
                    space = item.get("coordinate_space", "normalized")
                    if space not in {"normalized", "source_pixels"} or space == "source_pixels" and (not width or not height):
                        raise ValueError("QUALITY_KEEP_ANNOTATION_MISMATCH")
                    scale_x, scale_y = (width, height) if space == "source_pixels" else (1, 1)
                    points = [{"x": round(value["x"] / scale_x, 12), "y": round(value["y"] / scale_y, 12), "label": value["label"]}
                              for value in item.get("points", [])]
                    box = item.get("box")
                    mask_hash = None
                    if item.get("mask_path"):
                        mask_path = Path(item["mask_path"]).resolve()
                        if not mask_path.is_relative_to(storage) or not mask_path.is_file():
                            raise ValueError("QUALITY_KEEP_ANNOTATION_MISMATCH")
                        mask_hash = sha256_file(mask_path)
                    result.append({"frame_index": item["frame_index"], "object_id": item["object_id"], "points": points,
                        "box": [round(value / (scale_x if index % 2 == 0 else scale_y), 12) for index, value in enumerate(box)] if box else None,
                        "mask_sha256": mask_hash})
                return result
            if normalized_annotations(tracking.get("keyframes", [])) != normalized_annotations(expected_anchors):
                raise ValueError("QUALITY_KEEP_ANNOTATION_MISMATCH")
        indexes = [row.get("frame_index") for row in frames]
        expected = list(range(start, end)) if tracking["provider"] == "SAM2.1" else sorted({item["frame_index"] for item in request["keyframes"]})
        if indexes != expected:
            raise ValueError("QUALITY_KEEP_FRAME_EVIDENCE_MISSING")
        for row in frames:
            if not row.get("objects") or not set(row["objects"]).issubset(objects) or tracking["provider"] == "SAM2.1" and set(row["objects"]) != set(objects):
                raise ValueError("QUALITY_KEEP_FRAME_EVIDENCE_MISSING")
            for item in row["objects"].values():
                mask = Path(item["mask_path"]).resolve()
                if not mask.is_relative_to(storage) or not mask.is_file() or sha256_file(mask) != item["sha256"]:
                    raise ValueError("QUALITY_OBJECT_MASK_CHANGED")
        return {"object_ids": objects, "frames": frames, "summary_sha256": tracking_hash,
                "request_sha256": sha256_file(request_path), "action": "KEEP", "interval": "half_open",
                "start_frame": start, "end_frame_exclusive": end, "provider": tracking["provider"],
                "source_rgb_sha256": person_asset["sha256"], "annotation_count": len(anchors),
                "visibility_evidence": "user-annotated target plus source-bound non-empty tracking mask; actual occlusion remains UNKNOWN"}

    def _analyze(self, person_asset, directory, on_event, cancel_event):
        directory = directory.resolve()
        storage = self.settings.storage_root.resolve()
        if not directory.is_relative_to(storage):
            raise ValueError("quality output must be inside Factory storage")
        reference = person_asset.get("legacy_reference") or {}
        path_value = reference.get("alpha_path")
        if not path_value:
            return self._unknown("QUALITY_INPUT_MISSING", retryable=False)
        alpha = Path(path_value).expanduser().resolve()
        if not alpha.is_file() or alpha.stat().st_size == 0:
            return self._unknown("QUALITY_INPUT_MISSING", retryable=False)
        if cancel_event and cancel_event.is_set():
            return self._unknown("QUALITY_CANCELLED")
        directory.mkdir(parents=True, exist_ok=True)
        input_hash = sha256_file(alpha)
        if reference.get("alpha_sha256", input_hash) != input_hash:
            return self._unknown("QUALITY_ALPHA_CHANGED", retryable=False)
        try:
            keep_evidence = self._keep_evidence(person_asset, storage)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            code = str(exc) if str(exc).startswith("QUALITY_") else "QUALITY_KEEP_EVIDENCE_INVALID"
            return self._unknown(code, retryable=False)
        evidence_hash = hashlib.sha256(json.dumps(keep_evidence, sort_keys=True).encode()).hexdigest() if keep_evidence else None
        detector_hash = sha256_file(Path(__file__).with_name("quality_worker.py"))
        fingerprint = hashlib.sha256(json.dumps({"input_sha256": input_hash, "thresholds": self.thresholds,
                                                 "version": QUALITY_VERSION, "detector_sha256": detector_hash,
                                                 "adapter_sha256": sha256_file(Path(__file__)),
                                                 "keep_evidence_sha256": evidence_hash}, sort_keys=True).encode()).hexdigest()
        request = {"alpha_path": str(alpha), "input_sha256": input_hash, "fingerprint": fingerprint,
                   "thresholds": self.thresholds, "version": QUALITY_VERSION, "task_id": person_asset.get("task_id"),
                   "asset_id": person_asset.get("id"), "detector_sha256": detector_hash,
                   "keep_evidence": keep_evidence, "keep_evidence_sha256": evidence_hash}
        output = directory / "quality-report.json"
        if output.exists():
            previous = json.loads(output.read_text())
            if previous.get("fingerprint") != fingerprint:
                return self._unknown("QUALITY_RUN_DIRECTORY_OCCUPIED", retryable=False)
            return {**previous, "report_path": str(output), "cache_hit": True, "_process": {}}
        cache = storage / "quality_cache" / (fingerprint + ".json")
        if cache.is_file():
            result = json.loads(cache.read_text())
            if result.get("fingerprint") == fingerprint and result.get("status") in {"PASS", "FAIL"}:
                output.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
                if on_event:
                    on_event({"type": "STAGE_PROGRESS", "stage": "quality", "text": "复用相同遮罩与配置的质检结果"})
                return {**result, "report_path": str(output), "cache_hit": True, "_process": {}}
        input_path = directory / "quality-input.json"
        if input_path.exists() and json.loads(input_path.read_text()).get("fingerprint") != fingerprint:
            return self._unknown("QUALITY_RUN_DIRECTORY_OCCUPIED", retryable=False)
        input_path.write_text(json.dumps(request), encoding="utf-8")
        try:
            execution = self.runner.run([str(self.settings.quality_python), "-B", str(Path(__file__).with_name("quality_worker.py")),
                                         "--input", str(input_path), "--result", str(output)],
                                        cwd=directory, log_dir=directory / "logs", timeout_s=1200,
                                        on_event=on_event, cancel_event=cancel_event)
        except OSError:
            return self._unknown("QUALITY_ENVIRONMENT_MISSING")
        process = {key: value for key, value in asdict(execution).items() if key not in {"stdout", "stderr"}}
        if execution.cancelled or execution.timed_out or not output.is_file():
            code = "QUALITY_CANCELLED" if execution.cancelled else "QUALITY_TIMEOUT" if execution.timed_out else "QUALITY_PROCESS_FAILED"
            return {**self._unknown(code), "_process": process}
        result = json.loads(output.read_text())
        if result.get("fingerprint") != fingerprint or result.get("input_sha256") != input_hash:
            return {**self._unknown("QUALITY_RESULT_MISMATCH", retryable=False), "_process": process}
        if result.get("status") in {"PASS", "FAIL"}:
            cache.parent.mkdir(parents=True, exist_ok=True)
            if not cache.exists():
                try:
                    with cache.open("x", encoding="utf-8") as stream:
                        json.dump(result, stream, ensure_ascii=False)
                except FileExistsError:
                    pass
        return {**result, "report_path": str(output), "cache_hit": False, "_process": process}
