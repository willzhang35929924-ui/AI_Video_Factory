import copy
import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

COMFY = Path('/Users/xiaoyao/ComfyUI')
FACTORY = Path(__file__).resolve().parents[2]


@unittest.skipUnless(importlib.util.find_spec('torch') and COMFY.is_dir(), 'Run SAM3 node tests with the existing ComfyUI Python environment')
class SAM3HFNodeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(COMFY))
        source = FACTORY / 'comfy_nodes/FactorySAM3HFV1/__init__.py'
        spec = importlib.util.spec_from_file_location('factory_sam3_hf_node_tests', source)
        cls.node = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.node)
        import cv2
        import numpy as np
        cls.cv2, cls.np = cv2, np

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='sam3-node-unit-', dir=COMFY / 'input')
        self.directory = Path(self.temporary.name)
        self.source = self.directory / 'unit-source.dat'
        self.source.write_bytes(b'unit fixture, not video or production evidence')
        self.checkpoint = self.directory / 'unit-checkpoint.dat'
        self.checkpoint.write_bytes(b'unit fixture, not weights')
        self.bundle = self.directory / 'tokenizer'
        self.bundle.mkdir()
        (self.bundle / 'tokenizer.json').write_text('{}')
        (self.bundle / 'LICENSE').write_text('unit fixture')
        self.tracking = self.directory / 'tracking'
        self.tracking.mkdir()
        self.manifest_path = self.tracking / 'manifest.json'
        self.request_path = self.directory / 'request.json'
        self.execution_path = self.directory / 'execution.json'
        self.objects = [{'object_id': 'bag_1', 'text': 'handbag'}]
        self.manifest = {'status': 'READY', 'provider': 'SAM3_HF_LOCAL', 'source_sha256': self.node.sha256(self.source),
            'model_sha256': self.node.sha256(self.checkpoint), 'tokenizer_sha256': self.node.sha256(self.bundle / 'tokenizer.json'),
            'license_sha256': self.node.sha256(self.bundle / 'LICENSE'), 'worker_sha256': self.node.sha256(self.node.WORKER),
            'device': 'cpu', 'start_frame': 0, 'end_frame': 2, 'height': 2, 'width': 3, 'object_ids': ['bag_1'],
            'concepts': self.objects, 'strict_weight_load': {'missing_keys': [], 'unexpected_keys': [], 'mismatched_keys': [], 'error_msgs': []},
            'frames': [], 'scope': 'UNIT_FIXTURE_NOT_REAL_INFERENCE'}
        for index in range(2):
            path = self.tracking / f'{index}.png'
            self.cv2.imwrite(str(path), self.np.asarray([[0, 255, 0], [0, 255, 0]], dtype=self.np.uint8))
            self.manifest['frames'].append({'frame_index': index, 'objects': {'bag_1': {'mask_path': str(path), 'sha256': self.node.sha256(path)}}})
        self.request = {'source_rgb': str(self.source), 'model_path': str(self.checkpoint),
            'tokenizer_path': str(self.bundle / 'tokenizer.json'), 'license_path': str(self.bundle / 'LICENSE'),
            'start_frame': 0, 'end_frame': 2, 'concepts': self.objects, 'device': 'cpu', 'output_dir': str(self.tracking)}
        self.execution = {'command': [sys.executable, str(self.node.WORKER), '--request', str(self.request_path)],
            'worker_sha256': self.node.sha256(self.node.WORKER), 'exit_code': 0}
        self.persist()

    def tearDown(self):
        self.temporary.cleanup()

    def persist(self):
        self.manifest_path.write_text(json.dumps(self.manifest))
        self.request_path.write_text(json.dumps(self.request))
        self.execution['request_sha256'] = self.node.sha256(self.request_path)
        self.execution_path.write_text(json.dumps(self.execution))

    def cached_track(self):
        with patch.object(self.node.folder_paths, 'get_full_path_or_raise', return_value=str(self.checkpoint)):
            return self.node.FactorySAM3HFTrackV1().track(str(self.source), 'unit-checkpoint.dat', 'tokenizer',
                0, 2, json.dumps(self.objects), 'cpu', str(self.manifest_path), str(self.execution_path))

    def test_frame_coverage_and_mask_selection(self):
        result = self.cached_track()
        self.assertTrue(result['result'][0]['inference_reused'])
        output = self.node.FactorySAM3HFObjectMaskV1().select(result['result'][0], 'bag_1')['result'][0]
        self.assertEqual(list(output.shape), [2, 2, 3])
        self.assertEqual(set(output.unique().tolist()), {0.0, 1.0})

    def test_duplicate_frame_rejected(self):
        self.manifest['frames'].append(copy.deepcopy(self.manifest['frames'][0]))
        with self.assertRaises(ValueError):
            self.node.verify_masks(self.manifest)

    def test_object_canvas_and_hash_rejected(self):
        for kind in ('extra_object', 'width', 'hash'):
            with self.subTest(kind=kind):
                bad = copy.deepcopy(self.manifest)
                if kind == 'extra_object':
                    bad['frames'][0]['objects']['other_1'] = bad['frames'][0]['objects']['bag_1']
                elif kind == 'width':
                    bad['width'] += 1
                else:
                    bad['frames'][0]['objects']['bag_1']['sha256'] = '0' * 64
                with self.assertRaises(ValueError):
                    self.node.verify_masks(bad)

    def test_request_hash_rejected(self):
        self.request_path.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'request hash'):
            self.cached_track()

    def test_wrong_run_output_binding_rejected(self):
        self.request['output_dir'] = str(self.directory / 'other-run')
        self.persist()
        with self.assertRaisesRegex(ValueError, 'does not belong'):
            self.cached_track()

    def test_legacy_cache_missing_tokenizer_provenance_rejected(self):
        self.manifest.pop('tokenizer_sha256')
        self.persist()
        with self.assertRaisesRegex(ValueError, 'fingerprint'):
            self.cached_track()

    def test_path_containment(self):
        with self.assertRaises(ValueError):
            self.node.local_media('/etc/passwd')


if __name__ == '__main__':
    unittest.main()
