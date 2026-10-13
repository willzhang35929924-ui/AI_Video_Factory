import os
import subprocess
import unittest
from pathlib import Path

from app.integrations.process_runner import BASE_ENV_KEYS


SCRIPT = r'''
import copy, json, sys, tempfile
from pathlib import Path
import cv2, numpy as np
sys.path.insert(0, sys.argv[1])
import matte_repair_worker as worker

case = sys.argv[2]
with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    keyframes = [{'frame_index':10,'object_id':name,'coordinate_space':'source_pixels',
        'points':[{'x':6,'y':4,'label':1}]} for name in ('hand','clothing','white','red')]
    request = {'task_id':'isolated-unit','parent_rgb_sha256':'unit-source-sha','source_width':12,'source_height':8,
        'start_frame':10,'end_frame':12,'direction':'both','keyframes':keyframes}
    components = []
    records = []
    for index, objects in enumerate((['hand','clothing','white'], ['red'])):
        path, original = root / f'tracking-{index}.json', root / f'request-{index}.json'
        previous = {**request, 'run_id':f'actual-origin-{index}',
            'keyframes':copy.deepcopy([item for item in keyframes if item['object_id'] in objects])}
        original.write_text(json.dumps(previous))
        frames = []
        for frame in range(10,12):
            values = {}
            for name in objects:
                mask = root / f'{name}-{frame}.png'
                cv2.imwrite(str(mask), np.full((8,12), 255, dtype=np.uint8))
                values[name] = {'mask_path':str(mask),'sha256':worker.digest(mask)}
            frames.append({'frame_index':frame,'objects':values})
        tracking = {'status':'READY','provider':'SAM2.1','model_sha256':worker.SAM2_SHA256,'code_commit':worker.SAM2_COMMIT,
            'tracking_worker_sha256':worker.digest(Path(worker.__file__).with_name('object_tracking_worker.py')),
            'source_sha256':'unit-source-sha','start_frame':10,'end_frame':12,'direction':'both','interval':'half_open',
            'frame_count':2,'width':12,'height':8,'object_ids':objects,'frames':frames,'task_id':f'origin-{index}',
            'elapsed_seconds':34 + index,'device':'mps'}
        path.write_text(json.dumps(tracking))
        components.append({'path':str(path),'sha256':worker.digest(path),'request_path':str(original),
            'request_sha256':worker.digest(original),'object_ids':objects})
        records.append((tracking,previous,path,original))
    request['cached_tracking'] = {'sources':components}
    def save(index=1):
        tracking, previous, path, original = records[index]
        path.write_text(json.dumps(tracking)); original.write_text(json.dumps(previous))
        components[index].update(sha256=worker.digest(path),request_sha256=worker.digest(original))
    if case == 'single':
        request['keyframes'] = keyframes[:3]
        request['cached_tracking'] = {key:value for key,value in components[0].items() if key != 'object_ids'}
    elif case == 'normalized':
        request['keyframes'] = [{**item,'coordinate_space':'normalized','points':[{'x':.5,'y':.5,'label':1}]} for item in keyframes]
    elif case == 'evidence': components[1]['sha256'] = 'changed'
    elif case == 'duplicate': components[1]['object_ids'] = ['red','hand']
    elif case == 'duplicate_within': components[1]['object_ids'] = ['red','red']
    elif case == 'missing_source': request['cached_tracking']['sources'] = components[:1]
    elif case == 'model': records[1][0]['model_sha256'] = 'changed'; save()
    elif case == 'provider': records[1][0]['provider'] = 'Other'; save()
    elif case == 'code': records[1][0]['tracking_worker_sha256'] = 'changed'; save()
    elif case == 'source': records[1][0]['source_sha256'] = 'changed'; save()
    elif case == 'request_source': records[1][1]['parent_rgb_sha256'] = 'changed'; save()
    elif case == 'interval': records[1][0]['start_frame'] = 9; save()
    elif case == 'annotation': records[1][1]['keyframes'][0]['points'][0]['x'] = 5; save()
    elif case == 'missing_frame': records[1][0]['frames'].pop(); save()
    elif case == 'duplicate_frame': records[1][0]['frames'][1]['frame_index'] = 10; save()
    elif case == 'missing_object': records[1][0]['frames'][0]['objects'].clear(); save()
    elif case == 'png':
        Path(records[1][0]['frames'][0]['objects']['red']['mask_path']).write_bytes(b'changed')
    elif case == 'origin':
        path = root / 'native-manifest.json'; path.write_text('{"status":"READY"}')
        records[1][0].update(origin_manifest_path=str(path),origin_manifest_sha256=worker.digest(path)); save()
        path.write_text('{"status":"changed"}')
    elif case in {'canvas','bitdepth'}:
        evidence = records[1][0]['frames'][0]['objects']['red']
        cv2.imwrite(evidence['mask_path'],np.full((9,12) if case == 'canvas' else (8,12),255,
            dtype=np.uint8 if case == 'canvas' else np.uint16))
        evidence['sha256'] = worker.digest(Path(evidence['mask_path'])); save()
    def no_inference(*args, **kwargs):
        raise AssertionError('cache reuse must not call model')
    worker.track_objects = no_inference
    try:
        result = worker.masks_for(request, root)
    except ValueError as error:
        assert case not in {'valid','single','normalized'}, (case,str(error))
        assert str(error).startswith(('TRACK_CACHE_','TRACK_MASK_')), (case,str(error))
        print(case,str(error))
    else:
        assert case in {'valid','single','normalized'}, f'{case} wrongly accepted'
        assert result['reused_inference'] and len(result['frames']) == 2
        assert len(result['derived_sources']) == (1 if case == 'single' else 2)
        assert result['object_sources']['hand']['origin_run_id'] == 'actual-origin-0'
        if case != 'single':
            assert result['execution_kind'] == 'DERIVED_CACHE_COMBINATION' and result['new_inference_requests'] == 0
            assert result['object_sources']['red']['origin_run_id'] == 'actual-origin-1'
            assert 'elapsed_seconds' not in result and 'device' not in result
        assert set(result['frames'][0]['objects']) == {item['object_id'] for item in request['keyframes']}
        print(case,'verified')
'''


@unittest.skipUnless(Path('/Users/xiaoyao/ComfyUI/.venv/bin/python').is_file(), 'existing segmentation environment unavailable')
class TrackingCacheTests(unittest.TestCase):
    def run_case(self, case):
        directory = Path(__file__).resolve().parents[1] / 'app/integrations/legacy'
        result = subprocess.run(['/Users/xiaoyao/ComfyUI/.venv/bin/python','-B','-c',SCRIPT,str(directory),case],
            env={key:os.environ[key] for key in BASE_ENV_KEYS if key in os.environ},
            capture_output=True,timeout=60)
        self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace')[-2000:])

    def test_single_and_compound_reuse_preserve_real_object_origins_without_model(self):
        for case in ('valid','single','normalized'):
            with self.subTest(case=case):
                self.run_case(case)

    def test_each_cache_identity_frame_object_and_png_must_be_verified(self):
        for case in ('evidence','duplicate','duplicate_within','missing_source','model','provider','code','source','request_source',
                     'interval','annotation','missing_frame','duplicate_frame','missing_object','png','origin','canvas','bitdepth'):
            with self.subTest(case=case):
                self.run_case(case)


if __name__ == '__main__':
    unittest.main()
