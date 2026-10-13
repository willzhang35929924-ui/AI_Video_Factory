"""Lossless, bounded reads of Factory RGB/Alpha foreground masters."""
import json
import subprocess
from fractions import Fraction
from pathlib import Path

import numpy as np
import torch
import cv2

import folder_paths


def checked_path(value, source=False):
    path = Path(value).expanduser().resolve()
    roots = (Path(folder_paths.get_input_directory()).resolve(),
             Path(folder_paths.get_output_directory()).resolve(),
             Path('/Users/xiaoyao/AI_Video_Factory/storage').resolve())
    if source:
        roots += (Path('/Users/xiaoyao/TikTok_Auto/downloads').resolve(),)
    if not any(path.is_relative_to(root) for root in roots) or not path.is_file():
        raise ValueError('Foreground master is outside local authorized storage or missing')
    return path


def dimensions(path):
    result = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                             '-show_entries', 'stream=width,height,pix_fmt,avg_frame_rate,r_frame_rate', '-of', 'json', str(path)],
                            check=True, capture_output=True, timeout=30)
    stream = json.loads(result.stdout)['streams'][0]
    rate = stream.get('avg_frame_rate') if stream.get('avg_frame_rate') not in (None, '0/0') else stream['r_frame_rate']
    return int(stream['width']), int(stream['height']), stream.get('pix_fmt'), float(Fraction(rate))


def read_frames(path, start, count, width, height, pixel_format, channels, fps):
    # Every FFV1 master frame is independently seekable. Half a frame before the
    # target tolerates Matroska's millisecond PTS quantization without dropping it.
    seek = max(0.0, (start - 0.5) / fps)
    result = subprocess.run(['ffmpeg', '-v', 'error', '-ss', str(seek), '-i', str(path), '-an',
        '-frames:v', str(count), '-fps_mode', 'passthrough', '-f', 'rawvideo',
        '-pix_fmt', pixel_format, '-'], check=True, capture_output=True, timeout=180)
    expected = count * width * height * channels * 2
    if len(result.stdout) != expected:
        raise ValueError(f'Foreground frame range has {len(result.stdout)} bytes; expected {expected}')
    values = np.frombuffer(result.stdout, dtype='<u2').reshape(count, height, width, channels)
    return torch.from_numpy(values.astype(np.float32) / 65535.0)


class FactoryLoadForegroundV1:
    @classmethod
    def INPUT_TYPES(cls):
        return {'required': {
            'rgb_path': ('STRING', {'default': ''}),
            'alpha_path': ('STRING', {'default': ''}),
            'start_frame': ('INT', {'default': 0, 'min': 0}),
            'frame_count': ('INT', {'default': 8, 'min': 1}),
        }}

    RETURN_TYPES = ('IMAGE', 'MASK')
    RETURN_NAMES = ('straight_rgb', 'alpha')
    FUNCTION = 'load'
    CATEGORY = 'Factory/Foreground'

    @classmethod
    def IS_CHANGED(cls, rgb_path, alpha_path, start_frame, frame_count):
        versions = []
        for value in (rgb_path, alpha_path):
            path = checked_path(value)
            stat = path.stat()
            versions.append(f'{path}:{stat.st_size}:{stat.st_mtime_ns}')
        return '|'.join(versions) + f':{start_frame}:{frame_count}'

    def load(self, rgb_path, alpha_path, start_frame, frame_count):
        rgb, alpha = checked_path(rgb_path), checked_path(alpha_path)
        width, height, _, fps = dimensions(rgb)
        aw, ah, pixel_format, alpha_fps = dimensions(alpha)
        if (width, height) != (aw, ah) or abs(fps - alpha_fps) > .001 or pixel_format != 'gray16le':
            raise ValueError('Foreground masters must have matching canvas and gray16le Alpha')
        images = read_frames(rgb, start_frame, frame_count, width, height, 'rgb48le', 3, fps)
        masks = read_frames(alpha, start_frame, frame_count, width, height, 'gray16le', 1, fps)[..., 0]
        return images, masks


class FactoryLoadCFRContextV1:
    @classmethod
    def INPUT_TYPES(cls):
        return {'required': {
            'video': ('STRING', {'default': ''}),
            'start_frame': ('INT', {'default': 0, 'min': 0}),
            'frame_count': ('INT', {'default': 10, 'min': 1}),
        }}

    RETURN_TYPES = ('IMAGE', 'FLOAT')
    RETURN_NAMES = ('source_rgb', 'fps')
    FUNCTION = 'load'
    CATEGORY = 'Factory/Foreground'

    @classmethod
    def IS_CHANGED(cls, video, start_frame, frame_count):
        path = checked_path(video, source=True)
        stat = path.stat()
        return f'{path}:{stat.st_size}:{stat.st_mtime_ns}:{start_frame}:{frame_count}'

    def load(self, video, start_frame, frame_count):
        path = checked_path(video, source=True)
        cap = cv2.VideoCapture(str(path))
        if not cap.isOpened():
            raise ValueError('Canonical CFR source cannot be opened')
        fps = cap.get(cv2.CAP_PROP_FPS)
        if not cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame):
            cap.release()
            raise ValueError('Canonical CFR source cannot seek to the exact frame')
        images = []
        for _ in range(frame_count):
            success, frame = cap.read()
            if not success:
                cap.release()
                raise ValueError('Canonical CFR source range is incomplete')
            images.append(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        cap.release()
        return torch.from_numpy(np.stack(images).astype(np.float32) / 255), float(fps)


NODE_CLASS_MAPPINGS = {'FactoryLoadForegroundV1': FactoryLoadForegroundV1, 'FactoryLoadCFRContextV1': FactoryLoadCFRContextV1}
NODE_DISPLAY_NAME_MAPPINGS = {'FactoryLoadForegroundV1': 'Factory · Read RGB + Alpha Master v1',
                            'FactoryLoadCFRContextV1': 'Factory · Read Canonical Source Context v1'}
