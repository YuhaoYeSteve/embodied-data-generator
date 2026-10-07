"""Local, untracked provider settings. Authentication stays in ArkCLI profiles."""
from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
import re


DEFAULTS = {
    'video_profile': 'default',
    'planner_profile': '',
    'review_profile': '',
    'planner_model': '',
    'review_model': '',
    'video_models': {
        'seedance-2.0': 'doubao-seedance-2-0-260128',
        'seedance-2.5': 'doubao-seedance-2-5-260628',
    },
    'video_prices_cny_per_1k_tokens': {},
    'review_prices_cny_per_1m_tokens': {},
}


def _identifier(value, field, *, optional=False):
    if optional and value == '':
        return value
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}', value):
        raise ValueError('Invalid local configuration field: ' + field)
    return value


def _rate(value, field):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError('Expected a finite nonnegative price in ' + field)


def load_settings(path=None):
    """Read explicit JSON configuration without logging values or parser input."""
    if path is None:
        path = os.environ.get('STUDIO_CONFIG_PATH') or Path(__file__).with_name('studio.local.json')
    path = Path(path).expanduser()
    config = copy.deepcopy(DEFAULTS)
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError, UnicodeError):
            raise ValueError('Could not read local studio configuration JSON') from None
        if not isinstance(raw, dict) or set(raw) - set(DEFAULTS):
            raise ValueError('Unknown local configuration field; store credentials in ArkCLI, not this file')
        config.update(raw)
        if isinstance(raw.get('video_models', {}), dict):
            config['video_models'] = {**DEFAULTS['video_models'], **raw.get('video_models', {})}
    for field in ('video_profile', 'planner_profile', 'review_profile', 'planner_model', 'review_model'):
        _identifier(config[field], field, optional=field != 'video_profile')
    for field in ('planner_profile', 'review_profile'):
        config[field] = config[field] or config['video_profile']
    if not isinstance(config['video_models'], dict) or set(config['video_models']) != set(DEFAULTS['video_models']):
        raise ValueError('video_models must use supported model family names')
    for family, model in config['video_models'].items():
        _identifier(model, 'video_models.' + family)
    if len(set(config['video_models'].values())) != len(config['video_models']):
        raise ValueError('Each video family must use its own model or endpoint')
    video_prices = config['video_prices_cny_per_1k_tokens']
    if not isinstance(video_prices, dict) or set(video_prices) - set(DEFAULTS['video_models']):
        raise ValueError('Invalid video price model family')
    resolutions = {'seedance-2.0': {'480p', '720p', '1080p', '4k'}, 'seedance-2.5': {'480p', '720p', '1080p'}}
    for family, prices in video_prices.items():
        if not isinstance(prices, dict) or set(prices) - resolutions[family]:
            raise ValueError('Invalid video price resolution')
        for modes in prices.values():
            if not isinstance(modes, dict) or set(modes) - {'nv2v', 'v2v'}:
                raise ValueError('Invalid video price mode')
            for rate in modes.values():
                _rate(rate, 'video_prices_cny_per_1k_tokens')
    review_prices = config['review_prices_cny_per_1m_tokens']
    if not isinstance(review_prices, dict):
        raise ValueError('Invalid review prices')
    for model, prices in review_prices.items():
        _identifier(model, 'review price model')
        if not isinstance(prices, dict) or set(prices) != {'input', 'audio', 'output', 'max_input_tokens'}:
            raise ValueError('Review prices require input, audio, output and max_input_tokens')
        for field in ('input', 'audio', 'output'):
            _rate(prices[field], 'review_prices_cny_per_1m_tokens.' + field)
        if type(prices['max_input_tokens']) is not int or prices['max_input_tokens'] < 1:
            raise ValueError('Review price max_input_tokens must be a positive integer')
    return config


SETTINGS = load_settings()
