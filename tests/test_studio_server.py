import io
import tempfile
import unittest
import json
import wave
import threading
import hashlib
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

from studio_server import browser_media, create_app


class FakeManager:
    def __init__(self, root):
        self.root = self.data_dir = Path(root)
        self.created = []
        self.media = self.root / 'preview.mp4'
        self.media.write_bytes(b'0123456789')
    def bootstrap_data(self):
        return {'templates': [], 'settings': {}, 'environment': {}, 'api_key': 'secret-value'}
    def state(self): return {'batches': [], 'jobs': [], 'summary': {}}
    def job_detail(self, key):
        if key != 'ok': raise KeyError(key)
        return {'job': {'id': key}, 'segments': [], 'logs': []}
    def estimate(self, data): return {'estimated_total_cny': 20}
    def create_batch(self, data):
        self.created.append(data)
        return {'batch': {'id': 'batch-1'}, 'jobs': []}
    def pause(self,key): return self.job_detail(key)
    resume = pause
    retry = pause
    def review(self,key,data): return self.job_detail(key)
    def update_budget(self,key,value): return {'id':key,'budget_cny':value}
    def media_path(self,key,kind):
        if key != 'ok' or kind != 'preview': raise FileNotFoundError()
        return self.media
    def metadata(self,key): return {'id':key,'api_key':'private','request':{'authorization':'private'}}
    def close(self): pass


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.manager=FakeManager(self.tmp.name)
        self.app=create_app(self.manager, simulation=True)
        self.app.config['TESTING']=True
        self.client=self.app.test_client()
        self.token=self.client.get('/api/bootstrap').get_json()['csrf_token']
    def tearDown(self): self.tmp.cleanup()
    def headers(self): return {'X-Studio-Token':self.token}
    def test_bootstrap_does_not_disclose_credentials(self):
        result=self.client.get('/api/bootstrap')
        self.assertEqual(result.status_code,200)
        self.assertNotIn('secret-value',result.get_data(as_text=True))
        self.assertTrue(result.json['simulation'])
    def test_write_requires_session_token(self):
        result=self.client.post('/api/batches',json={'quantity':1})
        self.assertEqual(result.status_code,403)
        self.assertEqual(self.manager.created,[])
    def test_cross_origin_write_is_rejected_even_with_token(self):
        result=self.client.post('/api/batches',json={},headers=dict(self.headers(),Origin='https://evil.example'))
        self.assertEqual(result.status_code,403)
    def test_untrusted_host_is_rejected(self):
        result=self.client.get('/api/state',headers={'Host':'attacker.example'})
        self.assertEqual(result.status_code,403)
    def test_explicit_start_calls_manager_once(self):
        result=self.client.post('/api/batches',json={'quantity':2},headers=self.headers())
        self.assertEqual(result.status_code,201)
        self.assertEqual(len(self.manager.created),1)
    def test_non_json_post_is_rejected(self):
        result=self.client.post('/api/batches',data='quantity=1',headers=self.headers())
        self.assertEqual(result.status_code,415)
    def test_media_supports_ranges_for_video_seeking(self):
        result=self.client.get('/api/media/ok/preview',headers={'Range':'bytes=2-5'})
        self.assertEqual(result.status_code,206)
        self.assertEqual(result.data,b'2345')
        result.close()
    def test_metadata_export_does_not_leak_private_fields(self):
        result=self.client.get('/api/jobs/ok/metadata')
        self.assertNotIn('private',result.get_data(as_text=True))
    def test_unknown_job_has_structured_error(self):
        result=self.client.get('/api/jobs/absent')
        self.assertEqual(result.status_code,404)
        self.assertIn('error',result.json)

    def test_manual_review_endpoint_is_retired_without_changing_a_job(self):
        calls = []
        self.manager.review = lambda *args: calls.append(args)
        result = self.client.post('/api/jobs/ok/review', json={'decision': 'accept'}, headers=self.headers())
        self.assertEqual(result.status_code, 410)
        self.assertIn('自动验收', result.json['error'])
        self.assertEqual(calls, [])

    def test_known_reference_image_is_available_as_local_preview(self):
        from PIL import Image
        response = self.client.get('/api/reference-images/hotel_booking_screen')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'image/png')
        with Image.open(io.BytesIO(response.data)) as picture:
            self.assertEqual(picture.size, (960, 640))
        response.close()

    def test_reference_preview_rejects_unknown_ids_and_foreign_hosts(self):
        self.assertEqual(self.client.get('/api/reference-images/not-a-reference').status_code, 404)
        self.assertEqual(self.client.get('/api/reference-images/hotel_booking_screen',
                                        headers={'Host': 'attacker.example'}).status_code, 403)
    def test_media_outside_allowed_roots_is_rejected(self):
        self.manager.media_path=lambda *_: Path('/etc/passwd')
        self.assertEqual(self.client.get('/api/media/ok/preview').status_code,404)
    def test_unsupported_audio_upload_is_rejected(self):
        result=self.client.post('/api/assets/audio',data={'file':(io.BytesIO(b'<html>'),'x.html')},headers=self.headers())
        self.assertEqual(result.status_code,400)

    def test_valid_audio_upload_is_private_normalized_asset(self):
        data=io.BytesIO()
        with wave.open(data,'wb') as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000)
            wav.writeframes(b'\0\0'*16000*3)
        result=self.client.post('/api/assets/audio',data={'file':(io.BytesIO(data.getvalue()),'voice.wav')},headers=self.headers())
        self.assertEqual(result.status_code,200)
        asset_id=result.json['asset_id']
        saved=json.loads((self.manager.data_dir/'voice_assets'/(asset_id+'.json')).read_text())
        self.assertTrue(saved[0]['content']['audio_url']['url'].startswith('data:audio/mpeg;base64,'))
        self.assertNotIn('base64',result.get_data(as_text=True))

    def test_invalid_audio_is_not_saved(self):
        result=self.client.post('/api/assets/audio',data={'file':(io.BytesIO(b'not audio'),'voice.wav')},headers=self.headers())
        self.assertEqual(result.status_code,400)
        self.assertEqual(list((self.manager.data_dir/'voice_assets').glob('*.json')),[])

    def scenario(self):
        return {'description': '第一视角整理厨房台面的杯子和餐具',
                'task_type': 'manipulation', 'duration_seconds': 60,
                'quantity': 2, 'diversity': '改变物品和摆放顺序'}

    def test_bootstrap_offers_scenarios_without_a_template(self):
        result = self.client.get('/api/bootstrap').get_json()
        self.assertEqual(result['templates'], [])
        self.assertIn('scenarios', result)
        self.assertTrue(result['scenarios']['available'])
        self.assertEqual(result['scenarios']['durations'], [60, 90, 120, 180])
        self.assertEqual(result['scenarios']['default_quantity'], 3)

    def test_scenario_expansion_returns_drafts_without_creating_video_jobs(self):
        calls = []
        def planner(config, *, simulation):
            calls.append((config, simulation))
            return {'variants': [{'name': '厨房物品分类'}],
                    'planner': {'simulation': simulation, 'model': 'test'},
                    'api_key': 'secret-value'}
        self.app.config['STUDIO_SCENARIO_PLANNER'] = planner
        response = self.client.post('/api/scenarios/expand', json=self.scenario(), headers=self.headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, [(self.scenario(), True)])
        self.assertEqual(response.json['variants'][0]['name'], '厨房物品分类')
        self.assertNotIn('secret-value', response.get_data(as_text=True))
        self.assertEqual(self.manager.created, [])

    def test_scenario_expansion_requires_same_origin_and_session(self):
        calls = []
        self.app.config['STUDIO_SCENARIO_PLANNER'] = lambda *a, **k: calls.append(a)
        no_token = self.client.post('/api/scenarios/expand', json=self.scenario())
        other_origin = self.client.post('/api/scenarios/expand', json=self.scenario(),
                                        headers=dict(self.headers(), Origin='https://example.com'))
        self.assertEqual(no_token.status_code, 403)
        self.assertEqual(other_origin.status_code, 403)
        self.assertEqual(calls, [])

    def test_invalid_scenario_is_a_clear_error_without_video_side_effects(self):
        def invalid(*args, **kwargs):
            raise ValueError('场景描述至少需要8个字')
        self.app.config['STUDIO_SCENARIO_PLANNER'] = invalid
        response = self.client.post('/api/scenarios/expand', json={'description': ''}, headers=self.headers())
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json['error'], '场景描述至少需要8个字')
        self.assertEqual(self.manager.created, [])

    def test_planning_lock_prevents_duplicate_concurrent_model_calls(self):
        entered, release = threading.Event(), threading.Event()
        calls, results = [], []
        def blocked(config, *, simulation):
            calls.append(config)
            entered.set()
            release.wait(5)
            return {'variants': [], 'planner': {'simulation': simulation}}
        self.app.config['STUDIO_SCENARIO_PLANNER'] = blocked
        def first_request():
            with self.app.test_client() as client:
                results.append(client.post('/api/scenarios/expand', json=self.scenario(), headers=self.headers()))
        worker = threading.Thread(target=first_request)
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            result = self.client.post('/api/scenarios/expand', json=self.scenario(), headers=self.headers())
            self.assertEqual(result.status_code, 409)
            self.assertIn('扩写', result.json['error'])
            self.assertEqual(len(calls), 1)
        finally:
            release.set()
            worker.join(5)
        self.assertEqual(results[0].status_code, 200)

    def test_planner_lock_is_released_after_an_error(self):
        def invalid(*args, **kwargs):
            raise ValueError('输入无效')
        self.app.config['STUDIO_SCENARIO_PLANNER'] = invalid
        response = self.client.post('/api/scenarios/expand', json={}, headers=self.headers())
        self.assertEqual(response.status_code, 400)
        self.app.config['STUDIO_SCENARIO_PLANNER'] = lambda *a, **k: {'variants': []}
        response = self.client.post('/api/scenarios/expand', json=self.scenario(), headers=self.headers())
        self.assertEqual(response.status_code, 200)

    def test_provider_failure_does_not_silently_use_simulation(self):
        from studio_scenarios import ScenarioError
        calls = []
        def failed(config, *, simulation):
            calls.append(simulation)
            raise ScenarioError('脚本扩写未完成，请检查文字模型后重试。')
        self.app.config['STUDIO_SCENARIO_PLANNER'] = failed
        response = self.client.post('/api/scenarios/expand', json=self.scenario(), headers=self.headers())
        self.assertEqual(response.status_code, 502)
        self.assertIn('扩写未完成', response.json['error'])
        self.assertEqual(calls, [True])
        self.assertEqual(self.manager.created, [])

    def test_real_app_routes_expansion_to_real_mode(self):
        app = create_app(self.manager, simulation=False)
        calls = []
        def planner(config, *, simulation):
            calls.append(simulation)
            return {'variants': [], 'planner': {'simulation': simulation}}
        app.config['STUDIO_SCENARIO_PLANNER'] = planner
        with app.test_client() as client:
            token = client.get('/api/bootstrap').json['csrf_token']
            response = client.post('/api/scenarios/expand', json=self.scenario(),
                                   headers={'X-Studio-Token': token})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, [False])


class BrowserMediaTests(unittest.TestCase):
    def test_hevc_gets_hvc1_while_h264_remains_stream_copy_without_hevc_tag(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for codec in ('hevc', 'h264'):
                source = root / (codec + '.mov')
                source.write_bytes(b'original-' + codec.encode())
                calls = []
                def run(argv, **kwargs):
                    calls.append(argv)
                    if argv[0] == 'ffprobe':
                        return subprocess.CompletedProcess(argv, 0,
                            json.dumps({'streams': [{'codec_name': codec}]}), '')
                    Path(argv[-1]).write_bytes(b'remuxed')
                    return subprocess.CompletedProcess(argv, 0, b'', b'')
                with patch('studio_server.subprocess.run', side_effect=run):
                    output = browser_media(source, root / 'cache')
                    again = browser_media(source, root / 'cache')
                self.assertEqual(output, again)
                self.assertEqual([call[0] for call in calls], ['ffprobe', 'ffmpeg'])
                ffmpeg = calls[-1]
                self.assertEqual(ffmpeg[ffmpeg.index('-c') + 1], 'copy')
                self.assertEqual(ffmpeg[ffmpeg.index('-movflags') + 1], '+faststart')
                if codec == 'hevc':
                    self.assertEqual(ffmpeg[ffmpeg.index('-tag:v') + 1], 'hvc1')
                else:
                    self.assertNotIn('-tag:v', ffmpeg)
                self.assertEqual(source.read_bytes(), b'original-' + codec.encode())

    def test_old_browser_cache_is_not_reused_without_hevc_compatibility_handling(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'source.mov'
            source.write_bytes(b'original')
            cache = root / 'cache'
            cache.mkdir()
            stamp = '%s:%s:%s' % (source.resolve(), source.stat().st_mtime_ns, source.stat().st_size)
            legacy = cache / (hashlib.sha256(stamp.encode()).hexdigest()[:24] + '.mp4')
            legacy.write_bytes(b'old-hev1-remux')
            def run(argv, **kwargs):
                if argv[0] == 'ffprobe':
                    return subprocess.CompletedProcess(argv, 0, '{"streams":[{"codec_name":"hevc"}]}', '')
                Path(argv[-1]).write_bytes(b'new-hvc1-remux')
                return subprocess.CompletedProcess(argv, 0, b'', b'')
            with patch('studio_server.subprocess.run', side_effect=run):
                output = browser_media(source, cache)
            self.assertNotEqual(output, legacy)
            self.assertEqual(output.read_bytes(), b'new-hvc1-remux')
            self.assertEqual(legacy.read_bytes(), b'old-hev1-remux')

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg tools required')
    def test_small_hevc_10bit_remux_preserves_decoded_frames_and_bit_depth(self):
        encoders = subprocess.run(['ffmpeg', '-hide_banner', '-encoders'], check=True,
                                  capture_output=True, text=True, timeout=10).stdout
        if 'libx265' not in encoders:
            self.skipTest('libx265 encoder unavailable')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'hevc10.mov'
            subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                            'testsrc2=size=64x64:rate=24:duration=0.25', '-c:v', 'libx265',
                            '-preset', 'ultrafast', '-pix_fmt', 'yuv420p10le',
                            '-x265-params', 'pools=1:frame-threads=1:log-level=error', '-tag:v', 'hev1',
                            str(source)], check=True, capture_output=True, timeout=30)
            original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            output = browser_media(source, root / 'cache')
            metadata = json.loads(subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                '-show_entries', 'stream=codec_name,codec_tag_string,pix_fmt,width,height',
                '-of', 'json', str(output)], check=True, capture_output=True, text=True, timeout=10).stdout)
            stream = metadata['streams'][0]
            self.assertEqual(stream['codec_name'], 'hevc')
            self.assertEqual(stream['codec_tag_string'], 'hvc1')
            self.assertEqual(stream['pix_fmt'], 'yuv420p10le')
            self.assertEqual((stream['width'], stream['height']), (64, 64))
            def frames(path):
                text = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(path),
                    '-map', '0:v:0', '-f', 'framemd5', '-'], check=True,
                    capture_output=True, text=True, timeout=10).stdout
                return [line.rsplit(',', 1)[-1].strip() for line in text.splitlines() if not line.startswith('#')]
            self.assertEqual(frames(source), frames(output))
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), original_hash)


class FactoryModeTests(unittest.TestCase):
    def test_simulation_factory_uses_only_simulation_provider(self):
        from studio_simulation import SimulationClient, simulation_review
        with tempfile.TemporaryDirectory() as folder:
            app = create_app(simulation=True, data_dir=folder)
            manager = app.config['STUDIO_MANAGER']
            try:
                self.assertIsInstance(manager.client_factory('unused', 'unused'), SimulationClient)
                self.assertIs(manager.reviewer, simulation_review)
            finally:
                manager.close()

    def test_simulation_directory_cannot_be_opened_as_real(self):
        from unittest.mock import patch
        from studio_simulation import make_manager
        from studio_server import ROOT
        with tempfile.TemporaryDirectory() as folder:
            manager = make_manager(ROOT, Path(folder))
            manager.close()
            with patch('studio_backend.StudioManager') as real_manager:
                with self.assertRaisesRegex(ValueError, '运行模式'):
                    create_app(data_dir=folder)
                real_manager.assert_not_called()

    def test_unmarked_legacy_mutable_queue_requires_explicit_migration(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder)/'studio.json').write_text(json.dumps({
                'version': 1, 'jobs': {'legacy': {'read_only': False}}, 'batches': {}}))
            with self.assertRaisesRegex(ValueError, '运行模式'):
                create_app(simulation=True, data_dir=folder)


if __name__=='__main__': unittest.main()
