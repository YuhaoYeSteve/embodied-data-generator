"""Shared, explicit video-model contracts for the studio and generation layer.

Provider model identifiers and optional account prices come from local settings.
Unknown prices stay unknown and never become a zero-cost promise.
"""
from __future__ import annotations

import copy
import math
from studio_config import SETTINGS

DEFAULT_MODEL_FAMILY = 'seedance-2.5'
_DIMENSIONS = {'480p': (864, 480), '720p': (1280, 720),
               '1080p': (1920, 1080), '4k': (3840, 2160)}
_MODELS = {
    'seedance-2.5': {
        'id': 'seedance-2.5', 'label': 'Seedance 2.5',
        'model': SETTINGS['video_models']['seedance-2.5'],
        'foundation_model': 'doubao-seedance-2-5-260628',
        'resolutions': ['480p', '720p', '1080p'], 'durations': list(range(4, 31)),
        'default_resolution': '480p', 'default_segment_duration': 30,
        'max_reference_duration': 30,
        'explicit_task_type': True, 'request_output_format': 'mov',
    },
    'seedance-2.0': {
        'id': 'seedance-2.0', 'label': 'Seedance 2.0',
        'model': SETTINGS['video_models']['seedance-2.0'],
        'foundation_model': 'doubao-seedance-2-0-260128',
        'resolutions': ['480p', '720p', '1080p', '4k'], 'durations': list(range(4, 16)),
        'default_resolution': '720p', 'default_segment_duration': 15,
        'max_reference_duration': 15,
        'explicit_task_type': False, 'request_output_format': None,
    },
}
_PRICES = copy.deepcopy(SETTINGS['video_prices_cny_per_1k_tokens'])


def get_model(model_family=DEFAULT_MODEL_FAMILY):
    if not isinstance(model_family, str) or model_family not in _MODELS:
        raise ValueError('请选择 Seedance 2.0 或 Seedance 2.5')
    return copy.deepcopy(_MODELS[model_family])


def model_catalog():
    return [get_model(family) for family in _MODELS]


def infer_model_family(model, *, default=None):
    for family, item in _MODELS.items():
        if model in (item['model'], item['foundation_model']):
            return family
    return default


def resolve_selection(config):
    if not isinstance(config, dict):
        raise ValueError('模型配置必须为对象')
    raw_model = config.get('model')
    family = config.get('model_family')
    if family is None:
        family = infer_model_family(raw_model, default=DEFAULT_MODEL_FAMILY)
    capability = get_model(family)
    if raw_model is not None and raw_model not in (capability['model'], capability['foundation_model']):
        raise ValueError('模型接入点与所选型号不匹配，请重新选择模型')
    resolution = config.get('resolution', capability['default_resolution'])
    if resolution not in capability['resolutions']:
        raise ValueError('%s 不支持 %s，请选择 %s' %
                         (capability['label'], resolution, ' / '.join(capability['resolutions'])))
    duration = config.get('segment_duration_seconds', capability['default_segment_duration'])
    if type(duration) is not int or duration not in capability['durations']:
        raise ValueError('%s 单段时长须为 %d–%d 秒整数' %
                         (capability['label'], min(capability['durations']), max(capability['durations'])))
    return {'model_family':family, 'model':capability['model'], 'resolution':resolution,
            'segment_duration_seconds':duration}


def resolution_dimensions(resolution):
    if not isinstance(resolution, str) or resolution not in _DIMENSIONS:
        raise ValueError('分辨率无效')
    return _DIMENSIONS[resolution]


def plan_segment_durations(total_seconds, segment_duration_seconds, model_family=DEFAULT_MODEL_FAMILY):
    capability = get_model(model_family)
    if type(total_seconds) is not int or not 60 <= total_seconds <= 600:
        raise ValueError('总时长须为60–600秒整数')
    if type(segment_duration_seconds) is not int or segment_duration_seconds not in capability['durations']:
        raise ValueError('%s 不支持该单段时长' % capability['label'])
    count = math.ceil(total_seconds / segment_duration_seconds)
    if not 2 <= count <= 20:
        raise ValueError('该总时长和单段时长需要超过20段，请增加单段时长或缩短总时长')
    values = [segment_duration_seconds] * (count - 1)
    tail = total_seconds - sum(values)
    if tail < 4:
        values[-1] -= 4 - tail
        tail = 4
    values.append(tail)
    if any(value not in capability['durations'] for value in values):
        raise ValueError('无法按所选时长拆成合法分段')
    return values


def validate_spec_model(spec, model_family, resolution, *, full_length=True):
    capability = get_model(model_family)
    if resolution not in capability['resolutions']:
        raise ValueError('%s 不支持 %s' % (capability['label'], resolution))
    if not isinstance(spec, dict) or not isinstance(spec.get('segments'), list):
        raise ValueError('脚本需要分段数组')
    segments = spec['segments']
    if not segments:
        raise ValueError('脚本必须至少包含一段')
    if full_length and not 2 <= len(segments) <= 20:
        raise ValueError('脚本必须包含2–20段')
    total = 0
    for index, segment in enumerate(segments):
        duration = segment.get('duration') if isinstance(segment, dict) else None
        if type(duration) is not int or duration not in capability['durations']:
            raise ValueError('第%d段为%s秒，%s仅支持单段%d–%d秒；请调整脚本或切换模型' %
                             (index+1, duration, capability['label'], min(capability['durations']), max(capability['durations'])))
        total += duration
    if (full_length and not 60 <= total <= 600) or spec.get('target_duration_seconds', total) != total:
        raise ValueError('分段总长须为60–600秒，且与目标时长一致')


def price_rate(model_family, resolution, has_video=False):
    capability = get_model(model_family)
    if resolution not in capability['resolutions']:
        raise ValueError('%s 不支持 %s' % (capability['label'], resolution))
    return _PRICES.get(model_family, {}).get(resolution, {}).get('v2v' if has_video else 'nv2v')
