"""Synthetic public image references and their explicit segment bindings."""
from __future__ import annotations

import base64
from pathlib import Path


# The legacy ID is retained so existing local drafts can still resolve it.
# Its public fixture is drawn by this project and contains no external image.
_REFERENCES = {
    'hotel_booking_screen': {
        'path': 'refs/reference_board.png',
        'name': '物品分区 · 合成布局参考',
        'url': '/api/reference-images/hotel_booking_screen',
        'source_url': '/api/reference-images/hotel_booking_screen',
        'purpose': 'object_layout',
        'note': '本项目绘制的虚构布局；仅提供颜色、形状和分区参考',
        'width': 960,
        'height': 640,
        'description': (
            '@图片1仅作为物品颜色、形状和桌面分区的布局参考。'
            '左侧托盘放置红色方块，中间托盘放置蓝色球体，右侧托盘放置黄色圆柱。'
            '保持物体外观和数量连续；不把整张参考图用作场景首帧或全屏画面，'
            '不要求复现参考图中的英文说明或标记。'
        ),
    },
}
_MAX_REFERENCES = 8
_MAX_SEGMENT_BINDINGS = 20


def reference_path(root: Path, reference_id: str) -> Path:
    """Resolve a known ID only; never accept caller-supplied paths or links."""
    if not isinstance(reference_id, str) or reference_id not in _REFERENCES:
        raise FileNotFoundError('参考图片不存在')
    root = Path(root).resolve()
    candidate = root / _REFERENCES[reference_id]['path']
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        raise FileNotFoundError('参考图片不存在') from None
    # Reject symbolic links in the file or any of its relative parent paths.
    if resolved != candidate or not candidate.is_file():
        raise FileNotFoundError('参考图片不存在')
    return candidate


def catalog(root: Path) -> list[dict]:
    """Return public preview metadata for references available on this host."""
    result = []
    for reference_id, reference in _REFERENCES.items():
        try:
            reference_path(root, reference_id)
        except FileNotFoundError:
            continue
        result.append({'id': reference_id, **{key: value for key, value in reference.items()
                                             if key not in ('path', 'description')}})
    return result


def validate_reference_images(value, segment_ids, root: Path) -> list[dict]:
    """Validate the complete binding list before a batch can write any runs."""
    if not isinstance(value, list) or len(value) > _MAX_REFERENCES:
        raise ValueError('reference_images必须为不超过8项的数组')
    known_segments = set(segment_ids)
    result, seen = [], set()
    for binding in value:
        if not isinstance(binding, dict) or set(binding) != {'id', 'segment_ids'}:
            raise ValueError('每个参考图片绑定只能包含id和segment_ids')
        reference_id = binding['id']
        if not isinstance(reference_id, str) or reference_id not in _REFERENCES:
            raise ValueError('参考图片编号无效')
        if reference_id in seen:
            raise ValueError('参考图片编号不能重复')
        scope = binding['segment_ids']
        if not isinstance(scope, list) or not 1 <= len(scope) <= _MAX_SEGMENT_BINDINGS:
            raise ValueError('参考图片segment_ids必须包含1至20个分段编号')
        if any(not isinstance(item, str) or not item.strip() or len(item) > 100 for item in scope):
            raise ValueError('参考图片分段编号必须为1至100字的文字')
        if len(scope) != len(set(scope)) or any(item not in known_segments for item in scope):
            raise ValueError('参考图片分段编号不存在或重复')
        try:
            reference_path(root, reference_id)
        except FileNotFoundError:
            raise ValueError('参考图片文件不存在或不可用') from None
        seen.add(reference_id)
        result.append({'id': reference_id, 'segment_ids': list(scope)})
    return result


def build_reference_assets(root: Path, spec: dict) -> list[dict]:
    """Build private model inputs; the catalogue never includes these bytes."""
    bindings = validate_reference_images(spec.get('reference_images', []),
                                         [segment['id'] for segment in spec['segments']], root)
    assets = []
    for binding in bindings:
        reference_id = binding['id']
        try:
            payload = reference_path(root, reference_id).read_bytes()
        except OSError:
            raise ValueError('参考图片文件无法读取') from None
        assets.append({
            'id': reference_id, 'role': 'reference_image', 'segment_ids': binding['segment_ids'],
            'description': _REFERENCES[reference_id]['description'],
            'content': {'type': 'image_url', 'role': 'reference_image',
                        'image_url': {'url': 'data:image/png;base64,' + base64.b64encode(payload).decode('ascii')}},
        })
    return assets
