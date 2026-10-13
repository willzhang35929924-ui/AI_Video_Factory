import ast
import unittest
from pathlib import Path


class TrackingAnnotationIdentity(unittest.TestCase):
    def setUp(self):
        worker = Path(__file__).resolve().parents[1] / "app/integrations/legacy/matte_repair_worker.py"
        function = next(node for node in ast.parse(worker.read_text()).body if isinstance(node, ast.FunctionDef) and node.name == "annotation_identity")
        namespace = {}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(worker), "exec"), namespace)
        self.identity = namespace["annotation_identity"]

    def test_original_pixel_annotations_match_restored_ui_coordinates(self):
        original = {"frame_index": 149, "object_id": "bag_1", "coordinate_space": "source_pixels",
                    "points": [{"x": 650, "y": 995, "label": 1}], "box": [550, 865, 720, 1105]}
        restored = {"frame_index": 149, "object_id": "bag_1", "coordinate_space": "normalized",
                    "points": [{"x": 650/720, "y": 995/1280, "label": 1}],
                    "box": [550/720, 865/1280, 1., 1105/1280], "strokes": []}
        self.assertEqual(self.identity([original], 720, 1280), self.identity([restored], 720, 1280))
        restored["points"][0]["x"] += .001
        self.assertNotEqual(self.identity([original], 720, 1280), self.identity([restored], 720, 1280))

    def test_defaults_and_brush_radius_remain_part_of_actual_identity(self):
        original = {"frame_index": 12, "object_id": "product_1", "coordinate_space": "source_pixels",
                    "strokes": [{"radius": 20, "label": 0, "points": [{"x": 10, "y": 40, "label": 0}]}]}
        restored = {"frame_index": 12, "object_id": "product_1", "coordinate_space": "normalized", "points": [], "box": None,
                    "strokes": [{"radius": 20, "label": 0, "points": [{"x": .1, "y": .2, "label": 0}]}]}
        self.assertEqual(self.identity([original], 100, 200), self.identity([restored], 100, 200))
        restored["strokes"][0]["radius"] = 21
        self.assertNotEqual(self.identity([original], 100, 200), self.identity([restored], 100, 200))

    def test_unknown_coordinate_space_does_not_reuse_masks(self):
        with self.assertRaisesRegex(ValueError, "COORDINATE_SPACE_INVALID"):
            self.identity([{"coordinate_space": "display_pixels"}], 720, 1280)


if __name__ == "__main__":
    unittest.main()
