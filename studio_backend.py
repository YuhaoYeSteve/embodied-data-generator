"""Persistent automatic local batch queue around the existing Experiment.

New work and bounded automatic retries are authorized by the saved batch policy.
A lifetime process lock owns the queue; Experiment's own disk lock
and write-before-submit ledger protect individual requests. No raw model output
or remote media URL is returned through the public data methods.
"""
from __future__ import annotations

import copy
import fcntl
import json
import math
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

import longform_pipeline as lp
from longform_client import ArkCLIClient, _sanitize
from studio_config import SETTINGS
from studio_references import build_reference_assets, catalog, validate_reference_images
from studio_models import (DEFAULT_MODEL_FAMILY, get_model, infer_model_family, model_catalog,
                           resolve_selection, resolution_dimensions, validate_spec_model)


DEFAULT_PROFILE = SETTINGS['video_profile']
DEFAULT_MODEL = get_model(DEFAULT_MODEL_FAMILY)['model']
REVIEW_RESERVE_CNY = 1.0
QUALITY_FIELDS = ('visual_continuity', 'action_integrity', 'audio_consistency')
FAILURES = {'failed', 'cancelled', 'canceled', 'expired'}
UNKNOWN = {'submission_unknown', 'submitting'}
ACTIVE = {'queued', 'generating', 'reviewing', 'exporting'}
DEFAULT_AUTO_RETRY_LIMIT = 1
HOTEL_TITLES = {'01_greeting_reservation': '迎宾与预订确认',
                '02_identity_verification': '身份证核验',
                '03_order_room_preference': '房间安排',
                '04_keycard_farewell': '房卡交付与送客'}
TEMPLATE_FILES = {'object_sorting_60': 'object_sorting_60s.json',
                  'hotel_120_phone_reference': 'hotel_120s_phone_reference.json',
                  'hotel_120': 'hotel_120s_v4.json'}


def _text(value, limit=4000):
    value = _sanitize(value)
    value = re.sub(r'https?://[^\s<>"\']+', '[远程地址已隐藏]', value)
    value = re.sub(r'data:[^\s<>"\']+', '[媒体数据已隐藏]', value)
    return value[:limit]


def _public(value):
    if isinstance(value, str):
        return _text(value, 30000)
    if isinstance(value, list):
        return [_public(item) for item in value]
    if isinstance(value, dict):
        return {key: _public(item) for key, item in value.items()}
    return value


def _cost_sum(values, known_only=False):
    values = list(values)
    if not known_only and any(value is None for value in values):
        return None
    return math.fsum(value for value in values if value is not None)


def _money(value):
    return None if value is None else round(value, 6)


def _number(value, label, minimum=0, maximum=1000000):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(label + '必须是有限数字')
    if not minimum <= value <= maximum:
        raise ValueError('%s必须在%s至%s之间' % (label, minimum, maximum))
    return float(value)


def _integer(value, label, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError('%s必须是%s至%s之间的整数' % (label, minimum, maximum))
    return value


def _bounded_text(value, label, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError('%s必须为1至%d字的文字' % (label, maximum))
    return value


def _labels(value, label):
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError(label + '必须为不超过100项的文字数组')
    return [_bounded_text(item, label, 200) for item in value]


def _dataset(value):
    if not isinstance(value, dict):
        raise ValueError('dataset必须是JSON对象')
    limits = {'source_scenario': 30000, 'variant_id': 100, 'variation': 4000, 'task_label': 200}
    allowed = set(limits) | {'task_type', 'objects', 'annotation_scope'}
    if set(value) - allowed:
        raise ValueError('dataset包含不支持的字段')
    result = {key: _bounded_text(value[key], key, limit) for key, limit in limits.items() if key in value}
    if 'task_type' in value:
        if value['task_type'] not in ('manipulation', 'interaction'):
            raise ValueError('task_type必须为manipulation或interaction')
        result['task_type'] = value['task_type']
    if 'objects' in value:
        result['objects'] = _labels(value['objects'], 'objects')
    if value.get('annotation_scope', 'planned') != 'planned':
        raise ValueError('annotation_scope必须为planned，脚本标签不代表实测动作')
    result['annotation_scope'] = 'planned'
    return result


def _review_public(review):
    if not isinstance(review, dict):
        return None
    result = {key: _text(review[key], 20000) for key in
              ('decision', *QUALITY_FIELDS, 'observed_end_state', 'evidence', 'retry_kind', 'review_directory')
              if isinstance(review.get(key), str)}
    for key in ('reviewed_at', 'review_cost_cny'):
        value = review.get(key)
        if value is None or (isinstance(value, (int, float)) and not isinstance(value, bool)
                             and math.isfinite(value) and value >= 0):
            result[key] = value
    if isinstance(review.get('automatic'), bool):
        result['automatic'] = review['automatic']
    if isinstance(review.get('warnings'), list):
        result['warnings'] = [_text(item) for item in review['warnings'] if isinstance(item, str)][:30]
    return result


def _is_accepted(record):
    return bool(record and (record.get('review') or {}).get('decision') == 'accept')


def _retry_eligible(record):
    if not record or _is_accepted(record):
        return False
    return record['status'] in FAILURES | {'submission_rejected'} or (record['status'] == 'succeeded' and
                                            (record.get('review') or {}).get('decision') == 'reject')


def _pending(record):
    return bool(record and record.get('task_id') and record['status'] not in
                FAILURES | UNKNOWN | {'submission_rejected'} and
                (record['status'] != 'succeeded' or not record.get('media')))


class StudioManager:
    def __init__(self, root: Path, data_dir: Path, client_factory=None, reviewer=None,
                 exporter=None, poll_interval=10, start_workers=True):
        self.root = Path(root).resolve()
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / 'studio.json'
        self._lock = threading.RLock()
        self._submit_lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._workers = {}
        self._scheduler = None
        self._closed = False
        self.poll_interval = _number(poll_interval, '轮询间隔', .001, 3600)
        self.client_factory = client_factory or ArkCLIClient
        self.reviewer = reviewer
        self.exporter = exporter or self._export
        self._owner = (self.data_dir / '.manager.lock').open('a')
        try:
            fcntl.flock(self._owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._owner.close()
            raise RuntimeError('此工作台数据目录已被另一服务占用，请先停止原服务') from None
        try:
            self.data = json.loads(self.path.read_text()) if self.path.exists() else {
                'version': 1, 'batches': {}, 'jobs': {}}
            if self.data.get('version') != 1:
                raise ValueError('不支持此工作台数据版本')
            self._import_history()
            self._recover()
            self._save()
            if start_workers:
                self._scheduler = threading.Thread(target=self._schedule, name='studio-queue', daemon=True)
                self._scheduler.start()
        except BaseException:
            self._owner.close()
            raise

    def _save(self):
        lp.atomic_json(self.path, self.data)

    def _job(self, job_id, mutable=False):
        if job_id not in self.data['jobs']:
            raise KeyError('任务不存在')
        job = self.data['jobs'][job_id]
        if mutable and job.get('read_only'):
            raise ValueError('历史样片为只读，请新建批次')
        if mutable and self._closed:
            raise ValueError('工作台已经关闭')
        return job

    def _batch(self, batch_id):
        if batch_id not in self.data['batches']:
            raise KeyError('批次不存在')
        return self.data['batches'][batch_id]

    def _experiment(self, job):
        return lp.Experiment(job['run_dir'])

    def _assets(self, asset_id):
        if not asset_id:
            return []
        if not isinstance(asset_id, str) or not re.fullmatch(r'[0-9a-f]{32}', asset_id):
            raise ValueError('声音资产编号无效')
        path = self.data_dir / 'voice_assets' / (asset_id + '.json')
        if not path.is_file() or path.is_symlink():
            raise ValueError('声音资产不存在，请重新上传')
        try:
            assets = json.loads(path.read_text())
            valid = (isinstance(assets, list) and len(assets) == 1 and
                     assets[0]['role'] == 'reference_audio' and
                     assets[0]['content']['type'] == 'audio_url' and
                     isinstance(assets[0]['content']['audio_url']['url'], str) and
                     assets[0]['content']['audio_url']['url'].startswith('data:audio/') and
                     isinstance(assets[0]['description'], str))
        except (ValueError, KeyError, TypeError):
            valid = False
        if not valid:
            raise ValueError('声音资产记录不完整，请重新上传')
        return assets

    def bootstrap_data(self):
        reference_images = catalog(self.root)
        templates = []
        for template_id, filename in TEMPLATE_FILES.items():
            template = self.root / 'scenarios' / filename
            with_reference = template_id == 'hotel_120_phone_reference'
            if not template.is_file() or (with_reference and not reference_images):
                continue
            spec = json.loads(template.read_text())
            entry = {'id': template_id, 'name': spec['name'],
                     'description': ('包含首段参考图片，可编辑各段动作与时长。'
                                     if with_reference else '通用虚构任务示例，可编辑各段动作、场景与时长。'),
                     'duration_seconds': sum(s['duration'] for s in spec['segments']),
                     'segment_count': len(spec['segments']), 'spec': spec}
            if with_reference:
                entry['recommended_resolution'] = '720p'
            templates.append(entry)
        return {'templates': templates, 'reference_images': reference_images, 'models': model_catalog(),
                'settings': {'profile': DEFAULT_PROFILE, 'model': DEFAULT_MODEL, 'resolution': '480p',
                             'model_family': DEFAULT_MODEL_FAMILY, 'segment_duration_seconds': 30,
                             'quantity': 1, 'budget_cny': 200, 'concurrency': 1, 'review_mode': 'auto',
                             'auto_retry_limit': DEFAULT_AUTO_RETRY_LIMIT, 'budget_enforced': False},
                'environment': {'arkcli': bool(shutil.which('arkcli')), 'ffmpeg': bool(shutil.which('ffmpeg')),
                                'ffprobe': bool(shutil.which('ffprobe')), 'automatic_review': self.reviewer is not None},
                'voice_asset_available': False}

    def _normalize_spec(self, spec):
        if not isinstance(spec, dict):
            raise ValueError('场景脚本必须是JSON对象')
        segments = spec.get('segments')
        if not isinstance(segments, list) or not 2 <= len(segments) <= 20:
            raise ValueError('场景必须包含2至20个分段')
        if not isinstance(spec.get('global_prompt'), str) or not spec['global_prompt'].strip():
            raise ValueError('场景需要非空global_prompt')
        if len(spec['global_prompt']) > 30000:
            raise ValueError('全局提示词过长')
        normalized, segment_ids = [], set()
        for index, segment in enumerate(segments):
            if not isinstance(segment, dict):
                raise ValueError('每个分段必须是JSON对象')
            duration = _integer(segment.get('duration'), '分段时长', 4, 30)
            prompt = segment.get('prompt')
            if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 30000:
                raise ValueError('每个分段需要非空且不超过30000字的prompt')
            actions = segment.get('required_actions', [])
            if not isinstance(actions, list) or any(not isinstance(item, str) for item in actions):
                raise ValueError('required_actions必须为文字数组')
            row = {'id': str(segment.get('id', index + 1))[:100], 'duration': duration,
                   'prompt': prompt, 'required_actions': actions}
            if not row['id'].strip() or row['id'] in segment_ids:
                raise ValueError('每个分段需要非空且不重复的id')
            segment_ids.add(row['id'])
            if 'action_labels' in segment:
                row['action_labels'] = _labels(segment['action_labels'], 'action_labels')
            for key in ('title', 'name', 'expected_start_state', 'expected_end_state'):
                if key in segment:
                    if not isinstance(segment[key], str):
                        raise ValueError(key + '必须是文字')
                    row[key] = segment[key]
            normalized.append(row)
        duration = sum(row['duration'] for row in normalized)
        if not 60 <= duration <= 600:
            raise ValueError('所有分段时长合计必须在60至600秒之间')
        if spec.get('target_duration_seconds', duration) != duration:
            raise ValueError('目标时长必须等于分段时长之和')
        result = {'name': str(spec.get('name', '自定义具身场景'))[:120],
                  'version': str(spec.get('version', '1'))[:40], 'global_prompt': spec['global_prompt'],
                  'target_duration_seconds': duration, 'segments': normalized}
        if 'dataset' in spec:
            result['dataset'] = _dataset(spec['dataset'])
        if 'reference_images' in spec:
            result['reference_images'] = validate_reference_images(spec['reference_images'], segment_ids, self.root)
        return copy.deepcopy(result)

    def _config(self, config):
        if not isinstance(config, dict):
            raise ValueError('批次配置必须是JSON对象')
        selection = resolve_selection(config)
        template_id = config.get('template_id', 'hotel_120')
        if not isinstance(template_id, str) or template_id not in TEMPLATE_FILES:
            raise ValueError('模板不存在')
        quantity = _integer(config.get('quantity', 1), '生成数量', 1, 20)
        variations = config.get('variations', '')
        if not isinstance(variations, str) or len(variations) > 20000:
            raise ValueError('变化提示必须为不超过20000字的文本')
        explicit_variants = 'variants' in config
        if explicit_variants:
            if 'spec' in config or 'template_id' in config:
                raise ValueError('variants不能与spec或template_id同时提供')
            raw_variants = config['variants']
            if not isinstance(raw_variants, list) or not 1 <= len(raw_variants) <= 20:
                raise ValueError('variants必须包含1至20份完整脚本')
            if quantity != len(raw_variants):
                raise ValueError('生成数量必须等于variants脚本数量')
            if variations.strip():
                raise ValueError('variants已是完整脚本，不能再附加variations')
            specs = [self._normalize_spec(spec) for spec in raw_variants]
        else:
            spec = config.get('spec')
            if spec is None:
                path = self.root / 'scenarios' / TEMPLATE_FILES[template_id]
                if not path.is_file():
                    raise ValueError('酒店模板文件不存在')
                spec = json.loads(path.read_text())
            spec = self._normalize_spec(spec)
            lines = [line.strip() for line in variations.splitlines() if line.strip()]
            specs = []
            for index in range(quantity):
                varied = copy.deepcopy(spec)
                variation = lines[index] if len(lines) == quantity else variations.strip()
                if variation:
                    varied['global_prompt'] += '\n本次变化要求：' + variation
                specs.append(varied)
        name = config.get('name', specs[0]['name'])
        if not isinstance(name, str) or not name.strip() or len(name) > 120:
            raise ValueError('批次名称必须为1至120字')
        for spec in specs:
            validate_spec_model(spec, selection['model_family'], selection['resolution'])
        if config.get('review_mode', 'auto') not in ('manual', 'auto'):
            raise ValueError('审核模式必须为manual或auto')
        value = {'name': name.strip(), 'spec': specs[0], 'variants': specs,
                 'explicit_variants': explicit_variants, **selection, 'review_mode': 'auto',
                 'quantity': quantity,
                 'concurrency': _integer(config.get('concurrency', 1), '并发数', 1, 3),
                 'budget_cny': _number(config.get('budget_cny', 200), '参考预算', 0),
                 'auto_retry_limit': _integer(config.get('auto_retry_limit', DEFAULT_AUTO_RETRY_LIMIT), '自动重试次数', 0, 3),
                 'variations': variations, 'voice_asset_id': config.get('voice_asset_id')}
        for key, default in [('profile', DEFAULT_PROFILE)]:
            field = config.get(key, default)
            if not isinstance(field, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}', field):
                raise ValueError(key + '格式无效')
            value[key] = field
        return value

    def _estimate(self, config):
        width, height = resolution_dimensions(config['resolution'])
        jobs, generation_costs, review_costs = [], [], []
        for index, spec in enumerate(config['variants']):
            previous, costs = 0, []
            for segment in spec['segments']:
                costs.append(lp.estimate_cost(segment['duration'], previous, width, height,
                                              model_family=config['model_family'], resolution=config['resolution']))
                previous = segment['duration']
            generation = _cost_sum(costs)
            review = len(spec['segments']) * REVIEW_RESERVE_CNY
            generation_costs.append(generation)
            review_costs.append(review)
            jobs.append({'index': index, 'name': spec['name'], 'estimated_cny': _money(_cost_sum([generation, review])),
                         'generation_cny': _money(generation), 'review_reserve_cny': review,
                         'recommended_budget_cny': None if generation is None else math.ceil((generation * 1.05 + review) * 100) / 100,
                         'duration_seconds': spec['target_duration_seconds'], 'segment_count': len(spec['segments'])})
        count = len(jobs)
        generation, review = _cost_sum(generation_costs), math.fsum(review_costs)
        total = _cost_sum([generation, review])
        return {'estimated_per_job_cny': None if total is None else _money(total / count),
                'estimated_total_cny': _money(total),
                'recommended_budget_cny': None if generation is None else math.ceil((generation * 1.05 + review) * 100) / 100,
                'generation_per_job_cny': None if generation is None else _money(generation / count), 'review_reserve_per_job_cny': review / count,
                'duration_seconds': sum(job['duration_seconds'] for job in jobs) / count,
                'segment_count': sum(job['segment_count'] for job in jobs) / count,
                'jobs': jobs,
                'model_family': config['model_family'], 'budget_enforced': False,
                'cost_note': '仅供展示的用量估算，非结算账单；未知价格显示为空。含参考视频和检查预留，不因预算阻止执行，未包含可能的重试。'}

    def estimate(self, config):
        return self._estimate(self._config(config))

    def create_batch(self, config):
        normalized = self._config(config)
        estimate = self._estimate(normalized)
        voice_assets = self._assets(normalized['voice_asset_id'])
        # Resolve and read every variant's images before creating any run folder.
        assets_by_variant = [copy.deepcopy(voice_assets) + build_reference_assets(self.root, spec)
                             for spec in normalized['variants']]
        with self._lock:
            if self._closed:
                raise ValueError('工作台已经关闭')
            now = time.time()
            batch_id = uuid.uuid4().hex
            batch = {'id': batch_id, 'name': normalized['name'], 'created_at': now,
                     'budget_cny': normalized['budget_cny'], 'resolution': normalized['resolution'],
                     'concurrency': normalized['concurrency'], 'review_mode': normalized['review_mode'],
                     'model_family': normalized['model_family'], 'model': normalized['model'],
                     'auto_retry_limit': normalized['auto_retry_limit'], 'budget_enforced': False,
                     'estimated_cny': estimate['estimated_total_cny'], 'read_only': False}
            jobs = []
            for index, planned_spec in enumerate(normalized['variants']):
                job_id = uuid.uuid4().hex
                spec = copy.deepcopy(planned_spec)
                folder = self.data_dir / 'runs' / job_id
                lp.Experiment.create(folder, spec, normalized['profile'], normalized['model'],
                                     normalized['budget_cny'], normalized['resolution'], assets=assets_by_variant[index],
                                     model_family=normalized['model_family'], budget_enforced=False)
                title = spec['name'] if normalized['explicit_variants'] else normalized['name'] + ' · %02d' % (index + 1)
                jobs.append({'id': job_id, 'batch_id': batch_id, 'title': title,
                             'created_at': now + index * .00001, 'run_dir': str(folder), 'status': 'queued',
                             'paused': False, 'read_only': False, 'error': '', 'resume_stage': None,
                             'estimated_cny': estimate['jobs'][index]['estimated_cny'], 'retry_index': None,
                             'model_family': normalized['model_family'], 'automatic_retries': [], 'review_recovery_grants': [],
                             'review_attempts': [], 'logs': [{'at': now, 'message': '已创建自动批次任务，预算仅供展示'}]})
            self.data['batches'][batch_id] = batch
            for job in jobs:
                self.data['jobs'][job['id']] = job
            self._save()
            result = {'batch': self._batch_public(batch), 'jobs': [self._job_public(job) for job in jobs]}
            self._wake.set()
            return result

    def _costs(self, job, exp=None, known_only=False):
        exp = exp or self._experiment(job)
        if job.get('read_only'):
            # The imported work is the selected finished video, not the entire
            # research budget (external experiments and QA reservations).
            selected = [exp.record(index) for index in range(len(exp.data['spec']['segments']))]
            return (_cost_sum((row.get('actual_estimated_cny') or 0 for row in selected if row), known_only),
                    _cost_sum((row.get('reserved_cny', 0) for row in selected
                        if row and row.get('actual_estimated_cny') is None), known_only))
        spent = [exp.data.get('external_cost_cny', 0)]
        reserved = []
        for record in exp.data['segments']:
            actual = record.get('actual_estimated_cny')
            if actual is None:
                reserved.append(record.get('reserved_cny'))
            else:
                spent.append(actual)
        for row in job.get('review_attempts', []):
            if row.get('cost_cny') is None:
                reserved.append(row['reserved_cny'])
            else:
                spent.append(row['cost_cny'])
        return _cost_sum(spent, known_only), _cost_sum(reserved, known_only)

    def _batch_costs(self, batch_id, known_only=False):
        totals = [self._costs(job, known_only=known_only) for job in self.data['jobs'].values() if job['batch_id'] == batch_id]
        return _cost_sum((row[0] for row in totals), known_only), _cost_sum((row[1] for row in totals), known_only)

    def _current(self, exp):
        for index in range(len(exp.data['spec']['segments'])):
            if not _is_accepted(exp.record(index)):
                return index
        return len(exp.data['spec']['segments'])

    def _job_public(self, job, exp=None):
        exp = exp or self._experiment(job)
        count = len(exp.data['spec']['segments'])
        current = self._current(exp)
        record = exp.record(current) if current < count else None
        spent, reserved = self._costs(job, exp)
        known_spent, known_reserved = self._costs(job, exp, known_only=True)
        duration = exp.data['spec']['target_duration_seconds']
        completed = sum(exp.data['spec']['segments'][i]['duration'] for i in range(count) if _is_accepted(exp.record(i)))
        actual_duration = sum((exp.record(i).get('media') or {}).get('video_duration',
                              (exp.record(i).get('media') or {}).get('duration', 0))
                              for i in range(count) if exp.record(i) and exp.record(i)['status'] == 'succeeded')
        readonly = bool(job.get('read_only'))
        preview = self._media_candidate(job, 'preview', exp)
        # The HTTP poster renderer uses the completed preview, then the current
        # first segment, and finally a contact sheet. Version the same source.
        poster = preview or self._media_candidate(job, 'segment-0', exp) or self._media_candidate(job, 'poster', exp)
        result = {key: job[key] for key in ('id', 'batch_id', 'title', 'status', 'created_at', 'estimated_cny')}
        result.update(current_segment=min(current, count - 1), segment_count=count,
                      duration_seconds=duration, completed_seconds=completed,
                      actual_duration_seconds=round(actual_duration, 6),
                      spent_cny=_money(spent), reserved_cny=_money(reserved),
                      known_spent_cny=_money(known_spent), known_reserved_cny=_money(known_reserved),
                      error=_text(job.get('error', '')), paused=bool(job.get('paused')),
                      progress=100 if job['status'] == 'completed' else min(99, round(completed / duration * 100)),
                      preview_url=self._media_url(job, 'preview', preview, exp),
                      poster_url=self._media_url(job, 'poster', poster, exp),
                      can_review=False,
                      can_retry=not readonly and _retry_eligible(record) and job['status'] not in ACTIVE,
                      can_resume=not readonly and (job['status'] in {'paused', 'budget_blocked'} or
                                 (job['status'] == 'failed' and job.get('resume_stage') in {'poll', 'export', 'review'})),
                      read_only=readonly, resolution=exp.data['resolution'],
                      model_family=job.get('model_family', exp.data.get('model_family', DEFAULT_MODEL_FAMILY)),
                      review_mode=self._batch(job['batch_id'])['review_mode'],
                      auto_retry_limit=self._batch(job['batch_id']).get('auto_retry_limit', DEFAULT_AUTO_RETRY_LIMIT),
                      budget_enforced=False)
        dataset = exp.data['spec'].get('dataset') or {}
        labels = [label for segment in exp.data['spec']['segments']
                  for label in segment.get('action_labels', []) if isinstance(label, str)]
        result.update(task_type=_text(dataset.get('task_type', ''), 40),
                      task_label=_text(dataset.get('task_label', ''), 160),
                      source_scenario=_text(dataset.get('source_scenario', ''), 500),
                      objects=[_text(value, 120) for value in dataset.get('objects', [])
                               if isinstance(value, str)][:30],
                      action_labels=list(dict.fromkeys(_text(value, 100) for value in labels))[:64],
                      annotation_scope='planned')
        return _public(result)

    def _batch_public(self, batch):
        jobs = [job for job in self.data['jobs'].values() if job['batch_id'] == batch['id']]
        statuses = {job['status'] for job in jobs}
        if statuses == {'completed'}:
            status = 'completed'
        elif any(value in ACTIVE for value in statuses):
            status = next((value for value in ['generating', 'reviewing', 'exporting', 'queued'] if value in statuses), 'queued')
        else:
            status = next((value for value in ['submission_unknown', 'budget_blocked', 'awaiting_review', 'failed', 'paused'] if value in statuses), 'queued')
        spent, reserved = self._batch_costs(batch['id'])
        known_spent, known_reserved = self._batch_costs(batch['id'], known_only=True)
        return dict(_public(batch), status=status, spent_cny=_money(spent), reserved_cny=_money(reserved),
                    known_spent_cny=_money(known_spent), known_reserved_cny=_money(known_reserved),
                    total_jobs=len(jobs), completed_jobs=sum(job['status'] == 'completed' for job in jobs))

    def state(self):
        with self._lock:
            jobs = [self._job_public(job) for job in sorted(self.data['jobs'].values(), key=lambda row: row['created_at'], reverse=True)]
            batches = [self._batch_public(batch) for batch in sorted(self.data['batches'].values(), key=lambda row: row['created_at'], reverse=True)]
            return {'jobs': jobs, 'batches': batches, 'summary': {
                'total_jobs': len(jobs), 'completed_jobs': sum(job['status'] == 'completed' for job in jobs),
                'active_jobs': sum(job['status'] in ACTIVE for job in jobs),
                'awaiting_review': sum(job['can_review'] for job in jobs),
                'spent_cny': _money(_cost_sum(job['spent_cny'] for job in jobs)),
                'reserved_cny': _money(_cost_sum(job['reserved_cny'] for job in jobs)),
                'known_spent_cny': _money(_cost_sum(job['known_spent_cny'] for job in jobs)),
                'known_reserved_cny': _money(_cost_sum(job['known_reserved_cny'] for job in jobs))}}

    def job_detail(self, job_id):
        with self._lock:
            job = self._job(job_id)
            exp = self._experiment(job)
            segments = []
            for index, spec in enumerate(exp.data['spec']['segments']):
                row = exp.record(index)
                suggestion = next((item.get('result') for item in reversed(job.get('review_attempts', []))
                                   if item['index'] == index and (not row or item.get('attempt') == row.get('attempt', 1))), None)
                selected_review = (row.get('review') or suggestion) if row else None
                video = self._media_candidate(job, 'segment-%d' % index, exp)
                contact = self._media_candidate(job, 'contact-%d' % index, exp)
                title = spec.get('title') or spec.get('name') or HOTEL_TITLES.get(spec.get('id'), '第%d段' % (index + 1))
                segments.append({'index': index, 'id': spec.get('id'), 'title': title,
                                 'duration': spec['duration'], 'status': row['status'] if row else 'pending',
                                 'task_id': row.get('task_id') if row else None, 'review': _review_public(selected_review),
                                 'review_is_suggestion': bool(selected_review and row and not row.get('review')),
                                 'cost_cny': (row.get('actual_estimated_cny') if row.get('actual_estimated_cny') is not None
                                              else row.get('reserved_cny', 0)) if row else 0,
                                 'attempt': row.get('attempt', 1) if row else 0,
                                 'video_url': self._media_url(job, 'segment-%d' % index, video, exp),
                                 'contact_url': self._media_url(job, 'contact-%d' % index, contact, exp)})
            return _public({'job': self._job_public(job, exp), 'segments': segments, 'logs': job.get('logs', [])[-100:]})

    def metadata(self, job_id):
        with self._lock:
            job = self._job(job_id)
            exp = self._experiment(job)
            result = self.job_detail(job_id)
            result.update(spec=_public(exp.data['spec']), model=_text(exp.data['model']),
                          model_family=job.get('model_family', exp.data.get('model_family', DEFAULT_MODEL_FAMILY)),
                          profile=_text(exp.data['profile']),
                          cost_basis='selected video tasks usage estimate' if job.get('read_only') else '用量估算与未知费用预留，非结算账单',
                          attempts=[{key: record.get(key) for key in ('index', 'attempt', 'status', 'task_id',
                                    'estimated_cny', 'reserved_cny', 'actual_estimated_cny', 'sha256')}
                                    for record in exp.data['segments']],
                          automatic_review_costs=[{key: row.get(key) for key in ('index', 'attempt', 'status',
                                                  'check_attempt', 'reserved_cny', 'cost_cny')} for row in job.get('review_attempts', [])],
                          automatic_retries=_public(job.get('automatic_retries', [])),
                          review_recovery_grants=_public(job.get('review_recovery_grants', [])), budget_enforced=False)
            return _public(result)

    def _media_url(self, job, kind, path, exp):
        if path is None:
            return None
        stat = path.stat()
        identity = [kind, path.name, stat.st_mtime_ns, stat.st_size]
        if kind != 'preview':
            index = 0 if kind == 'poster' else int(kind.rsplit('-', 1)[1])
            row = exp.record(index)
            identity.append(row.get('attempt', 1) if row else None)
        # Query values contain only an opaque fingerprint, not task IDs, paths,
        # signed model URLs or credentials. A stable candidate stays cacheable.
        return '/api/media/%s/%s?v=%s' % (job['id'], kind, lp.digest(identity)[:16])

    def _media_candidate(self, job, kind, exp=None):
        exp = exp or self._experiment(job)
        if kind == 'preview':
            candidates = [Path(job['preview_path'])] if job.get('preview_path') else []
        elif kind == 'poster':
            candidates = [exp.folder / exp.record(0)['directory'] / name
                          for name in ('review_contact.jpg', 'contact.jpg')] if exp.record(0) else []
        else:
            match = re.fullmatch(r'(segment|contact)-(\d+)', kind)
            if not match:
                return None
            index = int(match.group(2))
            if not 0 <= index < len(exp.data['spec']['segments']):
                return None
            row = exp.record(index)
            names = ('original.mov',) if match.group(1) == 'segment' else ('review_contact.jpg', 'contact.jpg')
            candidates = [exp.folder / row['directory'] / name for name in names] if row else []
        for path in candidates:
            resolved = path.resolve()
            roots = [self.data_dir] if not job.get('read_only') else [self.root / 'experiments']
            if path.is_file() and any(resolved.is_relative_to(root.resolve()) for root in roots):
                return resolved
        return None

    def media_path(self, job_id, kind):
        with self._lock:
            job = self._job(job_id)
            path = self._media_candidate(job, kind)
            if path is None:
                raise FileNotFoundError('媒体尚未生成或不存在')
            return path

    def _set(self, job, status, message=None, error='', resume_stage=None):
        job.update(status=status, error=_text(error), resume_stage=resume_stage)
        if message:
            job.setdefault('logs', []).append({'at': time.time(), 'message': _text(message)})
            job['logs'] = job['logs'][-200:]
        self._save()

    def pause(self, job_id):
        with self._lock:
            job = self._job(job_id, mutable=True)
            if job['status'] in {'completed', 'submission_unknown'}:
                raise ValueError('当前任务不能暂停')
            job['paused'] = True
            if job['status'] == 'failed' and job.get('resume_stage') == 'poll':
                self._set(job, 'failed', '已暂停；此前查询失败，需显式恢复才继续查询',
                          error=job.get('error', ''), resume_stage='poll')
                return self.job_detail(job_id)
            self._set(job, 'paused', '已暂停后续提交；在途任务继续查询并保留下载结果')
            self._wake.set()
            return self.job_detail(job_id)

    def resume(self, job_id):
        with self._lock:
            job = self._job(job_id, mutable=True)
            if not self._job_public(job)['can_resume']:
                raise ValueError('当前任务不能恢复；失败候选需显式重试，未知提交需先核对原任务')
            if job.get('resume_stage') == 'review' and self.reviewer is not None:
                exp = self._experiment(job)
                index = self._current(exp)
                row = exp.record(index)
                if row and row['status'] == 'succeeded' and row.get('media') and not row.get('review'):
                    attempt = row.get('attempt', 1)
                    checks = [item for item in job['review_attempts']
                              if item['index'] == index and item.get('attempt', 1) == attempt]
                    grants = [item for item in job.get('review_recovery_grants', [])
                              if item['index'] == index and item['attempt'] == attempt]
                    base = self._batch(job['batch_id']).get('auto_retry_limit', DEFAULT_AUTO_RETRY_LIMIT) + 1
                    if len(checks) >= base + len(grants):
                        job.setdefault('review_recovery_grants', []).append({
                            'id': uuid.uuid4().hex, 'index': index, 'attempt': attempt,
                            'check_attempt': len(checks) + 1, 'created_at': time.time(), 'consumed_at': None})
            job['paused'] = False
            self._set(job, 'queued', '继续已有任务，从已保存的任务ID或检查点恢复')
            self._wake.set()
            return self.job_detail(job_id)

    def retry(self, job_id):
        with self._lock:
            job = self._job(job_id, mutable=True)
            exp = self._experiment(job)
            index = self._current(exp)
            row = exp.record(index)
            if not _retry_eligible(row) or job_id in self._workers or job.get('retry_index') is not None:
                raise ValueError('仅明确未受理、失败、取消、过期或审核拒绝的候选可显式重试；未知提交禁止重复付费')
            if any(item['index'] > index for item in exp.data['segments']):
                raise ValueError('已有下游段，不能重试上游')
            job.update(paused=False, retry_index=index, retry_attempt=row.get('attempt', 1) + 1)
            self._set(job, 'queued', '已确认重试此段，保留原候选和全部费用')
            self._wake.set()
            return self.job_detail(job_id)

    def review(self, job_id, payload):
        with self._lock:
            self._job(job_id, mutable=True)
            raise ValueError('工作台已改为自动验收，不再支持提交人工逐段审核；历史审核记录保持不变')

    def update_budget(self, batch_id, value):
        budget = _number(value, '参考预算', 0)
        with self._submit_lock, self._lock:
            batch = self._batch(batch_id)
            if batch.get('read_only'):
                raise ValueError('历史样片预算为只读')
            batch['budget_cny'] = budget
            self._save()
            return self._batch_public(batch)

    def _import_history(self):
        folder = self.root / 'experiments' / 'hotel_120s_v4'
        preview = self.root / 'experiments' / 'final_120s' / 'derived_preview.mov'
        if not (folder / 'experiment.json').is_file() or not preview.is_file():
            return
        exp = lp.Experiment(folder)
        count = len(exp.data['spec']['segments'])
        if not all(_is_accepted(exp.record(index)) for index in range(count)):
            return
        created = exp.data['created_at']
        batch_id, job_id = 'history-hotel', 'history-hotel-120'
        selected_cost = sum(exp.record(index).get('actual_estimated_cny') or 0 for index in range(count))
        if job_id in self.data['jobs']:
            self.data['jobs'][job_id]['estimated_cny'] = selected_cost
            self.data['batches'][self.data['jobs'][job_id]['batch_id']]['estimated_cny'] = selected_cost
            return
        self.data['batches'][batch_id] = {'id': batch_id, 'name': '已有酒店120秒样片', 'created_at': created,
            'budget_cny': exp.data['budget_cny'], 'resolution': exp.data['resolution'], 'concurrency': 1,
            'review_mode': 'manual', 'estimated_cny': selected_cost, 'read_only': True}
        self.data['jobs'][job_id] = {'id': job_id, 'batch_id': batch_id, 'title': exp.data['spec']['name'],
            'created_at': created, 'run_dir': str(folder), 'status': 'completed', 'paused': False, 'read_only': True,
            'preview_path': str(preview), 'error': '', 'resume_stage': None, 'retry_index': None,
            'estimated_cny': selected_cost, 'review_attempts': [],
            'logs': [{'at': created, 'message': '导入已有本地实验样片；历史记录只读，审核通过仅代表允许实验续写。'}]}

    def _recover(self):
        unfinished_batches = {job['batch_id'] for job in self.data['jobs'].values()
                              if not job.get('read_only') and job['status'] != 'completed'}
        for batch in self.data['batches'].values():
            if not batch.get('read_only') and batch['id'] in unfinished_batches:
                batch.update(review_mode='auto', budget_enforced=False)
                batch.setdefault('auto_retry_limit', DEFAULT_AUTO_RETRY_LIMIT)
        for job in self.data['jobs'].values():
            if job.get('read_only') or job['status'] == 'completed':
                continue
            exp = self._experiment(job)
            family = exp.data.get('model_family')
            family = (family if family in {entry['id'] for entry in model_catalog()} else
                      infer_model_family(family, default=infer_model_family(exp.data['model'], default=DEFAULT_MODEL_FAMILY)))
            job['model_family'] = family
            self._batch(job['batch_id']).setdefault('model_family', family)
            job.setdefault('automatic_retries', [])
            job.setdefault('review_recovery_grants', [])
            exp.data.update(model_family=family, budget_enforced=False)
            exp.save()
            checks = {}
            for item in job.setdefault('review_attempts', []):
                pair = (item['index'], item.get('attempt', 1))
                checks[pair] = checks.get(pair, 0) + 1
                item.setdefault('check_attempt', checks[pair])
            retry_index = job.get('retry_index')
            if retry_index is not None:
                retried = exp.record(retry_index)
                if retried and retried.get('attempt', 1) >= job.get('retry_attempt', float('inf')):
                    # Experiment already durably recorded this authorized retry.
                    # The queue flag may predate the crash; it is not permission
                    # for yet another candidate even if this one has failed.
                    job.update(retry_index=None, retry_attempt=None)
            current = self._current(exp)
            row = exp.record(current)
            if (job['status'] == 'failed' and job.get('resume_stage') is None and row and
                    row['status'] == 'succeeded' and row.get('media') and not row.get('review')):
                attempt = row.get('attempt', 1)
                granted = sum(item['index'] == current and item['attempt'] == attempt
                              for item in job['review_recovery_grants'])
                allowance = self._batch(job['batch_id']).get('auto_retry_limit', DEFAULT_AUTO_RETRY_LIMIT) + 1 + granted
                if checks.get((current, attempt), 0) >= allowance:
                    # Repair an older exhausted-review checkpoint without
                    # scheduling work or granting another paid check.
                    job['resume_stage'] = 'review'
            if row and row['status'] in UNKNOWN and not row.get('task_id'):
                job.update(status='submission_unknown', error='提交结果未知，保留原记录与预留；请核对原任务，禁止自动重发')
            elif job.get('paused') and job['status'] != 'failed':
                job['status'] = 'paused'
            elif job['status'] in ACTIVE | {'awaiting_review', 'budget_blocked'}:
                job['status'] = 'queued'

    def _should_run(self, job):
        if job.get('read_only'):
            return False
        if job['status'] in ACTIVE:
            return True
        if job['status'] == 'paused':
            exp = self._experiment(job)
            return _pending(exp.record(self._current(exp)))
        return False

    def _schedule(self):
        while not self._stop.is_set():
            with self._lock:
                counts = {}
                for job_id in self._workers:
                    batch_id = self.data['jobs'][job_id]['batch_id']
                    counts[batch_id] = counts.get(batch_id, 0) + 1
                for job in self.data['jobs'].values():
                    batch = self.data['batches'][job['batch_id']]
                    if (job['id'] in self._workers or counts.get(batch['id'], 0) >= batch['concurrency'] or
                            not self._should_run(job)):
                        continue
                    worker = threading.Thread(target=self._run, args=(job['id'],), name='studio-' + job['id'][:8], daemon=True)
                    self._workers[job['id']] = worker
                    counts[batch['id']] = counts.get(batch['id'], 0) + 1
                    worker.start()
            self._wake.wait(.1)
            self._wake.clear()

    def _submit(self, job, exp, index, client):
        # Keep write-before-submit serialized. Budget figures are informational.
        with self._submit_lock:
            with self._lock:
                if self._stop.is_set() or job['paused']:
                    return False
                if self.reviewer is None:
                    self._set(job, 'failed', error='自动检查器未配置，尚未提交新视频', resume_stage='review')
                    return False
                exp = self._experiment(job)
                exp.data['budget_enforced'] = False
                exp.save()
                resample = job.get('retry_index') == index
                self._set(job, 'generating', '正在%s第%d段；请求先持久化再提交' % ('重试' if resample else '生成', index + 1))
            try:
                exp.submit(index, client, resample=resample)
            except Exception as exc:
                with self._lock:
                    current = self._experiment(job).record(index)
                    job.update(retry_index=None, retry_attempt=None)
                    unknown = bool(current and current['status'] in UNKNOWN)
                    message = ('提交结果未知，已保留预留；请核对原任务，禁止自动重发' if unknown else
                               '提交失败：' + _text(exc))
                    self._set(job, 'submission_unknown' if unknown else 'failed', message, error=message)
                    if not unknown and _retry_eligible(current):
                        return self._automatic_retry(job, index, current, '明确未受理或生成失败')
                return False
            with self._lock:
                job.update(retry_index=None, retry_attempt=None)
                if job['paused']:
                    self._set(job, 'paused')
                else:
                    self._save()
            return True

    def _automatic_retry(self, job, index, record, reason):
        """Persist one bounded candidate retry without overriding a pause."""
        if self._stop.is_set() or job['paused']:
            if job['paused']:
                self._set(job, 'paused')
            return False
        exp = self._experiment(job)
        latest = exp.record(index)
        if not latest or latest.get('attempt', 1) != record.get('attempt', 1) or not _retry_eligible(latest):
            return False
        if any(row['index'] > index for row in exp.data['segments']):
            self._set(job, 'failed', error='已有下游分段，不能自动重试上游候选')
            return False
        limit = self._batch(job['batch_id']).get('auto_retry_limit', DEFAULT_AUTO_RETRY_LIMIT)
        retries = job.setdefault('automatic_retries', [])
        used = sum(row['index'] == index for row in retries)
        if used >= limit:
            self._set(job, 'failed', '自动生成重试次数已耗尽，任务已停止',
                      error='%s；本段自动重试已达%d次，保留全部候选和费用' % (reason, limit))
            return False
        target = latest.get('attempt', 1) + 1
        retries.append({'index': index, 'from_attempt': latest.get('attempt', 1), 'to_attempt': target,
                        'reason': _text(reason), 'created_at': time.time()})
        job.update(retry_index=index, retry_attempt=target)
        self._set(job, 'queued', '第%d段自动重试%d/%d；保留原候选及费用' % (index + 1, used + 1, limit))
        return True

    def _automatic_review(self, job, exp, index):
        """Accept verified results, resample failures, and recheck uncertainty."""
        attempt = exp.record(index).get('attempt', 1)
        while not self._stop.is_set():
            with self._submit_lock, self._lock:
                if job['paused']:
                    return False
                if self.reviewer is None:
                    self._set(job, 'failed', error='自动检查器未配置', resume_stage='review')
                    return False
                exp = self._experiment(job)
                row = exp.record(index)
                if not row or row.get('attempt', 1) != attempt:
                    return False
                if row.get('review'):
                    if _is_accepted(row):
                        return True
                    return self._automatic_retry(job, index, row, '当前候选已被拒绝')
                checks = [item for item in job['review_attempts']
                          if item['index'] == index and item.get('attempt', 1) == attempt]
                previous = checks[-1] if checks else None
                if previous and previous['status'] == 'running':
                    # A prior process stopped with an in-flight review. Its cost
                    # remains reserved and it consumes its saved check attempt.
                    previous['status'] = 'interrupted'
                    self._save()
                result = previous.get('result') if previous else None
                accepted = bool(result and result.get('decision') == 'accept' and
                                all(result.get(key) == 'pass' for key in QUALITY_FIELDS) and
                                isinstance(result.get('observed_end_state'), str) and result['observed_end_state'].strip() and
                                isinstance(result.get('evidence'), str) and result['evidence'].strip())
                failure = bool(result and any(result.get(key) == 'fail' for key in QUALITY_FIELDS))
                failure_reason = '自动检查确认画面、动作或声音不合格'
                if accepted:
                    try:
                        exp.review(index, result)
                    except ValueError as exc:
                        # An otherwise positive verdict cannot waive the actual
                        # media-duration or immutable-upstream checks.
                        failure = True
                        failure_reason = '自动验收失败：' + _text(exc)
                    else:
                        self._set(job, 'generating', '第%d段自动检查通过' % (index + 1))
                        return True
                if failure:
                    rejected = {key: result.get(key) if result.get(key) in {'pass', 'fail', 'unverified'}
                                else 'unverified' for key in QUALITY_FIELDS}
                    rejected.update(decision='reject', automatic=True,
                                    observed_end_state=result.get('observed_end_state') or '未核实：当前候选不能作为连续参考。',
                                    evidence=result.get('evidence') or failure_reason)
                    try:
                        exp.review(index, rejected)
                    except ValueError as exc:
                        self._set(job, 'failed', error='不能修改当前候选审核：' + _text(exc))
                        return False
                    return self._automatic_retry(job, index, exp.record(index), failure_reason)
                limit = self._batch(job['batch_id']).get('auto_retry_limit', DEFAULT_AUTO_RETRY_LIMIT)
                grants = [item for item in job.get('review_recovery_grants', [])
                          if item['index'] == index and item['attempt'] == attempt]
                if len(checks) >= limit + 1 + len(grants):
                    self._set(job, 'failed', '自动复查次数已耗尽，保留原片及全部检查结果',
                              error='自动检查仍未取得完整结论；已检查%d次，可显式恢复以再检查原片一次' % len(checks),
                              resume_stage='review')
                    return False
                current_check = {'index': index, 'attempt': attempt, 'check_attempt': len(checks) + 1,
                                 'status': 'running', 'reserved_cny': REVIEW_RESERVE_CNY,
                                 'cost_cny': None, 'created_at': time.time()}
                recovery = next((item for item in grants if item['check_attempt'] == current_check['check_attempt']
                                 and item.get('consumed_at') is None), None)
                if recovery is not None:
                    recovery['consumed_at'] = current_check['created_at']
                    current_check['recovery_grant_id'] = recovery['id']
                job['review_attempts'].append(current_check)
                suffix = '（显式恢复授权的一次复查）' if recovery else ('（复查%d/%d）' % (len(checks), limit) if checks else '')
                self._set(job, 'reviewing', '第%d段自动检查%s；画面与声音分别核验' % (index + 1, suffix))
            try:
                result = self.reviewer(exp, index)
                if not isinstance(result, dict):
                    raise ValueError('自动检查未返回结构化对象')
                result = _review_public(result)
            except Exception as exc:
                result = dict(decision='reject', observed_end_state='', evidence='自动检查失败：' + _text(exc),
                              automatic=True, **{key: 'unverified' for key in QUALITY_FIELDS})
            result['automatic'] = True
            with self._lock:
                actual = result.get('review_cost_cny')
                if isinstance(actual, (int, float)) and not isinstance(actual, bool) and math.isfinite(actual) and actual >= 0:
                    current_check['cost_cny'] = actual
                current_check.update(status='done', result=result)
                self._save()
                # Paid work already in flight may return after pause. Preserve
                # its evidence/cost, but do not accept, recheck, or resample now.
                if job['paused']:
                    self._set(job, 'paused')
                    return False
        return False

    def _run(self, job_id):
        try:
            with self._lock:
                job = self._job(job_id)
                exp = self._experiment(job)
                client = self.client_factory(exp.data['profile'], exp.data['model'])
            while not self._stop.is_set():
                with self._lock:
                    exp = self._experiment(job)
                    index = self._current(exp)
                    count = len(exp.data['spec']['segments'])
                    row = exp.record(index) if index < count else None
                    if job['paused'] and not _pending(row):
                        self._set(job, 'paused')
                        return
                if index == count:
                    with self._lock:
                        self._set(job, 'exporting', '全部分段已通过检查，正在合成并验证时间轴')
                    try:
                        preview = Path(self.exporter(exp)).resolve()
                        if not preview.is_file() or not preview.is_relative_to(self.data_dir):
                            raise ValueError('导出器未返回有效工作台媒体文件')
                    except Exception as exc:
                        with self._lock:
                            self._set(job, 'failed', '合成失败，可恢复后再次导出', error=_text(exc), resume_stage='export')
                        return
                    with self._lock:
                        job['preview_path'] = str(preview)
                        self._set(job, 'completed', '成片已生成，可本地预览与下载')
                    return
                if row is None or job.get('retry_index') == index:
                    if not self._submit(job, exp, index, client):
                        return
                    continue
                if row['status'] in UNKNOWN and not row.get('task_id'):
                    with self._lock:
                        self._set(job, 'submission_unknown', error='提交结果未知，请核对原任务，禁止自动重发')
                    return
                if row['status'] in FAILURES or row['status'] == 'submission_rejected' or _retry_eligible(row):
                    with self._lock:
                        if self._automatic_retry(job, index, row, '当前候选生成失败或未通过检查'):
                            continue
                    return
                if _pending(row):
                    with self._lock:
                        if not job['paused']:
                            self._set(job, 'generating')
                    try:
                        result = exp.poll(index, client)
                    except Exception as exc:
                        with self._lock:
                            self._set(job, 'failed',
                                      '查询或下载失败，保留现有任务ID；恢复只会继续查询',
                                      error=_text(exc), resume_stage='poll')
                        return
                    if lp.phase(result) not in FAILURES | {'succeeded'}:
                        self._stop.wait(self.poll_interval)
                    continue
                if row['status'] == 'succeeded' and row.get('media'):
                    with self._lock:
                        if job['paused']:
                            self._set(job, 'paused')
                            return
                    if not self._automatic_review(job, exp, index):
                        return
                    continue
                with self._lock:
                    self._set(job, 'failed', error='任务状态无法识别，原记录保留；请核对现有任务')
                return
        except Exception as exc:
            with self._lock:
                if job_id in self.data['jobs']:
                    self._set(self.data['jobs'][job_id], 'failed', '后台任务已停止，保留所有现有记录', error=_text(exc))
        finally:
            with self._lock:
                self._workers.pop(job_id, None)
                self._wake.set()
                self._release_owner_if_stopped()

    @staticmethod
    def _export(exp):
        from assemble_review import assemble_preview
        exp.assemble()
        inputs = [exp.folder / exp.record(index)['directory'] / 'original.mov'
                  for index in range(len(exp.data['spec']['segments']))]
        folder = exp.folder / ('export_' + uuid.uuid4().hex[:12])
        result = assemble_preview(inputs, folder)
        return Path(result['preview_file'])

    def _release_owner_if_stopped(self):
        if self._closed and not self._workers and not self._owner.closed:
            fcntl.flock(self._owner, fcntl.LOCK_UN)
            self._owner.close()

    def close(self):
        with self._lock:
            self._closed = True
            self._stop.set()
            self._wake.set()
        if self._scheduler:
            self._scheduler.join(timeout=2)
        for thread in list(self._workers.values()):
            thread.join(timeout=2)
        with self._lock:
            self._release_owner_if_stopped()
