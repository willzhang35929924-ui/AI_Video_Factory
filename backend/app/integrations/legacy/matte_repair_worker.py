"""Apply segmentation corrections only inside an explicit interval; preserve source RGB."""
import argparse
import hashlib
import json
import subprocess
import time
from pathlib import Path

import cv2
import numpy as np

from comfy_asset_worker import alpha_statistics, decoded_frame_hashes, decode_verify, digest, foreground_previews, probe, write_json
from object_tracking_worker import SAM2_COMMIT, SAM2_SHA256, track_objects


def emit(message, progress):
    print("FACTORY_EVENT " + json.dumps({"message": message, "progress": progress}, ensure_ascii=False), flush=True)


def segmentation_alpha(probabilities):
    foreground = (probabilities >= 128).astype(np.uint8)
    distance = cv2.distanceTransform(foreground, cv2.DIST_L2, 5)
    return np.rint(np.minimum(distance / 2.0, 1.0) * 65535).astype(np.uint16)


def annotation_identity(keyframes, width, height):
    result = []
    for item in keyframes:
        space = item.get("coordinate_space", "normalized")
        if space not in {"normalized", "source_pixels"}:
            raise ValueError("TRACK_CACHE_COORDINATE_SPACE_INVALID")
        scale_x, scale_y = (width, height) if space == "source_pixels" else (1, 1)
        def point(value):
            return {"x": value["x"] / scale_x, "y": value["y"] / scale_y, "label": value["label"]}
        box = item.get("box")
        result.append({"frame_index": item["frame_index"], "object_id": item["object_id"],
            "points": [point(value) for value in item.get("points", [])],
            "box": [value / (scale_x if index % 2 == 0 else scale_y) for index, value in enumerate(box)] if box else None,
            "strokes": [{**stroke, "points": [point(value) for value in stroke["points"]]} for stroke in item.get("strokes", [])]})
    return result


def keyframe_masks(request, directory):
    width, height = request["source_width"], request["source_height"]
    keyframes = []
    for index, item in enumerate(request["keyframes"]):
        frame = {key: value for key, value in item.items() if key != "strokes"}
        if item.get("strokes"):
            mask = np.zeros((height, width), dtype=np.uint8)
            for stroke in item["strokes"]:
                points = [(min(width-1, round(p["x"] * width)), min(height-1, round(p["y"] * height))) for p in stroke["points"]]
                value = 255 if stroke["label"] else 0
                if points:
                    cv2.circle(mask, points[0], round(stroke["radius"]), value, -1)
                for first, last in zip(points, points[1:]):
                    cv2.line(mask, first, last, value, round(stroke["radius"] * 2))
            path = directory / f"keyframe_{index:03d}.png"
            if not cv2.imwrite(str(path), mask):
                raise ValueError("KEYFRAME_MASK_WRITE_FAILED")
            frame["mask_path"] = str(path)
        keyframes.append(frame)
    return keyframes


def cached_masks(cache, request, objects):
    path, original_request = Path(cache["path"]), Path(cache["request_path"])
    if digest(path) != cache["sha256"] or digest(original_request) != cache["request_sha256"]:
        raise ValueError("TRACK_CACHE_EVIDENCE_CHANGED")
    tracking = json.loads(path.read_text())
    previous = json.loads(original_request.read_text())
    annotations = [item for item in previous["keyframes"] if item["object_id"] in objects]
    selected = [item for item in request["keyframes"] if item["object_id"] in objects]
    if (tracking["status"] != "READY" or tracking.get("provider") != "SAM2.1" or tracking.get("source_sha256") != request["parent_rgb_sha256"]
        or previous.get("parent_rgb_sha256", request["parent_rgb_sha256"]) != request["parent_rgb_sha256"]
        or tracking.get("model_sha256") != SAM2_SHA256 or tracking.get("code_commit") != SAM2_COMMIT
        or tracking.get("tracking_worker_sha256") != digest(Path(__file__).with_name("object_tracking_worker.py"))
        or annotation_identity(annotations, request["source_width"], request["source_height"])
            != annotation_identity(selected, request["source_width"], request["source_height"])
        or any(previous[key] != request[key] or tracking[key] != request[key] for key in ("start_frame", "end_frame", "direction"))
        or tracking.get("interval") != "half_open" or tracking.get("frame_count") != request["end_frame"] - request["start_frame"]
        or not objects.issubset(tracking["object_ids"])
        or len(tracking["object_ids"]) != len(set(tracking["object_ids"]))
        or (tracking["width"], tracking["height"]) != (request["source_width"], request["source_height"])):
        raise ValueError("TRACK_CACHE_IDENTITY_MISMATCH")
    frames = [{**frame, "objects": {key: item for key, item in frame["objects"].items() if key in objects}} for frame in tracking["frames"]]
    expected = set(range(request["start_frame"], request["end_frame"]))
    if len(frames) != len(expected) or {frame["frame_index"] for frame in frames} != expected or any(set(frame["objects"]) != objects for frame in frames):
        raise ValueError("TRACK_CACHE_FRAME_OR_OBJECT_MISSING")
    for frame in frames:
        for evidence in frame["objects"].values():
            mask = Path(evidence["mask_path"])
            if digest(mask) != evidence["sha256"]:
                raise ValueError("TRACK_MASK_HASH_MISMATCH")
            pixels = cv2.imread(str(mask), cv2.IMREAD_UNCHANGED)
            if pixels is None or pixels.dtype != np.uint8 or pixels.shape != (request["source_height"], request["source_width"]):
                raise ValueError("TRACK_MASK_CANVAS_MISMATCH")
    source = {"path": str(path), "sha256": cache["sha256"], "request_path": str(original_request),
        "request_sha256": cache["request_sha256"], "object_ids": sorted(objects),
        "source_sha256": tracking["source_sha256"], "model_sha256": tracking["model_sha256"],
        "code_commit": tracking["code_commit"], "tracking_worker_sha256": tracking["tracking_worker_sha256"],
        "origin_task_id": tracking.get("task_id"), "origin_run_id": previous.get("run_id")}
    for kind in ("manifest", "request"):
        origin_path, origin_hash = tracking.get("origin_" + kind + "_path"), tracking.get("origin_" + kind + "_sha256")
        if bool(origin_path) != bool(origin_hash) or origin_path and digest(Path(origin_path)) != origin_hash:
            raise ValueError("TRACK_CACHE_EVIDENCE_CHANGED")
        if origin_path:
            source["origin_" + kind + "_path"], source["origin_" + kind + "_sha256"] = origin_path, origin_hash
    return tracking, frames, source


def masks_for(request, directory):
    cache = request.get("cached_tracking")
    if cache:
        objects = {item["object_id"] for item in request["keyframes"]}
        components = cache.get("sources") if "sources" in cache else [{**cache, "object_ids": sorted(objects)}]
        if not isinstance(components, list) or not components:
            raise ValueError("TRACK_CACHE_SOURCES_INVALID")
        assigned = set()
        for component in components:
            if not isinstance(component, dict):
                raise ValueError("TRACK_CACHE_SOURCES_INVALID")
            ids = component.get("object_ids", [])
            if not isinstance(ids, list) or not ids or any(not isinstance(value, str) or not value for value in ids):
                raise ValueError("TRACK_CACHE_SOURCES_INVALID")
            if len(ids) != len(set(ids)) or assigned.intersection(ids):
                raise ValueError("TRACK_CACHE_DUPLICATE_OBJECT")
            assigned.update(ids)
        if assigned != objects:
            raise ValueError("TRACK_CACHE_FRAME_OR_OBJECT_MISSING")
        merged = {index: {"frame_index": index, "objects": {}} for index in range(request["start_frame"], request["end_frame"])}
        sources, object_sources = [], {}
        for component in components:
            tracking, frames, source = cached_masks(component, request, set(component["object_ids"]))
            sources.append(source)
            for object_id in component["object_ids"]:
                object_sources[object_id] = source
            for frame in frames:
                for object_id, evidence in frame["objects"].items():
                    merged[frame["frame_index"]]["objects"][object_id] = {**evidence,
                        "tracking_source_path": source["path"], "tracking_source_sha256": source["sha256"]}
        emit("复用已验证的逐帧对象传播结果；不重复模型推理", 15)
        if "sources" not in cache:
            return {**tracking, "object_ids": sorted(objects), "frames": list(merged.values()),
                "cache_source": str(cache["path"]), "reused_inference": True, "derived_sources": sources, "object_sources": object_sources}
        fields = ("status", "provider", "model", "license", "model_source", "model_sha256", "code_source", "code_commit",
            "source_rgb", "source_sha256", "start_frame", "end_frame", "interval", "width", "height", "frame_count",
            "direction", "mask_format", "mask_semantics", "analysis_input", "tracking_worker_sha256")
        return {**{key: tracking[key] for key in fields if key in tracking}, "task_id": request["task_id"],
            "object_ids": sorted(objects), "frames": list(merged.values()), "reused_inference": True,
            "execution_kind": "DERIVED_CACHE_COMBINATION", "new_inference_requests": 0,
            "derived_sources": sources, "object_sources": object_sources}
    keyframes = keyframe_masks(request, directory)
    if request["direction"] != "keyframes_only":
        result = track_objects(request["rgb_path"], request["start_frame"], request["end_frame"], keyframes,
            sorted({frame["object_id"] for frame in keyframes}), request["direction"], directory / "tracking", task_id=request["task_id"])
        return {**result, "tracking_worker_sha256": digest(Path(__file__).with_name("object_tracking_worker.py"))}
    # An explicit brush-only edit does not pretend to be temporal propagation.
    if any(not item.get("mask_path") for item in keyframes):
        raise ValueError("BRUSH_REQUIRED_WITHOUT_PROPAGATION")
    frames = {}
    for item in keyframes:
        frame = frames.setdefault(item["frame_index"], {"frame_index": item["frame_index"], "objects": {}})
        frame["objects"][item["object_id"]] = {"mask_path": item["mask_path"], "sha256": digest(Path(item["mask_path"]))}
    return {"status": "READY", "provider": "ManualBrush", "mask_semantics": "keyframes only; no tracking inferred", "frames": list(frames.values())}


def repair(request, directory):
    rgb, original_alpha = Path(request["rgb_path"]), Path(request["alpha_path"])
    if digest(rgb) != request["parent_rgb_sha256"] or digest(original_alpha) != request["parent_alpha_sha256"]:
        raise ValueError("REPAIR_PARENT_HASH_MISMATCH")
    media = request["parent_manifest"]["media"]
    width, height, count = media["width"], media["height"], media["frame_count"]
    emit("生成独立物品传播遮罩；不修改母资产", 5)
    tracking = masks_for(request, directory)
    write_json(directory / "tracking_summary.json", tracking)
    masks = {frame["frame_index"]: frame["objects"] for frame in tracking["frames"]}
    alpha = directory / "Alpha.v2.mkv"
    encoder = subprocess.Popen(["ffmpeg", "-n", "-v", "error", "-f", "rawvideo", "-pix_fmt", "gray16le", "-s", f"{width}x{height}",
        "-framerate", media["fps_rational"], "-i", "-", "-an", "-c:v", "ffv1", "-level", "3", "-pix_fmt", "gray16le", str(alpha)],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, shell=False)
    decoder = subprocess.Popen(["ffmpeg", "-v", "error", "-xerror", "-i", str(original_alpha), "-an", "-fps_mode", "passthrough",
        "-f", "rawvideo", "-pix_fmt", "gray16le", "-"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False)
    frame_bytes = width * height * 2
    before, after = [], []
    changed = []
    try:
        for index in range(count):
            raw = decoder.stdout.read(frame_bytes)
            if len(raw) != frame_bytes:
                raise ValueError("ALPHA_FRAME_DECODE_INCOMPLETE")
            before.append(hashlib.sha256(raw).hexdigest())
            if index in masks:
                if not request["start_frame"] <= index < request["end_frame"]:
                    raise ValueError("TRACK_MASK_OUTSIDE_REPAIR_INTERVAL")
                combined = np.zeros((height, width), dtype=np.uint16)
                for evidence in masks[index].values():
                    path = Path(evidence["mask_path"])
                    if digest(path) != evidence["sha256"]:
                        raise ValueError("TRACK_MASK_HASH_MISMATCH")
                    probabilities = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
                    if probabilities is None or probabilities.shape != (height, width):
                        raise ValueError("TRACK_MASK_CANVAS_MISMATCH")
                    combined = np.maximum(combined, segmentation_alpha(probabilities))
                baseline = np.frombuffer(raw, dtype="<u2").reshape(height, width)
                corrected = np.maximum(baseline, combined) if request["mode"] == "retain" else np.minimum(baseline, 65535 - combined)
                raw = corrected.astype("<u2").tobytes()
            after.append(hashlib.sha256(raw).hexdigest())
            if before[-1] != after[-1]:
                changed.append(index)
            encoder.stdin.write(raw)
            if index % 50 == 0:
                emit(f"写入修复 Alpha：帧 {index}/{count}", 20 + 55 * index / count)
        if decoder.stdout.read(1):
            raise ValueError("ALPHA_FRAME_COUNT_EXCEEDED")
        encoder.stdin.close()
        if decoder.wait(timeout=30) or encoder.wait(timeout=120):
            raise ValueError("ALPHA_FFV1_ENCODING_FAILED")
    finally:
        for process in (decoder, encoder):
            if process.poll() is None:
                process.kill()
                process.wait()
    decode_verify(alpha)
    actual = decoded_frame_hashes(alpha, "gray16le")
    if actual != after:
        raise ValueError("REPAIR_ALPHA_LOSSLESS_READBACK_FAILED")
    outside = [i for i in range(count) if not request["start_frame"] <= i < request["end_frame"]]
    if any(before[i] != actual[i] for i in outside):
        raise ValueError("REPAIR_OUTSIDE_INTERVAL_CHANGED")
    if not changed:
        raise ValueError("REPAIR_NO_PIXELS_CHANGED")
    alpha_media = probe(alpha)
    if alpha_media["frame_count"] != count or alpha_media["width"] != width or alpha_media["height"] != height:
        raise ValueError("REPAIR_MEDIA_ALIGNMENT_FAILED")
    evidence = {"interval": "half_open", "start_frame": request["start_frame"], "end_frame": request["end_frame"],
        "outside_frame_count": len(outside), "outside_alpha_hashes_unchanged": True, "rgb_reused_unchanged": True,
        "changed_frames": changed, "alpha_before": before, "alpha_after": actual,
        "tracking_provider": tracking["provider"], "automatic_semantic_pass": False,
        "object_mask_to_alpha": {"input": "segmentation probability PNG8, not matting alpha", "threshold": 128,
            "interior_opacity": 1.0, "inward_feather_pixels": 2.0}}
    write_json(directory / "frame_evidence.json", evidence)
    parent_hashes = json.loads(Path(request["parent_reference"]["frame_hashes_path"]).read_text())
    write_json(directory / "frame_hashes.json", {**parent_hashes, "alpha": actual})
    emit("生成新 Alpha 的黑、白、亮色全长诊断预览", 80)
    previews = foreground_previews(directory, media, rgb, alpha)
    manifest = {**request["parent_manifest"], "schema": "factory.foreground.repair.v1", "parent_rgb_path": str(rgb),
        "alpha_path": str(alpha), "parent_alpha_path": str(original_alpha),
        "alpha_sha256": digest(alpha), "alpha_media": alpha_media, "processing_run_id": request["run_id"],
        "parent_alpha_sha256": request["parent_alpha_sha256"], "repair_id": request["id"], "repair": evidence,
        "created_at": time.time(), "previews": previews, "alpha_per_frame": alpha_statistics(alpha, media),
        "time_mapping_file": request["parent_reference"]["time_mapping_path"],
        "semantic_review": "PENDING", "full_decode_verified": True}
    write_json(directory / "manifest.json", manifest)
    preview = directory / previews[0]["filename"]
    reference = {**request["parent_reference"], "alpha_path": str(alpha), "alpha_sha256": manifest["alpha_sha256"],
        "manifest_path": str(directory / "manifest.json"), "parent_person_asset_id": request["person_asset_id"],
        "repair_id": request["id"], "repair_evidence_path": str(directory / "frame_evidence.json"),
        "frame_hashes_path": str(directory / "frame_hashes.json"),
        "preview_path": str(preview), "preview_sha256": previews[0]["sha256"],
        "parent_factory_fingerprint": request["parent_reference"].get("parent_factory_fingerprint", request["parent_reference"].get("factory_fingerprint")),
        "factory_fingerprint": hashlib.sha256((request["parent_rgb_sha256"] + manifest["alpha_sha256"]).encode()).hexdigest(),
        "object_strategy": {"provider": tracking["provider"], "object_ids": sorted({key["object_id"] for key in request["keyframes"]}),
            "mode": request["mode"], "segmentation_not_fine_matting": True}}
    emit("局部修复读回验证完成；等待人工检查", 100)
    return {"status": "PREVIEW_READY", "asset": {"path": str(rgb), "sha256": request["parent_rgb_sha256"],
        "source_system": "FactoryMatteRepair", "legacy_reference": reference}, "preview_path": str(preview),
        "preview_sha256": digest(preview), "frame_evidence_path": str(directory / "frame_evidence.json"), "changed_frame_count": len(changed),
        "tracking_summary_path": str(directory / "tracking_summary.json"), "tracking_summary_sha256": digest(directory / "tracking_summary.json")}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    parser.add_argument("--result", required=True)
    args = parser.parse_args()
    request_path = Path(args.request).resolve()
    try:
        result = repair(json.loads(request_path.read_text()), request_path.parent)
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        write_json(Path(args.result), {"status": "FAILED", "error_code": str(exc) if isinstance(exc, ValueError) else type(exc).__name__})
        raise
    write_json(Path(args.result), result)


if __name__ == "__main__":
    main()
