"""Bounded, independent video-object propagation for Factory repair runs."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import resource
import subprocess
import sys
import time
from collections import OrderedDict
from pathlib import Path

import cv2
import numpy as np
import torch
from PIL import Image


FACTORY_ROOT = Path(__file__).resolve().parents[4]
SAM2_ROOT = FACTORY_ROOT / "storage/vendor/sam2"
SAM2_COMMIT = "2b90b9f5ceec907a1c18123530e92e794ad901a4"
SAM2_MODEL = FACTORY_ROOT / "storage/models/sam2.1_hiera_tiny.pt"
SAM2_SHA256 = "7402e0d864fa82708a20fbd15bc84245c2f26dff0eb43a4b5b93452deb34be69"


def sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def emit(stage: str, **value) -> None:
    print("FACTORY_EVENT " + json.dumps({"type": "STAGE_PROGRESS", "stage": "repair", "tracking_stage": stage,
                                         **value}, ensure_ascii=False), flush=True)


class FrameSequence:
    """SAM2's frame contract with a two-frame CPU cache instead of full-film RAM."""
    def __init__(self, paths: list[Path], image_size: int):
        self.paths = paths
        self.image_size = image_size
        self.cache: OrderedDict[int, torch.Tensor] = OrderedDict()
        self.mean = torch.tensor((0.485, 0.456, 0.406), dtype=torch.float32)[:, None, None]
        self.std = torch.tensor((0.229, 0.224, 0.225), dtype=torch.float32)[:, None, None]

    def __len__(self):
        return len(self.paths)

    def __getitem__(self, index):
        if index in self.cache:
            self.cache.move_to_end(index)
            return self.cache[index]
        with Image.open(self.paths[index]) as image:
            array = np.asarray(image.convert("RGB").resize((self.image_size, self.image_size)), dtype=np.float32) / 255
        tensor = (torch.from_numpy(array).permute(2, 0, 1) - self.mean) / self.std
        self.cache[index] = tensor
        if len(self.cache) > 2:
            self.cache.popitem(last=False)
        return tensor


def extract_interval(source: Path, start: int, end: int, directory: Path) -> tuple[list[Path], int, int]:
    directory.mkdir(parents=True, exist_ok=True)
    capture = cv2.VideoCapture(str(source))
    if not capture.isOpened():
        raise ValueError("TRACK_SOURCE_DECODE_FAILED")
    paths = []
    width = height = 0
    try:
        # Sequential decoding keeps frame indexes exact for FFV1 and VFR sources.
        for frame_index in range(end):
            ok, frame = capture.read()
            if not ok:
                raise ValueError(f"TRACK_INTERVAL_OUTSIDE_SOURCE:{frame_index}")
            if frame_index < start:
                continue
            height, width = frame.shape[:2]
            path = directory / f"{frame_index - start:06d}.jpg"
            if not cv2.imwrite(str(path), frame, [cv2.IMWRITE_JPEG_QUALITY, 95]):
                raise OSError("TRACK_ANALYSIS_FRAME_WRITE_FAILED")
            paths.append(path)
    finally:
        capture.release()
    return paths, width, height


def coordinates(keyframe: dict, width: int, height: int) -> tuple[np.ndarray | None, np.ndarray | None, np.ndarray | None]:
    space = keyframe.get("coordinate_space", "normalized")
    if space not in {"normalized", "source_pixels"}:
        raise ValueError("TRACK_COORDINATE_SPACE_INVALID")
    factors = np.asarray([width, height], dtype=np.float32) if space == "normalized" else np.ones(2, dtype=np.float32)
    points = keyframe.get("points", [])
    coords = np.asarray([[point["x"], point["y"]] for point in points], dtype=np.float32) * factors if points else None
    labels = np.asarray([point.get("label", 1) for point in points], dtype=np.int32) if points else None
    box = np.asarray(keyframe["box"], dtype=np.float32) * np.tile(factors, 2) if keyframe.get("box") is not None else None
    if labels is not None and not np.isin(labels, [0, 1]).all():
        raise ValueError("TRACK_POINT_LABEL_INVALID")
    if coords is not None and (not np.isfinite(coords).all() or (coords < 0).any() or (coords > [width, height]).any()):
        raise ValueError("TRACK_POINT_OUTSIDE_SOURCE")
    if box is not None and (box.shape != (4,) or not np.isfinite(box).all() or (box < 0).any() or (box > [width, height, width, height]).any() or (box[2:] <= box[:2]).any()):
        raise ValueError("TRACK_BOX_INVALID")
    return coords, labels, box


def track_windows(source, start, end, keyframes, object_ids, direction, directory, options):
    started = time.monotonic()
    frames, windows, overlaps = {}, [], []
    first = {object_id: min(item["frame_index"] for item in keyframes if item["object_id"] == object_id)
             for object_id in object_ids}
    if max(first.values()) - min(first.values()) >= options["max_window_frames"]:
        for object_id in object_ids:
            result = track_objects(source, start, end, [item for item in keyframes if item["object_id"] == object_id],
                                   [object_id], direction, directory / object_id, **options)
            windows.append(result)
            for frame in result["frames"]:
                frames.setdefault(frame["frame_index"], {"frame_index": frame["frame_index"], "objects": {}})["objects"].update(frame["objects"])
    else:
        anchor = min(first.values()) if direction != "backward" else end - 1
        plans = []
        if direction != "backward":
            position = anchor
            while position < end:
                stop = min(position + options["max_window_frames"], end)
                plans.append((position, stop, direction, position if position > anchor else None))
                if stop == end:
                    break
                position = stop - 1
        if direction != "forward" and anchor > start:
            stop = anchor + 1
            while stop > start:
                position = max(start, stop - options["max_window_frames"])
                plans.append((position, stop, "backward", stop - 1 if direction == "both" or stop <= anchor else None))
                if position == start:
                    break
                stop = position + 1
        for index, (position, stop, pass_direction, carry_frame) in enumerate(plans):
            prompts = [item for item in keyframes if position <= item["frame_index"] < stop]
            if carry_frame is not None:
                for object_id in object_ids:
                    if not any(item["frame_index"] == carry_frame and item["object_id"] == object_id for item in prompts):
                        prompts.append({"frame_index": carry_frame, "object_id": object_id,
                                        "mask_path": frames[carry_frame]["objects"][object_id]["mask_path"]})
            window_dir = directory / f"window_{index:03d}_{position:09d}_{stop:09d}"
            result = track_objects(source, position, stop, prompts, object_ids, pass_direction, window_dir, **options)
            windows.append(result)
            for frame in result["frames"]:
                absolute = frame["frame_index"]
                if absolute in frames:
                    comparison = {}
                    for object_id in object_ids:
                        previous, current = frames[absolute]["objects"][object_id], frame["objects"][object_id]
                        old = cv2.imread(previous["mask_path"], cv2.IMREAD_GRAYSCALE)
                        new = cv2.imread(current["mask_path"], cv2.IMREAD_GRAYSCALE)
                        union = np.count_nonzero((old > 127) | (new > 127))
                        comparison[object_id] = {"same_probability_hash": previous["sha256"] == current["sha256"],
                            "previous_sha256": previous["sha256"], "next_sha256": current["sha256"],
                            "mask_iou": float(np.count_nonzero((old > 127) & (new > 127)) / union) if union else 1.0,
                            "mean_probability_delta": float(np.abs(old.astype(np.float32) - new).mean() / 255)}
                    overlaps.append({"frame_index": absolute, "objects": comparison})
                frames[absolute] = frame
            if result["device"] == "mps":
                torch.mps.empty_cache()
    if set(frames) != set(range(start, end)) or any(set(frame["objects"]) != set(object_ids) for frame in frames.values()):
        raise ValueError("TRACK_WINDOW_FRAME_OR_OBJECT_MISSING")
    summaries = []
    for window in windows:
        summaries.extend(window["windows"] if "windows" in window else [{key: window[key] for key in
            ("start_frame", "end_frame", "object_ids", "direction", "elapsed_seconds", "propagation_seconds",
             "propagated_frame_evaluations", "peak_worker_rss_bytes", "peak_mps_allocated_bytes", "peak_mps_driver_bytes")}])
    result = {**windows[0], "start_frame": start, "end_frame": end, "frame_count": end - start,
              "object_ids": object_ids, "keyframes": keyframes, "direction": direction,
              "frames": [frames[index] for index in range(start, end)], "elapsed_seconds": time.monotonic() - started,
              "max_window_frames": options["max_window_frames"], "window_count": len(summaries),
              "window_state_reset": True, "window_overlap_frames": 1, "window_overlap_evidence": overlaps,
              "propagation_semantics": "bounded recurrent windows; prior same-frame masks seed overlap; model predicts every frame",
              "windows": summaries, "peak_worker_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
              "propagation_seconds": sum(window["propagation_seconds"] for window in windows),
              "propagated_frame_evaluations": sum(window["propagated_frame_evaluations"] for window in windows),
              "peak_mps_allocated_bytes": max((window["peak_mps_allocated_bytes"] or 0 for window in windows), default=0) or None,
              "peak_mps_driver_bytes": max((window["peak_mps_driver_bytes"] or 0 for window in windows), default=0) or None}
    write_json(directory / "manifest.json", result)
    emit("COMPLETED", frames=end - start, objects=len(object_ids), windows=len(windows), task_id=options["task_id"])
    return result


def track_objects(source_rgb, start_frame, end_frame, keyframes, object_ids, direction, output_dir,
                  *, device="auto", checkpoint=None, vendor_root=None, task_id=None, max_window_frames=96) -> dict:
    """Return PNG8 mask probabilities for each object and absolute frame in [start,end)."""
    started = time.monotonic()
    source = Path(source_rgb).resolve()
    directory = Path(output_dir).resolve()
    if not source.is_file() or start_frame < 0 or end_frame <= start_frame:
        raise ValueError("TRACK_INPUT_INVALID")
    if direction not in {"forward", "backward", "both"}:
        raise ValueError("TRACK_DIRECTION_INVALID")
    if max_window_frames < 2:
        raise ValueError("TRACK_WINDOW_TOO_SMALL")
    object_ids = list(dict.fromkeys(object_ids))
    if not object_ids or any(not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", item) for item in object_ids):
        raise ValueError("TRACK_OBJECT_ID_INVALID")
    if any(item.get("object_id") not in object_ids or not start_frame <= int(item["frame_index"]) < end_frame for item in keyframes):
        raise ValueError("TRACK_KEYFRAME_OUTSIDE_INTERVAL")
    per_object = {object_id: sorted(int(item["frame_index"]) for item in keyframes if item["object_id"] == object_id) for object_id in object_ids}
    if any(not items for items in per_object.values()):
        raise ValueError("TRACK_OBJECT_KEYFRAME_MISSING")
    if direction == "forward" and any(items[0] != start_frame for items in per_object.values()):
        raise ValueError("TRACK_FORWARD_REQUIRES_START_KEYFRAME")
    if direction == "backward" and any(items[-1] != end_frame - 1 for items in per_object.values()):
        raise ValueError("TRACK_BACKWARD_REQUIRES_END_KEYFRAME")
    checkpoint_path = Path(checkpoint or SAM2_MODEL).resolve()
    vendor = Path(vendor_root or SAM2_ROOT).resolve()
    if not checkpoint_path.is_file() or not (vendor / "sam2/build_sam.py").is_file():
        raise ValueError("TRACK_SAM2_MODEL_OR_CODE_MISSING")
    checkpoint_hash = sha256(checkpoint_path)
    if checkpoint is None and checkpoint_hash != SAM2_SHA256:
        raise ValueError("TRACK_MODEL_HASH_MISMATCH")
    commit = subprocess.run(["git", "-C", str(vendor), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    if vendor_root is None and commit != SAM2_COMMIT:
        raise ValueError("TRACK_CODE_HASH_MISMATCH")
    if directory.exists() and any(directory.iterdir()):
        raise ValueError("TRACK_OUTPUT_ALREADY_EXISTS")
    directory.mkdir(parents=True, exist_ok=True)
    source_hash = sha256(source)
    write_json(directory / "request.json", {"source_rgb": str(source), "source_sha256": source_hash,
               "start_frame": start_frame, "end_frame": end_frame, "interval": "half_open", "keyframes": keyframes,
               "object_ids": object_ids, "direction": direction, "operator": "Factory repair"})
    if end_frame - start_frame > max_window_frames:
        return track_windows(source, start_frame, end_frame, keyframes, object_ids, direction, directory,
                             {"device": device, "checkpoint": checkpoint, "vendor_root": vendor_root, "task_id": task_id,
                              "max_window_frames": max_window_frames})
    paths, width, height = extract_interval(source, start_frame, end_frame, directory / "analysis_frames")
    selected_device = "mps" if device == "auto" and torch.backends.mps.is_available() else "cpu" if device == "auto" else device
    if selected_device not in {"cpu", "mps", "cuda"}:
        raise ValueError("TRACK_DEVICE_INVALID")
    sys.path.insert(0, str(vendor))
    from sam2.build_sam import build_sam2_video_predictor
    emit("MODEL_LOADING", model="SAM2.1_hiera_tiny", device=selected_device, task_id=task_id)
    predictor = build_sam2_video_predictor("configs/sam2.1/sam2.1_hiera_t.yaml", str(checkpoint_path),
                                         device=selected_device, apply_postprocessing=False,
                                         hydra_overrides_extra=["++model.add_all_frames_to_correct_as_cond=true"])
    warmup = directory / "warmup"
    warmup.mkdir()
    os.link(paths[0], warmup / "000000.jpg")
    state = predictor.init_state(str(warmup), offload_video_to_cpu=True, offload_state_to_cpu=True)
    # Reuse official state initialization while avoiding allocation of N full float images.
    state["images"] = FrameSequence(paths, predictor.image_size)
    state["num_frames"] = len(paths)
    state["cached_features"] = {}
    peak_mps_allocated = torch.mps.current_allocated_memory() if selected_device == "mps" else None
    peak_mps_driver = torch.mps.driver_allocated_memory() if selected_device == "mps" else None
    object_mapping = {object_id: index + 1 for index, object_id in enumerate(object_ids)}
    for item in sorted(keyframes, key=lambda value: value["frame_index"]):
        relative = int(item["frame_index"]) - start_frame
        object_index = object_mapping[item["object_id"]]
        if item.get("mask_path"):
            mask_path = Path(item["mask_path"]).resolve()
            with Image.open(mask_path) as image:
                if image.size != (width, height):
                    raise ValueError("TRACK_KEYFRAME_MASK_CANVAS_MISMATCH")
                mask = np.asarray(image.convert("L")) > 127
            predictor.add_new_mask(state, relative, object_index, torch.from_numpy(mask))
        else:
            points, labels, box = coordinates(item, width, height)
            if points is None and box is None:
                raise ValueError("TRACK_KEYFRAME_PROMPT_MISSING")
            predictor.add_new_points_or_box(state, relative, object_index, points=points, labels=labels, box=box)
    selected = {frame_index: {"frame_index": frame_index, "objects": {}} for frame_index in range(start_frame, end_frame)}
    passes = ([False] if direction == "forward" else [True] if direction == "backward" else [False, True])
    first_anchor = min(min(items) for items in per_object.values()) - start_frame
    last_anchor = max(max(items) for items in per_object.values()) - start_frame
    propagation_started = time.monotonic()
    propagated_frames = 0
    for reverse in passes:
        anchor = last_anchor if reverse else first_anchor
        for relative, model_ids, masks in predictor.propagate_in_video(state, start_frame_idx=anchor, reverse=reverse):
            propagated_frames += 1
            absolute = start_frame + relative
            if selected_device == "mps":
                peak_mps_allocated = max(peak_mps_allocated, torch.mps.current_allocated_memory())
                peak_mps_driver = max(peak_mps_driver, torch.mps.driver_allocated_memory())
            for object_id, model_index in object_mapping.items():
                nearest = min(per_object[object_id], key=lambda item: (abs(item - absolute), item))
                preferred_reverse = absolute < nearest
                if direction == "both" and preferred_reverse != reverse:
                    continue
                output = directory / "objects" / object_id / f"{absolute:09d}.png"
                output.parent.mkdir(parents=True, exist_ok=True)
                index = list(model_ids).index(model_index)
                probability = masks[index, 0].sigmoid().detach().float().cpu().numpy()
                encoded = np.rint(np.clip(probability, 0, 1) * 255).astype(np.uint8)
                Image.fromarray(encoded).save(output)
                selected[absolute]["objects"][object_id] = {"mask_path": str(output), "sha256": sha256(output),
                                                          "area": int((encoded > 127).sum()), "keyframe": nearest,
                                                          "direction": "backward" if reverse else "forward"}
            if relative % 10 == 0 or relative == len(paths) - 1:
                emit("PROPAGATING", frame_index=absolute, total=len(paths), direction="backward" if reverse else "forward",
                     task_id=task_id)
    if any(set(frame["objects"]) != set(object_ids) for frame in selected.values()):
        raise ValueError("TRACK_FRAME_OR_OBJECT_MISSING")
    elapsed = time.monotonic() - started
    result = {"status": "READY", "provider": "SAM2.1", "model": "sam2.1_hiera_tiny", "license": "Apache-2.0",
              "model_source": "https://dl.fbaipublicfiles.com/segment_anything_2/092824/sam2.1_hiera_tiny.pt",
              "model_sha256": checkpoint_hash, "code_source": "https://github.com/facebookresearch/sam2",
              "code_commit": commit, "device": selected_device, "source_rgb": str(source), "source_sha256": source_hash,
              "start_frame": start_frame, "end_frame": end_frame, "interval": "half_open", "width": width,
              "height": height, "frame_count": len(paths), "object_ids": object_ids, "direction": direction,
              "mask_format": "PNG8 sigmoid probability; threshold=128", "mask_semantics": "object segmentation, not fine matting alpha",
              "analysis_input": "bounded original RGB interval decoded to JPEG95; source mother asset unchanged",
              "frames": list(selected.values()), "keyframes": keyframes, "elapsed_seconds": elapsed,
              "propagation_seconds": time.monotonic() - propagation_started, "propagated_frame_evaluations": propagated_frames,
              "task_id": task_id, "peak_worker_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
              "peak_mps_allocated_bytes": peak_mps_allocated, "peak_mps_driver_bytes": peak_mps_driver}
    write_json(directory / "manifest.json", result)
    emit("COMPLETED", frames=len(paths), objects=len(object_ids), elapsed_seconds=elapsed, task_id=task_id)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    parser.add_argument("--result", required=True)
    args = parser.parse_args()
    request = json.loads(Path(args.request).read_text(encoding="utf-8"))
    try:
        result = track_objects(**request)
    except Exception as error:
        result = {"status": "FAILED", "provider": "SAM2.1", "error_code": type(error).__name__, "error_message": str(error)}
        write_json(Path(args.result), result)
        raise
    write_json(Path(args.result), result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
