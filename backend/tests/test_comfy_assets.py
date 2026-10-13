import ast
import hashlib
import json
import os
import tempfile
import time
import unittest
import uuid
from fractions import Fraction
from pathlib import Path

from app.integrations.comfy_asset_adapter import ComfyAssetAdapter
from app.integrations.legacy.video_adapter import LegacyComfyUIAdapter, LegacyComposeAdapter


class ComfyAssetContracts(unittest.TestCase):
    root = Path(__file__).resolve().parents[2]

    def graph(self, name):
        return json.loads((self.root / 'workflows' / name).read_text())

    def test_cutout_exports_source_rgb_and_independent_continuous_alpha(self):
        graph = self.graph('cutout_rgb_alpha_v2.api.json')
        self.assertEqual(graph['save_rgb']['inputs']['images'], ['38', 0])
        self.assertEqual(graph['mask_image']['inputs']['mask'], ['38', 1])
        self.assertEqual(graph['save_alpha']['inputs']['images'], ['mask_image', 0])
        self.assertEqual(graph['save_alpha']['inputs']['pix_fmt'], 'gray16le')
        for node in ('save_rgb', 'save_alpha'):
            self.assertEqual(graph[node]['inputs']['format'], 'video/ffv1-mkv')
            self.assertEqual(graph[node]['inputs']['gop_size'], 1)
        self.assertEqual(graph['14']['class_type'], 'FactoryLoadCFRContextV1')

    def test_composition_has_no_model_or_source_video_input(self):
        graph = self.graph('compose_person_v2.api.json')
        classes = {node['class_type'] for node in graph.values()}
        self.assertFalse(classes & {'BiRefNetRMBG', 'VideoAutoMatteRepair', 'VHS_LoadVideoPath', 'MatAnyone2'})
        self.assertEqual(graph['foreground']['class_type'], 'FactoryLoadForegroundV1')
        self.assertEqual(graph['23']['class_type'], 'ImageCompositeMasked')
        self.assertEqual(graph['23']['inputs']['source'], ['foreground', 0])
        self.assertEqual(graph['23']['inputs']['mask'], ['foreground', 1])
        self.assertEqual(graph['23']['inputs']['destination'], ['background_repeat', 0])
        self.assertEqual(graph['background_repeat']['class_type'], 'RepeatImageBatch')
        self.assertEqual(graph['background_repeat']['inputs']['amount'], graph['foreground']['inputs']['frame_count'])

    def test_existing_providers_route_to_distinct_split_workflows(self):
        for provider in (LegacyComfyUIAdapter, LegacyComposeAdapter):
            adapter = provider(Path('/Users/xiaoyao/video_auto'), storage_root=self.root / 'storage')
            self.assertEqual(adapter.assets.python_path, Path('/Users/xiaoyao/ComfyUI/.venv/bin/python'))
            self.assertEqual(adapter.assets.cutout_workflow.name, 'cutout_rgb_alpha_v2.api.json')
            self.assertEqual(adapter.assets.compose_workflow.name, 'compose_person_v2.api.json')

    def test_raw_and_repaired_diagnostics_compare_the_same_frame_range(self):
        graph = self.graph('cutout_alpha_diagnostic_v1.api.json')
        self.assertEqual(graph['raw_mask_image']['inputs']['mask'], ['11', 1])
        self.assertEqual(graph['mask_image']['inputs']['mask'], ['38', 1])
        self.assertEqual(graph['raw_crop']['inputs']['batch_index'], graph['38']['inputs']['start_frame'])
        self.assertEqual(graph['raw_crop']['inputs']['length'], graph['38']['inputs']['frame_count'])

    def test_run_id_cannot_escape_immutable_run_directory(self):
        adapter = ComfyAssetAdapter('/Users/xiaoyao/video_auto')
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, 'run ID'):
                adapter._invoke('cutout', {'run_id': '../old'}, Path(temporary), None, None)
            self.assertFalse((Path(temporary) / 'runs').exists())


class CompletedComfyHistory(unittest.TestCase):
    def setUp(self):
        worker = Path(__file__).resolve().parents[1] / 'app/integrations/legacy/comfy_asset_worker.py'
        function = next(node for node in ast.parse(worker.read_text()).body if isinstance(node, ast.FunctionDef) and node.name == 'record_history')
        # Journaling has no NumPy dependency; image decoding remains in ComfyUI's interpreter.
        namespace = {'hashlib': hashlib, 'json': json, 'time': time, 'Path': Path,
                     'write_json': lambda path, value: path.write_text(json.dumps(value))}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(worker), 'exec'), namespace)
        self.record_history = namespace['record_history']
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.graph = {'foreground': {'class_type': 'FactoryLoadForegroundV1', 'inputs': {'start_frame': 0}}}
        data = json.dumps(self.graph, ensure_ascii=False, indent=2).encode()
        (self.directory / 'part.api.json').write_bytes(data)
        self.state = {'status': 'completed', 'prompt_id': 'unit-original-prompt', 'outputs': {'unit': []}}
        (self.directory / 'part.json').write_text(json.dumps(self.state))
        self.evidence = {'prompt_id': self.state['prompt_id'], 'submitted_graph_sha256': hashlib.sha256(data).hexdigest(),
                         'processing_run_id': 'unit-original-run', 'history': {'prompt': [0, self.state['prompt_id'], self.graph],
                         'status': {'completed': True, 'status_str': 'success'}}}

        class Client:
            requests = 0
            def execute(client, graph, directory, stage):
                return self.state['outputs']
            def request(client, path):
                client.requests += 1
                return {}
        self.client = Client()

    def save_evidence(self):
        (self.directory / 'part.history.json').write_text(json.dumps(self.evidence))

    def test_server_restart_reuses_saved_completed_history_without_overwriting_owner(self):
        self.save_evidence()
        before = (self.directory / 'part.history.json').read_bytes()
        outputs, evidence = self.record_history(self.client, self.graph, self.directory, 'part', {'run_id': 'unit-resume-run'})
        self.assertEqual(self.client.requests, 0)
        self.assertEqual(outputs, self.state['outputs'])
        self.assertEqual(evidence['processing_run_id'], 'unit-original-run')
        self.assertEqual((self.directory / 'part.history.json').read_bytes(), before)

    def test_changed_saved_graph_is_not_accepted(self):
        self.evidence['history']['prompt'][2] = {'different': {}}
        self.save_evidence()
        with self.assertRaisesRegex(RuntimeError, 'evidence_changed'):
            self.record_history(self.client, self.graph, self.directory, 'part', {})

    def test_queue_state_without_history_does_not_fake_completion(self):
        with self.assertRaisesRegex(RuntimeError, 'history_not_completed'):
            self.record_history(self.client, self.graph, self.directory, 'part', {})
        self.assertEqual(self.client.requests, 1)


class ComfySourceTransport(unittest.TestCase):
    def test_authorized_input_link_preserves_identity_without_copying_media(self):
        worker = Path(__file__).resolve().parents[1] / 'app/integrations/legacy/comfy_asset_worker.py'
        function = next(node for node in ast.parse(worker.read_text()).body if isinstance(node, ast.FunctionDef) and node.name == 'comfy_source_path')
        namespace = {'os': os, 'Path': Path, 'digest': lambda path: hashlib.sha256(Path(path).read_bytes()).hexdigest()}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(worker), 'exec'), namespace)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = directory / 'Original.mp4'
            source.write_bytes(b'UNIT_ONLY_SOURCE_TRANSPORT')
            linked = namespace['comfy_source_path'](source, directory / 'comfy-input')
            self.assertTrue(linked.samefile(source))
            self.assertTrue(linked.is_relative_to((directory / 'comfy-input').resolve()))
            self.assertEqual(namespace['comfy_source_path'](source, directory / 'comfy-input'), linked)
            linked.unlink()
            linked.write_bytes(b'UNIT_ONLY_CHANGED_LINK')
            with self.assertRaisesRegex(ValueError, 'identity_changed'):
                namespace['comfy_source_path'](source, directory / 'comfy-input')


class CompositionCacheInputs(unittest.TestCase):
    def setUp(self):
        self.worker = Path(__file__).resolve().parents[1] / 'app/integrations/legacy/comfy_asset_worker.py'
        self.tree = ast.parse(self.worker.read_text())

    def test_factory_polling_changes_receipt_wait_without_mutating_legacy_config(self):
        config = {'poll_interval_s': 3, 'output_crf': 18, 'color_enabled': True}
        for stage in ('cutout', 'compose'):
            function = next(node for node in self.tree.body if isinstance(node, ast.FunctionDef) and node.name == stage)
            assignment = next(node for node in ast.walk(function) if isinstance(node, ast.Assign)
                              and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name)
                              and node.value.func.id == 'ComfyClient')
            namespace = {'config': config, 'ComfyClient': lambda options: options}
            exec(compile(ast.Module(body=[assignment], type_ignores=[]), str(self.worker), 'exec'), namespace)
            self.assertEqual(namespace['client']['poll_interval_s'], 0.5)
            self.assertEqual(namespace['client']['output_crf'], 18)
            self.assertIsNot(namespace['client'], config)
            self.assertEqual(config, {'poll_interval_s': 3, 'output_crf': 18, 'color_enabled': True})

    def test_receipt_polling_does_not_change_composition_fingerprint(self):
        function = next(node for node in self.tree.body if isinstance(node, ast.FunctionDef)
                        and node.name == 'composition_fingerprint')
        namespace = {'hashlib': hashlib, 'json': json, 'digest': lambda path: 'workflow'}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(self.worker), 'exec'), namespace)
        fingerprint = namespace['composition_fingerprint']
        inputs = ({'sha256': 'rgb'}, {'alpha_sha256': 'alpha'}, {'sha256': 'background'},
                  {'factory_algorithm_version': 'current'}, {'source_sha256': 'source'}, 'workflow', {})
        config = {'poll_interval_s': 3, 'output_crf': 18, 'workflow_color_contrast': 1}
        self.assertEqual(fingerprint(*inputs, config), fingerprint(*inputs, {**config, 'poll_interval_s': 0.5}))

    def test_color_toggle_changes_output_identity_but_default_stays_enabled(self):
        function = next(node for node in self.tree.body if isinstance(node, ast.FunctionDef) and node.name == 'compose')
        assignment = next(node for node in function.body if isinstance(node, ast.Assign)
                          and any(isinstance(target, ast.Name) and target.id == 'identity' for target in node.targets))
        fingerprint = next(node for node in self.tree.body if isinstance(node, ast.FunctionDef) and node.name == 'composition_fingerprint')
        code = compile(ast.Module(body=[fingerprint, assignment], type_ignores=[]), str(self.worker), 'exec')
        def identity(config):
            namespace = {'hashlib': hashlib, 'json': json, 'person': {'sha256': 'rgb'},
                         'reference': {'alpha_sha256': 'alpha'}, 'background': {'sha256': 'background'},
                         'implementations': {}, 'source_inputs': {'source_sha256': 'source', 'audio': {'source': 'mother_rgb', 'rgb_sha256': 'rgb'}},
                         'digest': lambda path: 'workflow', 'workflow_path': 'workflow',
                         'context': {}, 'config': config}
            exec(code, namespace)
            return namespace['identity']
        self.assertEqual(identity({}), identity({'color_enabled': True}))
        self.assertNotEqual(identity({'color_enabled': True}), identity({'color_enabled': False}))

    def test_composition_fingerprint_covers_actual_color_and_video_nodes(self):
        function = next(node for node in self.tree.body if isinstance(node, ast.FunctionDef) and node.name == 'implementation_fingerprint')
        namespace = {'Path': Path, 'digest': lambda path: hashlib.sha256(path.read_bytes()).hexdigest()}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(self.worker), 'exec'), namespace)
        with tempfile.TemporaryDirectory() as temporary:
            comfy = Path(temporary)
            names = ['custom_nodes/ComfyUI_LayerStyle/py/color_correct_brightness_and_contrast.py',
                     'custom_nodes/ComfyUI-VideoHelperSuite/videohelpersuite/nodes.py',
                     'custom_nodes/ComfyUI-VideoHelperSuite/video_formats/h264-mp4.json']
            for name in names:
                path = comfy / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('version1')
            config = {'comfy_input_dir': str(comfy / 'input')}
            fingerprint = namespace['implementation_fingerprint'](config, 'compose', {})
            self.assertTrue(set(names).issubset(fingerprint))
            (comfy / names[0]).write_text('version2')
            changed = namespace['implementation_fingerprint'](config, 'compose', {})
            self.assertNotEqual(fingerprint, changed)

    def test_mother_audio_identity_does_not_read_original_source(self):
        function = next(node for node in self.tree.body if isinstance(node, ast.FunctionDef)
                        and node.name == 'composition_mother_identity')
        namespace = {'Path': Path, 'json': json}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(self.worker), 'exec'), namespace)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'manifest.json'
            original = '/missing-original-video-not-read.mp4'
            manifest = {'source_sha256': 'source', 'rgb_sha256': 'rgb-with-audio-v1', 'alpha_sha256': 'alpha',
                        'full_decode_verified': True, 'source_audio_path': original}
            path.write_text(json.dumps(manifest))
            reference = {'source_sha256': 'source', 'alpha_sha256': 'alpha', 'manifest_path': str(path), 'source_audio_path': original}
            helper = namespace['composition_mother_identity']
            first = helper({'sha256': 'rgb-with-audio-v1'}, reference, {'audio': [{'codec_name': 'aac'}]})
            self.assertEqual(first['audio'], {'source': 'mother_rgb', 'rgb_sha256': 'rgb-with-audio-v1'})
            manifest['rgb_sha256'] = 'rgb-with-audio-v2'
            path.write_text(json.dumps(manifest))
            changed = helper({'sha256': 'rgb-with-audio-v2'}, reference, {'audio': [{'codec_name': 'aac'}]})
            self.assertNotEqual(first, changed)
            with self.assertRaisesRegex(ValueError, 'mother_audio_missing'):
                helper({'sha256': 'rgb-with-audio-v2'}, reference, {'audio': []})
            with self.assertRaisesRegex(ValueError, 'mother_manifest_mismatch'):
                helper({'sha256': 'rgb-with-audio-v2'}, {**reference, 'source_sha256': 'unverified-declaration'}, {'audio': [{}]})

    def test_completed_composition_identity_ignores_run_id_but_changes_with_actual_inputs(self):
        function = next(node for node in self.tree.body if isinstance(node, ast.FunctionDef) and node.name == 'compose')
        assignment = next(node for node in function.body if isinstance(node, ast.Assign)
                          and any(isinstance(target, ast.Name) and target.id == 'identity' for target in node.targets))
        fingerprint = next(node for node in self.tree.body if isinstance(node, ast.FunctionDef) and node.name == 'composition_fingerprint')
        code = compile(ast.Module(body=[fingerprint, assignment], type_ignores=[]), str(self.worker), 'exec')
        def identity(**changes):
            values = {'rgb': 'rgb-v1', 'alpha': 'alpha-v1', 'background': 'background-v1', 'workflow': 'workflow-v1', 'run_id': 'run-1'}
            values.update(changes)
            namespace = {'hashlib': hashlib, 'json': json, 'person': {'sha256': values['rgb']},
                         'reference': {'alpha_sha256': values['alpha']}, 'background': {'sha256': values['background']},
                         'implementations': {}, 'source_inputs': {'source_sha256': 'source', 'audio': {'source': 'mother_rgb', 'rgb_sha256': values['rgb']}},
                         'digest': lambda path: values['workflow'], 'workflow_path': 'workflow',
                         'context': {'run_id': values['run_id']}, 'config': {}}
            exec(code, namespace)
            return namespace['identity']
        original = identity()
        self.assertEqual(original, identity(run_id='run-2'))
        for field in ('rgb', 'alpha', 'background', 'workflow'):
            self.assertNotEqual(original, identity(**{field: 'changed-real-input'}))

    def test_aac_clock_uses_actual_sample_rate_frame_size_and_priming(self):
        function = next(node for node in self.tree.body if isinstance(node, ast.FunctionDef) and node.name == 'aac_sample_clock')
        namespace = {'json': json}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(self.worker), 'exec'), namespace)
        for rate, packet_samples, padding in ((44100, 2048, 7106), (48000, 1024, 1024)):
            data = {'streams': [{'codec_name': 'aac', 'sample_rate': str(rate), 'initial_padding': padding, 'start_time': '0'}],
                    'frames': [{'nb_samples': 300}, {'nb_samples': packet_samples}, {'nb_samples': 200}]}
            namespace['command'] = lambda arguments: json.dumps(data).encode()
            result = namespace['aac_sample_clock'](Path('mother.mkv'))
            self.assertEqual(result['packet_samples'], packet_samples)
            self.assertEqual(result['start_samples'], -padding)
            self.assertIn(f'time_base=1/{rate}:prescale=1', result['filter'])
        namespace['command'] = lambda arguments: json.dumps({'streams': [{'codec_name': 'flac'}]}).encode()
        self.assertIsNone(namespace['aac_sample_clock'](Path('mother.mkv')))

    def test_remux_video_origin_is_removed_without_losing_b_frame_order(self):
        function = next(node for node in self.tree.body if isinstance(node, ast.FunctionDef) and node.name == 'concat_paths')
        calls = []
        namespace = {'Fraction': Fraction, 'command': calls.append,
                     'aac_sample_clock': lambda path: {'filter': 'unit-actual-audio-clock'}}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(self.worker), 'exec'), namespace)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            namespace['concat_paths']([root / 'previous.mp4'], root / 'next.mp4', root, root / 'RGB.mkv',
                                      presentation_fps='30000/1001')
        arguments = calls[0]
        clock = arguments[arguments.index('-bsf:v') + 1]
        self.assertIn('pts=round((PTS-STARTPTS)', clock)
        self.assertIn('dts=round((DTS-STARTPTS)', clock)
        self.assertIn('(30000/1001)*TB', clock)
        self.assertEqual(arguments[arguments.index('-c:v') + 1], 'copy')
        self.assertEqual(arguments[arguments.index('-bsf:a') + 1], 'unit-actual-audio-clock')

    def test_timeline_gate_rejects_av_origin_shift_and_frame_clock_drift(self):
        function = next(node for node in self.tree.body if isinstance(node, ast.FunctionDef) and node.name == 'verify_composition_timeline')
        namespace = {'hashlib': hashlib, 'json': json, 'frame_timestamps': lambda path: [index / 30 for index in range(8)]}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(self.worker), 'exec'), namespace)
        helper = namespace['verify_composition_timeline']
        source = {'fps': 30, 'frame_count': 8, 'duration': 8 / 30, 'video_start_time': '0.000000',
                  'audio': [{'start_time': '0.000000', 'sample_rate': '44100'}]}
        self.assertTrue(helper('unit.mp4', source, source)['all_frame_timestamps_verified'])
        with self.assertRaisesRegex(RuntimeError, 'video_origin_mismatch'):
            helper('unit.mp4', source, {**source, 'video_start_time': '0.166667'})
        with self.assertRaisesRegex(RuntimeError, 'audio_origin_mismatch'):
            helper('unit.mp4', source, {**source, 'audio': [{'start_time': '0.033333', 'sample_rate': '44100'}]})
        with self.assertRaisesRegex(RuntimeError, 'video_origin_unknown'):
            helper('unit.mp4', source, {**source, 'video_start_time': None})
        namespace['frame_timestamps'] = lambda path: [index / 30 + (.033333 if index == 4 else 0) for index in range(8)]
        with self.assertRaisesRegex(RuntimeError, 'frame_clock_mismatch'):
            helper('unit.mp4', source, source)

    def test_timeline_gate_preserves_non_30_fps_and_silent_video(self):
        function = next(node for node in self.tree.body if isinstance(node, ast.FunctionDef) and node.name == 'verify_composition_timeline')
        fps = float(Fraction(30000, 1001))
        namespace = {'hashlib': hashlib, 'json': json,
                     'frame_timestamps': lambda path: [round(index / fps, 6) for index in range(8)]}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(self.worker), 'exec'), namespace)
        media = {'fps': fps, 'frame_count': 8, 'duration': 8 / fps, 'video_start_time': '0.000000', 'audio': []}
        result = namespace['verify_composition_timeline']('unit.mp4', media, media)
        self.assertTrue(result['video_origin_identical'])
        self.assertFalse(result['audio_origins_identical'])

    def test_remux_preserves_origin_and_rejects_changed_image_settings(self):
        names = {'composition_fingerprint', 'reusable_composition'}
        functions = [node for node in self.tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
        digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
        namespace = {'Path': Path, 'hashlib': hashlib, 'json': json, 'digest': digest, 'decode_verify': lambda path: None}
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(self.worker), 'exec'), namespace)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path, graph_path, workflow = root / 'composition.mp4', root / 'part.api.json', root / 'workflow.json'
            path.write_bytes(b'unit-existing-file')
            graph_path.write_text('{"unit":{}}')
            workflow.write_text('{"template":{}}')
            history_path = root / 'part.history.json'
            history_path.write_text(json.dumps({'prompt_id': 'original-real-prompt', 'processing_run_id': 'original-run',
                'history': {'prompt': [0, 'original-real-prompt', {'unit': {}}], 'status': {'completed': True, 'status_str': 'success'}}}))
            context, person, reference, background = {'task_id': 'task', 'context_id': 'context'}, {'sha256': 'rgb'}, {'alpha_sha256': 'alpha'}, {'sha256': 'bg'}
            source, old_implementation = {'source_sha256': 'source'}, {'factory_algorithm_version': 'v3', 'node_sha': 'node'}
            fingerprint = namespace['composition_fingerprint'](person, reference, background, old_implementation, source, workflow, context, {})
            prior = {'task_id': 'task', 'context_id': 'context', 'output_sha256': digest(path), 'full_decode_verified': True,
                'input_fingerprint': fingerprint, 'implementation_hashes': old_implementation,
                'executions': [{'prompt_id': 'original-real-prompt', 'submitted_graph_sha256': digest(graph_path), 'workflow_path': str(graph_path)}]}
            manifest_path = root / 'manifest.json'
            manifest_path.write_text(json.dumps(prior))
            asset = {'storage_path': str(path), 'sha256': digest(path), 'legacy_reference': {'manifest_path': str(manifest_path)}}
            current = {'factory_algorithm_version': 'v4', 'node_sha': 'node'}
            returned, proof = namespace['reusable_composition'](asset, context, person, reference, background, current, source, workflow, {})
            self.assertEqual(returned, path.resolve())
            self.assertEqual(proof['executions'], prior['executions'])
            before = history_path.read_bytes()
            with self.assertRaisesRegex(ValueError, 'parent_inputs_mismatch'):
                namespace['reusable_composition'](asset, context, person, reference, background, current, source, workflow, {'workflow_color_brightness': 2})
            self.assertEqual(before, history_path.read_bytes())


class SnapshotOriginEvidence(unittest.TestCase):
    def test_cached_snapshot_retains_original_submission_owner(self):
        worker = Path(__file__).resolve().parents[1] / 'app/integrations/legacy/comfy_asset_worker.py'
        function = next(node for node in ast.parse(worker.read_text()).body if isinstance(node, ast.FunctionDef)
                        and node.name == 'snapshot_metadata')
        digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
        namespace = {'Path': Path, 'json': json, 'uuid': uuid, 'digest': digest}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(worker), 'exec'), namespace)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'comfy/part.api.json'
            source.parent.mkdir()
            source.write_text('{"unit":{}}')
            history = source.with_name('part.history.json')
            history.write_text(json.dumps({'prompt_id': 'original-prompt', 'processing_run_id': 'original-run',
                                           'task_id': 'original-task'}))
            before = history.read_bytes()
            manifest = {'executions': [{'prompt_id': 'original-prompt', 'workflow_path': str(source),
                                        'submitted_graph_sha256': digest(source)}]}
            result = namespace['snapshot_metadata'](manifest, root, root / 'snapshot.json',
                {'task_id': 'current-task', 'run_id': 'cache-read-run'}, True)
            self.assertFalse(result['submitted'])
            self.assertTrue(result['cache_hit'])
            self.assertEqual(result['origin_processing_run_id'], 'original-run')
            self.assertEqual(result['origin_task_id'], 'original-task')
            self.assertEqual(result['origin_history_sha256'], digest(history))
            self.assertEqual(history.read_bytes(), before)
            self.assertEqual(result['sha256'], digest(source))


if __name__ == '__main__':
    unittest.main()
