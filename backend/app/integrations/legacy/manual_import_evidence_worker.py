"""Derive the same frame/timeline evidence consumed by local matte repair."""
import argparse
import bisect
from pathlib import Path

from comfy_asset_worker import decoded_frame_hashes, digest, frame_timestamps, probe, write_json


def derive(directory):
    rgb, alpha = directory / "RGB.mkv", directory / "Alpha.mkv"
    original = next(directory.glob("Original.*"))
    original_media, media = probe(original), probe(rgb)
    source_pts, rgb_pts, alpha_pts = frame_timestamps(original), frame_timestamps(rgb), frame_timestamps(alpha)
    if (not source_pts or len(source_pts) != original_media["frame_count"]
            or rgb_pts != alpha_pts or len(rgb_pts) != media["frame_count"]
            or any(abs(at - index / media["fps"]) > .0015 for index, at in enumerate(rgb_pts))):
        raise ValueError("MANUAL_IMPORT_PRESENTATION_TIMESTAMPS_MISMATCH")
    if any(after < before for before, after in zip(source_pts, source_pts[1:])):
        raise ValueError("MANUAL_IMPORT_SOURCE_TIMESTAMPS_INVALID")
    elapsed = [at - source_pts[0] for at in source_pts]
    indices = []
    for at in rgb_pts:
        position = bisect.bisect_left(elapsed, at)
        choices = [index for index in (position - 1, position) if 0 <= index < len(elapsed)]
        indices.append(min(choices, key=lambda index: abs(elapsed[index] - at)))
    hashes = {"algorithm": "sha256", "rgb_pixel_format": "rgb48le", "alpha_pixel_format": "gray16le",
              "rgb": decoded_frame_hashes(rgb, "rgb48le"), "alpha": decoded_frame_hashes(alpha, "gray16le")}
    if len(hashes["rgb"]) != media["frame_count"] or len(hashes["alpha"]) != media["frame_count"]:
        raise ValueError("MANUAL_IMPORT_DECODED_HASH_COUNT_MISMATCH")
    mapping = {"source_sha256": digest(original), "original": original_media, "normalized": media,
               "normalization_applied": elapsed != rgb_pts, "reason": "manual import; actual decoded presentation timestamps",
               "original_frame_timestamps_s": source_pts, "normalized_to_source_frame": indices,
               "normalized_frame_timestamps_s": rgb_pts, "alpha_frame_timestamps_s": alpha_pts}
    write_json(directory / "frame_hashes.json", hashes)
    write_json(directory / "time_mapping.json", mapping)
    write_json(directory / "import_evidence.json", {"media": media, "presentation_timestamps_verified": True})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", required=True)
    derive(Path(parser.parse_args().directory).resolve())
