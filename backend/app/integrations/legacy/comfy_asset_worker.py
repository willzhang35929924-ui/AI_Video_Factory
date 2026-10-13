"""Two persistent foreground workflows; legacy business/configuration stays read-only."""
from __future__ import annotations

import argparse
import bisect
import copy
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from fractions import Fraction
from pathlib import Path

import numpy as np

from video_worker import base_result, digest, emit, load_legacy, safe, write_json


FACTORY_ROOT = Path(__file__).resolve().parents[4]


def command(arguments, timeout=600):
    process = subprocess.run(arguments, shell=False, capture_output=True, check=False, timeout=timeout)
    if process.returncode:
        raise RuntimeError(Path(arguments[0]).name + ': ' + process.stderr.decode(errors='replace')[-1500:])
    return process.stdout


def probe(path, count=True):
    # FFV1 has one packet per independently coded frame. Count packets cheaply;
    # READY still requires a separate complete ffmpeg decode below.
    arguments = ['ffprobe', '-v', 'error'] + (['-count_packets'] if count else [])
    data = json.loads(command(arguments + ['-show_streams', '-show_format', '-of', 'json', str(path)]))
    video = next(stream for stream in data['streams'] if stream['codec_type'] == 'video')
    rate = video.get('avg_frame_rate') if video.get('avg_frame_rate') not in (None, '0/0') else video['r_frame_rate']
    frames = int(video.get('nb_frames') or video.get('nb_read_packets') or 0)
    duration = float(video['duration']) if video.get('duration') else (frames / float(Fraction(rate)) if frames else float(data['format'].get('duration') or 0))
    return {'width': video['width'], 'height': video['height'], 'fps': float(Fraction(rate)), 'fps_rational': rate,
            'r_frame_rate': video.get('r_frame_rate'), 'duration': duration, 'container_duration': float(data['format'].get('duration') or 0),
            'frame_count': frames, 'codec': video.get('codec_name'), 'pix_fmt': video.get('pix_fmt'),
            'rotation': next((entry.get('rotation', 0) for entry in video.get('side_data_list', []) if 'rotation' in entry), 0),
            'audio': [{key: stream.get(key) for key in ('codec_name', 'sample_rate', 'channels', 'start_time', 'duration')}
                      for stream in data['streams'] if stream['codec_type'] == 'audio'], 'video_start_time': video.get('start_time')}


def frame_timestamps(path):
    data = json.loads(command(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_frames',
        '-show_entries', 'frame=best_effort_timestamp_time,pkt_duration_time', '-of', 'json', str(path)]))
    return [float(frame['best_effort_timestamp_time']) for frame in data['frames'] if 'best_effort_timestamp_time' in frame]


def decoded_frame_hashes(path, pixel_format):
    output = command(['ffmpeg', '-v', 'error', '-xerror', '-i', str(path), '-map', '0:v:0', '-an',
                      '-pix_fmt', pixel_format, '-fps_mode', 'passthrough', '-f', 'framehash', '-hash', 'sha256', '-'])
    return [line.split(',')[-1].strip() for line in output.decode().splitlines() if line and not line.startswith('#')]


def decode_verify(path):
    command(['ffmpeg', '-v', 'error', '-xerror', '-i', str(path), '-map', '0:v:0', '-an', '-f', 'null', '-'])


def comfy_source_path(source, input_directory):
    """Authorize transport through ComfyUI input without duplicating video bytes."""
    source = Path(source).resolve()
    directory = Path(input_directory).resolve() / 'factory-source-links'
    directory.mkdir(parents=True, exist_ok=True)
    linked = directory / (digest(source) + '-' + str(source.stat().st_ino) + source.suffix.lower())
    if linked.exists():
        if not linked.samefile(source):
            raise ValueError('canonical_source_link_identity_changed')
    else:
        os.link(source, linked)
    return linked


def alpha_statistics(path, media):
    arguments = ['ffmpeg', '-v', 'error', '-xerror', '-i', str(path), '-an', '-fps_mode', 'passthrough',
                 '-f', 'rawvideo', '-pix_fmt', 'gray16le', '-']
    frame_bytes = media['width'] * media['height'] * 2
    process = subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False)
    frames = []
    try:
        while True:
            raw = process.stdout.read(frame_bytes)
            if not raw:
                break
            if len(raw) != frame_bytes:
                raise RuntimeError('alpha_decode_incomplete_frame')
            values = np.frombuffer(raw, dtype='<u2')
            frames.append({'frame': len(frames), 'minimum': int(values.min()), 'maximum': int(values.max()),
                           'area_fraction': float(np.count_nonzero(values >= 32768) / values.size),
                           'fractional_pixels': int(np.count_nonzero((values > 0) & (values < 65535)))})
        error = process.stderr.read()
        if process.wait(timeout=30):
            raise RuntimeError('alpha_decode_failed: ' + error.decode(errors='replace')[-1000:])
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    if len(frames) != media['frame_count']:
        raise RuntimeError('alpha_frame_count_mismatch')
    if not frames or all(frame['maximum'] == 0 for frame in frames) or all(frame['minimum'] == 65535 for frame in frames):
        raise RuntimeError('alpha_is_degenerate')
    return frames


def workflow(path, stage):
    graph = json.loads(Path(path).read_text(encoding='utf-8'))
    for node in graph.values():
        for name, value in node.get('inputs', {}).items():
            if value and any(part in name.lower() for part in ('api_key', 'token', 'secret', 'password')):
                raise ValueError('workflow_snapshot_contains_secret')
    classes = {node['class_type'] for node in graph.values()}
    if stage == 'compose' and classes & {'BiRefNetRMBG', 'VideoAutoMatteRepair', 'MatAnyone2'}:
        raise ValueError('compose_workflow_contains_cutout_inference')
    if stage == 'compose' and not {'FactoryLoadForegroundV1', 'RepeatImageBatch', 'ImageCompositeMasked'}.issubset(classes):
        raise ValueError('compose_workflow_requires_repeated_background_batch')
    if stage == 'cutout' and not {'VHS_VideoCombine', 'VideoAutoMatteRepair', 'BiRefNetRMBG'}.issubset(classes):
        raise ValueError('cutout_workflow_contract_mismatch')
    return graph


def implementation_fingerprint(config, stage, graph):
    comfy = Path(config['comfy_input_dir']).resolve().parent
    paths = [comfy / 'comfy_extras/nodes_mask.py']
    if stage == 'cutout':
        paths += [comfy / 'custom_nodes/video_auto_matting/__init__.py',
                  comfy / 'custom_nodes/ComfyUI-RMBG/py/AILab_BiRefNet.py',
                  comfy / 'models/RMBG/BiRefNet' / (graph['11']['inputs']['model'] + '.safetensors')]
        if graph['14']['class_type'] == 'FactoryLoadCFRContextV1':
            paths.append(comfy / 'custom_nodes/factory_foreground_assets/__init__.py')
    else:
        paths += [comfy / 'custom_nodes/factory_foreground_assets/__init__.py', comfy / 'comfy_extras/nodes_images.py',
                  comfy / 'nodes.py',
                  comfy / 'custom_nodes/ComfyUI-VideoHelperSuite/videohelpersuite/nodes.py',
                  comfy / 'custom_nodes/ComfyUI-VideoHelperSuite/videohelpersuite/load_video_nodes.py',
                  comfy / 'custom_nodes/ComfyUI-VideoHelperSuite/video_formats/h264-mp4.json',
                  comfy / 'custom_nodes/ComfyUI_LayerStyle/py/color_correct_brightness_and_contrast.py',
                  comfy / 'custom_nodes/ComfyUI_LayerStyle/py/imagefunc.py']
    return {'factory_algorithm_version': 'foreground-context-v1' if stage == 'cutout' else 'straight-master-compose-v5-av-origin',
            **{str(path.relative_to(comfy)): digest(path) for path in paths if path.is_file()}}


def record_history(client, graph, directory, stage, context):
    directory.mkdir(parents=True, exist_ok=True)
    graph_path = directory / (stage + '.api.json')
    data = json.dumps(graph, ensure_ascii=False, indent=2).encode()
    if graph_path.exists() and graph_path.read_bytes() != data:
        raise RuntimeError('submitted_workflow_snapshot_changed')
    if not graph_path.exists():
        graph_path.write_bytes(data)
    outputs = client.execute(graph, directory, stage)
    state = json.loads((directory / (stage + '.json')).read_text())
    prompt_id = state.get('prompt_id')
    if not prompt_id or state.get('status') != 'completed':
        raise RuntimeError('comfy_execution_not_confirmed')
    history_path = directory / (stage + '.history.json')
    if history_path.exists():
        original = json.loads(history_path.read_text())
        history = original['history']
        if (original['prompt_id'] != prompt_id or original['submitted_graph_sha256'] != hashlib.sha256(data).hexdigest()
                or history.get('prompt', [None, None, None])[2] != graph):
            raise RuntimeError('completed_execution_evidence_changed')
        if not history.get('status', {}).get('completed') or history['status'].get('status_str') == 'error':
            raise RuntimeError('saved_comfy_history_not_completed')
        return outputs, original
    history = client.request('/history/' + prompt_id).get(prompt_id)
    if not history or not history.get('status', {}).get('completed') or history.get('status', {}).get('status_str') == 'error':
        raise RuntimeError('comfy_history_not_completed')
    if history.get('prompt', [None, None, None])[2] != graph or history.get('outputs', {}) != outputs:
        raise RuntimeError('comfy_history_execution_mismatch')
    evidence = {'task_id': context.get('task_id'), 'processing_run_id': context.get('run_id'), 'stage': stage,
                'prompt_id': prompt_id, 'submitted_graph_sha256': hashlib.sha256(data).hexdigest(),
                'workflow_path': str(graph_path), 'history': history, 'verified_at': time.time()}
    write_json(history_path, evidence)
    return outputs, evidence


def result_path(client, outputs, node, suffix):
    record = next((record for record in outputs.get(node, {}).get('gifs', []) if record['filename'].endswith(suffix)), None)
    if not record:
        raise RuntimeError('comfy_output_missing:' + node)
    return client.output_path(record)


def aac_sample_clock(audio):
    data = json.loads(command(['ffprobe', '-v', 'error', '-select_streams', 'a:0', '-show_streams', '-show_frames',
        '-show_entries', 'stream=codec_name,sample_rate,initial_padding,start_time:frame=nb_samples', '-of', 'json', str(audio)]))
    stream = data['streams'][0]
    if stream['codec_name'] != 'aac':
        return None
    rate = int(stream['sample_rate'])
    packet_samples = max(int(frame['nb_samples']) for frame in data['frames'])
    padding = int(stream.get('initial_padding') or 0)
    offset = round(float(stream.get('start_time') or 0) * rate) - padding
    return {'sample_rate': rate, 'packet_samples': packet_samples, 'initial_padding': padding,
            'start_samples': offset,
            'filter': f'setts=ts=N*{packet_samples}+({offset}):duration={packet_samples}:time_base=1/{rate}:prescale=1'}


def verify_composition_audio(mother, composition):
    def evidence(path):
        pcm = command(['ffmpeg', '-v', 'error', '-xerror', '-i', str(path), '-map', '0:a:0',
                       '-f', 'hash', '-hash', 'sha256', '-']).decode().strip()
        data = json.loads(command(['ffprobe', '-v', 'error', '-select_streams', 'a:0', '-show_packets',
            '-show_entries', 'packet=data_hash', '-show_data_hash', 'sha256', '-of', 'json', str(path)]))
        return pcm, [packet['data_hash'] for packet in data['packets']]
    source_pcm, source_packets = evidence(mother)
    output_pcm, output_packets = evidence(composition)
    if source_pcm != output_pcm or source_packets != output_packets:
        raise RuntimeError('composition_mother_audio_content_mismatch')
    return {'source': 'mother_rgb', 'pcm_sha256': source_pcm, 'packet_count': len(source_packets),
            'packets_identical': True, 'pcm_identical': True}


def verify_composition_timeline(composition, source, output):
    if (output['frame_count'] != source['frame_count'] or abs(output['fps'] - source['fps']) > .001
            or abs(output['duration'] - source['frame_count'] / source['fps']) > 1 / source['fps']):
        raise RuntimeError('composition_time_alignment_failed')
    if source['video_start_time'] is None or output['video_start_time'] is None:
        raise RuntimeError('composition_video_origin_unknown')
    origin = float(source['video_start_time'])
    if abs(float(output['video_start_time']) - origin) > .000001:
        raise RuntimeError('composition_video_origin_mismatch')
    timestamps = frame_timestamps(composition)
    if len(timestamps) != source['frame_count'] or any(
            abs(at - origin - index / source['fps']) > .0015 for index, at in enumerate(timestamps)):
        raise RuntimeError('composition_frame_clock_mismatch')
    if source['audio']:
        if len(output['audio']) != len(source['audio']):
            raise RuntimeError('composition_audio_missing')
        for before, after in zip(source['audio'], output['audio']):
            if before['start_time'] is None or after['start_time'] is None:
                raise RuntimeError('composition_audio_origin_unknown')
            if abs(float(before['start_time']) - float(after['start_time'])) > 1 / int(before['sample_rate']) + .000001:
                raise RuntimeError('composition_audio_origin_mismatch')
    return {'video_origin_s': origin, 'video_origin_identical': True, 'audio_origins_identical': bool(source['audio']),
            'frame_count': len(timestamps), 'frame_timestamps_sha256': hashlib.sha256(json.dumps(timestamps).encode()).hexdigest(),
            'all_frame_timestamps_verified': True}


def concat_paths(paths, output, directory, audio=None, lossless_fps=None, presentation_fps=None):
    listing = directory / (output.stem + '.concat.txt')
    listing.write_text(''.join("file '" + str(path).replace("'", "'\\''") + "'\n" for path in paths), encoding='utf-8')
    arguments = ['ffmpeg', '-n', '-v', 'error', '-f', 'concat', '-safe', '0', '-i', str(listing)]
    if audio:
        arguments += ['-i', str(audio), '-map', '0:v:0', '-map', '1:a:0', '-c:a', 'copy']
        if presentation_fps:
            clock = aac_sample_clock(audio)
            if clock:
                arguments += ['-bsf:a', clock['filter']]
    if lossless_fps:
        rate = Fraction(lossless_fps)
        arguments += ['-bsf:v', f'setts=ts=N/(({rate.numerator}/{rate.denominator})*TB):duration=1/(({rate.numerator}/{rate.denominator})*TB)']
    elif presentation_fps:
        rate = Fraction(presentation_fps)
        clock = f'({rate.numerator}/{rate.denominator})*TB'
        arguments += ['-bsf:v', f'setts=pts=round((PTS-STARTPTS)*{clock})/({clock}):dts=round((DTS-STARTPTS)*{clock})/({clock}):duration=1/({clock})']
    arguments += ['-c:v', 'copy', str(output)]
    command(arguments)


def normalize(source, directory):
    original = probe(source)
    timestamps = frame_timestamps(source)
    if len(timestamps) != original['frame_count']:
        raise RuntimeError('source_timestamp_frame_count_mismatch')
    interval = 1 / original['fps']
    variable = any(abs(timestamps[index + 1] - timestamps[index] - interval) > max(.001, interval * .02)
                   for index in range(len(timestamps) - 1))
    if not variable and not original['rotation']:
        normalized_source, media = source, original
        indices = list(range(len(timestamps)))
    else:
        timeline = directory.parent.parent / 'timelines' / digest(source)
        timeline.mkdir(parents=True, exist_ok=True)
        normalized_source = timeline / 'source_timeline_ffv1.mkv'
        old = next(directory.parent.glob('*.partial/source_timeline_ffv1.mkv'), None)
        if not normalized_source.exists() and old:
            old_mapping = old.parent / 'time_mapping.json'
            previous = json.loads(old_mapping.read_text()) if old_mapping.exists() else {}
            if (previous.get('source_sha256') == digest(source) and previous.get('original') == original
                    and previous.get('original_frame_timestamps_s') == timestamps):
                os.link(old, normalized_source)
        if not normalized_source.exists():
            command(['ffmpeg', '-n', '-v', 'error', '-i', str(source), '-an', '-vf', 'fps=' + original['fps_rational'],
                     '-c:v', 'ffv1', '-level', '3', '-pix_fmt', 'rgba64le', str(normalized_source)])
        media = probe(normalized_source)
        elapsed = [value - timestamps[0] for value in timestamps]
        indices = []
        for index in range(media['frame_count']):
            at = index / media['fps']
            position = bisect.bisect_left(elapsed, at)
            choices = [candidate for candidate in (position - 1, position) if 0 <= candidate < len(elapsed)]
            indices.append(min(choices, key=lambda candidate: abs(elapsed[candidate] - at)))
    mapping = {'source_sha256': digest(source), 'original': original, 'normalized': media, 'normalization_applied': normalized_source != source,
               'reason': 'VFR/rotation canonicalization' if normalized_source != source else 'CFR source timeline preserved',
               'original_frame_timestamps_s': timestamps, 'normalized_to_source_frame': indices,
               'normalized_frame_timestamps_s': [index / media['fps'] for index in range(media['frame_count'])]}
    write_json(directory / 'time_mapping.json', mapping)
    return normalized_source, media, mapping


def presentation_timestamps_verify(rgb, alpha, media):
    rgb_pts, alpha_pts = frame_timestamps(rgb), frame_timestamps(alpha)
    if rgb_pts != alpha_pts or len(rgb_pts) != media['frame_count']:
        raise RuntimeError('foreground_presentation_timestamps_mismatch')
    if any(abs(at - index / media['fps']) > .0015 for index, at in enumerate(rgb_pts)):
        raise RuntimeError('foreground_cfr_presentation_timestamps_failed')


def paired_verify(rgb, alpha, media):
    rgb_media, alpha_media = probe(rgb), probe(alpha)
    for checked in (rgb_media, alpha_media):
        if any(checked[key] != media[key] for key in ('width', 'height', 'frame_count')) or abs(checked['fps'] - media['fps']) > .001:
            raise RuntimeError('foreground_alignment_failed')
        if abs(checked['frame_count'] / checked['fps'] - media['frame_count'] / media['fps']) > 1 / media['fps']:
            raise RuntimeError('foreground_duration_failed')
        decode_verify(rgb if checked is rgb_media else alpha)
    if rgb_media['codec'] != 'ffv1' or alpha_media['codec'] != 'ffv1' or alpha_media['pix_fmt'] != 'gray16le':
        raise RuntimeError('foreground_lossless_contract_failed')
    presentation_timestamps_verify(rgb, alpha, media)
    return rgb_media, alpha_media


def foreground_previews(directory, media, rgb_path=None, alpha_path=None):
    previews = []
    rgb_path = Path(rgb_path) if rgb_path else directory / 'RGB.mkv'
    alpha_path = Path(alpha_path) if alpha_path else directory / 'Alpha.mkv'
    width, height, fps = media['width'], media['height'], media['fps_rational']
    for name, color in (('black', 'black'), ('white', 'white'), ('bright', 'magenta'),
                        ('source', None), ('alpha_overlay', '0x42E89B')):
        output = directory / ('Preview_' + name + '.mp4')
        if not output.exists():
            arguments = ['ffmpeg', '-n', '-v', 'error', '-i', str(rgb_path)]
            if name == 'source':
                arguments += ['-map', '0:v:0', '-vf', f'settb=expr=1/({fps}),setpts=N']
            else:
                arguments += ['-i', str(alpha_path), '-f', 'lavfi', '-i', f'color={color}:s={width}x{height}:r={fps}']
                timeline = f'[0:v]settb=expr=1/({fps}),setpts=N[rgb];[1:v]settb=expr=1/({fps}),setpts=N'
                filtering = (timeline + ',format=gray,lut=y=val*0.28[a];[2:v][a]alphamerge[tint];'
                             '[rgb][tint]overlay=shortest=1:format=auto[video]') if name == 'alpha_overlay' else (
                             timeline + '[alpha];[rgb][alpha]alphamerge[person];[2:v][person]overlay=shortest=1:format=auto[video]')
                arguments += ['-filter_complex', filtering, '-map', '[video]']
            command(arguments + ['-map', '0:a?', '-frames:v', str(media['frame_count']),
                '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-threads', '2', '-pix_fmt', 'yuv420p',
                '-c:a', 'copy', '-movflags', '+faststart', str(output)])
        checked = probe(output)
        decode_verify(output)
        if checked['frame_count'] != media['frame_count'] or abs(checked['fps'] - media['fps']) > .001:
            raise RuntimeError('foreground_preview_alignment_failed')
        previews.append({'background': name, 'filename': output.name, 'sha256': digest(output),
                         'media': checked, 'full_decode_verified': True, 'diagnostic_only': True})
    return previews


def foreground_evidence(manifest, root):
    return {'asset_type': 'PERSON_VIDEO', 'path': str(root / 'RGB.mkv'), 'sha256': manifest['rgb_sha256'],
            'source_system': 'Factory ComfyUI cutout', 'legacy_reference': {
                'alpha_path': str(root / 'Alpha.mkv'), 'alpha_sha256': manifest['alpha_sha256'],
                'manifest_path': str(root / 'manifest.json'), 'alpha_mode': 'straight',
                'rgb_source': 'uncomposited original RGB from VideoAutoMatteRepair slot 0',
                'source_sha256': manifest['source_sha256'], 'source_audio_path': manifest['source_audio_path'],
                'input_fingerprint': manifest['input_fingerprint'], 'workflow_sha256': manifest['workflow_sha256'],
                'frame_hashes_path': str(root / 'frame_hashes.json'), 'time_mapping_path': str(root / 'time_mapping.json'),
                'preview_path': str(root / manifest['previews'][0]['filename']),
                'preview_sha256': manifest['previews'][0]['sha256'],
                'source_binding_path': str(root / 'source_binding.json') if manifest.get('resolved_source') else None,
                'legacy_video_id': manifest.get('resolved_source', {}).get('video_id'),
                'full_cutout_runs': 1, 'object_strategy': manifest['object_strategy']}}


def cutout_identity(context, config, graph, workflow_path, chunk_frames):
    source = Path(context['source_video']).resolve()
    if not source.is_file():
        raise ValueError('source_video_missing')
    source_hash = digest(source)
    implementations = implementation_fingerprint(config, 'cutout', graph)
    identity_inputs = {'source': source_hash, 'workflow': digest(workflow_path), 'implementations': implementations,
        'object_strategy': context.get('object_strategy') or {'person': 'BiRefNet-general full foreground'},
        'chunk_frames': chunk_frames, 'loader_precision': 'CV source 8bit; lossless 16bit container'}
    if context.get('cutout_generation', 1) > 1:
        identity_inputs['cutout_generation'] = context['cutout_generation']
    identity = hashlib.sha256(json.dumps(identity_inputs, sort_keys=True).encode()).hexdigest()
    return source, source_hash, implementations, identity


def cutout_chunk_graph(graph, source, media, context, identity, start, chunk_frames):
    count = min(chunk_frames, media['frame_count'] - start)
    radius = int(graph['38']['inputs']['temporal_radius'])
    first, last = max(0, start - radius), min(media['frame_count'], start + count + radius)
    run_graph = copy.deepcopy(graph)
    if run_graph['14']['class_type'] == 'FactoryLoadCFRContextV1':
        run_graph['14']['inputs'].update(video=str(source), frame_count=last - first, start_frame=first)
    else:
        run_graph['14']['inputs'].update(video=str(source), force_rate=0, frame_load_cap=last - first, skip_first_frames=first)
    run_graph['38']['inputs'].update(start_frame=start - first, frame_count=count)
    for node, name in (('save_rgb', 'RGB'), ('save_alpha', 'Alpha')):
        run_graph[node]['inputs']['filename_prefix'] = f'factory/{context["task_id"]}/{identity}/chunks/{start:06d}_{name}'
    return run_graph, count


def cutout_receipt(client, graph, directory, stage, media, count):
    state_path, graph_path = directory / (stage + '.json'), directory / (stage + '.api.json')
    state = json.loads(state_path.read_text())
    if json.loads(graph_path.read_text()) != graph:
        raise RuntimeError('cutout_checkpoint_workflow_changed')
    if state['graph_fingerprint'] != hashlib.sha256(json.dumps(graph, sort_keys=True).encode()).hexdigest():
        raise RuntimeError('cutout_checkpoint_journal_changed')
    history_path = directory / (stage + '.history.json')
    history = None
    prompt_id = state.get('prompt_id')
    if history_path.is_file():
        evidence = json.loads(history_path.read_text())
        if (state.get('status') != 'completed' or evidence['prompt_id'] != prompt_id
                or evidence['submitted_graph_sha256'] != digest(graph_path)):
            raise RuntimeError('cutout_checkpoint_evidence_changed')
        history = evidence['history']
    else:
        if prompt_id:
            history = client.request('/history/' + prompt_id).get(prompt_id)
        if not history:
            queue = client.request('/queue')
            items = queue['queue_running'] + queue['queue_pending']
            found = next((item for item in items if (prompt_id and item[1] == prompt_id)
                or (len(item) > 3 and item[3].get('video_auto_job') == state.get('token'))), None)
            if found:
                if found[2] != graph:
                    raise RuntimeError('cutout_checkpoint_live_graph_changed')
                return {'status': 'RUNNING', 'prompt_id': found[1], 'journal_sha256': digest(state_path)}
            if not prompt_id:
                for candidate_id, candidate in client.request('/history?max_items=1000').items():
                    prompt = candidate.get('prompt', [])
                    if len(prompt) > 3 and prompt[3].get('video_auto_job') == state.get('token'):
                        prompt_id, history = candidate_id, candidate
                        break
    if history:
        if history.get('prompt', [None, None, None])[2] != graph:
            raise RuntimeError('cutout_checkpoint_history_graph_changed')
        status = history.get('status', {})
        if status.get('status_str') == 'error' or state.get('status') == 'failed':
            raise RuntimeError('cutout_checkpoint_execution_failed')
        if not status.get('completed'):
            raise RuntimeError('cutout_checkpoint_history_incomplete')
        outputs = history.get('outputs', {})
        if state.get('status') == 'completed' and state.get('outputs') != outputs:
            raise RuntimeError('cutout_checkpoint_outputs_changed')
        verified = []
        for node in ('save_rgb', 'save_alpha'):
            path = result_path(client, outputs, node, '.mkv')
            checked = probe(path)
            if (checked['frame_count'] != count or checked['codec'] != 'ffv1'
                    or any(checked[key] != media[key] for key in ('width', 'height'))
                    or abs(checked['fps'] - media['fps']) > .001):
                raise RuntimeError('cutout_checkpoint_chunk_alignment_failed')
            verified.append({'path': str(path), 'sha256': digest(path), 'frame_count': count})
        return {'status': 'COMPLETED', 'prompt_id': prompt_id, 'journal_sha256': digest(state_path),
                'history_sha256': digest(history_path) if history_path.exists() else None, 'outputs': verified}
    if state.get('status') not in {'queued', 'submitting', 'submission_uncertain'}:
        raise RuntimeError('cutout_checkpoint_receipt_unrecoverable')
    return {'status': 'RECEIPT_LOST', 'prompt_id': prompt_id, 'journal_sha256': digest(state_path)}


def cutout_recovery_graph(graph, directory, stage):
    pointer = directory / (stage + '.resume.json')
    if not pointer.exists():
        return graph, directory
    recovery = json.loads(pointer.read_text())
    if recovery['base_graph_sha256'] != hashlib.sha256(json.dumps(graph, ensure_ascii=False, indent=2).encode()).hexdigest():
        raise RuntimeError('cutout_checkpoint_recovery_graph_changed')
    recovered_directory = (directory / recovery['directory']).resolve()
    if not recovered_directory.is_relative_to(directory.resolve()):
        raise RuntimeError('cutout_checkpoint_recovery_outside_root')
    if len(recovery['attempt']) != 32 or any(character not in '0123456789abcdef' for character in recovery['attempt']):
        raise RuntimeError('cutout_checkpoint_recovery_attempt_invalid')
    recovered_graph = copy.deepcopy(graph)
    for node in ('save_rgb', 'save_alpha'):
        recovered_graph[node]['inputs']['filename_prefix'] += '/checkpoint_' + recovery['attempt']
    return recovered_graph, recovered_directory


def cutout_checkpoint(context, directory, config, graph, workflow_path, chunk_frames, client):
    source, source_hash, implementations, identity = cutout_identity(context, config, graph, workflow_path, chunk_frames)
    partial = directory / 'foreground' / (identity + '.partial')
    if not partial.is_dir():
        raise RuntimeError('cutout_checkpoint_partial_missing')
    mapping = json.loads((partial / 'time_mapping.json').read_text())
    if mapping['source_sha256'] != source_hash:
        raise RuntimeError('cutout_checkpoint_source_changed')
    media = mapping['normalized']
    first_graph = json.loads((partial / 'comfy/cutout_000000.api.json').read_text())
    canonical_input = Path(first_graph['14']['inputs']['video']).resolve()
    normalized = source if not mapping['normalization_applied'] else directory / 'timelines' / source_hash / 'source_timeline_ffv1.mkv'
    if not normalized.exists():
        normalized = partial / 'source_timeline_ffv1.mkv'
    if not canonical_input.is_file() or not canonical_input.samefile(normalized):
        raise RuntimeError('cutout_checkpoint_transport_changed')
    if probe(normalized) != media:
        raise RuntimeError('cutout_checkpoint_timeline_changed')
    chunks, saw_unsubmitted = [], False
    for start in range(0, media['frame_count'], chunk_frames):
        stage = f'cutout_{start:06d}'
        base_graph, count = cutout_chunk_graph(graph, canonical_input, media, context, identity, start, chunk_frames)
        chosen_graph, journal = cutout_recovery_graph(base_graph, partial / 'comfy', stage)
        state_path = journal / (stage + '.json')
        if not state_path.exists():
            if chosen_graph != base_graph:
                # The recovery reservation exists, but POST has not begun.
                chunks.append({'start_frame': start, 'frame_count': count, 'status': 'RESERVED',
                               'journal_directory': str(journal)})
            else:
                saw_unsubmitted = True
            continue
        if saw_unsubmitted:
            raise RuntimeError('cutout_checkpoint_noncontiguous_journal')
        receipt = cutout_receipt(client, chosen_graph, journal, stage, media, count)
        chunks.append({'start_frame': start, 'frame_count': count, 'journal_directory': str(journal), **receipt})
    if not chunks:
        raise RuntimeError('cutout_checkpoint_no_submitted_chunks')
    plan = {'task_id': context['task_id'], 'context_id': context['context_id'],
            'generation': context.get('cutout_generation', 1), 'input_fingerprint': identity,
            'source_sha256': source_hash, 'workflow_sha256': digest(workflow_path),
            'implementation_hashes': implementations, 'partial_directory': str(partial), 'chunks': chunks,
            'total_frames': media['frame_count'], 'completed_frames': sum(chunk['frame_count'] for chunk in chunks if chunk['status'] == 'COMPLETED'),
            'receipt_lost_frames': sum(chunk['frame_count'] for chunk in chunks if chunk['status'] == 'RECEIPT_LOST'),
            'business_executed': False, 'paid_requests': 0, 'comfy_submissions': 0}
    plan['checkpoint_fingerprint'] = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
    return plan


def resume_cutout_chunk(client, graph, directory, stage, context, receipt):
    graph, journal = cutout_recovery_graph(graph, directory, stage)
    if receipt['status'] != 'RECEIPT_LOST':
        return record_history(client, graph, journal, stage, context)
    attempt = uuid.uuid4().hex
    recovery_directory = directory / 'checkpoint_recovery' / stage / attempt
    recovery_directory.mkdir(parents=True)
    prior = json.loads((journal / (stage + '.json')).read_text())
    # Original journals and orphan media remain untouched. A new namespace can
    # only become completed through a fresh, real ComfyUI history receipt.
    write_json(recovery_directory / 'recovery.json', {'reason': 'LOCAL_COMFY_RECEIPT_LOST',
        'prior_journal_path': str(journal / (stage + '.json')), 'prior_journal_sha256': receipt['journal_sha256'],
        'prior_state': prior, 'source_run_id': context.get('run_id'), 'created_at': time.time(),
        'orphan_output_prefixes': [graph[node]['inputs']['filename_prefix'] for node in ('save_rgb', 'save_alpha')]})
    base_graph = json.loads((directory / (stage + '.api.json')).read_text())
    write_json(directory / (stage + '.resume.json'), {'attempt': attempt,
        'directory': str(recovery_directory.relative_to(directory)),
        'base_graph_sha256': hashlib.sha256(json.dumps(base_graph, ensure_ascii=False, indent=2).encode()).hexdigest()})
    recovered_graph, journal = cutout_recovery_graph(base_graph, directory, stage)
    emit(context, 'cutout', '仅恢复丢回执分块；旧 journal、孤立输出和已完成分块保留', checkpoint_stage=stage)
    return record_history(client, recovered_graph, journal, stage, context)


def cutout(context, directory, config, graph, workflow_path, chunk_frames):
    from comfy_client import ComfyClient
    source, source_hash, implementations, identity = cutout_identity(context, config, graph, workflow_path, chunk_frames)
    root = directory / 'foreground' / identity
    if (root / 'manifest.json').is_file():
        manifest = json.loads((root / 'manifest.json').read_text())
        if digest(root / 'RGB.mkv') != manifest['rgb_sha256'] or digest(root / 'Alpha.mkv') != manifest['alpha_sha256']:
            raise RuntimeError('foreground_cache_hash_mismatch')
        paired_verify(root / 'RGB.mkv', root / 'Alpha.mkv', manifest['media'])
        return manifest, root, True
    partial = directory / 'foreground' / (identity + '.partial')
    client = ComfyClient({**config, 'poll_interval_s': 0.5})
    resume = context.get('cutout_checkpoint_resume')
    plan = None
    if resume:
        plan = cutout_checkpoint(context, directory, config, graph, workflow_path, chunk_frames, client)
        if any(resume[key] != plan[key] for key in ('input_fingerprint', 'checkpoint_fingerprint', 'generation')):
            raise RuntimeError('cutout_checkpoint_resume_identity_changed')
    partial.mkdir(parents=True, exist_ok=True)
    normalized_source, media, mapping = normalize(source, partial)
    canonical_input = comfy_source_path(normalized_source, config['comfy_input_dir']) if graph['14']['class_type'] == 'FactoryLoadCFRContextV1' else normalized_source
    # Factory-local receipt polling only; this does not change render inputs.
    rgb_parts, alpha_parts, executions = [], [], []
    for start in range(0, media['frame_count'], chunk_frames):
        run_graph, count = cutout_chunk_graph(graph, canonical_input, media, context, identity, start, chunk_frames)
        emit(context, 'cutout', f'抠像母资产帧 {start}–{start + count - 1}', progress=100 * start / media['frame_count'])
        receipt = next((chunk for chunk in plan['chunks'] if chunk['start_frame'] == start), None) if plan else None
        if receipt:
            outputs, execution = resume_cutout_chunk(client, run_graph, partial / 'comfy', f'cutout_{start:06d}', context, receipt)
        else:
            outputs, execution = record_history(client, run_graph, partial / 'comfy', f'cutout_{start:06d}', context)
        rgb_path, alpha_path = result_path(client, outputs, 'save_rgb', '.mkv'), result_path(client, outputs, 'save_alpha', '.mkv')
        for path in (rgb_path, alpha_path):
            if probe(path)['frame_count'] != count:
                raise RuntimeError('foreground_chunk_frame_count_mismatch')
        rgb_parts.append(rgb_path)
        alpha_parts.append(alpha_path)
        executions.append({**{key: execution[key] for key in ('prompt_id', 'submitted_graph_sha256')},
                           'workflow_path': str(root / Path(execution['workflow_path']).relative_to(partial))})
    if (partial / 'RGB.mkv').exists() and (partial / 'Alpha.mkv').exists():
        try:
            presentation_timestamps_verify(partial / 'RGB.mkv', partial / 'Alpha.mkv', media)
        except RuntimeError as error:
            previous = partial / 'postprocess_attempts' / ('timestamp_' + uuid.uuid4().hex)
            previous.mkdir(parents=True)
            for name in ('RGB.mkv', 'Alpha.mkv', 'frame_hashes.json', 'Preview_black.mp4', 'Preview_white.mp4',
                         'Preview_bright.mp4', 'Preview_source.mp4', 'Preview_alpha_overlay.mp4'):
                path = partial / name
                if path.exists():
                    path.rename(previous / name)
            write_json(previous / 'recovery.json', {'reason': str(error), 'run_id': context['run_id'],
                'cutout_resubmissions': 0, 'recovery': 'rebuild packet timestamps from verified ComfyUI chunks'})
    if not (partial / 'RGB.mkv').exists():
        concat_paths(rgb_parts, partial / 'RGB.mkv', partial, source if mapping['original']['audio'] else None, media['fps_rational'])
    if not (partial / 'Alpha.mkv').exists():
        concat_paths(alpha_parts, partial / 'Alpha.mkv', partial, lossless_fps=media['fps_rational'])
    rgb_media, alpha_media = paired_verify(partial / 'RGB.mkv', partial / 'Alpha.mkv', media)
    frames = alpha_statistics(partial / 'Alpha.mkv', alpha_media)
    hashes = {'algorithm': 'sha256', 'rgb_pixel_format': 'rgb48le', 'alpha_pixel_format': 'gray16le',
              'rgb': decoded_frame_hashes(partial / 'RGB.mkv', 'rgb48le'),
              'alpha': decoded_frame_hashes(partial / 'Alpha.mkv', 'gray16le')}
    if len(hashes['rgb']) != media['frame_count'] or len(hashes['alpha']) != media['frame_count']:
        raise RuntimeError('foreground_decoded_hash_count_failed')
    write_json(partial / 'frame_hashes.json', hashes)
    emit(context, 'cutout', '母资产已全解码验证；正在生成黑、白、亮色全长诊断预览', progress=99)
    previews = foreground_previews(partial, media)
    binding_path = directory / 'source_binding.json'
    resolved_source = json.loads(binding_path.read_text()) if binding_path.exists() else {}
    if resolved_source:
        if resolved_source['sha256'] != source_hash or Path(resolved_source['file_path']).resolve() != source:
            raise RuntimeError('resolved_source_binding_mismatch')
        write_json(partial / 'source_binding.json', resolved_source)
    manifest = {'schema': 'factory.foreground.v1', 'status': 'READY', 'task_id': context['task_id'],
        'context_id': context.get('context_id'), 'processing_run_id': context['run_id'], 'created_at': time.time(),
        'execution_scope': context.get('execution_scope', 'PRODUCTION'),
        'task_context': {key: context.get(key) for key in ('task_id', 'project_id', 'country', 'keyword', 'product_id',
                         'product_images', 'source_document', 'source_row', 'context_id', 'video_id')},
        'resolved_source': resolved_source,
        'source_path': str(source), 'source_sha256': source_hash, 'source_audio_path': str(source) if mapping['original']['audio'] else None,
        'rgb_sha256': digest(partial / 'RGB.mkv'), 'alpha_sha256': digest(partial / 'Alpha.mkv'),
        'media': media, 'rgb_media': rgb_media, 'alpha_media': alpha_media, 'alpha_mode': 'straight',
        'rgb_source': 'original RGB, uncomposited and not premultiplied; foreground edge decontamination not yet applied',
        'precision': {'RGB': 'VHS/OpenCV source decode 8bit, stored losslessly in rgba64le; no claim of increased source precision',
                      'Alpha': 'BiRefNet node first quantizes model mask to native 8bit; motion repair produces float32 values, then gray16le stores them without claiming increased model precision'},
        'object_strategy': context.get('object_strategy') or {'person': 'BiRefNet-general foreground'},
        'model': graph['11']['inputs']['model'], 'workflow_sha256': digest(workflow_path), 'implementation_hashes': implementations,
        'input_fingerprint': identity, 'full_cutout_runs': 1, 'cutout_chunk_executions': len(executions),
        'checkpoint_resume': plan,
        'executions': executions, 'alpha_per_frame': frames, 'full_decode_verified': True,
        'presentation_timestamps_verified': True, 'export_timeline': 'FFV1 packets re-timed by exact frame ordinal; no image re-encoding',
        'previews': previews,
        'semantic_review': 'PENDING', 'rgb_hashes_file': 'frame_hashes.json', 'time_mapping_file': 'time_mapping.json'}
    write_json(partial / 'manifest.json', manifest)
    if root.exists():
        raise RuntimeError('foreground_version_publish_conflict')
    partial.rename(root)
    return manifest, root, False


def composition_mother_identity(person, reference, media):
    manifest = json.loads(Path(reference['manifest_path']).read_text())
    if (manifest['source_sha256'] != reference.get('source_sha256') or manifest['rgb_sha256'] != person['sha256']
            or manifest['alpha_sha256'] != reference['alpha_sha256'] or not manifest.get('full_decode_verified')):
        raise ValueError('composition_mother_manifest_mismatch')
    claims_audio = bool(reference.get('source_audio_path') or manifest.get('source_audio_path')
                        or manifest.get('rgb_media', {}).get('audio'))
    if claims_audio and not media['audio']:
        raise ValueError('composition_mother_audio_missing')
    return {'source_sha256': manifest['source_sha256'],
            'audio': {'source': 'mother_rgb', 'rgb_sha256': person['sha256']} if media['audio'] else {'source': None}}


def composition_fingerprint(person, reference, background, implementations, source_inputs, workflow_path, context, config):
    return hashlib.sha256(json.dumps({'rgb': person['sha256'], 'alpha': reference['alpha_sha256'], 'implementations': implementations,
        'background': background['sha256'], 'source': source_inputs, 'workflow': digest(workflow_path), 'job': context.get('job'),
        'settings': {'color_enabled': config.get('color_enabled', True),
                     **{key: value for key, value in config.items() if key.startswith(('workflow_color_', 'composite_', 'output_'))}}}, sort_keys=True).encode()).hexdigest()


def reusable_composition(asset, context, person, reference, background, implementations, source_inputs, workflow_path, config):
    path = Path(asset.get('external_path') or asset['storage_path']).resolve()
    manifest_path = Path(asset['legacy_reference']['manifest_path']).resolve()
    prior = json.loads(manifest_path.read_text())
    actual = {key: value for key, value in implementations.items() if key != 'factory_algorithm_version'}
    previous = {key: value for key, value in prior['implementation_hashes'].items() if key != 'factory_algorithm_version'}
    if (actual != previous or prior['task_id'] != context['task_id'] or prior.get('context_id') != context.get('context_id')
            or digest(path) != asset['sha256'] or asset['sha256'] != prior['output_sha256'] or not prior.get('full_decode_verified')
            or prior['input_fingerprint'] != composition_fingerprint(person, reference, background, prior['implementation_hashes'],
                source_inputs, workflow_path, context, config)):
        raise ValueError('composition_remux_parent_inputs_mismatch')
    for execution in prior['executions']:
        graph_path = Path(execution['workflow_path'])
        history_path = graph_path.with_name(graph_path.name.removesuffix('.api.json') + '.history.json')
        history = json.loads(history_path.read_text())
        if (digest(graph_path) != execution['submitted_graph_sha256'] or history['prompt_id'] != execution['prompt_id']
                or history['history'].get('prompt', [None, None, None])[2] != json.loads(graph_path.read_text())
                or not history['history'].get('status', {}).get('completed')
                or history['history']['status'].get('status_str') == 'error'):
            raise ValueError('composition_remux_parent_execution_not_verified')
    decode_verify(path)
    return path, prior


def compose(context, directory, config, graph, workflow_path, chunk_frames):
    from comfy_client import ComfyClient
    person, background = context['person_asset'], context['background_asset']
    rgb = Path(person.get('external_path') or person['storage_path']).resolve()
    reference = person['legacy_reference']
    alpha = Path(reference['alpha_path']).resolve()
    background_path = Path(background.get('external_path') or background['storage_path']).resolve()
    if reference.get('alpha_mode', 'straight') != 'straight':
        raise ValueError('unsupported_foreground_alpha_mode')
    emit(context, 'compose', '校验母资产 RGB/Alpha 哈希与帧时间轴；只读校验，不重新抠像', progress=0)
    if digest(rgb) != person['sha256'] or digest(alpha) != reference['alpha_sha256'] or digest(background_path) != background['sha256']:
        raise ValueError('compose_input_hash_mismatch')
    media = probe(rgb)
    paired_verify(rgb, alpha, media)
    source_inputs = composition_mother_identity(person, reference, media)
    implementations = implementation_fingerprint(config, 'compose', graph)
    identity = composition_fingerprint(person, reference, background, implementations, source_inputs, workflow_path, context, config)
    root = directory / 'compositions' / identity
    if (root / 'manifest.json').is_file():
        manifest = json.loads((root / 'manifest.json').read_text())
        if digest(root / 'Composite.mp4') != manifest['output_sha256']:
            raise ValueError('compose_cache_hash_mismatch')
        decode_verify(root / 'Composite.mp4')
        verify_composition_timeline(root / 'Composite.mp4', media, probe(root / 'Composite.mp4'))
        if media['audio']:
            verify_composition_audio(rgb, root / 'Composite.mp4')
        return manifest, root, True
    partial = directory / 'compositions' / (identity + '.partial')
    partial.mkdir(parents=True, exist_ok=True)
    pieces, executions = [], []
    scale = int(config.get('output_short_side', 1080)) / min(media['width'], media['height'])
    output_width, output_height = round(media['width'] * scale / 2) * 2, round(media['height'] * scale / 2) * 2
    previous = context.get('composition_asset')
    if previous:
        path, parent = reusable_composition(previous, context, person, reference, background, implementations, source_inputs, workflow_path, config)
        pieces, executions = [path], copy.deepcopy(parent['executions'])
        emit(context, 'compose', '复用已验证合成画面，仅修正母资产音轨封装；不提交 ComfyUI', progress=95)
    else:
        # Factory-local receipt polling only; this does not change render inputs.
        client = ComfyClient({**config, 'poll_interval_s': 0.5})
    for start in ([] if previous else range(0, media['frame_count'], chunk_frames)):
        count = min(chunk_frames, media['frame_count'] - start)
        run_graph = copy.deepcopy(graph)
        run_graph['foreground']['inputs'].update(rgb_path=str(rgb), alpha_path=str(alpha), start_frame=start, frame_count=count)
        run_graph['background']['inputs']['image'] = str(background_path)
        run_graph['41']['inputs'].update(width=media['width'], height=media['height'])
        run_graph['background_repeat']['inputs']['amount'] = count
        run_graph['46']['inputs'].update(width=output_width, height=output_height)
        run_graph['25']['inputs'].update(frame_rate=media['fps'], crf=int(config.get('output_crf', 18)),
            filename_prefix=f'factory/{context["task_id"]}/{identity}/compose/{start:06d}')
        for field in ('brightness', 'contrast', 'saturation'):
            run_graph['42']['inputs'][field] = float(config.get('workflow_color_' + field, 1)) if config.get('color_enabled', True) else 1
        if any(config.get('composite_' + field, default) != default for field, default in
               (('opacity', 100), ('x_percent', 50), ('y_percent', 50), ('scale', 1))):
            raise ValueError('mother_asset_composition_requires_original_canvas_transform')
        emit(context, 'compose', f'复用母资产合成帧 {start}–{start + count - 1}', progress=100 * start / media['frame_count'])
        outputs, execution = record_history(client, run_graph, partial / 'comfy', f'compose_{start:06d}', context)
        path = result_path(client, outputs, '25', '.mp4')
        if probe(path)['frame_count'] != count:
            raise RuntimeError('compose_chunk_frame_count_mismatch')
        pieces.append(path)
        executions.append({**{key: execution[key] for key in ('prompt_id', 'submitted_graph_sha256')},
                           'workflow_path': str(root / 'comfy' / Path(execution['workflow_path']).name)})
    if not (partial / 'Composite.mp4').exists():
        audio = rgb if media['audio'] else None
        concat_paths(pieces, partial / 'Composite.mp4', partial, audio, presentation_fps=media['fps_rational'])
    output = probe(partial / 'Composite.mp4')
    decode_verify(partial / 'Composite.mp4')
    timeline_validation = verify_composition_timeline(partial / 'Composite.mp4', media, output)
    if (output['width'], output['height']) != (output_width, output_height):
        raise RuntimeError('composition_dimensions_failed')
    if media['audio'] and not output['audio']:
        raise RuntimeError('composition_audio_missing')
    audio_validation = verify_composition_audio(rgb, partial / 'Composite.mp4') if media['audio'] else None
    manifest = {'schema': 'factory.composition.v1', 'status': 'READY', 'task_id': context['task_id'],
        'context_id': context.get('context_id'), 'processing_run_id': context['run_id'],
        'execution_scope': context.get('execution_scope', 'PRODUCTION'),
        'person_asset_id': person.get('id'), 'person_version': reference.get('version'),
        'rgb_sha256': person['sha256'], 'alpha_sha256': reference['alpha_sha256'],
        'background_asset_id': background.get('id'), 'background_sha256': background['sha256'],
        'input_fingerprint': identity, 'workflow_sha256': digest(workflow_path), 'implementation_hashes': implementations, 'executions': executions,
        'cutout_inference_executions': 0, 'output_sha256': digest(partial / 'Composite.mp4'),
        'source_media': media, 'output_media': output, 'source_audio_path': reference.get('source_audio_path'),
        'audio_asset_path': str(rgb) if media['audio'] else None, **source_inputs,
        'audio_validation': audio_validation, 'audio_sample_clock': aac_sample_clock(rgb) if media['audio'] else None,
        'timeline_validation': timeline_validation,
        'video_reused': bool(previous), 'new_comfy_submissions': 0 if previous else len(executions),
        'parent_composition_asset_id': previous.get('id') if previous else None,
        'parent_composition_manifest_path': previous['legacy_reference']['manifest_path'] if previous else None,
        'parent_composition_sha256': previous['sha256'] if previous else None,
        'alpha_mode': 'straight', 'full_decode_verified': True, 'semantic_review': 'PENDING', 'created_at': time.time()}
    write_json(partial / 'manifest.json', manifest)
    if root.exists():
        raise RuntimeError('composition_version_publish_conflict')
    partial.rename(root)
    return manifest, root, False


def readiness(config, cutout_path, compose_path):
    from comfy_client import ComfyClient
    client = ComfyClient({**config, 'request_timeout_s': 5, 'read_retries': 1})
    info = client.request('/object_info')
    graphs = [workflow(cutout_path, 'cutout'), workflow(compose_path, 'compose')]
    missing = sorted({node['class_type'] for graph in graphs for node in graph.values()} - info.keys())
    formats = info.get('VHS_VideoCombine', {}).get('input', {}).get('required', {}).get('format', [[]])[0]
    if 'video/ffv1-mkv' not in formats:
        missing.append('VHS FFV1')
    checks = [{'name': 'ComfyUI', 'status': 'MISSING' if missing else 'READY', 'ok': not missing,
               'message': '缺少节点：' + ', '.join(missing) if missing else '独立抠像与母资产复用节点已连接'},
              {'name': 'Person Asset', 'status': 'READY' if not missing else 'NOT_CONNECTED', 'ok': not missing,
               'message': 'RGB + Alpha 双无损输出；合成图仅从母资产读取'},
              {'name': 'Configuration', 'status': 'READY', 'ok': True, 'workflow_api': str(cutout_path),
               'workflow_sha256': digest(cutout_path), 'compose_workflow_api': str(compose_path),
               'compose_workflow_sha256': digest(compose_path),
               'implementation_fingerprints': {'cutout': implementation_fingerprint(config, 'cutout', graphs[0]),
                                              'compose': implementation_fingerprint(config, 'compose', graphs[1])}}]
    result = base_result('readiness', {})
    result.update(status='READY' if not missing else 'NOT_CONNECTED', metadata={'checks': checks, 'business_executed': False, 'paid_requests': 0})
    return result


def snapshot_metadata(manifest, root, target, context, cached):
    first = manifest['executions'][0]
    source = Path(first['workflow_path'])
    if not source.exists():
        source = root / 'comfy' / source.name
    history_path = source.with_name(source.name.removesuffix('.api.json') + '.history.json')
    origin = json.loads(history_path.read_text())
    if digest(source) != first['submitted_graph_sha256'] or origin['prompt_id'] != first['prompt_id']:
        raise RuntimeError('workflow_snapshot_origin_evidence_changed')
    target.write_bytes(source.read_bytes())
    return {'workflow_snapshot_id': 'wfs-' + uuid.uuid4().hex, 'task_id': context['task_id'],
            'processing_run_id': context['run_id'], 'prompt_id': first['prompt_id'], 'submitted': not cached,
            'origin_processing_run_id': origin['processing_run_id'], 'origin_task_id': origin.get('task_id'),
            'origin_history_path': str(history_path), 'origin_history_sha256': digest(history_path),
            'cache_hit': cached, 'path': str(target), 'sha256': digest(target)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--legacy-root', type=Path, required=True)
    parser.add_argument('--stage', choices=['readiness', 'identity', 'cutout-checkpoint', 'cutout', 'person', 'compose'], required=True)
    parser.add_argument('--context', type=Path, required=True)
    parser.add_argument('--result', type=Path, required=True)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--cutout-workflow', type=Path, required=True)
    parser.add_argument('--compose-workflow', type=Path, required=True)
    parser.add_argument('--chunk-frames', type=int, default=8)
    arguments = parser.parse_args()
    arguments.directory = arguments.directory.resolve()
    arguments.cutout_workflow = arguments.cutout_workflow.resolve()
    arguments.compose_workflow = arguments.compose_workflow.resolve()
    context = json.loads(arguments.context.read_text())
    config = load_legacy(arguments.legacy_root)
    result = base_result(arguments.stage, context)
    try:
        if arguments.stage == 'readiness':
            result = readiness(config, arguments.cutout_workflow, arguments.compose_workflow)
        elif arguments.stage == 'identity':
            stage = context.get('identity_stage')
            if stage not in {'cutout', 'compose'}:
                raise ValueError('invalid_implementation_identity_stage')
            chosen = arguments.compose_workflow if stage == 'compose' else arguments.cutout_workflow
            graph = workflow(chosen, stage)
            result['metadata'] = {'implementation_fingerprints': {stage: implementation_fingerprint(config, stage, graph)},
                                  'business_executed': False, 'paid_requests': 0, 'comfy_contacted': False}
        elif arguments.stage == 'cutout-checkpoint':
            from comfy_client import ComfyClient
            graph = workflow(arguments.cutout_workflow, 'cutout')
            if arguments.chunk_frames < 1:
                raise ValueError('chunk_frames_must_be_positive')
            result['metadata'] = {'checkpoint': cutout_checkpoint(context, Path(context['checkpoint_directory']), config,
                graph, arguments.cutout_workflow, arguments.chunk_frames, ComfyClient(config)),
                'business_executed': False, 'paid_requests': 0, 'comfy_submissions': 0}
        else:
            chosen = arguments.compose_workflow if arguments.stage == 'compose' else arguments.cutout_workflow
            graph = workflow(chosen, 'compose' if arguments.stage == 'compose' else 'cutout')
            if arguments.chunk_frames < 1:
                raise ValueError('chunk_frames_must_be_positive')
            arguments.directory.mkdir(parents=True, exist_ok=True)
            if arguments.stage == 'compose':
                manifest, root, cached = compose(context, arguments.directory, config, graph, chosen, arguments.chunk_frames)
                result['assets'] = [{'asset_type': 'COMPOSITION_VIDEO', 'path': str(root / 'Composite.mp4'),
                    'sha256': manifest['output_sha256'], 'source_system': 'Factory ComfyUI mother-asset composition',
                    'legacy_reference': {'manifest_path': str(root / 'manifest.json'), 'input_fingerprint': manifest['input_fingerprint'],
                        'rgb_sha256': manifest['rgb_sha256'], 'alpha_sha256': manifest['alpha_sha256'], 'cutout_inference_executions': 0,
                        'source_fps': manifest['source_media']['fps'], 'output_fps': manifest['output_media']['fps']}}]
            else:
                manifest, root, cached = cutout(context, arguments.directory, config, graph, chosen, arguments.chunk_frames)
                result['assets'] = [foreground_evidence(manifest, root)]
            result['metadata'] = {'manifest_path': str(root / 'manifest.json'), 'cache_hit': cached,
                'full_decode_verified': True, 'comfy_prompt_ids': [entry['prompt_id'] for entry in manifest['executions']],
                'video_reused': manifest.get('video_reused', False), 'new_comfy_submissions': manifest.get('new_comfy_submissions'),
                'workflow_snapshot': snapshot_metadata(manifest, root, arguments.result.parent / 'workflow-api.json', context, cached or manifest.get('video_reused', False))}
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as error:
        result.update(status='FAILED', error_code='COMFY_ASSET_' + type(error).__name__.upper(),
                      error_message=safe(str(error)), retryable=False)
    write_json(arguments.result, result)
    print('FACTORY_RESULT ' + json.dumps({'status': result['status'], 'task_id': result.get('task_id'), 'error_code': result.get('error_code')}, ensure_ascii=False), flush=True)
    return 0 if result['status'] == 'READY' else 1


if __name__ == '__main__':
    raise SystemExit(main())
