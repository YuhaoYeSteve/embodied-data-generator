"""Ark video-task adapter compatible with the customer's pipeline_30s.py.

Configuration comes only from this process's environment. API keys are never
written to the task journal or forwarded to video-download URLs.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import socket
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen


class ArkError(RuntimeError):
    pass


def _settings():
    key = os.environ.get('ARK_API_KEY', '').strip()
    base = os.environ.get('ARK_BASE_URL', '').strip().rstrip('/')
    model = os.environ.get('ARK_MODEL', 'doubao-seedance-2-5-260628').strip()
    if not key:
        raise ArkError('缺少 ARK_API_KEY 环境变量。')
    if not base:
        raise ArkError('缺少 ARK_BASE_URL；请确认接口地址后设置。')
    parsed = urlparse(base)
    if parsed.scheme not in ('https', 'http') or not parsed.netloc or parsed.username or parsed.query or parsed.fragment:
        raise ArkError('ARK_BASE_URL 应为不含凭证、查询参数的 HTTP(S) 接口根地址。')
    if parsed.scheme == 'http' and parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
        raise ArkError('非本地接口请使用 HTTPS，避免明文传输 API Key。')
    if not model:
        raise ArkError('缺少 ARK_MODEL。')
    return key, base, model


def _number(name, default):
    try:
        value = float(os.environ.get(name, default))
    except ValueError:
        raise ArkError(f'{name} 必须是数字。') from None
    if value < 0:
        raise ArkError(f'{name} 不能为负数。')
    return value


def _redact(text, key):
    return str(text).replace(key, '[REDACTED]') if key else str(text)


def _request(method, path, body=None):
    key, base, _ = _settings()
    encoded = json.dumps(body, ensure_ascii=False).encode('utf-8') if body is not None else None
    req = Request(base + path, data=encoded, method=method, headers={
        'Authorization': 'Bearer ' + key,
        'Content-Type': 'application/json',
        'Accept': 'application/json',
    })
    try:
        with urlopen(req, timeout=_number('ARK_HTTP_TIMEOUT', '60')) as response:
            raw = response.read()
    except HTTPError as exc:
        detail = _redact(exc.read(16384).decode('utf-8', 'replace'), key)
        raise ArkError(f'HTTP {exc.code}: {detail}') from None
    except (URLError, TimeoutError, socket.timeout, ConnectionError, OSError) as exc:
        detail = _redact(str(exc), key)
        if method == 'POST':
            raise ArkError('提交状态未知；为避免重复生成，不自动重试。请先查控制台任务。' + detail) from None
        raise ArkError('查询请求失败；可使用原 task_id 继续查询。' + detail) from None
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        label = '提交状态未知；请先查控制台任务。' if method == 'POST' else ''
        raise ArkError(label + '接口未返回有效 JSON。') from None
    if not isinstance(result, dict):
        raise ArkError('接口返回值不是 JSON 对象。')
    return result


def _journal_path():
    return Path(os.environ.get('ARK_TASK_JOURNAL', str(Path(__file__).with_name('.ark_tasks.json'))))


def _read_journal(path):
    if not path.exists():
        return {}
    try:
        result = json.loads(path.read_text())
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (ValueError, OSError):
        raise ArkError('任务记录无法读取；请先核对已提交任务，避免重复生成。') from None


def _write_journal(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + '.', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def create_video_task(content, duration):
    """Submit once and persist the task ID; resume identical requests on rerun."""
    key, base, model = _settings()
    if not isinstance(duration, int) or (duration <= 0 and duration != -1):
        raise ArkError('duration 应为正整数或模型支持的 -1 自动时长。')
    body = {'model': model, 'content': content, 'duration': duration}
    for env, field in [('ARK_RESOLUTION', 'resolution'), ('ARK_RATIO', 'ratio')]:
        value = os.environ.get(env, '').strip()
        if value:
            body[field] = value
    for env, field in [('ARK_GENERATE_AUDIO', 'generate_audio'), ('ARK_RETURN_LAST_FRAME', 'return_last_frame')]:
        value = os.environ.get(env, '').strip().lower()
        if value:
            if value not in ('true', 'false', '1', '0'):
                raise ArkError(f'{env} 应为 true 或 false。')
            body[field] = value in ('true', '1')
    canonical = json.dumps([base, hashlib.sha256(key.encode()).hexdigest(), body], sort_keys=True, ensure_ascii=False)
    fingerprint = hashlib.sha256(canonical.encode()).hexdigest()
    path = _journal_path()
    journal = _read_journal(path)
    previous = journal.get(fingerprint)
    if previous:
        if previous.get('id'):
            return previous['id']
        raise ArkError('该请求的提交状态未知；请先查控制台任务，不自动重复提交。')
    journal[fingerprint] = {'status': 'submitting', 'model': model, 'created_at': int(time.time())}
    _write_journal(path, journal)
    try:
        result = _request('POST', '/contents/generations/tasks', body)
    except ArkError as exc:
        if str(exc).startswith('HTTP 4') and not str(exc).startswith('HTTP 408'):
            journal.pop(fingerprint, None)
            _write_journal(path, journal)
        raise
    task_id = result.get('id')
    if not isinstance(task_id, str) or not task_id:
        raise ArkError('提交响应缺少 task_id；状态未知，请先查控制台任务。')
    journal[fingerprint].update({'id': task_id, 'status': 'submitted'})
    _write_journal(path, journal)
    return task_id


def get_video_task(task_id):
    if not isinstance(task_id, str) or not task_id.strip():
        raise ArkError('task_id 不能为空。')
    return _request('GET', '/contents/generations/tasks/' + quote(task_id, safe=''))


def poll_video_task(task_id):
    """Wait for one task; never create a replacement task on failure/timeout."""
    deadline = time.monotonic() + _number('ARK_POLL_TIMEOUT', '1800')
    interval = _number('ARK_POLL_INTERVAL', '10')
    last_status = None
    while True:
        result = get_video_task(task_id)
        status = result.get('status')
        if status != last_status:
            print(f'  {task_id}: {status}', flush=True)
            last_status = status
        if status == 'succeeded':
            url = (result.get('content') or {}).get('video_url')
            if not url:
                raise ArkError(f'{task_id} succeeded 但缺少 content.video_url。')
            return url
        if status in ('failed', 'cancelled', 'canceled', 'expired'):
            key, _, _ = _settings()
            detail = _redact(json.dumps(result.get('error', {}), ensure_ascii=False), key)
            raise ArkError(f'{task_id} {status}: {detail}')
        if status not in ('queued', 'running', 'pending'):
            raise ArkError(f'{task_id} 返回未知状态：{status!r}')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ArkError(f'等待超时；保留 task_id={task_id}，稍后继续查询，不自动重新提交。')
        time.sleep(min(interval, remaining))


def download_file(url, output_path):
    """Download atomically, without forwarding the Ark Authorization header."""
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = Path(str(target) + '.part')
    try:
        with urlopen(url, timeout=_number('ARK_DOWNLOAD_TIMEOUT', '120')) as response, partial.open('wb') as stream:
            expected = response.headers.get('Content-Length')
            received = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                stream.write(chunk)
                received += len(chunk)
            if expected is not None and received != int(expected):
                raise ArkError('下载不完整，未替换目标文件。')
        if partial.stat().st_size == 0:
            raise ArkError('下载结果为空，未替换目标文件。')
        partial.replace(target)
    except Exception:
        partial.unlink(missing_ok=True)
        raise
