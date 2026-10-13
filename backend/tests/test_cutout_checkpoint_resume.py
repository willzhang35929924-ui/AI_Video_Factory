import ast
import copy
import hashlib
import json
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from pydantic import ValidationError

from app.integrations.comfy_asset_adapter import CutoutCheckpointResume, CutoutContext


class CutoutCheckpointRecovery(unittest.TestCase):
    def setUp(self):
        worker = Path(__file__).resolve().parents[1] / 'app/integrations/legacy/comfy_asset_worker.py'
        names = {'cutout_identity', 'cutout_chunk_graph', 'cutout_receipt', 'cutout_recovery_graph',
                 'cutout_checkpoint', 'resume_cutout_chunk', 'record_history', 'result_path'}
        functions = [node for node in ast.parse(worker.read_text()).body
                     if isinstance(node, ast.FunctionDef) and node.name in names]
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.output = self.directory / 'comfy-output'
        self.output.mkdir()
        self.source = self.directory / 'Original.mp4'
        self.source.write_bytes(b'UNIT_ONLY_SOURCE')
        self.workflow = self.directory / 'workflow.api.json'
        self.graph = {'14': {'class_type': 'FactoryLoadCFRContextV1', 'inputs': {'video': ''}},
            '38': {'class_type': 'VideoAutoMatteRepair', 'inputs': {'temporal_radius': 2}},
            'save_rgb': {'class_type': 'VHS_VideoCombine', 'inputs': {'filename_prefix': ''}},
            'save_alpha': {'class_type': 'VHS_VideoCombine', 'inputs': {'filename_prefix': ''}}}
        self.workflow.write_text(json.dumps(self.graph))
        self.context = {'task_id': 'unit-cutout-task', 'context_id': 'unit-context', 'source_video': str(self.source),
                        'run_id': 'unit-recovery-run', 'cutout_generation': 1}
        self.media = {'frame_count': 32, 'fps': 30, 'width': 2, 'height': 2, 'codec': 'ffv1'}
        self.ns = {'Path': Path, 'json': json, 'copy': copy, 'hashlib': hashlib, 'uuid': uuid, 'time': time,
            'digest': lambda path: hashlib.sha256(Path(path).read_bytes()).hexdigest(),
            'implementation_fingerprint': lambda *args: {'unit_algorithm': 'unit-v1'},
            'write_json': lambda path, value: path.write_text(json.dumps(value)),
            'emit': lambda *args, **kwargs: None, 'probe': self.probe}
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(worker), 'exec'), self.ns)
        _, source_hash, _, self.identity = self.ns['cutout_identity'](
            self.context, {}, self.graph, self.workflow, 8)
        self.partial = self.directory / 'foreground' / (self.identity + '.partial')
        self.journal = self.partial / 'comfy'
        self.journal.mkdir(parents=True)
        (self.partial / 'time_mapping.json').write_text(json.dumps({'source_sha256': source_hash,
            'normalized': self.media, 'normalization_applied': False}))
        self.live_history, self.queue, self.submissions = {}, [], []
        owner = self

        class Client:
            def request(client, route):
                if route == '/queue':
                    return {'queue_running': owner.queue, 'queue_pending': []}
                if route == '/history?max_items=1000':
                    return owner.live_history
                if route.startswith('/history/'):
                    prompt = route.split('/')[-1]
                    return {prompt: owner.live_history[prompt]} if prompt in owner.live_history else {}
                raise AssertionError('Read-only checkpoint must not POST: ' + route)

            def output_path(client, record):
                path = owner.output / record['filename']
                if not path.is_file():
                    raise RuntimeError('unit_output_missing')
                return path

            def execute(client, graph, directory, stage):
                state_path = directory / (stage + '.json')
                if state_path.exists():
                    state = json.loads(state_path.read_text())
                    if state['status'] == 'completed':
                        return state['outputs']
                    raise AssertionError('Lost receipt must not call the old journal')
                owner.submissions.append(copy.deepcopy(graph))
                prompt_id = 'unit-real-new-' + str(len(owner.submissions))
                outputs = owner.outputs(prompt_id)
                state = owner.state(graph, prompt_id, 'completed', outputs)
                state_path.write_text(json.dumps(state))
                owner.live_history[prompt_id] = {'prompt': [0, prompt_id, graph], 'outputs': outputs,
                                                'status': {'completed': True, 'status_str': 'success'}}
                return outputs

        self.client = Client()
        self.save_chunk(0, True)
        self.save_chunk(8, True)
        self.save_chunk(16, False)

    def probe(self, path):
        return self.media if Path(path) == self.source else {**self.media, 'frame_count': 8}

    def outputs(self, prefix):
        outputs = {}
        for node in ('save_rgb', 'save_alpha'):
            filename = prefix + '_' + node + '.mkv'
            (self.output / filename).write_bytes(('UNIT_ONLY_' + filename).encode())
            outputs[node] = {'gifs': [{'filename': filename, 'type': 'output'}]}
        return outputs

    def state(self, graph, prompt_id, status, outputs):
        return {'status': status, 'prompt_id': prompt_id, 'token': prompt_id + '-token',
            'graph_fingerprint': hashlib.sha256(json.dumps(graph, sort_keys=True).encode()).hexdigest(),
            **({'outputs': outputs} if status == 'completed' else {})}

    def save_chunk(self, start, completed):
        graph, _ = self.ns['cutout_chunk_graph'](self.graph, self.source, self.media, self.context,
                                              self.identity, start, 8)
        stage = f'cutout_{start:06d}'
        data = json.dumps(graph, ensure_ascii=False, indent=2).encode()
        (self.journal / (stage + '.api.json')).write_bytes(data)
        prompt_id = 'unit-original-' + str(start)
        outputs = self.outputs(prompt_id)
        (self.journal / (stage + '.json')).write_text(json.dumps(self.state(
            graph, prompt_id, 'completed' if completed else 'queued', outputs)))
        if completed:
            evidence = {'prompt_id': prompt_id, 'submitted_graph_sha256': hashlib.sha256(data).hexdigest(),
                'processing_run_id': 'unit-original-run', 'workflow_path': str(self.journal / (stage + '.api.json')),
                'history': {'prompt': [0, prompt_id, graph], 'outputs': outputs,
                            'status': {'completed': True, 'status_str': 'success'}}}
            (self.journal / (stage + '.history.json')).write_text(json.dumps(evidence))
        return graph

    def plan(self):
        return self.ns['cutout_checkpoint'](self.context, self.directory, {}, self.graph, self.workflow, 8, self.client)

    def test_plan_reads_real_receipts_and_never_submits_or_promotes_orphan_files(self):
        before = {path: path.read_bytes() for path in self.journal.iterdir()}
        plan = self.plan()
        self.assertEqual(plan['completed_frames'], 16)
        self.assertEqual(plan['receipt_lost_frames'], 8)
        self.assertEqual(plan['comfy_submissions'], 0)
        self.assertEqual(self.submissions, [])
        self.assertEqual({path: path.read_bytes() for path in self.journal.iterdir()}, before)
        self.assertFalse((self.journal / 'cutout_000016.history.json').exists())

    def test_lost_receipt_resubmits_only_its_chunk_in_new_namespace_and_preserves_original(self):
        plan = self.plan()
        original = {path: path.read_bytes() for path in self.journal.iterdir()}
        original_media = {path: path.read_bytes() for path in self.output.iterdir()}
        for receipt in plan['chunks']:
            graph, _ = self.ns['cutout_chunk_graph'](self.graph, self.source, self.media, self.context,
                                                   self.identity, receipt['start_frame'], 8)
            self.ns['resume_cutout_chunk'](self.client, graph, self.journal,
                f'cutout_{receipt["start_frame"]:06d}', self.context, receipt)
        self.assertEqual(len(self.submissions), 1)
        self.assertEqual(self.submissions[0]['38']['inputs']['frame_count'], 8)
        self.assertEqual(self.submissions[0]['14']['inputs']['start_frame'], 14)
        self.assertTrue(all(path.read_bytes() == data for path, data in original.items()))
        self.assertTrue(all(path.read_bytes() == data for path, data in original_media.items()))
        self.assertFalse((self.journal / 'cutout_000016.history.json').exists())
        resumed = self.plan()
        self.assertEqual(resumed['completed_frames'], 24)
        self.assertEqual(resumed['receipt_lost_frames'], 0)
        self.assertEqual(resumed['chunks'][-1]['prompt_id'], 'unit-real-new-1')
        graph, _ = self.ns['cutout_chunk_graph'](self.graph, self.source, self.media, self.context, self.identity, 16, 8)
        self.ns['resume_cutout_chunk'](self.client, graph, self.journal, 'cutout_000016', self.context, resumed['chunks'][-1])
        self.assertEqual(len(self.submissions), 1)

    def test_live_pending_prompt_is_not_classified_as_lost(self):
        graph = json.loads((self.journal / 'cutout_000016.api.json').read_text())
        self.queue = [[0, 'unit-original-16', graph, {'video_auto_job': 'unit-original-16-token'}]]
        plan = self.plan()
        self.assertEqual(plan['chunks'][-1]['status'], 'RUNNING')
        self.assertEqual(plan['receipt_lost_frames'], 0)
        self.assertEqual(self.submissions, [])

    def test_live_completed_receipt_is_recovered_without_new_submission(self):
        graph = json.loads((self.journal / 'cutout_000016.api.json').read_text())
        outputs = self.outputs('unit-original-16')
        self.live_history['unit-original-16'] = {'prompt': [0, 'unit-original-16', graph],
            'outputs': outputs, 'status': {'completed': True, 'status_str': 'success'}}
        plan = self.plan()
        self.assertEqual(plan['completed_frames'], 24)
        self.assertEqual(plan['receipt_lost_frames'], 0)
        self.assertEqual(self.submissions, [])

    def test_changed_graph_output_and_source_are_not_silently_reused(self):
        path = self.journal / 'cutout_000000.history.json'
        history = json.loads(path.read_text())
        history['history']['prompt'][2]['38']['inputs']['frame_count'] = 7
        path.write_text(json.dumps(history))
        with self.assertRaisesRegex(RuntimeError, 'history_graph_changed'):
            self.plan()
        self.source.write_bytes(b'CHANGED_UNIT_SOURCE')
        with self.assertRaisesRegex(RuntimeError, 'partial_missing'):
            self.plan()
        self.assertEqual(self.submissions, [])

    def test_failed_external_history_is_never_automatically_resubmitted(self):
        graph = json.loads((self.journal / 'cutout_000016.api.json').read_text())
        self.live_history['unit-original-16'] = {'prompt': [0, 'unit-original-16', graph],
            'status': {'completed': False, 'status_str': 'error'}}
        with self.assertRaisesRegex(RuntimeError, 'execution_failed'):
            self.plan()
        self.assertEqual(self.submissions, [])

    def test_recovery_pointer_cannot_escape_the_factory_checkpoint(self):
        graph = json.loads((self.journal / 'cutout_000016.api.json').read_text())
        (self.journal / 'cutout_000016.resume.json').write_text(json.dumps({'directory': '../../../outside',
            'attempt': 'unit', 'base_graph_sha256': hashlib.sha256(
                json.dumps(graph, ensure_ascii=False, indent=2).encode()).hexdigest()}))
        with self.assertRaisesRegex(RuntimeError, 'outside_root'):
            self.plan()

    def test_resume_authorization_cannot_change_the_generation_or_accept_arbitrary_fields(self):
        plan = self.plan()
        authorization = {key: plan[key] for key in ('input_fingerprint', 'checkpoint_fingerprint', 'generation')}
        self.assertEqual(CutoutCheckpointResume.model_validate(authorization).generation, 1)
        with self.assertRaises(ValidationError):
            CutoutCheckpointResume.model_validate({**authorization, 'ignore_history': True})
        context = CutoutContext.model_validate({**self.context, 'project_id': 'unit-project', 'country': 'MX',
            'keyword': 'unit', 'product_id': 'unit-product', 'video_id': 'unit-video',
            'source_document': 'unit', 'source_row': 1, 'cutout_checkpoint_resume': authorization})
        self.assertEqual(context.model_dump()['cutout_checkpoint_resume'], authorization)
