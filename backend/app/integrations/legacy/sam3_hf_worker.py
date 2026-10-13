"""Local-only HF SAM3 video checkpoint execution; separate from native Comfy models."""
import argparse
import hashlib
import importlib.metadata
import json
import os
import resource
import struct
import tempfile
import time
from pathlib import Path

import cv2
import numpy as np
import torch
from transformers import CLIPTokenizerFast, Sam3VideoConfig, Sam3VideoModel
from transformers.models.sam2_video.video_processing_sam2_video import Sam2VideoVideoProcessor
from transformers.models.sam3.image_processing_sam3 import Sam3ImageProcessor
from transformers.models.sam3_video.processing_sam3_video import Sam3VideoProcessor

from object_tracking_worker import extract_interval, sha256, write_json


def emit(stage, **fields):
    print('FACTORY_EVENT ' + json.dumps({'stage':'repair','provider':'SAM3_HF_LOCAL','event':stage,**fields},ensure_ascii=False),flush=True)


def checkpoint_schema(path, config):
    with path.open('rb') as handle:
        size=struct.unpack('<Q',handle.read(8))[0]
        if size>16*1024*1024:
            raise ValueError('SAM3_HEADER_TOO_LARGE')
        header=json.loads(handle.read(size))
    tensors={key:value for key,value in header.items() if key!='__metadata__'}
    with torch.device('meta'):
        model=Sam3VideoModel(config)
    expected=model.state_dict()
    result={'local_tensors':len(tensors),'expected_tensors':len(expected),
            'missing':sorted(set(expected)-set(tensors)),
            'unexpected':sorted(set(tensors)-set(expected)),
            'shape_mismatch':{key:{'actual':tensors[key]['shape'],'expected':list(expected[key].shape)}
                              for key in set(tensors)&set(expected) if tensors[key]['shape']!=list(expected[key].shape)}}
    if result['missing'] or result['unexpected'] or result['shape_mismatch']:
        raise ValueError('SAM3_HF_SCHEMA_MISMATCH:'+json.dumps(result))
    return result


def track(source_rgb, start_frame, end_frame, concepts, output_dir, model_path, tokenizer_path, license_path,
          device='mps', task_id=None):
    started=time.monotonic()
    directory=Path(output_dir).resolve()
    directory.mkdir(parents=True,exist_ok=False)
    source,checkpoint=Path(source_rgb).resolve(),Path(model_path).resolve()
    tokenizer_file,license_file=Path(tokenizer_path).resolve(),Path(license_path).resolve()
    if end_frame<=start_frame or start_frame<0:
        raise ValueError('SAM3_INTERVAL_INVALID')
    if len({item['object_id'] for item in concepts})!=len(concepts) or not concepts:
        raise ValueError('SAM3_OBJECT_IDENTITIES_INVALID')
    for item in concepts:
        object_id=item['object_id']
        if not object_id or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in object_id):
            raise ValueError('SAM3_OBJECT_ID_INVALID')
    source_hash,checkpoint_hash=sha256(source),sha256(checkpoint)
    tokenizer_hash,license_hash,worker_hash=sha256(tokenizer_file),sha256(license_file),sha256(Path(__file__))
    config=Sam3VideoConfig()
    schema=checkpoint_schema(checkpoint,config)
    write_json(directory/'schema_evidence.json',schema)
    emit('SCHEMA_VERIFIED',tensor_count=schema['local_tensors'],task_id=task_id)
    dtype=torch.float16 if device=='mps' else torch.float32
    if device=='mps':
        if not torch.backends.mps.is_available():
            raise ValueError('SAM3_MPS_UNAVAILABLE')
        torch.mps.set_per_process_memory_fraction(0.55)
    # The local bundle references existing bytes; no checkpoint copy or Hub request.
    with tempfile.TemporaryDirectory(prefix='factory-sam3-hf-') as bundle_name:
        bundle=Path(bundle_name)
        (bundle/'config.json').write_text(config.to_json_string(),encoding='utf-8')
        (bundle/'model.safetensors').symlink_to(checkpoint)
        emit('WEIGHTS_LOADING',device=device,dtype=str(dtype),task_id=task_id)
        model,loading=Sam3VideoModel.from_pretrained(bundle,local_files_only=True,dtype=dtype,
                                                    output_loading_info=True,attn_implementation='sdpa')
    loading={key:sorted(value) if isinstance(value,set) else value for key,value in loading.items()}
    if loading.get('missing_keys') or loading.get('unexpected_keys') or loading.get('mismatched_keys') or loading.get('error_msgs'):
        write_json(directory/'loading_evidence.json',loading)
        raise ValueError('SAM3_STRICT_WEIGHT_LOAD_FAILED')
    write_json(directory/'loading_evidence.json',{'strict_schema_verified':True,'load_state':loading,
               'weights_loaded':True,'model_sha256':checkpoint_hash,'dtype':str(dtype)})
    emit('WEIGHTS_LOADED',task_id=task_id)
    model=model.to(device)
    paths,width,height=extract_interval(source,start_frame,end_frame,directory/'analysis_frames')
    frames=[cv2.cvtColor(cv2.imread(str(path)),cv2.COLOR_BGR2RGB) for path in paths]
    tokenizer=CLIPTokenizerFast(tokenizer_file=str(tokenizer_file),model_max_length=32)
    processor=Sam3VideoProcessor(image_processor=Sam3ImageProcessor(),
        video_processor=Sam2VideoVideoProcessor(image_mean=[0.5,0.5,0.5],image_std=[0.5,0.5,0.5],
                                                size={'height':1008,'width':1008}),tokenizer=tokenizer)
    session=processor.init_video_session(video=frames,inference_device=device,inference_state_device='cpu',
        processing_device='cpu',video_storage_device='cpu',max_vision_features_cache_size=1,dtype=dtype)
    for item in concepts:
        processor.add_text_prompt(session,item['text'])
    outputs=[]
    selected_ids={}
    peak_allocated=peak_driver=0
    emit('PROPAGATING',frames=len(paths),concepts=[item['text'] for item in concepts],task_id=task_id)
    for output in model.propagate_in_video_iterator(session,max_frame_num_to_track=len(paths),show_progress_bar=True):
        processed=processor.postprocess_outputs(session,output)
        frame={'frame_index':start_frame+output.frame_idx,'objects':{}}
        for item in concepts:
            model_ids=processed['prompt_to_obj_ids'].get(item['text'],[])
            previous=selected_ids.get(item['object_id'])
            if previous is None:
                if len(model_ids)!=1:
                    raise ValueError(f'SAM3_CONCEPT_IDENTITY_UNCONFIRMED:{item["object_id"]}:{len(model_ids)}')
                selected_ids[item['object_id']]=previous=int(model_ids[0])
            if previous not in model_ids:
                raise ValueError('SAM3_OBJECT_LOST:'+item['object_id'])
            index=processed['object_ids'].tolist().index(previous)
            mask=processed['masks'][index].detach().cpu().numpy().squeeze()
            encoded=(mask>0).astype(np.uint8)*255
            if encoded.shape!=(height,width) or not encoded.any():
                raise ValueError('SAM3_OBJECT_MASK_EMPTY:'+item['object_id'])
            path=directory/'objects'/item['object_id']/f'{frame["frame_index"]:09d}.png'
            path.parent.mkdir(parents=True,exist_ok=True)
            if not cv2.imwrite(str(path),encoded):
                raise OSError('SAM3_MASK_WRITE_FAILED')
            frame['objects'][item['object_id']]={'mask_path':str(path),'sha256':sha256(path),
                'model_object_id':previous,'concept':item['text'],'area':int((encoded>0).sum()),
                'score':float(processed['scores'][index])}
        outputs.append(frame)
        if device=='mps':
            peak_allocated=max(peak_allocated,torch.mps.current_allocated_memory())
            peak_driver=max(peak_driver,torch.mps.driver_allocated_memory())
        emit('FRAME_SAVED',frame_index=frame['frame_index'],task_id=task_id)
    if {frame['frame_index'] for frame in outputs}!=set(range(start_frame,end_frame)):
        raise ValueError('SAM3_TRACK_FRAME_COVERAGE_INCOMPLETE')
    result={'status':'READY','provider':'SAM3_HF_LOCAL','native_comfy_model':False,'license':'SAM License',
            'license_path':str(license_file),'license_sha256':license_hash,
            'tokenizer_path':str(tokenizer_file),'tokenizer_sha256':tokenizer_hash,'worker_sha256':worker_hash,
            'model_path':str(checkpoint),'model_sha256':checkpoint_hash,'source_rgb':str(source),'source_sha256':source_hash,
            'transformers_version':importlib.metadata.version('transformers'),
            'conversion_reference':'https://github.com/huggingface/transformers/blob/main/src/transformers/models/sam3_video/convert_sam3_video_to_hf.py',
            'checkpoint_schema':schema,'strict_weight_load':loading,'device':device,'dtype':str(dtype),
            'start_frame':start_frame,'end_frame':end_frame,'interval':'half_open','width':width,'height':height,
            'frame_count':len(outputs),'direction':'forward','object_ids':[item['object_id'] for item in concepts],
            'concepts':concepts,'object_binding':selected_ids,'frames':outputs,'task_id':task_id,
            'mask_format':'PNG8 binary object segmentation; not fine matting alpha',
            'analysis_input':'selected source RGB decoded to JPEG95 for model input; source mother unchanged',
            'elapsed_seconds':time.monotonic()-started,'peak_worker_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            'peak_mps_allocated_bytes':peak_allocated,'peak_mps_driver_bytes':peak_driver,
            'mps_memory_measurement':'maximum observed at emitted frames; process RSS is OS high-water mark',
            'automatic_semantic_pass':False,'scope':'BOUNDED_TECHNICAL_VALIDATION_NOT_PRODUCTION_SUCCESS'}
    write_json(directory/'manifest.json',result)
    return result


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--request',required=True)
    parser.add_argument('--result',required=True)
    args=parser.parse_args()
    for key in list(os.environ):
        if key not in {'PATH','LANG','LC_ALL','TMPDIR','PYTORCH_ENABLE_MPS_FALLBACK'}:
            os.environ.pop(key,None)
    os.environ.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',HF_HUB_DISABLE_TELEMETRY='1',PYTORCH_ENABLE_MPS_FALLBACK='1')
    try:
        result=track(**json.loads(Path(args.request).read_text()))
    except (ValueError,RuntimeError,OSError,TypeError,KeyError) as error:
        write_json(Path(args.result),{'status':'FAILED','provider':'SAM3_HF_LOCAL','error_code':type(error).__name__,'error_message':str(error)})
        raise
    write_json(Path(args.result),result)


if __name__=='__main__':
    main()
