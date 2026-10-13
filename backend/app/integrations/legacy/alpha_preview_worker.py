"""Read-only browser diagnostic derived from the registered lossless Alpha."""
import argparse
import json
from pathlib import Path

from comfy_asset_worker import command, decode_verify, digest, probe


def render(request, result_path):
    inputs = request["inputs"]
    storage = Path(request["storage_root"]).resolve()
    read_roots = (storage, *(Path(value).resolve() for value in request.get("readonly_asset_roots", [])))
    alpha, manifest = Path(inputs["alpha_path"]).resolve(), Path(inputs["manifest_path"]).resolve()
    output = Path(request["output_path"]).resolve()
    result_path = Path(result_path).resolve()
    if (not all(any(path.is_relative_to(root) for root in read_roots) for path in (alpha, manifest))
            or not all(path.is_relative_to(storage) for path in (output, result_path))
            or not output.is_relative_to(result_path.parent / "runs") or output.parent == alpha.parent):
        raise ValueError("ALPHA_PREVIEW_PATH_OUTSIDE_FACTORY")
    if (digest(alpha) != inputs["alpha_sha256"] or digest(manifest) != inputs["manifest_sha256"]
            or digest(Path(__file__).resolve()) != inputs["worker_sha256"]
            or digest(Path(__file__).with_name("comfy_asset_worker.py")) != inputs["helper_sha256"]):
        raise ValueError("ALPHA_PREVIEW_INPUT_CHANGED")
    media = probe(alpha)
    expected = inputs["media"]
    if (media["codec"] != "ffv1" or media["pix_fmt"] != "gray16le"
            or any(media[key] != expected[key] for key in ("width", "height", "frame_count"))
            or abs(media["fps"] - expected["fps"]) > .001):
        raise ValueError("ALPHA_PREVIEW_INPUT_ALIGNMENT_MISMATCH")
    fps = media["fps_rational"]
    command(["ffmpeg", "-n", "-v", "error", "-xerror", "-i", str(alpha), "-map", "0:v:0", "-an",
             "-vf", f"settb=expr=1/({fps}),setpts=N,format=gray", "-frames:v", str(media["frame_count"]),
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-threads", "2", "-pix_fmt", "yuv420p",
             "-movflags", "+faststart", str(output)])
    checked = probe(output)
    decode_verify(output)
    if (checked["codec"] != "h264" or any(checked[key] != media[key] for key in ("width", "height", "frame_count"))
            or abs(checked["fps"] - media["fps"]) > .001 or checked["audio"]):
        raise ValueError("ALPHA_PREVIEW_OUTPUT_ALIGNMENT_MISMATCH")
    if digest(alpha) != inputs["alpha_sha256"] or digest(manifest) != inputs["manifest_sha256"]:
        raise ValueError("ALPHA_PREVIEW_INPUT_CHANGED")
    result = {"schema": "factory.alpha-preview.v1", "status": "READY", "inputs": inputs,
              "path": str(output), "sha256": digest(output), "media": checked,
              "full_decode_verified": True, "diagnostic_only": True, "cutout_inference_executions": 0}
    with result_path.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    arguments = parser.parse_args()
    render(json.loads(arguments.request.read_text()), arguments.result)


if __name__ == "__main__":
    main()
