"""Local alpha geometry checks. This worker does not infer semantic body completeness."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

import cv2
import numpy as np

ISSUES = {"MASK_LOSS": "前景遮罩面积过小或消失", "BODY_MISSING": "遮罩几何异常，疑似局部缺失，需要结合原片复核",
          "MASK_FLICKER": "运动对齐后遮罩仍有突变", "HOLES": "前景遮罩内部孔洞比例过高",
          "TEMPORAL_DISCONTINUITY": "相邻帧运动对齐后形状仍不连续，需排除切镜或真实遮挡",
          "BBOX_JUMP": "前景边界框突然跳动，需排除真实运动或切镜",
          "ALPHA_AREA_ANOMALY": "前景面积突然增减，需结合原片复核",
          "OBJECT_DISAPPEARANCE": "KEEP 对象的已验证跟踪区域未被 Alpha 充分保留"}


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def frame_metrics(gray, previous, thresholds):
    mask = np.asarray(gray > thresholds["alpha_threshold"], dtype=np.uint8)
    foreground = int(np.count_nonzero(mask))
    area = foreground / mask.size
    _count, _labels, stats, _centers = cv2.connectedComponentsWithStats(mask, 8)
    largest = float(stats[1:, cv2.CC_STAT_AREA].max() / foreground) if foreground else 0.0
    moments = cv2.moments(mask, binaryImage=True)
    centroid = (moments["m10"] / foreground, moments["m01"] / foreground) if foreground else (0.0, 0.0)
    bbox = cv2.boundingRect(mask) if foreground else (0, 0, 0, 0)
    contours, hierarchy = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    holes = sum(cv2.contourArea(contour) for contour, relation in zip(contours, hierarchy[0]) if relation[3] >= 0) if hierarchy is not None else 0.0
    hole_ratio = holes / (foreground + holes) if foreground else 0.0
    perimeter = sum(cv2.arcLength(contour, True) for contour in contours)
    edge = perimeter / math.sqrt(foreground) if foreground else 0.0
    iou, area_change, edge_change, area_drop, bbox_jump = 1.0, 0.0, 0.0, 0.0, 0.0
    if previous is not None:
        area_change = abs(area - previous["area"]) / max(area, previous["area"], 1 / mask.size)
        area_drop = max(0.0, previous["area"] - area) / max(previous["area"], 1 / mask.size)
        edge_change = abs(edge - previous["edge"]) / max(edge, previous["edge"], 1.0)
        delta_x, delta_y = centroid[0] - previous["centroid"][0], centroid[1] - previous["centroid"][1]
        aligned = cv2.warpAffine(previous["mask"], np.float32([[1, 0, delta_x], [0, 1, delta_y]]),
                                 (mask.shape[1], mask.shape[0]), flags=cv2.INTER_NEAREST, borderValue=0)
        union = int(np.count_nonzero(mask | aligned))
        iou = float(np.count_nonzero(mask & aligned) / union) if union else 1.0
        if foreground and previous["area"]:
            corners = lambda box: np.array([box[0], box[1], box[0] + box[2], box[1] + box[3]])
            bbox_jump = float(np.max(np.abs(corners(bbox) - corners(previous["bbox"]))) / math.hypot(*mask.shape))
    values = {"area": area, "largest_component_ratio": largest, "hole_ratio": hole_ratio,
              "motion_aligned_iou": iou, "area_change": area_change, "edge_change": edge_change,
              "bbox_jump": bbox_jump, "bbox": list(bbox)}
    flags = set()
    if area > thresholds["max_foreground_area"]:
        flags.add("ALPHA_AREA_ANOMALY")
    if area < thresholds["min_foreground_area"]:
        flags.add("MASK_LOSS")
    else:
        if largest < thresholds["min_component_ratio"] or area_drop > 0.55:
            flags.add("BODY_MISSING")
        if hole_ratio > thresholds["max_hole_ratio"]:
            flags.add("HOLES")
        previous_present = previous is not None and previous["area"] >= thresholds["min_foreground_area"]
        if previous_present and iou < thresholds["min_motion_iou"] and (
                area_change > thresholds["max_area_change"] or edge_change > thresholds["max_edge_change"]):
            flags.add("MASK_FLICKER")
        if previous_present and iou < thresholds["min_continuity_iou"]:
            flags.add("TEMPORAL_DISCONTINUITY")
        if previous_present and bbox_jump > thresholds["max_bbox_jump"]:
            flags.add("BBOX_JUMP")
        if previous_present and area_change > thresholds["max_area_anomaly"]:
            flags.add("ALPHA_AREA_ANOMALY")
    state = {"mask": mask, "area": area, "edge": edge, "centroid": centroid, "bbox": bbox}
    return values, flags, state


def segments_for_flags(frame_flags, fps, thresholds, keep_objects=None):
    segments = []
    identifiers = list(ISSUES) + ["OBJECT_DISAPPEARANCE:" + object_id for object_id in (keep_objects or [])]
    for identifier in identifiers:
        issue = identifier.split(":", 1)[0]
        if identifier == "OBJECT_DISAPPEARANCE":
            continue
        start = None
        for index in range(len(frame_flags) + 1):
            active = index < len(frame_flags) and identifier in frame_flags[index]
            if active and start is None:
                start = index
            elif not active and start is not None:
                minimum = 1 if issue in {"MASK_LOSS", "MASK_FLICKER", "TEMPORAL_DISCONTINUITY", "BBOX_JUMP", "ALPHA_AREA_ANOMALY", "OBJECT_DISAPPEARANCE"} else max(1, math.ceil(thresholds["min_issue_duration_s"] * fps))
                if index - start >= minimum:
                    segment = {"start": round(start / fps, 4), "end": round(index / fps, 4),
                                     "issue": issue, "severity": "error" if issue == "MASK_LOSS" else "warning",
                                     "severity_level": "high" if issue in {"MASK_LOSS", "OBJECT_DISAPPEARANCE"} else "medium",
                                     "message": ISSUES[issue], "start_frame": start, "end_frame": index - 1,
                                     "end_frame_exclusive": index, "interval": "half_open",
                                     "legacy_end_frame_convention": "inclusive", "issue_type": issue.lower()}
                    if ":" in identifier:
                        segment.update(object_id=identifier.split(":", 1)[1], action="KEEP")
                    segments.append(segment)
                start = None
    return sorted(segments, key=lambda item: (item["start"], item["issue"]))


def summarize(values, flags, fps, thresholds, keep_objects=None):
    count = len(values)
    segments = segments_for_flags(flags, fps, thresholds, keep_objects)
    fractions = {issue: sum(any(item.split(":", 1)[0] == issue for item in row) for row in flags) / count for issue in ISSUES}
    penalty = fractions["MASK_LOSS"] * 100 + fractions["BODY_MISSING"] * 75 + fractions["MASK_FLICKER"] * 50 + fractions["HOLES"] * 40
    penalty += sum(fractions[key] * 40 for key in ("TEMPORAL_DISCONTINUITY", "BBOX_JUMP", "ALPHA_AREA_ANOMALY", "OBJECT_DISAPPEARANCE"))
    field = lambda key: np.array([row[key] for row in values])
    metrics = {"frame_count": count, "fps": fps, "duration": count / fps,
               "mask_area_mean": float(field("area").mean()), "mask_area_min": float(field("area").min()),
               "person_presence_ratio": 1 - fractions["MASK_LOSS"],
               "temporal_stability": float(field("motion_aligned_iou").mean()),
               "motion_aligned_iou_min": float(field("motion_aligned_iou").min()),
               "body_integrity_proxy_min": float(field("largest_component_ratio").min()),
               "edge_stability": 1 - float(field("edge_change").mean()),
               "hole_ratio_max": float(field("hole_ratio").max()),
               "bbox_jump_max": float(field("bbox_jump").max()),
               "area_change_p95": float(np.percentile(field("area_change"), 95)),
               "issue_frame_ratios": fractions, "semantic_body_completeness": "UNKNOWN", "product_presence": "UNKNOWN",
               "foreground_presence_ratio": 1 - fractions["MASK_LOSS"],
               "person_presence_ratio_semantics": "foreground geometry proxy, not a person detector",
               "keep_objects": keep_objects or {},
               "object_disappearance_status": "UNKNOWN_NO_KEEP_ANNOTATION" if not keep_objects else "PARTIAL_TRACKING_VISIBILITY_UNKNOWN" if any(row["empty_tracking_frames"] for row in keep_objects.values()) else "CHECKED_TRACKED_REGIONS_ONLY",
               "object_disappearance_scope": "Alpha coverage of verified KEEP tracking masks only; no inferred object visibility"}
    return {"status": "FAIL" if segments else "PASS", "quality_score": round(max(0.0, 100 - penalty), 2),
            "metrics": metrics, "issue_segments": segments, "error_code": None, "retryable": False,
            "geometry_status": "GEOMETRY_FAIL" if segments else "GEOMETRY_PASS", "semantic_status": "UNKNOWN",
            "scope": "alpha_temporal_geometry", "interval": "half_open",
            "quality_score_semantics": "uncalibrated geometry issue-frame risk score; not a semantic correctness probability",
            "limitations": "几何与时序启发式检查；PASS 不等于人体完整或无闪烁保证。真实运动、切镜、遮挡及多个保留物可触发代理异常；不能确认人体语义或产品身份；空跟踪 mask 不证明物体消失"}


def analyze_video(path: Path, thresholds: dict, on_progress=None, keep_evidence=None):
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        raise ValueError("QUALITY_DECODE_FAILED")
    fps = float(capture.get(cv2.CAP_PROP_FPS))
    expected = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    if not math.isfinite(fps) or fps <= 0:
        capture.release()
        raise ValueError("QUALITY_FPS_MISSING")
    values, flags, previous = [], [], None
    evidence_rows = (keep_evidence or {}).get("frames", [])
    object_frames = {row["frame_index"]: row["objects"] for row in evidence_rows}
    if keep_evidence and (len(object_frames) != len(evidence_rows) or keep_evidence.get("action") != "KEEP"
            or keep_evidence.get("interval") != "half_open" or any(not keep_evidence["start_frame"] <= index < keep_evidence["end_frame_exclusive"] for index in object_frames)):
        capture.release()
        raise ValueError("QUALITY_KEEP_INTERVAL_MISMATCH")
    keep_objects = {object_id: {"checked_frames": 0, "empty_tracking_frames": 0, "coverage_min": None}
                    for object_id in (keep_evidence or {}).get("object_ids", [])}
    original_size = None
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            height, width = frame.shape[:2]
            original_size = (width, height)
            scale = min(1.0, thresholds["max_dimension"] / max(width, height))
            if scale < 1:
                frame = cv2.resize(frame, (max(1, round(width * scale)), max(1, round(height * scale))), interpolation=cv2.INTER_AREA)
            if frame.ndim == 3:
                color_difference = np.mean(cv2.absdiff(frame[:, :, 0], frame[:, :, 2])) / 255
                if color_difference > 0.02:
                    raise ValueError("QUALITY_INPUT_NOT_ALPHA")
                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            measured, issues, previous = frame_metrics(frame, previous, thresholds)
            for object_id, evidence in object_frames.get(len(values), {}).items():
                object_path = Path(evidence["mask_path"])
                if sha256_file(object_path) != evidence["sha256"]:
                    raise ValueError("QUALITY_OBJECT_MASK_CHANGED")
                object_gray = cv2.imread(str(object_path), cv2.IMREAD_GRAYSCALE)
                if object_gray is None or object_gray.shape != (height, width):
                    raise ValueError("QUALITY_OBJECT_MASK_CANVAS_MISMATCH")
                object_mask = (object_gray >= 128).astype(np.uint8)
                if frame.shape != object_mask.shape:
                    object_mask = cv2.resize(object_mask, (frame.shape[1], frame.shape[0]), interpolation=cv2.INTER_NEAREST)
                area = int(np.count_nonzero(object_mask))
                evidence_metrics = keep_objects[object_id]
                if area < thresholds["min_object_pixels"]:
                    evidence_metrics["empty_tracking_frames"] += 1
                    continue
                coverage = float(np.count_nonzero(object_mask & previous["mask"]) / area)
                evidence_metrics["checked_frames"] += 1
                evidence_metrics["coverage_min"] = min(coverage, evidence_metrics["coverage_min"]) if evidence_metrics["coverage_min"] is not None else coverage
                if coverage < thresholds["min_keep_object_coverage"]:
                    issues.add("OBJECT_DISAPPEARANCE:" + object_id)
            values.append(measured)
            flags.append(issues)
            if on_progress and (len(values) == 1 or len(values) % 60 == 0):
                on_progress(len(values), expected)
    finally:
        capture.release()
    if not values:
        raise ValueError("QUALITY_EMPTY_VIDEO")
    if expected > 0 and abs(len(values) - expected) > 1:
        raise ValueError("QUALITY_DECODE_INCOMPLETE")
    if object_frames and max(object_frames) >= len(values):
        raise ValueError("QUALITY_OBJECT_MASK_OUTSIDE_ALPHA")
    result = summarize(values, flags, fps, thresholds, keep_objects)
    result["metrics"].update(width=original_size[0], height=original_size[1], reported_frame_count=expected,
                             analysis_width=previous["mask"].shape[1], analysis_height=previous["mask"].shape[0],
                             analysis_alpha_precision="OpenCV geometry decode to uint8; original lossless mother unchanged")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    request = json.loads(args.input.read_text())
    try:
        def progress(frame, total):
            print("FACTORY_EVENT " + json.dumps({"type": "STAGE_PROGRESS", "stage": "quality", "frame": frame,
                                                "frame_count": total, "text": "正在分析遮罩时序与边缘"}), flush=True)
        alpha = Path(request["alpha_path"])
        if sha256_file(alpha) != request["input_sha256"]:
            raise ValueError("QUALITY_ALPHA_CHANGED")
        result = analyze_video(alpha, request["thresholds"], progress, request.get("keep_evidence"))
        if sha256_file(alpha) != request["input_sha256"]:
            raise ValueError("QUALITY_ALPHA_CHANGED")
    except (OSError, ValueError, cv2.error) as exc:
        known = str(exc) if isinstance(exc, ValueError) and str(exc).startswith("QUALITY_") else "QUALITY_ANALYSIS_FAILED"
        result = {"status": "UNKNOWN", "quality_score": None, "metrics": {}, "issue_segments": [],
                  "error_code": known, "retryable": True}
    result.update(provider="OpenCVCutoutQualityAnalyzer", version=request["version"], fingerprint=request["fingerprint"],
                  input_sha256=request["input_sha256"], thresholds=request["thresholds"], mode="REAL",
                  task_id=request.get("task_id"), asset_id=request.get("asset_id"),
                  detector_sha256=request.get("detector_sha256"),
                  keep_evidence_sha256=request.get("keep_evidence_sha256"))
    args.result.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return 0 if result["status"] in {"PASS", "FAIL"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
