"""Local HTML workspace for the existing long-form generation pipeline."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import os
import secrets
import shutil
import subprocess
import tempfile
import threading
import uuid
import webbrowser
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, jsonify, request, send_file
from werkzeug.exceptions import HTTPException
from werkzeug.utils import secure_filename

from longform_client import _sanitize
from longform_pipeline import atomic_json
from studio_scenarios import ScenarioError, expand_scenario

ROOT = Path(__file__).resolve().parent
MEDIA_LOCK = threading.Lock()
PRIVATE_KEYS = {'api_key', 'access_token', 'refresh_token', 'authorization', 'secret',
                'raw', 'request', 'assets', 'available_api_keys', 'headers'}


def public(value):
    if isinstance(value, dict):
        return {k: public(v) for k, v in value.items() if k.lower() not in PRIVATE_KEYS}
    if isinstance(value, (list, tuple)):
        return [public(v) for v in value]
    if isinstance(value, Path):
        return value.name
    if isinstance(value, str):
        if value.startswith('data:audio/'):
            return '[声音素材]'
        return _sanitize(value)
    return value


def browser_media(source, cache_dir):
    """Remux original streams, marking HEVC as hvc1 for compatible browsers."""
    source = Path(source)
    if source.suffix.lower() != '.mov':
        return source
    # Invalidate older HEVC caches that retained the less-compatible hev1 tag.
    stamp = 'browser-mp4-hvc1-v1:%s:%s:%s' % (source.resolve(), source.stat().st_mtime_ns, source.stat().st_size)
    target = Path(cache_dir) / (hashlib.sha256(stamp.encode()).hexdigest()[:24] + '.mp4')
    with MEDIA_LOCK:
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            partial = target.with_suffix('.part.mp4')
            try:
                info = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                                       '-show_entries', 'stream=codec_name', '-of', 'json', str(source)],
                                      check=True, capture_output=True, text=True, timeout=20)
                codec = json.loads(info.stdout)['streams'][0]['codec_name']
                tag = ['-tag:v', 'hvc1'] if codec == 'hevc' else []
                subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
                                '-i', str(source), '-map', '0:v:0', '-map', '0:a:0?', '-c', 'copy',
                                *tag, '-movflags', '+faststart', str(partial)],
                               check=True, capture_output=True, timeout=120)
                partial.replace(target)
            finally:
                partial.unlink(missing_ok=True)
    return target


def poster_media(source, cache_dir):
    source = Path(source)
    if source.suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp'):
        return source
    stamp = '%s:%s:%s:poster' % (source.resolve(), source.stat().st_mtime_ns, source.stat().st_size)
    target = Path(cache_dir)/(hashlib.sha256(stamp.encode()).hexdigest()[:24]+'.jpg')
    with MEDIA_LOCK:
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            partial = target.with_suffix('.part.jpg')
            try:
                subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-ss','3','-i',str(source),
                                '-frames:v','1','-vf','scale=960:-2',str(partial)],
                               check=True,capture_output=True,timeout=60)
                partial.replace(target)
            finally:
                partial.unlink(missing_ok=True)
    return target


def exporter(exp):
    from assemble_review import assemble_preview
    exp.assemble()
    folder = exp.folder / 'studio_export'
    receipt = folder / 'verification.json'
    if receipt.exists():
        data = json.loads(receipt.read_text())
        if all(data.get('checks', {}).values()) and Path(data.get('preview_file', '')).is_file():
            return browser_media(Path(data['preview_file']), exp.folder / 'browser_media')
    if folder.exists():
        folder = exp.folder / ('studio_export_' + uuid.uuid4().hex[:8])
    inputs = [exp.folder / exp.record(i)['directory'] / 'original.mov'
              for i in range(len(exp.data['spec']['segments']))]
    result = assemble_preview(inputs, folder)
    if not isinstance(result, dict):
        result = json.loads((folder / 'verification.json').read_text())
    return browser_media(Path(result['preview_file']), exp.folder / 'browser_media')


def create_app(manager=None, *, simulation=False, data_dir=None):
    if manager is None:
        from studio_mode import bind_mode
        folder = Path(data_dir or ROOT / ('.studio-simulation' if simulation else '.studio'))
        bind_mode(folder, simulation)
        if simulation:
            from studio_simulation import make_manager
            manager = make_manager(ROOT, folder)
        else:
            from studio_backend import StudioManager
            from studio_review import DEFAULT_MODEL as review_model, review_segment
            manager = StudioManager(ROOT, folder, reviewer=review_segment if review_model else None,
                                    exporter=exporter)
    app = Flask(__name__, static_folder=str(ROOT / 'web'), static_url_path='')
    app.config.update(MAX_CONTENT_LENGTH=17 * 1024 * 1024, STUDIO_MANAGER=manager,
                      STUDIO_TOKEN=secrets.token_urlsafe(32), STUDIO_SIMULATION=simulation,
                      STUDIO_SCENARIO_PLANNER=expand_scenario)
    data_path = Path(manager.data_dir)
    planning_lock = threading.Lock()

    @app.before_request
    def protect_local_surface():
        try:
            host = urlsplit('http://' + request.host).hostname
        except ValueError:
            host = None
        if host not in ('localhost', '127.0.0.1', '::1'):
            return jsonify(error='工作台仅允许本机访问'), 403
        if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            origin = request.headers.get('Origin')
            if origin and origin.rstrip('/') != request.host_url.rstrip('/'):
                return jsonify(error='拒绝跨站请求，请从工作台页面操作'), 403
            if not secrets.compare_digest(request.headers.get('X-Studio-Token', ''), app.config['STUDIO_TOKEN']):
                return jsonify(error='页面会话已失效，请刷新后重试'), 403
            if request.path != '/api/assets/audio' and not request.is_json:
                return jsonify(error='请求需要JSON格式'), 415

    @app.after_request
    def security_headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; "
            "object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if request.path.startswith('/api/') and not request.path.startswith('/api/media/'):
            response.headers.setdefault('Cache-Control', 'no-store')
        return response

    @app.errorhandler(Exception)
    def handle_error(exc):
        if isinstance(exc, HTTPException):
            return jsonify(error='请求过大' if exc.code == 413 else exc.description), exc.code
        if isinstance(exc, ScenarioError):
            return jsonify(error=_sanitize(str(exc))), 502
        if isinstance(exc, (KeyError, FileNotFoundError)):
            return jsonify(error='没有找到这项任务或文件'), 404
        if isinstance(exc, (ValueError, TypeError)):
            return jsonify(error=_sanitize(str(exc))), 400
        app.logger.error('Studio operation failed: %s', _sanitize(str(exc)))
        return jsonify(error='操作失败，请查看任务日志后重试'), 500

    def body():
        result = request.get_json()
        if not isinstance(result, dict):
            raise ValueError('请求必须是JSON对象')
        return result

    def permitted_file(path):
        path = Path(path).resolve()
        roots = [data_path.resolve(), (ROOT / 'experiments').resolve()]
        if not any(path.is_relative_to(root) for root in roots):
            raise FileNotFoundError()
        if not path.is_file() or path.suffix.lower() not in ('.mp4', '.mov', '.jpg', '.jpeg', '.png', '.webp', '.wav', '.mp3'):
            raise FileNotFoundError()
        return path

    @app.get('/')
    def index():
        return send_file(ROOT / 'web/index.html', max_age=0)

    @app.get('/api/bootstrap')
    def bootstrap():
        data = public(manager.bootstrap_data())
        data.update(csrf_token=app.config['STUDIO_TOKEN'], simulation=simulation)
        data['scenarios'] = {
            'available': simulation or bool(shutil.which('arkcli')),
            'durations': [60, 90, 120, 180], 'max_quantity': 6,
            'default_quantity': 3, 'default_duration': 60,
        }
        data.setdefault('environment', {}).update(
            server_mode='本机演练' if simulation else '本机工作台',
            arkcli_available=bool(shutil.which('arkcli')),
            ffmpeg_available=bool(shutil.which('ffmpeg')),
            ffprobe_available=bool(shutil.which('ffprobe')))
        return jsonify(data)

    @app.post('/api/scenarios/expand')
    def expand():
        config = body()
        if not planning_lock.acquire(blocking=False):
            return jsonify(error='已有脚本正在扩写，请等待完成后再试。'), 409
        try:
            result = app.config['STUDIO_SCENARIO_PLANNER'](config, simulation=simulation)
            return jsonify(public(result))
        finally:
            planning_lock.release()

    @app.get('/api/state')
    def state():
        return jsonify(public(manager.state()))

    @app.get('/api/reference-images/<reference_id>')
    def reference_image(reference_id):
        from studio_references import reference_path
        return send_file(reference_path(ROOT, reference_id), conditional=True, max_age=3600)

    @app.get('/api/jobs/<job_id>')
    def job_detail(job_id):
        return jsonify(public(manager.job_detail(job_id)))

    @app.post('/api/batches/estimate')
    def estimate():
        return jsonify(public(manager.estimate(body())))

    @app.post('/api/batches')
    def create_batch():
        return jsonify(public(manager.create_batch(body()))), 201

    @app.post('/api/jobs/<job_id>/pause')
    def pause(job_id):
        return jsonify(public(manager.pause(job_id)))

    @app.post('/api/jobs/<job_id>/resume')
    def resume(job_id):
        return jsonify(public(manager.resume(job_id)))

    @app.post('/api/jobs/<job_id>/retry')
    def retry(job_id):
        return jsonify(public(manager.retry(job_id)))

    @app.post('/api/jobs/<job_id>/review')
    def review(job_id):
        return jsonify(error='当前任务使用自动验收，不再接受人工提交验收结论。'), 410

    @app.post('/api/batches/<batch_id>/budget')
    def budget(batch_id):
        return jsonify(public(manager.update_budget(batch_id, body().get('budget_cny'))))

    @app.get('/api/media/<job_id>/<kind>')
    def media(job_id, kind):
        if kind == 'poster':
            try:
                source = manager.media_path(job_id, 'preview')
            except (FileNotFoundError, ValueError):
                try: source = manager.media_path(job_id, 'segment-0')
                except (FileNotFoundError, ValueError): source = manager.media_path(job_id, 'poster')
            path = poster_media(permitted_file(source), data_path/'browser_media')
            return send_file(path, conditional=True, max_age=0)
        path = permitted_file(manager.media_path(job_id, kind))
        if path.suffix.lower() == '.mov':
            path = browser_media(path, data_path / 'browser_media')
        return send_file(path, conditional=True, max_age=3600)

    @app.get('/api/jobs/<job_id>/download')
    def download(job_id):
        path = permitted_file(manager.media_path(job_id, 'preview'))
        path = browser_media(path, data_path / 'browser_media')
        return send_file(path, as_attachment=True, download_name='seedance-'+job_id+path.suffix)

    @app.get('/api/jobs/<job_id>/metadata')
    def metadata(job_id):
        response = jsonify(public(manager.metadata(job_id)))
        response.headers['Content-Disposition'] = 'attachment; filename="'+secure_filename(job_id)+'-record.json"'
        return response

    @app.post('/api/assets/audio')
    def upload_audio():
        uploaded = request.files.get('file')
        if not uploaded or not uploaded.filename:
            raise ValueError('请选择声音参考文件')
        suffix = Path(uploaded.filename).suffix.lower()
        if suffix not in ('.wav', '.mp3'):
            raise ValueError('声音参考仅支持WAV或MP3')
        payload = uploaded.read(15 * 1024 * 1024 + 1)
        if not payload or len(payload) > 15 * 1024 * 1024:
            raise ValueError('声音参考需非空且不超过15MB')
        asset_id = uuid.uuid4().hex
        folder = data_path / 'voice_assets'
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='studio-audio-', dir=str(folder)) as tmp:
            original = Path(tmp) / ('input'+suffix)
            original.write_bytes(payload)
            normalized = Path(tmp) / 'voice.mp3'
            try:
                check = subprocess.run(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(original)],
                                       check=True,capture_output=True,text=True,timeout=20)
                info = json.loads(check.stdout)
                duration = float(info['format']['duration'])
                if not any(s.get('codec_type')=='audio' for s in info['streams']) or not 2 <= duration <= 30:
                    raise ValueError('声音参考必须含2至30秒音频')
                subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-i',str(original),'-vn','-ac','1','-ar','16000',
                                '-c:a','libmp3lame','-b:a','32k',str(normalized)],
                               check=True,capture_output=True,timeout=30)
                encoded = normalized.read_bytes()
            except (subprocess.SubprocessError, KeyError, json.JSONDecodeError):
                raise ValueError('无法读取此音频，请使用有效WAV或MP3') from None
            if len(encoded) > 135000:
                raise ValueError('规范化后的声音素材过大，请缩短音频')
            assets = [{'role':'reference_audio',
                       'content':{'type':'audio_url','audio_url':{'url':'data:audio/mpeg;base64,'+base64.b64encode(encoded).decode()},'role':'reference_audio'},
                       'description':'@音频1用于本片人物的固定声线参考；角色与声音的对应关系以当前剧本说明为准，不重复参考录音台词。'}]
            atomic_json(folder/(asset_id+'.json'), assets)
            atomic_json(folder/(asset_id+'.meta.json'), {'name':Path(uploaded.filename).name,'duration_seconds':duration,'normalized_format':'mono MP3 32k'})
        return jsonify(asset_id=asset_id,name=Path(uploaded.filename).name,duration_seconds=round(duration,3))

    @app.post('/api/environment/check')
    def check_environment():
        if simulation:
            return jsonify(ready=True, logged_in=False, simulation=True, message='演练环境，不调用方舟')
        settings = manager.bootstrap_data().get('settings', {})
        from longform_client import _parse_json_output
        env = dict(os.environ, ARKCLI_NO_UPDATE_NOTIFIER='1', ARKCLI_CALLER_TYPE='ai_agent',
                   ARKCLI_CALLER_NAME='codex', ARKCLI_SKILL_NAME='arkcli-auth')
        result = subprocess.run(['arkcli','auth','status','--profile',settings.get('profile','default'),
                                 '--format','json'],env=env,capture_output=True,text=True,timeout=25)
        if result.returncode:
            return jsonify(ready=False, logged_in=False, message='认证检查失败，请在本机完成ArkCLI登录')
        data = _parse_json_output(result.stdout)
        return jsonify(ready=bool(data.get('logged_in')),logged_in=bool(data.get('logged_in')),
                       message='本机账号已登录' if data.get('logged_in') else '请先在本机完成ArkCLI登录')

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8787)
    parser.add_argument('--data-dir')
    parser.add_argument('--open', action='store_true')
    parser.add_argument('--simulate', action='store_true', help='Explicit offline simulation; never calls a model')
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('port must be between 1024 and 65535')
    app = create_app(simulation=args.simulate, data_dir=args.data_dir)
    url = 'http://127.0.0.1:%d' % args.port
    print('Seedance Studio: '+url, flush=True)
    if args.open:
        threading.Timer(1, lambda: webbrowser.open(url)).start()
    try:
        app.run(host='127.0.0.1',port=args.port,debug=False,threaded=True,use_reloader=False)
    finally:
        app.config['STUDIO_MANAGER'].close()


if __name__ == '__main__':
    main()
