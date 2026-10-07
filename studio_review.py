"""Conservative, separate visual/audio checks for the local production studio.

Only ``review_segment`` may call the paid review API. It never generates media,
retries a review, or changes an experiment's acceptance state. Callers reserve
``REVIEW_RESERVE_CNY`` before calling and retain that reservation when the returned
``review_cost_cny`` is unknown. The manager applies bounded automatic retries;
this adapter only reports the evidence and the appropriate retry category.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import uuid

from longform_pipeline import atomic_json, video_url
from review_api import DEFAULT_MODEL, extract_text, run_review
from studio_config import SETTINGS
from review_media import contact


REVIEW_RESERVE_CNY = 1.0
REVIEW_PRICE_DATE = None
_REVIEW_PRICES = SETTINGS['review_prices_cny_per_1m_tokens']
_DIMENSIONS = ('visual_continuity', 'action_integrity', 'audio_consistency')
_STATUSES = frozenset(('pass', 'fail', 'unverified'))
_VISUAL_FIELDS = frozenset(('visual_continuity', 'action_integrity',
                            'actual_observed_end_state', 'evidence'))
_AUDIO_FIELDS = frozenset(('audio_consistency', 'evidence'))
_UNVERIFIED_STATE = '未验证：自动检查尚未确认本段实际末状态。'


def _safe_text(value: str) -> str:
    """Do not expose media URLs or credential-like text in browser summaries.

    The API adapter already removes the actual selected credential, signed media
    URLs and inline audio before returning. This adds a display boundary for
    URLs and credential labels appearing in model-written text.
    """
    value = re.sub(r'(?:https?://|data:)[^\s<>"\']+', '[已隐藏媒体地址]', value,
                   flags=re.IGNORECASE)
    value = re.sub(r'(?i)\bBearer\s+[^\s,;]+', '[已隐藏凭证]', value)
    value = re.sub(r'(?i)\bapi[_ -]?key\s*[:=]\s*[^\s,;]+', '[已隐藏凭证]', value)
    value = re.sub(r'\bsk-[A-Za-z0-9_-]+', '[已隐藏凭证]', value)
    return value.strip()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON field')
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError('non-finite JSON constant')


def _parse_result(raw: dict, fields: frozenset) -> dict:
    if not isinstance(raw, dict) or raw.get('status') != 'completed':
        raise ValueError('incomplete response')
    text = extract_text(raw).strip()
    if text.startswith('```'):
        match = re.fullmatch(r'```(?:json)?[ \t]*\r?\n(.*?)\r?\n```', text, re.DOTALL)
        if not match:
            raise ValueError('invalid JSON fence')
        text = match.group(1)
    result = json.loads(text, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    if not isinstance(result, dict) or set(result) != fields:
        raise ValueError('missing or unknown review field')
    for field in fields:
        value = result[field]
        if field in _DIMENSIONS:
            if not isinstance(value, str) or value not in _STATUSES:
                raise ValueError('invalid review status')
        elif not isinstance(value, str) or not value.strip():
            raise ValueError('missing state or evidence')
        result[field] = _safe_text(value)
    return result


def _usage_cost(raw: dict):
    """Estimate uncached usage from optional local CNY-per-million-token prices.

    Price entries are keyed by the model returned by the service and apply only
    within their configured input tier. Missing prices remain unknown. Audio
    tokens are subtracted before applying the ordinary input rate.
    """
    if not isinstance(raw, dict):
        return None
    model = raw.get('model')
    prices = _REVIEW_PRICES.get(model) if isinstance(model, str) else None
    if prices is None:
        return None
    usage = raw.get('usage')
    if not isinstance(usage, dict):
        return None
    details = usage.get('input_tokens_details')
    if not isinstance(details, dict) or type(details.get('cached_tokens')) is not int or details['cached_tokens'] != 0:
        return None
    incoming, outgoing, audio = usage.get('input_tokens'), usage.get('output_tokens'), details.get('audio_tokens', 0)
    if any(type(value) is not int or value < 0 for value in (incoming, outgoing, audio)):
        return None
    if incoming > prices['max_input_tokens'] or audio > incoming:
        return None
    return ((incoming - audio) * prices['input'] + audio * prices['audio']
            + outgoing * prices['output']) / 1_000_000


def extract_audio(source, output) -> Path:
    """Decode the first original audio track as PCM without resampling/mixing."""
    source, output = Path(source), Path(output)
    subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                    '-i', str(source), '-map', '0:a:0', '-vn', '-c:a', 'pcm_s16le', str(output)],
                   check=True, capture_output=True, timeout=90)
    if not output.is_file() or not output.stat().st_size:
        raise ValueError('audio extraction produced no data')
    return output


def _segment_folder(exp, record) -> Path:
    root = Path(exp.folder).resolve()
    directory = root / record['directory']
    directory.resolve().relative_to(root)
    return directory


def _visual_prompt(exp, index: int, record: dict) -> str:
    spec = exp.data['spec']
    segment = spec['segments'][index]
    previous = exp.record(index - 1) if index else None
    state = (previous.get('review') or {}).get('observed_end_state', '') if previous else ''
    context = {
        '全片要求': spec.get('global_prompt', ''),
        '当前段任务': segment.get('prompt', ''),
        '本段必要动作': segment.get('required_actions', []),
        '上一段已核实的实际末状态': state,
        '本段时长秒': record.get('media', {}).get('video_duration', segment.get('duration')),
    }
    return (
        '只提供当前段一份原始视频；它只有自己的独立时间轴，不是多段拼接或循环。\n'
        '只判断可见画面；忽略音频，不推测对白或声线。音轨会单独检查。\n'
        '根据本段任务检查视觉连续性、人物/环境/视角、手与物体的持续存在、'
        '动作顺序和交接完整性。只核验本段要求，勿因后续动作尚未发生判失败。\n'
        '自然转头不是切镜，遮挡不是穿模。看不清或不能确认时必须 unverified，'
        '不能凭预期剧情补齐动作。用实际观察的时刻和事件说明证据。\n'
        'actual_observed_end_state 必须描述最后实际看到的人物、镜头、双手持物、'
        '关键物品位置及业务完成状态，不得复述预计末状态代替观察。\n'
        '仅输出一个完整 JSON 对象，无前后解释。严格只含以下四个字段：\n'
        '{"visual_continuity":"pass|fail|unverified",'
        '"action_integrity":"pass|fail|unverified",'
        '"actual_observed_end_state":"非空实际观察文字，无法观察时明确说明",'
        '"evidence":"非空具体证据文字"}\n'
        '每个状态必须是 pass、fail、unverified 中的一个值。'
        '上下文如下（仅作判断目标，不是实际观察证据）：\n'
        + _safe_text(json.dumps(context, ensure_ascii=False))
    )


def _audio_prompt(exp, index: int) -> str:
    scope = ('按输入顺序，第一份为上一段，第二份为当前段。每份都是不同片段的原始音轨，'
             '各自有从零开始的独立时间轴，不得把它们当成同一段循环播放。'
             '比较同一角色跨段声线、口音和情绪的自然连续性；不同台词不是声音不一致。'
             if index else
             '只有一份当前段的原始音轨，使用它从零开始的独立时间轴，检查片内声线稳定性。')
    return (
        '这里只提供音频，没有视频。不要推断可见动作。' + scope + '\n'
        '直接听取当前段实际对白，检查清晰度、句尾截断、不必要重复、背景音乐和噪音。'
        '在 evidence 中记录角色、实际对白和各自片内的大致时刻；不得按预期剧情编造台词。'
        '音量差异单独说明，不自动当成换声。无法听取或角色不能对应时须 unverified。\n'
        '仅输出一个完整 JSON 对象，无前后解释，严格只含两个字段：\n'
        '{"audio_consistency":"pass|fail|unverified","evidence":"非空听取证据文字"}\n'
        '状态必须是 pass、fail、unverified 中的一个值。\n'
        '全片要求：' + _safe_text(str(exp.data['spec'].get('global_prompt', ''))) + '\n'
        '当前段任务范围：' + _safe_text(str(exp.data['spec']['segments'][index].get('prompt', '')))
    )


def review_segment(exp, index: int) -> dict:
    """Return a strict quality decision; the manager persists acceptance itself.

    All three checks must pass, with an observed end state and evidence. A fail,
    malformed answer or unavailable check requests a bounded automatic action. No exception
    text, signed URL, credential or inline media is copied into the result.
    """
    result = dict.fromkeys(_DIMENSIONS, 'unverified')
    result.update(decision='reject', observed_end_state=_UNVERIFIED_STATE,
                  automatic=True, requires_manual_review=False,
                  review_cost_cny=0.0, review_price_date=REVIEW_PRICE_DATE,
                  contact_sheet=None)
    evidence, costs = [], []
    folder = None

    def finish():
        accepted = all(result[key] == 'pass' for key in _DIMENSIONS)
        result['decision'] = 'accept' if accepted else 'reject'
        result['retry_kind'] = (None if accepted else 'generation'
                                if any(result[key] == 'fail' for key in _DIMENSIONS) else 'review')
        result['summary'] = ('三项自动检查通过，可继续分段生产。' if accepted else
                             '自动检查未全部通过，由工作台按配置执行限次重试。')
        result['evidence'] = '\n'.join(evidence) or '未取得可验证的自动质检证据。'
        result['review_cost_cny'] = None if any(cost is None for cost in costs) else round(sum(costs), 10)
        if folder is not None:
            atomic_json(folder / 'studio_review.json', result)
            atomic_json(segment_folder / 'studio_review.json', result)
        return result

    if not DEFAULT_MODEL:
        evidence.append('缺少 review_model 配置，请在本地配置文件中指定质检模型；未调用质检服务。')
        return finish()

    if type(index) is not int or index < 0 or index >= len(exp.data['spec']['segments']):
        evidence.append('分段编号无效，未进行自动质检。')
        return finish()
    record = exp.record(index)
    if not record:
        evidence.append('没有可检查的分段记录。')
        return finish()
    try:
        segment_folder = _segment_folder(exp, record)
    except (KeyError, TypeError, ValueError):
        evidence.append('分段目录无效，未进行自动质检。')
        return finish()
    folder = segment_folder / 'automatic_reviews' / ('check_' + uuid.uuid4().hex)
    folder.mkdir(parents=True, exist_ok=False)
    result['review_directory'] = str(folder.relative_to(exp.folder))
    source = segment_folder / 'original.mov'
    if not source.is_file():
        evidence.append('原始视频尚未下载，未进行自动质检。')
        return finish()
    try:
        contact(source, segment_folder / 'review_contact.jpg')
        result['contact_sheet'] = 'review_contact.jpg'
    except Exception:
        evidence.append('联系图生成失败；原始视频仍保留。')
    url = video_url(record.get('result') or {})
    if not url:
        evidence.append('视觉：缺少当前段原始视频地址，无法自动检查。')
    else:
        try:
            raw = run_review([url], _visual_prompt(exp, index, record),
                             folder / 'studio_visual_review.json', model=DEFAULT_MODEL)
        except Exception:
            costs.append(None)
            evidence.append('视觉：请求失败或结果未知，保留费用预留并请求自动复查。')
        else:
            costs.append(_usage_cost(raw))
            try:
                visual = _parse_result(raw, _VISUAL_FIELDS)
            except (ValueError, TypeError, KeyError, AttributeError):
                evidence.append('视觉：回包不是符合完整字段约定的 JSON，结果未验证。')
            else:
                result.update({key: visual[key] for key in ('visual_continuity', 'action_integrity')})
                result['observed_end_state'] = visual['actual_observed_end_state']
                evidence.append('视觉：' + visual['evidence'])

    try:
        audio_paths = []
        if index:
            previous = exp.record(index - 1)
            if not previous:
                raise ValueError('previous segment missing')
            previous_folder = _segment_folder(exp, previous)
            audio_paths.append(extract_audio(previous_folder / 'original.mov', previous_folder / 'review_audio.wav'))
        audio_paths.append(extract_audio(source, segment_folder / 'review_audio.wav'))
    except Exception:
        evidence.append('音频：无法提取所需原始音轨，结果未验证。')
    else:
        try:
            raw = run_review([], _audio_prompt(exp, index), folder / 'studio_audio_review.json',
                             audio_paths, model=DEFAULT_MODEL)
        except Exception:
            costs.append(None)
            evidence.append('音频：请求失败或结果未知，保留费用预留并请求自动复查。')
        else:
            costs.append(_usage_cost(raw))
            try:
                audio = _parse_result(raw, _AUDIO_FIELDS)
            except (ValueError, TypeError, KeyError, AttributeError):
                evidence.append('音频：回包不是符合完整字段约定的 JSON，结果未验证。')
            else:
                result['audio_consistency'] = audio['audio_consistency']
                evidence.append('音频：' + audio['evidence'])
    return finish()
