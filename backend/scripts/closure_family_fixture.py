"""Disposable, local-media family for the 3030/8030 deletion acceptance only."""
import argparse
import hashlib
import json
import re
import subprocess
from copy import deepcopy
from pathlib import Path

from sqlalchemy.orm import Session

from app import models as m
from app.domain import now
from app.integrations.asset_registry import file_hash, probe_media
from app.repository import StateRepository

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "backend/delivery-20261009.db"
STORAGE = ROOT / "storage/acceptance-delivery-20261009/closure-family-20261009"
TASK = "tsk-test-closure-family-20261009"
RUN = "run-test-closure-family-20261009"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suffix", default="", help="Independent disposable TEST fixture; never re-create a deleted identity")
    parser.add_argument("--bound-manifest", action="store_true", help="Include fingerprint-owned render directories and part outputs")
    args = parser.parse_args()
    assert not args.suffix or re.fullmatch(r"[a-z0-9-]{1,40}", args.suffix), "Unsafe TEST suffix"
    assert not args.bound_manifest or args.suffix, "Complete-directory acceptance requires a new independent TEST identity"
    suffix = "-" + args.suffix if args.suffix else ""
    task_id, run_id = TASK + suffix, RUN + suffix
    storage = STORAGE.with_name(STORAGE.name + suffix)
    identifier = lambda name: name + suffix
    assert DB.is_file(), "Reuse the existing isolated database, not formal factory.db"
    repository = StateRepository("sqlite:///" + str(DB), ROOT / "backend/app/seed.json")
    repository.app_mode = "integration"
    state = repository.state(scope="history")
    existing = next((row for row in state["tasks"] if row["id"] == task_id), None)
    assert existing is None or existing["keyword"] == "TEST_Cascade_Disposable_20261009" + suffix, "Unexpected task identity"
    assert existing is None or not existing["context"].get("production", {}).get("asset_family_deleted"), "Deleted fixture must not be regenerated"
    storage.mkdir(parents=True, exist_ok=True)
    def generate(command, path):
        if not path.exists():
            subprocess.run(command, check=True)
    person_fingerprint = hashlib.sha256((task_id + ":person:UNIT_ONLY").encode()).hexdigest()
    compose_fingerprint = hashlib.sha256((task_id + ":compose:UNIT_ONLY").encode()).hexdigest()
    person_directory = storage / "tasks" / task_id / "cutout" / "foreground" / person_fingerprint if args.bound_manifest else storage
    compose_directory = storage / "tasks" / task_id / "compose" / "compositions" / compose_fingerprint if args.bound_manifest else storage
    person_directory.mkdir(parents=True, exist_ok=True)
    compose_directory.mkdir(parents=True, exist_ok=True)
    source = person_directory / "RGB.mp4"
    generate(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x288:rate=10", "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)], source)
    alpha = person_directory / "Alpha.mkv"
    generate(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=white:size=160x288:rate=10", "-t", "1", "-c:v", "ffv1", "-pix_fmt", "gray16le", str(alpha)], alpha)
    background = storage / "Background.png"
    generate(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=blue:size=160x288", "-frames:v", "1", str(background)], background)
    composed = compose_directory / "Composite.mp4"
    generate(["ffmpeg", "-v", "error", "-i", str(source), "-vf", "eq=contrast=0.9", "-c:v", "libx264", str(composed)], composed)
    control = storage / "Unrelated.png"
    generate(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=green:size=160x288", "-frames:v", "1", str(control)], control)
    manifest = person_directory / "manifest.json"
    manifest_data = {"execution_scope": "TEST_DISPOSABLE", "parent_person_asset_id": identifier("ast-test-family-v1"), "previews": []}
    compose_manifest = compose_directory / "manifest.json" if args.bound_manifest else None
    shared_part = None
    if args.bound_manifest:
        for path in (source, alpha, composed):
            subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(path), "-map", "0:v:0", "-an", "-f", "null", "-"], check=True)
        manifest_data.update(schema="factory.foreground.v1", status="READY", task_id=task_id,
            processing_run_id=run_id, input_fingerprint=person_fingerprint, rgb_sha256=file_hash(source),
            alpha_sha256=file_hash(alpha), rgb_media=probe_media(source), alpha_media=probe_media(alpha),
            full_decode_verified=True, test_only=True)
        compose_manifest.write_text(json.dumps({"schema": "factory.composition.v1", "status": "READY", "task_id": task_id,
            "processing_run_id": run_id, "input_fingerprint": compose_fingerprint, "output_sha256": file_hash(composed),
            "person_asset_id": identifier("ast-test-family-v2"), "background_asset_id": identifier("ast-test-family-bg"),
            "source_media": probe_media(source), "output_media": probe_media(composed), "test_only": True}), encoding="utf-8")
        for directory in (person_directory, compose_directory):
            output_directory = directory / "comfy"
            output_directory.mkdir(exist_ok=True)
            (output_directory / "part_000000.api.json").write_text('{"test_only":true,"purpose":"family-file-coverage"}', encoding="utf-8")
            (output_directory / "part_000000.history.json").write_text('{"test_only":true,"external_execution":false}', encoding="utf-8")
            (directory / "render-manifest.json").write_text('{"test_only":true,"file_coverage":true}', encoding="utf-8")
            (directory / "worker-output.json").write_text('{"test_only":true,"local_ffmpeg_exit_code":0}', encoding="utf-8")
            cache = directory / "cache"
            cache.mkdir(exist_ok=True)
            (cache / "preview.json").write_text('{"test_only":true,"cache_coverage":true}', encoding="utf-8")
        shared_part = compose_directory / "comfy" / "part_000000.mp4"
        generate(["ffmpeg", "-v", "error", "-i", str(source), "-t", "0.5", "-c:v", "libx264", str(shared_part)], shared_part)
        (compose_directory / "Composite.concat.txt").write_text("file 'comfy/part_000000.mp4'\n", encoding="utf-8")
        (person_directory / "RGB.concat.txt").write_text("file 'RGB.mp4'\n", encoding="utf-8")
    manifest.write_text(json.dumps(manifest_data), encoding="utf-8")
    run_dir = storage / run_id
    run_dir.mkdir(exist_ok=True)
    stdout, stderr = run_dir / "stdout.log", run_dir / "stderr.log"
    stdout.write_text("Disposable deletion acceptance: completed local ffmpeg media\n", encoding="utf-8")
    stderr.write_text("", encoding="utf-8")
    (run_dir / "cache.json").write_text('{"test_only":true}', encoding="utf-8")
    preview_dir = storage.parent / "diagnostic_previews" / ("test-family-preview" + suffix)
    preview_dir.mkdir(parents=True, exist_ok=True)
    (preview_dir / "preview.json").write_text(json.dumps({"inputs": {"asset_id": identifier("ast-test-family-v1")}}), encoding="utf-8")
    generate(["ffmpeg", "-v", "error", "-i", str(source), "-vf", "scale=80:144", "-c:v", "libx264", str(preview_dir / "Preview.mp4")], preview_dir / "Preview.mp4")
    task = deepcopy(next(row for row in state["tasks"] if row["execution_mode"] == "integration"))
    task.update(id=task_id, keyword="TEST_Cascade_Disposable_20261009" + suffix, status="FAILED", source_video_id=None,
        current_stage="review", context={"task_context": {"context_id": "ctx-test-family" + suffix}, "production": {"current_person_asset_id": identifier("ast-test-family-v2")}}, collector_result={}, created_at=now(), updated_at=now())
    template = deepcopy(state["assets"][0]) if state["assets"] else {"thumbnail": "", "duration": 0, "width": 0, "height": 0, "fps": 0, "codec": "", "country": task["country"], "keyword": task["keyword"]}
    assets = []

    def asset(identifier, kind, path, reference):
        media = probe_media(path)
        row = {**template, "id": identifier, "task_id": task_id, "type": kind, "name": "TEST " + identifier,
            "status": "READY", "thumbnail": "", "storage_path": str(path), "external_path": str(path),
            "duration": media["duration"], "width": media["width"], "height": media["height"], "fps": media["fps"], "codec": media["codec"],
            "sha256": file_hash(path), "size": path.stat().st_size, "registry_status": "registered",
            "source_system": "TEST_DISPOSABLE_LOCAL_FFMPEG", "created_at": now(), "legacy_reference": reference, "probe_metadata": media}
        assets.append(row)
        return row

    pair = {"alpha_path": str(alpha), "manifest_path": str(manifest), "processing_run_id": run_id,
        **({"input_fingerprint": person_fingerprint, "alpha_sha256": file_hash(alpha)} if args.bound_manifest else {})}
    asset(identifier("ast-test-family-v1"), "person", source, {**pair, "version": 1})
    asset(identifier("ast-test-family-v2"), "person", source, {**pair, "version": 2, "parent_person_asset_id": identifier("ast-test-family-v1")})
    asset(identifier("ast-test-family-bg"), "background", background, {"processing_run_id": run_id})
    composition_reference = {"person_asset_id": identifier("ast-test-family-v2"), "background_asset_id": identifier("ast-test-family-bg"), "processing_run_id": run_id,
        **({"manifest_path": str(compose_manifest), "input_fingerprint": compose_fingerprint} if args.bound_manifest else {})}
    asset(identifier("ast-test-family-compose"), "compose", composed, composition_reference)
    asset(identifier("ast-test-family-shared"), "compose", composed, {"processing_run_id": run_id})
    if shared_part:
        asset(identifier("ast-test-family-part-shared"), "compose", shared_part, {"processing_run_id": run_id})
    asset(identifier("ast-test-family-control"), "background", control, {})
    run = {"id": run_id, "task_id": task_id, "stage": "compose", "status": "success", "progress": 100, "logs": [], "created_at": now(),
        "provider": "TEST_LOCAL_FFMPEG", "exit_code": 0, "stdout_log_path": str(stdout), "stderr_log_path": str(stderr)}
    if existing is None:
        repository.mutate(lambda value: (value["tasks"].append(task), value["assets"].extend(assets), value["runs"].append(run),
            value["reviews"].append({"id": identifier("review-test-family"), "task_id": task_id, "asset_id": identifier("ast-test-family-compose"), "status": "approved", "issues": [], "note": "TEST_DISPOSABLE"})))
    for row in assets:
        repository.register_real_asset_links(row, task)
    with Session(repository.engine) as session:
        if session.get(m.WorkflowSnapshot, identifier("wf-test-family")) is None:
            session.add(m.WorkflowSnapshot(id=identifier("wf-test-family"), task_id=task_id, version="TEST", workflow={}, processing_run_id=run_id))
            session.flush()
        if session.get(m.CutoutAsset, identifier("cutout-test-family")) is None:
            session.add(m.CutoutAsset(id=identifier("cutout-test-family"), task_id=task_id, asset_id=identifier("ast-test-family-v1"), workflow_id=identifier("wf-test-family"), model="TEST_LOCAL", model_version="TEST", alpha_quality=0, temporal_stability=0, missing_body_score=0, flicker_score=0, intervals=[]))
        session.commit()
    evidence = {"task_id": task_id, "root_asset_id": identifier("ast-test-family-v1"), "asset_ids": [row["id"] for row in assets], "control_id": identifier("ast-test-family-control"),
        "test_only": True, "bound_manifest": args.bound_manifest, "suffix": args.suffix,
        "database": str(DB), "files": [{"path": str(path), "sha256": file_hash(path), "size": path.stat().st_size} for directory in [storage, preview_dir] for path in directory.rglob("*") if path.is_file()]}
    evidence_path = ROOT / ("docs/acceptance/closure-family-before" + suffix + ".json")
    evidence_path.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    print(json.dumps(evidence, indent=2))
    repository.engine.dispose()


if __name__ == "__main__":
    main()
