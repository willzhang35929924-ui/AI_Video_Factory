"""Local HF-schema SAM3 runner; does not replace the native Comfy SAM3 MODEL path."""
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import cv2
import numpy as np
import torch

import folder_paths

FACTORY_ROOT = Path('/Users/xiaoyao/AI_Video_Factory')
WORKER = FACTORY_ROOT / 'backend/app/integrations/legacy/sam3_hf_worker.py'


def sha256(path):
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def local_media(value):
    path = Path(value).expanduser().resolve()
    roots = (FACTORY_ROOT / 'storage', Path(folder_paths.get_input_directory()).resolve(),
             Path(folder_paths.get_output_directory()).resolve())
    if not any(path.is_relative_to(root) for root in roots) or not path.is_file():
        raise ValueError('Select an existing local Factory or Comfy asset')
    return path


def verify_masks(manifest):
    frames = manifest['frames']
    indices = [item['frame_index'] for item in frames]
    objects = set(manifest['object_ids'])
    if len(objects) != len(manifest['object_ids']) or manifest['object_ids'] != [item['object_id'] for item in manifest['concepts']]:
        raise ValueError('SAM3 object identities do not match concepts')
    if (len(frames) != manifest['end_frame'] - manifest['start_frame']
            or len(indices) != len(set(indices))
            or set(indices) != set(range(manifest['start_frame'], manifest['end_frame']))):
        raise ValueError('SAM3 cache has incomplete frame coverage')
    for frame in frames:
        if set(frame['objects']) != objects:
            raise ValueError('SAM3 frame object identities do not match')
        for object_id in manifest['object_ids']:
            item = frame['objects'][object_id]
            path = local_media(item['mask_path'])
            if sha256(path) != item['sha256']:
                raise ValueError('SAM3 object mask hash changed')
            image = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
            if image is None or image.dtype != np.uint8 or image.shape != (manifest['height'], manifest['width']):
                raise ValueError('SAM3 object mask has an invalid dtype or canvas')


class FactorySAM3HFTrackV1:
    @classmethod
    def INPUT_TYPES(cls):
        return {'required': {
            'source_rgb': ('STRING', {'default': ''}),
            'checkpoint': (folder_paths.get_filename_list('checkpoints'),),
            'tokenizer_folder': ('STRING', {'default': 'sam3-mlx'}),
            'start_frame': ('INT', {'default': 0, 'min': 0}),
            'end_frame': ('INT', {'default': 10, 'min': 1}),
            'concepts': ('STRING', {'default': '[{"object_id":"person_1","text":"person"},{"object_id":"bag_1","text":"handbag"}]', 'multiline': True}),
            'device': (['mps', 'cpu'], {'default': 'mps'}),
        }, 'optional': {
            'cached_manifest': ('STRING', {'default': ''}),
            'cached_execution': ('STRING', {'default': ''}),
        }}

    RETURN_TYPES = ('FACTORY_SAM3_HF_TRACKS',)
    RETURN_NAMES = ('object_tracks',)
    FUNCTION = 'track'
    CATEGORY = 'Factory/SAM3 HF local'
    OUTPUT_NODE = True

    def track(self, source_rgb, checkpoint, tokenizer_folder, start_frame, end_frame,
              concepts, device, cached_manifest='', cached_execution=''):
        source = local_media(source_rgb)
        checkpoint_path = Path(folder_paths.get_full_path_or_raise('checkpoints', checkpoint)).resolve()
        tokenizer_root = (checkpoint_path.parent / tokenizer_folder).resolve()
        if not tokenizer_root.is_relative_to(checkpoint_path.parent):
            raise ValueError('Tokenizer folder must stay inside the checkpoint directory')
        objects = json.loads(concepts)
        source_hash, model_hash, worker_hash = sha256(source), sha256(checkpoint_path), sha256(WORKER)
        tokenizer_hash, license_hash = sha256(tokenizer_root / 'tokenizer.json'), sha256(tokenizer_root / 'LICENSE')
        if cached_manifest or cached_execution:
            if not cached_manifest or not cached_execution:
                raise ValueError('Cached manifest and execution evidence are both required')
            manifest_path, execution_path = local_media(cached_manifest), local_media(cached_execution)
            manifest = json.loads(manifest_path.read_text())
            execution = json.loads(execution_path.read_text())
            if (manifest['status'] != 'READY' or manifest['provider'] != 'SAM3_HF_LOCAL'
                    or manifest['source_sha256'] != source_hash or manifest['model_sha256'] != model_hash
                    or manifest['start_frame'] != start_frame or manifest['end_frame'] != end_frame
                    or manifest['concepts'] != objects or manifest['device'] != device
                    or manifest.get('tokenizer_sha256') != tokenizer_hash or manifest.get('license_sha256') != license_hash
                    or manifest.get('worker_sha256') != worker_hash
                    or execution['exit_code'] != 0 or execution['worker_sha256'] != worker_hash):
                raise ValueError('SAM3 cached inference fingerprint does not match')
            command = execution['command']
            request_path = local_media(command[command.index('--request') + 1])
            if sha256(request_path) != execution['request_sha256']:
                raise ValueError('SAM3 original request hash changed')
            request = json.loads(request_path.read_text())
            if (local_media(request['source_rgb']) != source or request['start_frame'] != start_frame
                    or request['end_frame'] != end_frame or request['concepts'] != objects or request['device'] != device
                    or Path(request['model_path']).resolve() != checkpoint_path
                    or Path(request['tokenizer_path']).resolve() != tokenizer_root / 'tokenizer.json'
                    or Path(request['license_path']).resolve() != tokenizer_root / 'LICENSE'
                    or (Path(request['output_dir']).resolve() / 'manifest.json') != manifest_path):
                raise ValueError('SAM3 execution does not belong to this manifest')
            if any(manifest['strict_weight_load'].values()):
                raise ValueError('SAM3 cached weights were not strictly loaded')
            verify_masks(manifest)
            inference_reused = True
            execution_reference = {'path': str(execution_path), 'sha256': sha256(execution_path)}
        else:
            run_dir = Path(folder_paths.get_output_directory()) / 'FactorySAM3HF' / uuid.uuid4().hex
            run_dir.mkdir(parents=True)
            request = {'source_rgb': str(source), 'start_frame': start_frame, 'end_frame': end_frame,
                       'concepts': objects, 'output_dir': str(run_dir / 'tracking'), 'model_path': str(checkpoint_path),
                       'tokenizer_path': str(tokenizer_root / 'tokenizer.json'),
                       'license_path': str(tokenizer_root / 'LICENSE'), 'device': device}
            request_path, result_path = run_dir / 'request.json', run_dir / 'result.json'
            request_path.write_text(json.dumps(request, ensure_ascii=False, indent=2))
            environment = {key: value for key, value in os.environ.items() if key in {'PATH', 'LANG', 'LC_ALL', 'TMPDIR'}}
            environment.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1', PYTORCH_ENABLE_MPS_FALLBACK='1')
            command = [sys.executable, str(WORKER), '--request', str(request_path), '--result', str(result_path)]
            started = time.time()
            with (run_dir / 'stdout.log').open('w') as stdout, (run_dir / 'stderr.log').open('w') as stderr:
                process = subprocess.Popen(command, cwd=Path(folder_paths.__file__).parent, env=environment,
                                           shell=False, stdout=stdout, stderr=stderr)
                timed_out = False
                try:
                    exit_code = process.wait(timeout=900)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    process.kill()
                    exit_code = process.wait()
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
            execution_reference = {'command': command, 'cwd': str(Path(folder_paths.__file__).parent),
                    'pid': process.pid, 'worker_sha256': worker_hash, 'request_sha256': sha256(request_path),
                    'exit_code': exit_code, 'started_at': started, 'finished_at': time.time(),
                    'timed_out': timed_out,
                    'stdout_log_path': str(run_dir / 'stdout.log'), 'stderr_log_path': str(run_dir / 'stderr.log')}
            execution_path = run_dir / 'execution.json'
            execution_path.write_text(json.dumps(execution_reference, ensure_ascii=False, indent=2))
            if timed_out:
                raise RuntimeError('SAM3 HF timed out: ' + str(execution_path))
            if exit_code != 0:
                failure = json.loads(result_path.read_text()) if result_path.is_file() else {}
                raise RuntimeError('SAM3 HF failed: ' + failure.get('error_message', str(run_dir)))
            manifest_path = run_dir / 'tracking/manifest.json'
            manifest = json.loads(manifest_path.read_text())
            verify_masks(manifest)
            inference_reused = False
            execution_reference = {'path': str(execution_path), 'sha256': sha256(execution_path)}
        tracks = dict(manifest, manifest_path=str(manifest_path), manifest_sha256=sha256(manifest_path),
                      inference_reused=inference_reused, execution_reference=execution_reference)
        return {'ui': {'text': [json.dumps({'provider': 'SAM3_HF_LOCAL', 'frames': len(tracks['frames']),
                    'object_ids': tracks['object_ids'], 'manifest_path': str(manifest_path),
                    'inference_reused': inference_reused, 'native_comfy_model': False}, ensure_ascii=False)]},
                'result': (tracks,)}


class FactorySAM3HFObjectMaskV1:
    @classmethod
    def INPUT_TYPES(cls):
        return {'required': {'object_tracks': ('FACTORY_SAM3_HF_TRACKS',),
                             'object_id': ('STRING', {'default': 'bag_1'})}}

    RETURN_TYPES = ('MASK',)
    RETURN_NAMES = ('selected_object_mask',)
    FUNCTION = 'select'
    CATEGORY = 'Factory/SAM3 HF local'
    OUTPUT_NODE = True

    def select(self, object_tracks, object_id):
        if object_id not in object_tracks['object_ids']:
            raise ValueError('Unknown object ID: ' + object_id)
        masks = []
        for frame in sorted(object_tracks['frames'], key=lambda item: item['frame_index']):
            item = frame['objects'][object_id]
            path = local_media(item['mask_path'])
            if sha256(path) != item['sha256']:
                raise ValueError('SAM3 object mask hash changed')
            image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
            if image is None or image.shape != (object_tracks['height'], object_tracks['width']):
                raise ValueError('SAM3 object mask has an invalid canvas')
            masks.append(torch.from_numpy(image.astype(np.float32) / 255.0))
        output = torch.stack(masks)
        return {'ui': {'text': [json.dumps({'object_id': object_id, 'frames': output.shape[0],
                    'shape': list(output.shape), 'mask_semantics': 'object segmentation, not fine matting'})]},
                'result': (output,)}


NODE_CLASS_MAPPINGS = {'FactorySAM3HFTrackV1': FactorySAM3HFTrackV1,
                       'FactorySAM3HFObjectMaskV1': FactorySAM3HFObjectMaskV1}
NODE_DISPLAY_NAME_MAPPINGS = {'FactorySAM3HFTrackV1': 'Factory SAM3 HF Local Video Objects v1',
                              'FactorySAM3HFObjectMaskV1': 'Factory SAM3 HF Select Object v1'}
