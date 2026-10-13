import asyncio
import hashlib
import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.integrations.process_runner import BASE_ENV_KEYS
from app.integrations.quality_adapter import CutoutQualityAdapter

QUALITY_PYTHON = Path("/Users/xiaoyao/ComfyUI/.venv/bin/python")
FIXTURE_SCRIPT = """
import cv2, numpy as np, sys
from pathlib import Path
directory = Path(sys.argv[1])
for mode in ('moving', 'holes', 'loss', 'flicker', 'fragments', 'color', 'shape', 'jump', 'object', 'opaque'):
    writer = cv2.VideoWriter(str(directory / (mode + '.avi')), cv2.VideoWriter_fourcc(*'MJPG'), 30, (96, 96))
    assert writer.isOpened()
    for index in range(60):
        frame = np.zeros((96,96,3), dtype=np.uint8)
        if mode == 'moving':
            cv2.rectangle(frame, (15+index//3,20), (39+index//3,75), (255,255,255), -1)
        elif mode == 'holes':
            cv2.rectangle(frame, (10,10), (85,85), (255,255,255), -1)
            cv2.rectangle(frame, (30,30), (65,65), (0,0,0), -1)
        elif mode == 'loss':
            if not 20 <= index < 24:
                cv2.rectangle(frame, (20,20), (60,80), (255,255,255), -1)
        elif mode == 'flicker':
            cv2.rectangle(frame, (20,20), (40 if index%2 else 70,80), (255,255,255), -1)
        elif mode == 'fragments':
            for center in ((20,20),(75,20),(20,75),(75,75)):
                cv2.circle(frame,center,10,(255,255,255),-1)
        elif mode == 'color':
            cv2.rectangle(frame,(20,20),(70,80),(0,0,255),-1)
        elif mode == 'shape':
            corners = ((8,36),(87,59)) if index%2 else ((36,8),(59,87))
            cv2.rectangle(frame,*corners,(255,255,255),-1)
        elif mode == 'jump':
            x = 15 if index < 30 else 55
            cv2.rectangle(frame,(x,20),(x+24,75),(255,255,255),-1)
        elif mode == 'object':
            cv2.rectangle(frame,(15,15),(40,80),(255,255,255),-1)
            if not 20 <= index < 24:
                cv2.rectangle(frame,(60,50),(75,75),(255,255,255),-1)
        elif mode == 'opaque':
            frame[:] = 255
        writer.write(frame)
    writer.release()
"""


@unittest.skipUnless(QUALITY_PYTHON.is_file(), "existing OpenCV environment unavailable")
class CutoutQualityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures = tempfile.TemporaryDirectory()
        subprocess.run([str(QUALITY_PYTHON), "-B", "-c", FIXTURE_SCRIPT, cls.fixtures.name], check=True,
                       env={key: os.environ[key] for key in BASE_ENV_KEYS if key in os.environ},
                       capture_output=True, timeout=30)

    @classmethod
    def tearDownClass(cls):
        cls.fixtures.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = SimpleNamespace(storage_root=Path(self.temp.name), quality_python=QUALITY_PYTHON)
        self.adapter = CutoutQualityAdapter(self.settings)

    def tearDown(self):
        self.temp.cleanup()

    def analyze(self, kind, directory="run-1"):
        asset = {"legacy_reference": {"alpha_path": str(Path(self.fixtures.name) / (kind + ".avi"))}}
        return asyncio.run(self.adapter.analyze(asset, Path(self.temp.name) / directory))

    def test_global_motion_is_aligned_and_stable_mask_passes(self):
        result = self.analyze("moving")
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual(result["metrics"]["frame_count"], 60)
        self.assertEqual(result["metrics"]["fps"], 30)
        self.assertGreater(result["metrics"]["temporal_stability"], .98)
        self.assertEqual(result["metrics"]["semantic_body_completeness"], "UNKNOWN")
        self.assertEqual(result["geometry_status"], "GEOMETRY_PASS")
        self.assertEqual(result["semantic_status"], "UNKNOWN")
        self.assertEqual(result["metrics"]["object_disappearance_status"], "UNKNOWN_NO_KEEP_ANNOTATION")
        self.assertEqual(result["metrics"]["product_presence"], "UNKNOWN")
        self.assertTrue(Path(result["report_path"]).is_file())
        self.assertGreater(result["_process"]["pid"], 0)

    def test_mask_loss_holes_and_flicker_have_timeline_segments(self):
        for kind, expected in (("loss", "MASK_LOSS"), ("holes", "HOLES"), ("flicker", "MASK_FLICKER"), ("fragments", "BODY_MISSING")):
            with self.subTest(kind=kind):
                result = self.analyze(kind, "run-" + kind)
                self.assertEqual(result["status"], "FAIL", result)
                self.assertIn(expected, [segment["issue"] for segment in result["issue_segments"]])
                self.assertLess(result["quality_score"], 100)
                for segment in result["issue_segments"]:
                    self.assertLess(segment["start"], segment["end"])
                    self.assertLessEqual(segment["end"], 2)
        loss = json.loads((Path(self.temp.name) / "run-loss/quality-report.json").read_text())
        segment = next(item for item in loss["issue_segments"] if item["issue"] == "MASK_LOSS")
        self.assertEqual(segment["start_frame"], 20)
        self.assertEqual(segment["end_frame"], 23)
        self.assertEqual(segment["end_frame_exclusive"], 24)
        self.assertEqual(segment["interval"], "half_open")

    def test_discontinuity_bbox_and_area_have_explicit_geometry_scope(self):
        for kind, issue in (("shape", "TEMPORAL_DISCONTINUITY"), ("jump", "BBOX_JUMP"), ("flicker", "ALPHA_AREA_ANOMALY"), ("opaque", "ALPHA_AREA_ANOMALY")):
            with self.subTest(kind=kind):
                result = self.analyze(kind, kind)
                self.assertEqual(result["status"], "FAIL", result)
                self.assertIn(issue, [item["issue"] for item in result["issue_segments"]])
                self.assertEqual(result["metrics"]["semantic_body_completeness"], "UNKNOWN")
                self.assertIn("PASS 不等于", result["limitations"])

    def object_asset(self):
        root = Path(self.temp.name)
        directory = root / "repair"
        directory.mkdir()
        mask = directory / "object.png"
        script = "import cv2,numpy as np,sys; a=np.zeros((96,96),np.uint8); a[50:76,60:76]=255; cv2.imwrite(sys.argv[1],a)"
        subprocess.run([str(QUALITY_PYTHON), "-c", script, str(mask)], check=True, capture_output=True)
        mask_hash = hashlib.sha256(mask.read_bytes()).hexdigest()
        rgb = directory / "isolated-rgb-fixture"
        rgb.write_bytes(b"isolated unit test source binding, not a production video")
        rgb_hash = hashlib.sha256(rgb.read_bytes()).hexdigest()
        evidence = {"frame_index": 0, "objects": {"bag_1": {"mask_path": str(mask), "sha256": mask_hash}}}
        anchors = [{"object_id": "bag_1", "frame_index": 0, "points": [{"x": .7, "y": .6, "label": 1}]}]
        tracking = {"status": "READY", "provider": "SAM2.1", "source_sha256": rgb_hash, "source_rgb": str(rgb), "interval": "half_open",
                    "start_frame": 0, "end_frame": 60, "object_ids": ["bag_1"],
                    "frames": [{**evidence, "frame_index": index} for index in range(60)], "keyframes": anchors}
        (directory / "tracking_summary.json").write_text(json.dumps(tracking))
        (directory / "request.json").write_text(json.dumps({"mode": "retain", "id": "repair-unit-fixture", "start_frame": 0,
            "end_frame": 60, "parent_rgb_sha256": rgb_hash, "rgb_path": str(rgb), "keyframes": anchors}))
        (directory / "frame_evidence.json").write_text("{}")
        asset = {"id": "asset-unit-fixture", "task_id": "unit-fixture-only", "sha256": rgb_hash, "storage_path": str(rgb),
                 "legacy_reference": {"alpha_path": str(Path(self.fixtures.name) / "object.avi"),
                    "repair_evidence_path": str(directory / "frame_evidence.json"), "repair_id": "repair-unit-fixture",
                    "object_strategy": {"mode": "retain", "object_ids": ["bag_1"]}}}
        return asset, directory

    def test_keep_object_uses_verified_masks_and_distinct_cache_identity(self):
        asset, directory = self.object_asset()
        result = asyncio.run(self.adapter.analyze(asset, Path(self.temp.name) / "objects-1"))
        self.assertEqual(result["status"], "FAIL", result)
        issue = next(item for item in result["issue_segments"] if item["issue"] == "OBJECT_DISAPPEARANCE")
        self.assertEqual((issue["start_frame"], issue["end_frame_exclusive"]), (20, 24))
        self.assertEqual(issue["object_id"], "bag_1")
        self.assertEqual(result["metrics"]["keep_objects"]["bag_1"]["checked_frames"], 60)
        self.assertEqual(result["task_id"], "unit-fixture-only")
        again = asyncio.run(self.adapter.analyze(asset, Path(self.temp.name) / "objects-2"))
        self.assertTrue(again["cache_hit"])
        plain = self.analyze("object", "without-object")
        self.assertNotEqual(result["fingerprint"], plain["fingerprint"])
        self.assertNotIn("OBJECT_DISAPPEARANCE", [item["issue"] for item in plain["issue_segments"]])
        (directory / "object.png").write_bytes(b"changed")
        changed = asyncio.run(self.adapter.analyze(asset, Path(self.temp.name) / "objects-3"))
        self.assertEqual(changed["status"], "UNKNOWN")
        self.assertEqual(changed["error_code"], "QUALITY_OBJECT_MASK_CHANGED")

    def test_remove_object_is_not_misclassified_as_keep_loss(self):
        asset, _directory = self.object_asset()
        asset["legacy_reference"]["object_strategy"]["mode"] = "remove"
        result = asyncio.run(self.adapter.analyze(asset, Path(self.temp.name) / "removed"))
        self.assertEqual(result["metrics"]["keep_objects"], {})
        self.assertNotIn("OBJECT_DISAPPEARANCE", [item["issue"] for item in result["issue_segments"]])

    def test_keep_annotation_and_original_rgb_binding_are_required(self):
        asset, directory = self.object_asset()
        request_path = directory / "request.json"
        request = json.loads(request_path.read_text())
        request["keyframes"] = []
        request_path.write_text(json.dumps(request))
        result = asyncio.run(self.adapter.analyze(asset, Path(self.temp.name) / "unannotated"))
        self.assertEqual(result["error_code"], "QUALITY_KEEP_ANNOTATION_MISSING")
        (directory / "isolated-rgb-fixture").write_bytes(b"changed original")
        result = asyncio.run(self.adapter.analyze(asset, Path(self.temp.name) / "source-changed"))
        self.assertEqual(result["error_code"], "QUALITY_KEEP_SOURCE_CHANGED")

    def test_equivalent_source_pixel_annotations_are_supported_but_wrong_target_is_rejected(self):
        asset, directory = self.object_asset()
        request_path, tracking_path = directory / "request.json", directory / "tracking_summary.json"
        request, tracking = json.loads(request_path.read_text()), json.loads(tracking_path.read_text())
        request.update(source_width=96, source_height=96)
        request_path.write_text(json.dumps(request))
        tracking["keyframes"] = [{"object_id": "bag_1", "frame_index": 0, "coordinate_space": "source_pixels",
                                  "points": [{"x": .7 * 96, "y": .6 * 96, "label": 1}]}]
        tracking_path.write_text(json.dumps(tracking))
        result = asyncio.run(self.adapter.analyze(asset, Path(self.temp.name) / "pixel-annotation"))
        self.assertEqual(result["status"], "FAIL", result)
        self.assertIsNone(result["error_code"])
        tracking["keyframes"][0]["points"][0]["x"] += 10
        tracking_path.write_text(json.dumps(tracking))
        wrong = asyncio.run(self.adapter.analyze(asset, Path(self.temp.name) / "wrong-annotation"))
        self.assertEqual(wrong["error_code"], "QUALITY_KEEP_ANNOTATION_MISMATCH")

    def test_missing_or_rgb_input_never_passes(self):
        missing = asyncio.run(self.adapter.analyze({}, Path(self.temp.name) / "missing"))
        self.assertEqual(missing["status"], "UNKNOWN")
        self.assertEqual(missing["error_code"], "QUALITY_INPUT_MISSING")
        colored = self.analyze("color")
        self.assertEqual(colored["status"], "UNKNOWN")
        self.assertEqual(colored["error_code"], "QUALITY_INPUT_NOT_ALPHA")
        self.assertEqual(list((Path(self.temp.name) / "quality_cache").glob("*")), [])

    def test_cache_requires_identical_input_and_thresholds_and_preserves_reports(self):
        first = self.analyze("moving", "first")
        first_bytes = Path(first["report_path"]).read_bytes()
        second = self.analyze("moving", "second")
        self.assertTrue(second["cache_hit"])
        self.assertEqual(second["_process"], {})
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.adapter = CutoutQualityAdapter(self.settings, thresholds={"min_foreground_area": .3})
        changed = self.analyze("moving", "third")
        self.assertFalse(changed["cache_hit"])
        self.assertEqual(changed["status"], "FAIL")
        self.assertNotEqual(changed["fingerprint"], first["fingerprint"])
        occupied = self.analyze("loss", "first")
        self.assertEqual(occupied["error_code"], "QUALITY_RUN_DIRECTORY_OCCUPIED")
        self.assertEqual(Path(first["report_path"]).read_bytes(), first_bytes)

    def test_cancellation_does_not_spawn_and_output_stays_contained(self):
        event = threading.Event()
        event.set()
        asset = {"legacy_reference": {"alpha_path": str(Path(self.fixtures.name) / "moving.avi")}}
        result = asyncio.run(self.adapter.analyze(asset, Path(self.temp.name) / "cancelled", cancel_event=event))
        self.assertEqual(result["error_code"], "QUALITY_CANCELLED")
        self.assertEqual(result["_process"], {})
        with self.assertRaises(ValueError):
            asyncio.run(self.adapter.analyze(asset, Path(self.fixtures.name) / "outside"))


if __name__ == "__main__":
    unittest.main()
