"""Offline contract tests: real experiment ledgers with fake network/media IO."""
import importlib.util
import copy
import json
import math
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import longform_pipeline as lp
from longform_client import ArkCLIError

if importlib.util.find_spec('studio_backend'):
    from studio_backend import StudioManager
else:
    StudioManager = None

ROOT = Path(__file__).resolve().parents[1]


def scenario():
    return {'name': '测试第一视角', 'version': '1', 'target_duration_seconds': 60,
            'global_prompt': '自然第一视角，固定人物、声音和场景。',
            'segments': [{'id': str(i), 'duration': 30, 'prompt': '连续办理动作',
                          'expected_start_state': '开始', 'expected_end_state': '完成',
                          'required_actions': ['办理']} for i in range(2)]}


def approval(**extra):
    return dict(decision='accept', visual_continuity='pass', action_integrity='pass',
                audio_consistency='pass', observed_end_state='实际观察到双手空闲',
                evidence='已观看整段并听原声', **extra)


class FakeClient:
    def __init__(self):
        self.calls, self.queries = [], []
        self.status = 'succeeded'
        self.tokens = 1000
        self.raise_submit = None
        self.entered = threading.Event()

    def submit(self, body):
        self.calls.append(body)
        self.entered.set()
        if self.raise_submit:
            raise self.raise_submit
        return {'id': 'cgt-fake-%s' % len(self.calls), 'status': 'queued'}

    def get(self, task_id):
        self.queries.append(task_id)
        return {'id': task_id, 'status': self.status,
                'output_url': 'https://example.invalid/video?token=SECRET',
                'usage': {'completion_tokens': self.tokens},
                'raw': {'api_key': 'sk-FAKESECRET', 'audio': 'data:audio/wav;base64,SECRET'}}


class StudioTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(StudioManager, 'StudioManager batch backend must be implemented')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data_dir = Path(self.temp.name) / 'studio'
        self.client = FakeClient()
        # Deliberately synthetic unit prices, independent of any local account.
        prices = {'seedance-2.5': {
            '480p': {'nv2v': .1, 'v2v': .05},
            '720p': {'nv2v': .1, 'v2v': .05},
            '1080p': {'nv2v': .2, 'v2v': .1}}, 'seedance-2.0': {}}
        price_patch = patch.object(lp.models, '_PRICES', prices)
        price_patch.start()
        self.addCleanup(price_patch.stop)
        self.managers = []
        self.download = patch.object(lp, 'download_file', side_effect=lambda url, path: Path(path).write_bytes(b'offline video'))
        self.download.start()
        self.addCleanup(self.download.stop)
        self.probe = patch.object(lp, 'probe', return_value={
            'duration': 30, 'video_duration': 30, 'width': 864, 'height': 480,
            'fps': '24/1', 'video_codec': 'h264', 'audio_streams': [{'codec': 'aac'}]})
        self.probe.start()
        self.addCleanup(self.probe.stop)
        # Stop workers while fake IO is still installed, before clearing mocks.
        self.addCleanup(self.close_managers)

    def close_managers(self):
        for manager in self.managers:
            manager.close()

    def manager(self, workers=False, **kwargs):
        def exporter(exp):
            path = exp.folder / 'offline-preview.mov'
            path.write_bytes(b'offline final')
            return path
        kwargs.setdefault('exporter', exporter)
        kwargs.setdefault('reviewer', lambda *_: approval(review_cost_cny=0.0, automatic=True))
        root = kwargs.pop('root', ROOT)
        manager = StudioManager(root, self.data_dir, client_factory=lambda profile, model: self.client,
                                poll_interval=.01, start_workers=workers, **kwargs)
        self.managers.append(manager)
        return manager

    def config(self, **changes):
        return dict({'name': '测试批次', 'spec': scenario(), 'quantity': 1,
                     'resolution': '480p', 'budget_cny': 200, 'concurrency': 1,
                     'review_mode': 'auto'}, **changes)

    def create(self, manager, **changes):
        result = manager.create_batch(self.config(**changes))
        return result['jobs'][0]['id'], result['batch']['id']

    def variants_config(self, *specs, **changes):
        value = self.config(variants=list(specs), quantity=len(specs), budget_cny=2000)
        value.pop('spec')
        value.update(changes)
        return value

    def wait_status(self, manager, job_id, expected, timeout=15):
        # Conditional wait: normally returns in milliseconds; allow shared-host
        # scheduling and filesystem delays when the full media suite also runs.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            detail = manager.job_detail(job_id)
            if detail['job']['status'] in expected:
                return detail
            time.sleep(.01)
        self.fail('state did not reach %r: %r' % (expected, manager.job_detail(job_id)))

    def experiment(self, manager, job_id):
        return lp.Experiment(self.data_dir / 'runs' / job_id)

    def test_bootstrap_contains_public_templates_without_model_calls(self):
        manager = self.manager()
        bootstrap = manager.bootstrap_data()
        templates = {row['id']: row for row in bootstrap['templates']}
        self.assertIn('object_sorting_60', templates)
        self.assertEqual(templates['object_sorting_60']['duration_seconds'], 60)
        self.assertIn('hotel_120', templates)
        template = templates['hotel_120_phone_reference']
        self.assertEqual(bootstrap['reference_images'][0]['id'], 'hotel_booking_screen')
        self.assertEqual(sum(row['duration'] for row in template['spec']['segments']), 120)
        self.assertEqual(self.client.calls, [])

    def reference_root(self):
        root = Path(self.temp.name) / 'reference-root'
        path = root / 'refs' / 'reference_board.png'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'\x89PNG\r\n\x1a\nOFFLINE_IMAGE')
        return root

    def reference_spec(self):
        spec = scenario()
        spec['reference_images'] = [{'id': 'hotel_booking_screen', 'segment_ids': ['0']}]
        return spec

    def test_reference_binding_survives_normalization_metadata_and_restart_without_media_bytes(self):
        manager = self.manager()
        manager.root = self.reference_root()
        spec = self.reference_spec()
        result = manager.create_batch(self.variants_config(spec))
        job_id = result['jobs'][0]['id']
        saved = self.experiment(manager, job_id).data
        self.assertEqual(saved['spec'].get('reference_images'), spec['reference_images'])
        self.assertEqual(saved['assets'][0]['segment_ids'], ['0'])
        self.assertEqual(saved['assets'][0]['content']['role'], 'reference_image')
        manager.close()
        again = self.manager()
        metadata = again.metadata(job_id)
        self.assertEqual(metadata['spec']['reference_images'], spec['reference_images'])
        self.assertNotIn('data:image', json.dumps(metadata))
        self.assertNotIn('T0ZGTElORV9JTUFHRQ', json.dumps(metadata))
        self.assertEqual(again.data['version'], 1)

    def test_mixed_variants_build_independent_references_and_keep_explicit_voice(self):
        manager = self.manager()
        manager.root = self.reference_root()
        asset_id = 'c' * 32
        voices = [{'role': 'reference_audio', 'description': '明确提供的声音参考',
                   'content': {'type': 'audio_url', 'audio_url': {'url': 'data:audio/wav;base64,T0ZGTElORQ=='}}}]
        folder = self.data_dir / 'voice_assets'
        folder.mkdir(parents=True)
        (folder / (asset_id + '.json')).write_text(json.dumps(voices))
        result = manager.create_batch(self.variants_config(self.reference_spec(), scenario(), voice_asset_id=asset_id))
        first, second = [self.experiment(manager, job['id']).data for job in result['jobs']]
        self.assertEqual([asset['role'] for asset in first['assets']], ['reference_audio', 'reference_image'])
        self.assertEqual(second['assets'], voices)
        self.assertNotIn('reference_images', second['spec'])
        self.assertEqual(self.client.calls, [])

    def test_invalid_reference_in_later_variant_creates_no_jobs_or_run_files(self):
        manager = self.manager()
        manager.root = self.reference_root()
        initial = set(manager.data['jobs'])
        bad = self.reference_spec()
        for binding in [[{'id': 'unknown', 'segment_ids': ['0']}],
                        [{'id': 'hotel_booking_screen', 'segment_ids': ['absent']}],
                        [{'id': 'hotel_booking_screen', 'segment_ids': ['0', '0']}]]:
            bad['reference_images'] = binding
            with self.subTest(binding=binding), self.assertRaises(ValueError):
                manager.create_batch(self.variants_config(scenario(), bad))
        (manager.root / 'refs' / 'reference_board.png').unlink()
        with self.assertRaises(ValueError):
            manager.create_batch(self.variants_config(scenario(), self.reference_spec()))
        self.assertEqual(set(manager.data['jobs']), initial)
        self.assertEqual(list((self.data_dir / 'runs').glob('*')), [])

    def test_all_variant_asset_reads_finish_before_any_run_write(self):
        manager = self.manager()
        manager.root = self.reference_root()
        import studio_backend
        self.assertTrue(hasattr(studio_backend, 'build_reference_assets'), 'precompute per-variant assets before writes')
        with patch.object(studio_backend, 'build_reference_assets', side_effect=[[], ValueError('图片读取失败')]):
            with self.assertRaisesRegex(ValueError, '图片读取失败'):
                manager.create_batch(self.variants_config(scenario(), self.reference_spec()))
        self.assertEqual(list((self.data_dir / 'runs').glob('*')), [])

    def test_config_supports_both_templates_and_rejects_unknown_template_ids(self):
        manager = self.manager()
        self.assertIn('hotel_120_phone_reference', [row['id'] for row in manager.bootstrap_data()['templates']])
        for template_id, has_reference in [('object_sorting_60', False), ('hotel_120_phone_reference', True), ('hotel_120', False)]:
            result = manager.create_batch({'template_id': template_id, 'budget_cny': 2000})
            spec = self.experiment(manager, result['jobs'][0]['id']).data['spec']
            self.assertEqual('reference_images' in spec, has_reference)
        with self.assertRaises(ValueError):
            manager.create_batch({'template_id': '../hotel_120s_v4', 'budget_cny': 2000})
        with self.assertRaises(ValueError):
            manager.create_batch(self.config(template_id='unknown'))

    def test_reference_template_is_hidden_when_asset_is_not_available(self):
        manager = self.manager()
        root = Path(self.temp.name) / 'missing-image-root'
        (root / 'scenarios').mkdir(parents=True)
        for name in ['object_sorting_60s.json', 'hotel_120s_v4.json', 'hotel_120s_phone_reference.json']:
            source = ROOT / 'scenarios' / name
            if source.is_file():
                (root / 'scenarios' / name).write_text(source.read_text())
        manager.root = root
        data = manager.bootstrap_data()
        self.assertEqual({row['id'] for row in data['templates']}, {'object_sorting_60', 'hotel_120'})
        self.assertIn('reference_images', data)
        self.assertEqual(data['reference_images'], [])

    def test_estimate_counts_previous_reference_duration_and_auto_review_reserve(self):
        manager = self.manager()
        value = manager.estimate(self.config(quantity=2, review_mode='auto'))
        expected = lp.estimate_cost(30, 0, 864, 480) + lp.estimate_cost(30, 30, 864, 480)
        self.assertAlmostEqual(value['estimated_per_job_cny'], expected + 2, places=4)
        exact_reservation = expected * 2 * 1.05 + 4
        self.assertGreaterEqual(value['recommended_budget_cny'], exact_reservation)
        self.assertLess(value['recommended_budget_cny'], exact_reservation + .01)

    def test_invalid_specs_and_nonfinite_budget_create_no_jobs(self):
        manager = self.manager()
        count = len(manager.state()['jobs'])
        for changes in [{'quantity': 0}, {'quantity': 21}, {'concurrency': 4},
                        {'budget_cny': float('nan')}, {'resolution': '8k'}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                manager.create_batch(self.config(**changes))
        spec = scenario()
        spec['segments'][0]['duration'] = True
        with self.assertRaises(ValueError):
            manager.create_batch(self.config(spec=spec))
        self.assertEqual(len(manager.state()['jobs']), count)
        self.assertEqual(self.client.calls, [])

    def test_copies_have_explicit_variations_and_survive_restart(self):
        manager = self.manager()
        result = manager.create_batch(self.config(quantity=2, variations='上午的窗边光线\n下午的暖光'))
        ids = [job['id'] for job in result['jobs']]
        self.assertIn('上午', self.experiment(manager, ids[0]).data['spec']['global_prompt'])
        self.assertIn('下午', self.experiment(manager, ids[1]).data['spec']['global_prompt'])
        manager.close()
        again = self.manager()
        self.assertTrue(all(again.job_detail(job_id)['job']['status'] == 'queued' for job_id in ids))

    def test_variants_submit_their_own_full_script_and_title(self):
        manager = self.manager()
        first, second = scenario(), scenario()
        first.update(name='归置蓝色杯子', global_prompt='第一视角整理厨房台面。')
        first['segments'][0]['prompt'] = '伸手握住蓝色杯子的把手。'
        second.update(name='分拣红色积木', global_prompt='第一视角整理儿童活动桌。')
        second['segments'][0]['prompt'] = '捏起红色积木并放入红色分类盒。'
        result = manager.create_batch(self.variants_config(first, second))
        self.assertEqual([job['title'] for job in result['jobs']], [first['name'], second['name']])
        for expected, job in zip((first, second), result['jobs']):
            exp = self.experiment(manager, job['id'])
            self.assertEqual(exp.data['spec']['global_prompt'], expected['global_prompt'])
            self.assertEqual(exp.data['spec']['segments'], expected['segments'])
            exp.submit(0, self.client)
        self.assertIn('蓝色杯子的把手', self.client.calls[0]['content'][0]['text'])
        self.assertNotIn('红色积木', self.client.calls[0]['content'][0]['text'])
        self.assertIn('红色积木', self.client.calls[1]['content'][0]['text'])
        self.assertNotIn('蓝色杯子', self.client.calls[1]['content'][0]['text'])

    def test_variant_estimate_sums_each_timeline_and_assigns_matching_job_cost(self):
        manager = self.manager()
        first, second = scenario(), scenario()
        second['segments'].append(dict(second['segments'][0], id='2', duration=20))
        second['target_duration_seconds'] = 80
        config = self.variants_config(first, second, review_mode='auto')
        estimate = manager.estimate(config)
        first_generation = lp.estimate_cost(30, 0, 864, 480) + lp.estimate_cost(30, 30, 864, 480)
        second_generation = first_generation + lp.estimate_cost(20, 30, 864, 480)
        expected = [first_generation + 2, second_generation + 3]
        self.assertIn('jobs', estimate)
        self.assertEqual([row['duration_seconds'] for row in estimate['jobs']], [60, 80])
        self.assertEqual([row['segment_count'] for row in estimate['jobs']], [2, 3])
        for row, cost in zip(estimate['jobs'], expected):
            self.assertAlmostEqual(row['estimated_cny'], cost, places=6)
        self.assertAlmostEqual(estimate['estimated_total_cny'], sum(expected), places=6)
        self.assertAlmostEqual(estimate['estimated_per_job_cny'], sum(expected) / 2, places=6)
        self.assertEqual(estimate['duration_seconds'], 70)
        self.assertEqual(estimate['segment_count'], 2.5)
        self.assertEqual(estimate['recommended_budget_cny'],
                         math.ceil(((first_generation + second_generation) * 1.05 + 5) * 100) / 100)
        result = manager.create_batch(config)
        for job, cost in zip(result['jobs'], expected):
            self.assertAlmostEqual(job['estimated_cny'], cost, places=6)

    def test_planned_dataset_labels_survive_normalization_metadata_and_restart(self):
        manager = self.manager()
        spec = scenario()
        spec['dataset'] = {'source_scenario': '把散落在台面的杯子放入收纳格。',
                           'task_type': 'manipulation', 'variant_id': 'variant-01',
                           'variation': '左手抓取，台面上增加一个干扰物。',
                           'task_label': 'object_sorting', 'objects': ['杯子', '收纳格'],
                           'annotation_scope': 'planned'}
        spec['segments'][0]['action_labels'] = ['reach', 'grasp']
        spec['segments'][1]['action_labels'] = ['transport', 'place', 'release']
        result = manager.create_batch(self.variants_config(spec))
        job_id = result['jobs'][0]['id']
        saved = self.experiment(manager, job_id).data['spec']
        self.assertIn('dataset', saved)
        self.assertEqual(saved['dataset'], spec['dataset'])
        self.assertEqual(saved['segments'], spec['segments'])
        manager.close()
        again = self.manager()
        self.assertEqual(again.metadata(job_id)['spec'], saved)
        self.assertEqual(again.data['version'], 1)

    def test_invalid_later_variant_creates_no_run_files_or_jobs(self):
        manager = self.manager()
        initial = set(manager.data['jobs'])
        bad = scenario()
        bad['segments'][1]['duration'] = True
        with self.assertRaises(ValueError):
            manager.create_batch(self.variants_config(scenario(), bad))
        self.assertEqual(set(manager.data['jobs']), initial)
        self.assertEqual(list((self.data_dir / 'runs').glob('*')), [])
        self.assertEqual(self.client.calls, [])

    def test_variants_reject_ambiguous_sources_counts_and_mutating_variations(self):
        manager = self.manager()
        cases = [self.variants_config(scenario(), spec=scenario()),
                 self.variants_config(scenario(), template_id='hotel_120'),
                 self.variants_config(scenario(), variations='再加一个对象'),
                 self.variants_config(scenario(), quantity=2),
                 self.variants_config(scenario(), quantity=True),
                 self.variants_config(),
                 self.variants_config(*[scenario() for _ in range(21)]),
                 self.variants_config(scenario(), variants='invalid')]
        for index, config in enumerate(cases):
            with self.subTest(case=index), self.assertRaises(ValueError):
                manager.create_batch(config)

    def test_dataset_labels_are_typed_bounded_and_planned(self):
        manager = self.manager()
        invalid_fields = [('source_scenario', []), ('source_scenario', 'x' * 30001),
                          ('task_type', 'unverified'), ('variant_id', 'x' * 101),
                          ('variation', {}), ('variation', 'x' * 4001),
                          ('task_label', 'x' * 201), ('objects', [123]),
                          ('objects', ['object'] * 101), ('annotation_scope', 'observed')]
        for key, value in invalid_fields:
            spec = scenario()
            spec['dataset'] = {key: value}
            with self.subTest(key=key), self.assertRaises(ValueError):
                manager.create_batch(self.variants_config(spec))
        for labels in ['reach', [1], ['reach'] * 101, ['x' * 201]]:
            spec = scenario()
            spec['segments'][0]['action_labels'] = labels
            with self.subTest(labels=labels), self.assertRaises(ValueError):
                manager.create_batch(self.variants_config(spec))

    def test_duplicate_segment_ids_are_rejected_before_run_creation(self):
        manager = self.manager()
        spec = scenario()
        spec['segments'][1]['id'] = spec['segments'][0]['id']
        with self.assertRaises(ValueError):
            manager.create_batch(self.config(spec=spec))
        self.assertEqual(list((self.data_dir / 'runs').glob('*')), [])

    def test_new_jobs_never_implicitly_inherit_hotel_audio(self):
        root = Path(self.temp.name) / 'legacy-root'
        folder = root / 'experiments' / 'hotel_120s_v4'
        folder.mkdir(parents=True)
        legacy = [{'role': 'reference_audio', 'description': '酒店参考声线',
                   'content': {'type': 'audio_url', 'audio_url': {'url': 'data:audio/wav;base64,FAKE'}}}]
        (folder / 'experiment.json').write_text(json.dumps({'assets': legacy}))
        manager = StudioManager(root, self.data_dir, start_workers=False)
        self.managers.append(manager)
        job_id, _ = self.create(manager)
        self.assertEqual(self.experiment(manager, job_id).data['assets'], [])
        self.assertFalse(manager.bootstrap_data()['voice_asset_available'])

    def test_unnamed_custom_spec_uses_generic_default(self):
        manager = self.manager()
        spec = scenario()
        spec.pop('name')
        result = manager.create_batch(self.variants_config(spec))
        self.assertEqual(result['jobs'][0]['title'], '自定义具身场景')

    def test_automatic_review_gates_next_segment_and_exports_without_manual_calls(self):
        reviews = []
        def reviewer(exp, index):
            self.assertEqual(len(self.client.calls), index + 1)
            if index:
                self.assertEqual(exp.record(index - 1)['review']['decision'], 'accept')
            reviews.append(index)
            return approval(review_cost_cny=0.0)
        manager = self.manager(workers=True, reviewer=reviewer)
        job_id, _ = self.create(manager)
        complete = self.wait_status(manager, job_id, {'completed'})
        self.assertEqual(reviews, [0, 1])
        self.assertEqual(len(self.client.calls), 2)
        self.assertFalse(complete['job']['can_review'])
        self.assertEqual(complete['job']['progress'], 100)
        self.assertEqual(manager.media_path(job_id, 'preview').read_bytes(), b'offline final')
        self.assertNotIn('SECRET', json.dumps(manager.metadata(job_id)))

    def test_pause_polls_inflight_task_but_never_checks_or_submits_next(self):
        self.client.status = 'running'
        reviews = []
        def reviewer(exp, index):
            reviews.append(index)
            return approval()
        manager = self.manager(workers=True, reviewer=reviewer)
        job_id, _ = self.create(manager)
        self.assertTrue(self.client.entered.wait(2))
        manager.pause(job_id)
        self.client.status = 'succeeded'
        deadline = time.monotonic() + 3
        while not self.experiment(manager, job_id).record(0).get('media') and time.monotonic() < deadline:
            time.sleep(.01)
        detail = manager.job_detail(job_id)
        self.assertEqual(detail['job']['status'], 'paused')
        self.assertTrue(self.experiment(manager, job_id).record(0).get('media'))
        self.assertFalse(detail['job']['can_review'])
        self.assertEqual(reviews, [])
        self.assertEqual(len(self.client.calls), 1)
        manager.resume(job_id)
        self.wait_status(manager, job_id, {'completed'})
        self.assertEqual(reviews, [0, 1])
        self.assertEqual(len(self.client.calls), 2)

    def test_restart_resumes_existing_task_without_repeating_submission(self):
        manager = self.manager()
        job_id, _ = self.create(manager)
        self.experiment(manager, job_id).submit(0, self.client)
        manager.close()
        again = self.manager(workers=True)
        self.wait_status(again, job_id, {'completed'})
        self.assertEqual(len(self.client.calls), 2)
        self.assertIn('cgt-fake-1', self.client.queries)
        exp = self.experiment(again, job_id)
        self.assertEqual([r['attempt'] for r in exp.data['segments']], [1, 1])

    def test_unknown_submission_is_reserved_and_never_retryable(self):
        self.client.raise_submit = TimeoutError('response lost https://example.invalid/?token=SECRET')
        manager = self.manager(workers=True)
        job_id, _ = self.create(manager)
        detail = self.wait_status(manager, job_id, {'submission_unknown'})
        self.assertGreater(detail['job']['reserved_cny'], 0)
        self.assertFalse(detail['job']['can_retry'])
        self.assertNotIn('SECRET', json.dumps(detail))
        with self.assertRaises(ValueError):
            manager.retry(job_id)
        manager.close()
        again = self.manager(workers=True)
        self.assertEqual(again.job_detail(job_id)['job']['status'], 'submission_unknown')
        self.assertEqual(len(self.client.calls), 1)

    def test_explicit_rejected_retry_keeps_every_cost_and_original_candidate(self):
        def reviewer(exp, index):
            if index == 0 and exp.record(index)['attempt'] == 1:
                return dict(approval(review_cost_cny=.1), decision='reject', action_integrity='fail')
            return approval(review_cost_cny=.1)
        manager = self.manager(workers=True, reviewer=reviewer)
        job_id, _ = self.create(manager, auto_retry_limit=0)
        first = self.wait_status(manager, job_id, {'failed'})
        first_url = first['segments'][0]['video_url']
        first_cost = first['job']['spent_cny']
        deadline = time.monotonic() + 2
        while job_id in manager._workers and time.monotonic() < deadline:
            time.sleep(.01)
        manager.retry(job_id)
        complete = self.wait_status(manager, job_id, {'completed'})
        self.assertNotEqual(first_url, complete['segments'][0]['video_url'])
        self.assertGreater(complete['job']['spent_cny'], first_cost * 2)
        exp = self.experiment(manager, job_id)
        self.assertEqual(len(exp.data['segments']), 3)
        self.assertEqual(exp.record(0)['attempt'], 2)
        self.assertEqual((exp.folder / 'segment_01' / 'original.mov').read_bytes(), b'offline video')
        self.assertEqual(exp.data['segments'][0]['review']['decision'], 'reject')

    def test_shared_cost_display_includes_other_jobs_and_failed_attempts_without_blocking(self):
        manager = self.manager()
        result = manager.create_batch(self.config(quantity=2, concurrency=2))
        for job in result['jobs']:
            exp = self.experiment(manager, job['id'])
            exp.submit(0, self.client)
            exp.record(0).update(status='failed', actual_estimated_cny=40)
            exp.save()
            manager.retry(job['id'])
        shown = manager.update_budget(result['batch']['id'], 0)
        self.assertEqual(shown['spent_cny'], 80)
        manager.close()
        again = self.manager(workers=True)
        for job in result['jobs']:
            self.wait_status(again, job['id'], {'completed'}, timeout=15)
        shown = again.update_budget(result['batch']['id'], 0)
        self.assertGreater(shown['spent_cny'], 80)
        self.assertEqual(shown['budget_cny'], 0)
        self.assertEqual(len(self.client.calls), 6)

    def test_auto_unverified_with_no_rechecks_stops_and_preserves_cost_reservation(self):
        def reviewer(exp, index):
            return dict(approval(review_cost_cny=None), decision='reject', audio_consistency='unverified')
        manager = self.manager(workers=True, reviewer=reviewer)
        job_id, _ = self.create(manager, auto_retry_limit=0)
        detail = self.wait_status(manager, job_id, {'failed'})
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(detail['segments'][0]['review']['audio_consistency'], 'unverified')
        self.assertTrue(detail['segments'][0]['review_is_suggestion'])
        self.assertFalse(detail['job']['can_review'])
        self.assertEqual(detail['job']['reserved_cny'], 1)

    def test_only_one_manager_can_own_a_persistent_queue(self):
        self.manager()
        with self.assertRaisesRegex(RuntimeError, '占用|运行|already'):
            self.manager()

    def test_unknown_media_kind_and_traversal_are_not_exposed(self):
        manager = self.manager()
        job_id, _ = self.create(manager)
        for kind in ['../../experiment.json', 'segment--1', 'segment-999', 'request.json']:
            with self.subTest(kind=kind), self.assertRaises((ValueError, FileNotFoundError)):
                manager.media_path(job_id, kind)

    def test_auto_pass_completes_and_tracks_actual_review_usage(self):
        manager = self.manager(workers=True, reviewer=lambda exp, index: approval(review_cost_cny=.25, automatic=True))
        job_id, _ = self.create(manager, review_mode='auto')
        detail = self.wait_status(manager, job_id, {'completed'})
        self.assertEqual(len(self.client.calls), 2)
        self.assertAlmostEqual(detail['job']['spent_cny'], .1 + .05 + .5)
        self.assertEqual(detail['job']['reserved_cny'], 0)

    def test_invalid_auto_review_shape_stops_with_sanitized_evidence_and_reservation(self):
        def reviewer(exp, index):
            return {'decision': {'api_key': 'SECRET'}, 'evidence': {'raw': 'SECRET'},
                    'review_cost_cny': float('nan'), 'audio_consistency': 'unverified'}
        manager = self.manager(workers=True, reviewer=reviewer)
        job_id, _ = self.create(manager, auto_retry_limit=0)
        detail = self.wait_status(manager, job_id, {'failed'})
        self.assertNotIn('SECRET', json.dumps(detail))
        self.assertFalse(detail['job']['can_review'])
        self.assertEqual(detail['job']['reserved_cny'], 1)

    def test_restart_after_retry_was_submitted_polls_that_attempt_without_duplicate_submission(self):
        manager = self.manager()
        job_id, _ = self.create(manager)
        exp = self.experiment(manager, job_id)
        exp.submit(0, self.client)
        exp.record(0)['status'] = 'failed'
        exp.save()
        manager.retry(job_id)
        exp.submit(0, self.client, resample=True)
        manager.close()
        again = self.manager(workers=True)
        self.wait_status(again, job_id, {'completed'})
        self.assertEqual(len(self.client.calls), 3)
        self.assertIn('cgt-fake-2', self.client.queries)
        self.assertEqual(self.experiment(again, job_id).record(0)['attempt'], 2)

    def test_restart_after_retry_already_failed_does_not_automatically_retry_it_again(self):
        manager = self.manager()
        job_id, _ = self.create(manager, auto_retry_limit=0)
        exp = self.experiment(manager, job_id)
        exp.submit(0, self.client)
        exp.record(0)['status'] = 'failed'
        exp.save()
        manager.retry(job_id)
        exp.submit(0, self.client, resample=True)
        exp.record(0)['status'] = 'failed'
        exp.save()
        manager.close()
        again = self.manager(workers=True)
        self.wait_status(again, job_id, {'failed'})
        self.assertEqual(len(self.client.calls), 2)

    def test_poll_failure_resume_keeps_task_id_and_does_not_resample(self):
        manager = self.manager(workers=True)
        with patch.object(self.client, 'get', side_effect=RuntimeError('temporary query outage')):
            job_id, _ = self.create(manager)
            detail = self.wait_status(manager, job_id, {'failed'})
        self.assertTrue(detail['job']['can_resume'])
        self.assertFalse(detail['job']['can_retry'])
        self.assertEqual(len(self.client.calls), 1)
        manager.resume(job_id)
        self.wait_status(manager, job_id, {'completed'})
        self.assertEqual(len(self.client.calls), 2)
        self.assertEqual(self.experiment(manager, job_id).record(0)['task_id'], 'cgt-fake-1')

    def history_fixture(self):
        """Make a standalone legacy ledger; no local experiments are required."""
        root = (Path(self.temp.name) / 'history-root').resolve()
        folder = root / 'experiments' / 'hotel_120s_v4'
        if (folder / 'experiment.json').is_file():
            return root
        spec = scenario()
        spec['target_duration_seconds'] = 120
        spec['segments'] = [dict(copy.deepcopy(spec['segments'][0]), id=str(i)) for i in range(4)]
        exp = lp.Experiment.create(folder, spec, 'fixture-profile', 'ep-fixture-history',
                                   budget_cny=200, external_cost_cny=20,
                                   model_family='seedance-2.5')
        exp.data['segments'].append({'index': 0, 'attempt': 1, 'status': 'failed',
                                    'directory': 'failed-candidate', 'actual_estimated_cny': 9.0})
        for index in range(4):
            directory = 'selected-%d' % index
            media = folder / directory / 'original.mov'
            media.parent.mkdir(parents=True)
            media.write_bytes(b'synthetic historical media')
            exp.data['segments'].append({
                'index': index, 'attempt': 2 if index == 0 else 1, 'status': 'succeeded',
                'directory': directory, 'task_id': 'cgt-fixture-history-%d' % index,
                'actual_estimated_cny': 1.25, 'reserved_cny': 1.5,
                'media': {'duration': 30.125, 'video_duration': 30.125},
                'review': approval(), 'result': {'status': 'succeeded'}})
        exp.save()
        preview = root / 'experiments' / 'final_120s' / 'derived_preview.mov'
        preview.parent.mkdir(parents=True)
        preview.write_bytes(b'synthetic completed preview')
        return root

    def test_history_is_read_only_and_counts_real_selected_segments(self):
        root = self.history_fixture()
        manager = self.manager(root=root)
        history = next(job for job in manager.state()['jobs'] if job['read_only'])
        self.assertEqual(history['status'], 'completed')
        self.assertEqual(history['duration_seconds'], 120)
        self.assertEqual(history['actual_duration_seconds'], 120.5)
        self.assertEqual(len(manager.job_detail(history['id'])['segments']), 4)
        with self.assertRaises(ValueError):
            manager.pause(history['id'])
        self.assertEqual(manager.media_path(history['id'], 'preview'), root / 'experiments/final_120s/derived_preview.mov')

    def test_uploaded_voice_asset_is_private_and_paths_are_not_accepted(self):
        manager = self.manager()
        asset_id = 'a' * 32
        folder = self.data_dir / 'voice_assets'
        folder.mkdir()
        assets = [{'role': 'reference_audio', 'description': '男女声线参考',
                   'content': {'type': 'audio_url', 'role': 'reference_audio',
                               'audio_url': {'url': 'data:audio/wav;base64,U0VDUkVU'}}}]
        (folder / (asset_id + '.json')).write_text(json.dumps(assets))
        job_id, _ = self.create(manager, voice_asset_id=asset_id)
        self.assertEqual(self.experiment(manager, job_id).data['assets'], assets)
        self.assertNotIn('U0VDUkVU', json.dumps(manager.metadata(job_id)))
        with self.assertRaises(ValueError):
            self.create(manager, voice_asset_id='../private')

    def test_concurrent_retries_both_run_even_when_reference_budget_is_exhausted(self):
        manager = self.manager()
        result = manager.create_batch(self.config(quantity=2, concurrency=2))
        for job in result['jobs']:
            exp = self.experiment(manager, job['id'])
            exp.submit(0, self.client)
            exp.record(0).update(status='failed', actual_estimated_cny=40)
            exp.save()
            manager.retry(job['id'])
        manager.update_budget(result['batch']['id'], 0)
        manager.close()
        with patch.object(self.client, 'get', return_value={'status': 'running'}):
            again = self.manager(workers=True)
            deadline = time.monotonic() + 3
            while len(self.client.calls) < 4 and time.monotonic() < deadline:
                time.sleep(.01)
            statuses = [again.job_detail(job['id'])['job']['status'] for job in result['jobs']]
            self.assertEqual(statuses, ['generating', 'generating'])
            self.assertEqual(len(self.client.calls), 4)
            self.assertEqual(len(again._workers), 2)
            again.close()

    def test_interrupted_automatic_review_with_no_rechecks_stops_without_another_model_call(self):
        manager = self.manager()
        job_id, _ = self.create(manager, auto_retry_limit=0)
        exp = self.experiment(manager, job_id)
        exp.submit(0, self.client)
        exp.poll(0, self.client)
        job = manager.data['jobs'][job_id]
        job['status'] = 'reviewing'
        job['review_attempts'] = [{'index': 0, 'attempt': 1, 'status': 'running',
                                  'reserved_cny': 1, 'cost_cny': None}]
        manager._save()
        manager.close()
        calls = []
        again = self.manager(workers=True, reviewer=lambda exp, index: calls.append(index))
        detail = self.wait_status(again, job_id, {'failed'})
        self.assertEqual(calls, [])
        self.assertEqual(detail['job']['reserved_cny'], 1)
        self.assertEqual(again.data['jobs'][job_id]['review_attempts'][0]['status'], 'interrupted')

    def test_paused_inflight_task_is_downloaded_after_restart_without_new_submission(self):
        manager = self.manager()
        job_id, _ = self.create(manager)
        self.experiment(manager, job_id).submit(0, self.client)
        manager.pause(job_id)
        manager.close()
        again = self.manager(workers=True)
        deadline = time.monotonic() + 3
        while not self.experiment(again, job_id).record(0).get('media') and time.monotonic() < deadline:
            time.sleep(.01)
        detail = again.job_detail(job_id)
        self.assertEqual(detail['job']['status'], 'paused')
        self.assertFalse(detail['job']['can_review'])
        self.assertEqual(len(self.client.calls), 1)

    def test_late_automatic_review_preserves_pause_and_reuses_saved_result_on_resume(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        reviews = []
        def reviewer(exp, index):
            reviews.append(index)
            entered.set()
            release.wait(3)
            return approval(review_cost_cny=.1, automatic=True)
        manager = self.manager(workers=True, reviewer=reviewer)
        job_id, _ = self.create(manager)
        self.assertTrue(entered.wait(2))
        manager.pause(job_id)
        release.set()
        deadline = time.monotonic() + 3
        while job_id in manager._workers and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertIsNone(self.experiment(manager, job_id).record(0)['review'])
        self.assertEqual(manager.job_detail(job_id)['job']['status'], 'paused')
        self.assertEqual(len(self.client.calls), 1)
        manager.resume(job_id)
        self.wait_status(manager, job_id, {'completed'})
        self.assertEqual(reviews, [0, 1])
        self.assertEqual(len(self.client.calls), 2)

    def test_paused_query_failure_requires_explicit_resume_even_after_restart(self):
        manager = self.manager()
        job_id, _ = self.create(manager)
        self.experiment(manager, job_id).submit(0, self.client)
        manager.pause(job_id)
        manager.close()
        with patch.object(self.client, 'get', side_effect=RuntimeError('temporary outage')) as query:
            again = self.manager(workers=True)
            time.sleep(.12)
            self.assertEqual(query.call_count, 1, 'paused query failures must not trigger a tight reschedule loop')
            detail = again.job_detail(job_id)
            self.assertEqual(detail['job']['status'], 'failed')
            self.assertTrue(detail['job']['paused'])
            self.assertTrue(detail['job']['can_resume'])
            again.close()
            recovered = self.manager(workers=True)
            time.sleep(.05)
            self.assertEqual(query.call_count, 1)
        recovered.resume(job_id)
        self.wait_status(recovered, job_id, {'completed'})
        self.assertEqual(len(self.client.calls), 2)

    def test_definite_submission_rejection_releases_reserve_and_allows_explicit_retry(self):
        self.client.raise_submit = ArkCLIError('Unauthorized: request not accepted')
        manager = self.manager(workers=True)
        job_id, _ = self.create(manager, auto_retry_limit=0)
        detail = self.wait_status(manager, job_id, {'failed'})
        self.assertTrue(detail['job']['can_retry'])
        self.assertEqual(detail['job']['reserved_cny'], 0)
        self.assertEqual(detail['job']['spent_cny'], 0)
        self.client.raise_submit = None
        deadline = time.monotonic() + 2
        while job_id in manager._workers and time.monotonic() < deadline:
            time.sleep(.01)
        manager.retry(job_id)
        self.wait_status(manager, job_id, {'completed'})
        self.assertEqual(len(self.client.calls), 3)
        self.assertEqual(self.experiment(manager, job_id).record(0)['attempt'], 2)

    def test_review_contact_and_legacy_contact_are_both_media_candidates(self):
        manager = self.manager()
        job_id, _ = self.create(manager)
        exp = self.experiment(manager, job_id)
        exp.submit(0, self.client)
        directory = exp.folder / exp.record(0)['directory']
        review_contact = directory / 'review_contact.jpg'
        review_contact.write_bytes(b'contact sheet')
        self.assertIsNotNone(manager.job_detail(job_id)['segments'][0]['contact_url'])
        self.assertEqual(manager.media_path(job_id, 'contact-0'), review_contact.resolve())
        self.assertEqual(manager.media_path(job_id, 'poster'), review_contact.resolve())
        review_contact.rename(directory / 'contact.jpg')
        self.assertEqual(manager.media_path(job_id, 'contact-0').name, 'contact.jpg')

    def test_history_costs_include_selected_video_tasks_only_and_migrate_saved_entries(self):
        root = self.history_fixture()
        manager = self.manager(root=root)
        job_id, batch_id = 'history-hotel-120', 'history-hotel'
        self.assertIn(job_id, manager.data['jobs'])
        self.assertAlmostEqual(manager.job_detail(job_id)['job']['spent_cny'], 5.0)
        self.assertEqual(manager.job_detail(job_id)['job']['reserved_cny'], 0)
        self.assertEqual(manager.metadata(job_id)['cost_basis'], 'selected video tasks usage estimate')
        # An obsolete total included rejected candidates and external work.
        manager.data['jobs'][job_id]['estimated_cny'] = 34.0
        manager.data['batches'][batch_id]['estimated_cny'] = 34.0
        manager._save()
        manager.close()
        again = self.manager(root=root)
        self.assertAlmostEqual(again.job_detail(job_id)['job']['estimated_cny'], 5.0)
        self.assertAlmostEqual(again.state()['batches'][0]['estimated_cny'], 5.0)

    def test_segment_titles_use_user_names_or_readable_fallback_and_preserve_ids(self):
        manager = self.manager()
        spec = scenario()
        spec['segments'][0].update(id='01_greeting_reservation')
        spec['segments'][1].update(id='internal_action_02', name='自定义办理环节')
        job_id, _ = self.create(manager, spec=spec)
        segments = manager.job_detail(job_id)['segments']
        self.assertEqual(segments[0]['title'], '迎宾与预订确认')
        self.assertEqual(segments[1]['title'], '自定义办理环节')
        self.assertEqual(segments[1].get('id'), 'internal_action_02')
        self.assertEqual(manager.metadata(job_id)['spec']['segments'][1]['name'], '自定义办理环节')

    def test_media_urls_version_contact_changes_and_final_poster_source(self):
        manager = self.manager()
        job_id, _ = self.create(manager)
        exp = self.experiment(manager, job_id)
        exp.submit(0, self.client)
        exp.poll(0, self.client)
        contact = exp.folder / exp.record(0)['directory'] / 'review_contact.jpg'
        contact.write_bytes(b'first contact sheet')
        first = manager.job_detail(job_id)
        self.assertEqual(first['segments'][0]['contact_url'], manager.job_detail(job_id)['segments'][0]['contact_url'])
        contact.write_bytes(b'updated contact sheet with a different size')
        changed = manager.job_detail(job_id)
        self.assertNotEqual(first['segments'][0]['contact_url'], changed['segments'][0]['contact_url'])
        preview = exp.folder / 'final.mov'
        preview.write_bytes(b'final preview')
        manager.data['jobs'][job_id]['preview_path'] = str(preview)
        final = manager.job_detail(job_id)
        self.assertNotEqual(changed['job']['poster_url'], final['job']['poster_url'])
        again = manager.job_detail(job_id)
        self.assertEqual(final['job']['poster_url'], again['job']['poster_url'])
        self.assertEqual(final['job']['preview_url'], again['job']['preview_url'])
        self.assertIn('?v=', final['segments'][0]['video_url'])
        self.assertNotIn('cgt-fake', final['segments'][0]['video_url'])


    def test_zero_budget_is_display_only_and_all_segments_finish_automatically(self):
        manager = self.manager(workers=True, reviewer=lambda *_: approval(review_cost_cny=None, automatic=True))
        job_id, batch_id = self.create(manager, budget_cny=0, review_mode='manual')
        result = self.wait_status(manager, job_id, {'completed'})
        self.assertFalse(result['job']['can_review'])
        self.assertEqual(len(self.client.calls), 2)
        self.assertEqual(result['job']['reserved_cny'], 2)
        self.assertEqual(manager.data['batches'][batch_id]['review_mode'], 'auto')
        self.assertFalse(self.experiment(manager, job_id).data['budget_enforced'])
        self.assertEqual(manager.update_budget(batch_id, 0)['budget_cny'], 0)

    def test_manual_review_api_is_retired_even_for_a_downloaded_paused_candidate(self):
        manager = self.manager()
        job_id, _ = self.create(manager)
        exp = self.experiment(manager, job_id)
        exp.submit(0, self.client)
        exp.poll(0, self.client)
        manager.pause(job_id)
        self.assertFalse(manager.job_detail(job_id)['job']['can_review'])
        with self.assertRaisesRegex(ValueError, '自动|人工'):
            manager.review(job_id, dict(approval(), segment_index=0))
        self.assertIsNone(self.experiment(manager, job_id).record(0)['review'])

    def test_content_failure_automatically_resamples_once_then_continues(self):
        def reviewer(exp, index):
            if index == 0 and exp.record(index)['attempt'] == 1:
                return dict(approval(review_cost_cny=.2), decision='reject', action_integrity='fail')
            return approval(review_cost_cny=.2)
        manager = self.manager(workers=True, reviewer=reviewer)
        job_id, _ = self.create(manager, review_mode='auto')
        self.wait_status(manager, job_id, {'completed'})
        exp = self.experiment(manager, job_id)
        self.assertEqual(len(self.client.calls), 3)
        self.assertEqual(exp.record(0)['attempt'], 2)
        self.assertEqual(exp.data['segments'][0]['review']['decision'], 'reject')
        self.assertTrue((exp.folder / 'segment_01' / 'original.mov').is_file())
        self.assertEqual(len(manager.data['jobs'][job_id]['automatic_retries']), 1)

    def test_content_failure_limit_is_durable_and_restart_does_not_restart_failed_job(self):
        reject = lambda *_: dict(approval(), decision='reject', audio_consistency='fail')
        manager = self.manager(workers=True, reviewer=reject)
        job_id, _ = self.create(manager, review_mode='auto', auto_retry_limit=1)
        result = self.wait_status(manager, job_id, {'failed'})
        self.assertEqual(len(self.client.calls), 2)
        self.assertIn('重试', result['job']['error'])
        manager.close()
        again = self.manager(workers=True, reviewer=reject)
        time.sleep(.08)
        self.assertEqual(again.job_detail(job_id)['job']['status'], 'failed')
        self.assertEqual(len(self.client.calls), 2)

    def test_inconclusive_review_rechecks_the_same_video_without_resampling(self):
        seen = []
        def reviewer(exp, index):
            seen.append((index, exp.record(index)['attempt']))
            if len(seen) == 1:
                return dict(approval(review_cost_cny=None), decision='reject', audio_consistency='unverified')
            return approval(review_cost_cny=.1)
        manager = self.manager(workers=True, reviewer=reviewer)
        job_id, _ = self.create(manager, review_mode='auto')
        result = self.wait_status(manager, job_id, {'completed'})
        self.assertEqual(seen, [(0, 1), (0, 1), (1, 1)])
        self.assertEqual(len(self.client.calls), 2)
        self.assertEqual(result['job']['reserved_cny'], 1)
        checks = manager.data['jobs'][job_id]['review_attempts']
        self.assertEqual([r['check_attempt'] for r in checks], [1, 2, 1])

    def test_inconclusive_review_stops_after_one_recheck_without_a_new_video(self):
        seen = []
        def reviewer(exp, index):
            seen.append(index)
            return dict(approval(review_cost_cny=None), decision='reject', audio_consistency='unverified')
        manager = self.manager(workers=True, reviewer=reviewer)
        job_id, _ = self.create(manager, review_mode='auto')
        result = self.wait_status(manager, job_id, {'failed'})
        self.assertEqual(seen, [0, 0])
        self.assertEqual(len(self.client.calls), 1)
        self.assertFalse(result['job']['can_review'])
        self.assertEqual(result['job']['reserved_cny'], 2)
        self.assertTrue(result['job']['can_resume'])
        self.assertFalse(result['job']['can_retry'])

    def exhausted_review_job(self):
        reviewer = lambda *_: dict(approval(review_cost_cny=None), decision='reject', audio_consistency='unverified')
        manager = self.manager(reviewer=reviewer)
        job_id, _ = self.create(manager)
        manager._run(job_id)
        self.assertEqual(manager.job_detail(job_id)['job']['status'], 'failed')
        self.assertEqual(len(manager.data['jobs'][job_id]['review_attempts']), 2)
        return manager, job_id

    def test_explicit_resume_after_inconclusive_exhaustion_rechecks_original_video_once(self):
        manager, job_id = self.exhausted_review_job()
        exp = self.experiment(manager, job_id)
        first_id = exp.record(0)['task_id']
        original = (exp.folder / 'segment_01' / 'original.mov').read_bytes()
        checks = []
        def healthy(exp, index):
            checks.append((index, exp.record(index)['task_id']))
            return approval(review_cost_cny=.1)
        manager.reviewer = healthy
        manager.resume(job_id)
        manager._run(job_id)
        detail = manager.job_detail(job_id)
        self.assertEqual(detail['job']['status'], 'completed')
        self.assertEqual(checks[0], (0, first_id))
        self.assertEqual(len(checks), 2)
        self.assertEqual(len(self.client.calls), 2)
        self.assertEqual((exp.folder / 'segment_01' / 'original.mov').read_bytes(), original)
        self.assertEqual(detail['job']['reserved_cny'], 2)
        self.assertAlmostEqual(detail['job']['spent_cny'], .15 + .2)
        grants = manager.data['jobs'][job_id]['review_recovery_grants']
        self.assertEqual(len(grants), 1)
        self.assertEqual((grants[0]['index'], grants[0]['attempt'], grants[0]['check_attempt']), (0, 1, 3))
        self.assertIsNotNone(grants[0]['consumed_at'])
        self.assertEqual(manager.data['jobs'][job_id]['automatic_retries'], [])

    def test_each_explicit_review_resume_grants_one_check_without_new_automatic_chain(self):
        manager, job_id = self.exhausted_review_job()
        for expected_checks in (3, 4):
            manager.resume(job_id)
            manager._run(job_id)
            detail = manager.job_detail(job_id)
            self.assertEqual(detail['job']['status'], 'failed')
            self.assertTrue(detail['job']['can_resume'])
            self.assertEqual(len(manager.data['jobs'][job_id]['review_attempts']), expected_checks)
            self.assertEqual(detail['job']['reserved_cny'], expected_checks)
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(len(manager.data['jobs'][job_id]['review_recovery_grants']), 2)

    def test_interrupted_recovery_check_is_consumed_and_not_reissued_on_restart(self):
        manager, job_id = self.exhausted_review_job()
        manager.resume(job_id)
        def interrupted(*_):
            raise KeyboardInterrupt('simulate process loss after durable check record')
        manager.reviewer = interrupted
        with self.assertRaises(KeyboardInterrupt):
            manager._automatic_review(manager.data['jobs'][job_id], self.experiment(manager, job_id), 0)
        job = manager.data['jobs'][job_id]
        self.assertEqual(job['review_attempts'][-1]['status'], 'running')
        self.assertIsNotNone(job['review_recovery_grants'][0]['consumed_at'])
        manager.close()
        checks = []
        again = self.manager(workers=True, reviewer=lambda *args: checks.append(args))
        detail = self.wait_status(again, job_id, {'failed'})
        self.assertEqual(checks, [])
        self.assertTrue(detail['job']['can_resume'])
        self.assertEqual(len(again.data['jobs'][job_id]['review_attempts']), 3)
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(detail['job']['reserved_cny'], 3)

    def test_pausing_a_pending_review_recovery_preserves_the_single_grant(self):
        manager, job_id = self.exhausted_review_job()
        manager.resume(job_id)
        manager.pause(job_id)
        manager.close()
        checks = []
        def healthy(exp, index):
            checks.append(index)
            return approval(review_cost_cny=.1)
        again = self.manager(workers=True, reviewer=healthy)
        time.sleep(.1)
        self.assertEqual(again.job_detail(job_id)['job']['status'], 'paused')
        self.assertEqual(checks, [])
        self.assertEqual(len(again.data['jobs'][job_id]['review_recovery_grants']), 1)
        again.resume(job_id)
        self.wait_status(again, job_id, {'completed'})
        self.assertEqual(checks, [0, 1])
        self.assertEqual(len(again.data['jobs'][job_id]['review_recovery_grants']), 1)

    def test_legacy_exhausted_review_gets_a_resume_entry_without_restarting(self):
        manager, job_id = self.exhausted_review_job()
        manager.data['jobs'][job_id]['resume_stage'] = None
        manager._save()
        manager.close()
        checks = []
        again = self.manager(workers=True, reviewer=lambda *args: checks.append(args))
        time.sleep(.08)
        detail = again.job_detail(job_id)
        self.assertEqual(detail['job']['status'], 'failed')
        self.assertTrue(detail['job']['can_resume'])
        self.assertEqual(checks, [])
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(again.data['jobs'][job_id]['review_recovery_grants'], [])

    def test_retry_limit_zero_disables_resampling_and_invalid_limits_are_rejected(self):
        reject = lambda *_: dict(approval(), decision='reject', visual_continuity='fail')
        manager = self.manager(workers=True, reviewer=reject)
        job_id, _ = self.create(manager, review_mode='auto', auto_retry_limit=0)
        self.wait_status(manager, job_id, {'failed'})
        self.assertEqual(len(self.client.calls), 1)
        for value in (-1, True, 4, 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                manager.create_batch(self.config(auto_retry_limit=value))

    def test_missing_reviewer_fails_before_a_new_generation_request(self):
        manager = self.manager(workers=True, reviewer=None)
        job_id, _ = self.create(manager, review_mode='auto')
        detail = self.wait_status(manager, job_id, {'failed'})
        self.assertEqual(self.client.calls, [])
        self.assertIn('自动', detail['job']['error'])
        manager.reviewer = lambda *_: approval(review_cost_cny=.1)
        manager.resume(job_id)
        self.wait_status(manager, job_id, {'completed'})
        self.assertEqual(manager.data['jobs'][job_id].get('review_recovery_grants', []), [])

    def test_remote_generation_failure_automatically_retries_once(self):
        self.client.status = 'failed'
        manager = self.manager(workers=True, reviewer=lambda *_: approval())
        job_id, _ = self.create(manager, review_mode='auto')
        self.wait_status(manager, job_id, {'failed'})
        self.assertEqual(len(self.client.calls), 2)
        self.assertEqual(len(self.experiment(manager, job_id).data['segments']), 2)

    def test_legacy_waiting_and_budget_states_migrate_without_unpausing_or_restarting_failures(self):
        manager = self.manager()
        result = manager.create_batch(self.config(quantity=4))
        jobs = [manager.data['jobs'][row['id']] for row in result['jobs']]
        for job, status in zip(jobs, ['awaiting_review', 'budget_blocked', 'paused', 'failed']):
            job.update(status=status, paused=status == 'paused')
        manager.data['batches'][result['batch']['id']]['review_mode'] = 'manual'
        manager._save()
        manager.close()
        again = self.manager()
        self.assertEqual([again.job_detail(job['id'])['job']['status'] for job in jobs],
                         ['queued', 'queued', 'paused', 'failed'])
        self.assertTrue(again.data['jobs'][jobs[2]['id']]['paused'])
        self.assertEqual(self.client.calls, [])

    def test_interrupted_review_is_rechecked_once_and_keeps_unknown_cost(self):
        manager = self.manager()
        job_id, _ = self.create(manager, review_mode='auto')
        exp = self.experiment(manager, job_id)
        exp.submit(0, self.client)
        exp.poll(0, self.client)
        job = manager.data['jobs'][job_id]
        job.update(status='reviewing', review_attempts=[{'index': 0, 'attempt': 1, 'status': 'running',
                                                       'reserved_cny': 1, 'cost_cny': None}])
        manager._save()
        manager.close()
        checks = []
        def reviewer(exp, index):
            checks.append(index)
            return approval(review_cost_cny=.1)
        again = self.manager(workers=True, reviewer=reviewer)
        detail = self.wait_status(again, job_id, {'completed'})
        self.assertEqual(checks, [0, 1])
        self.assertEqual(detail['job']['reserved_cny'], 1)
        self.assertEqual(len(self.client.calls), 2)

    def test_completed_legacy_batch_policy_is_not_rewritten_on_restart(self):
        manager = self.manager(workers=True)
        job_id, batch_id = self.create(manager)
        self.wait_status(manager, job_id, {'completed'})
        manager.data['batches'][batch_id].update(review_mode='manual', budget_enforced=True)
        saved = json.loads(json.dumps(manager.data['batches'][batch_id]))
        manager._save()
        manager.close()
        again = self.manager()
        self.assertEqual(again.data['batches'][batch_id], saved)
        self.assertEqual(again.job_detail(job_id)['job']['review_mode'], 'manual')
        self.assertEqual(again.job_detail(job_id)['job']['status'], 'completed')

    def test_unknown_model_price_is_null_and_does_not_block_generation(self):
        spec = scenario()
        spec['segments'] = [dict(spec['segments'][0], id=str(i), duration=15) for i in range(4)]
        manager = self.manager(workers=True, reviewer=lambda *_: approval(review_cost_cny=.1))
        config = self.config(spec=spec, model_family='seedance-2.0', resolution='720p', budget_cny=0)
        estimate = manager.estimate(config)
        self.assertIsNone(estimate['estimated_total_cny'])
        self.assertIsNone(estimate['recommended_budget_cny'])
        with patch.object(lp, 'probe', return_value={'duration': 15, 'video_duration': 15,
                          'width': 1280, 'height': 720, 'fps': '24/1', 'video_codec': 'h264', 'audio_streams': []}):
            created = manager.create_batch(config)
            job_id = created['jobs'][0]['id']
            detail = self.wait_status(manager, job_id, {'completed'}, timeout=15)
        self.assertEqual(detail['job']['model_family'], 'seedance-2.0')
        self.assertIsNone(detail['job']['reserved_cny'])
        self.assertEqual(detail['job']['known_reserved_cny'], 0)
        self.assertAlmostEqual(detail['job']['known_spent_cny'], .4)
        self.assertEqual(manager.metadata(job_id)['model_family'], 'seedance-2.0')
        self.assertEqual(len(self.client.calls), 4)
        self.assertTrue(all(row['model'] == 'doubao-seedance-2-0-260128' for row in self.client.calls))

    def test_retry_limit_three_is_honored_and_saved(self):
        reject = lambda *_: dict(approval(), decision='reject', action_integrity='fail')
        manager = self.manager(workers=True, reviewer=reject)
        job_id, _ = self.create(manager, auto_retry_limit=3)
        detail = self.wait_status(manager, job_id, {'failed'})
        self.assertEqual(detail['job']['auto_retry_limit'], 3)
        self.assertEqual(len(self.client.calls), 4)
        self.assertEqual(len(manager.metadata(job_id)['automatic_retries']), 3)

    def test_pausing_during_failed_check_does_not_spend_automatic_retry_allowance(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def reviewer(exp, index):
            entered.set()
            release.wait(3)
            return dict(approval(), decision='reject', action_integrity='fail')
        manager = self.manager(workers=True, reviewer=reviewer)
        job_id, _ = self.create(manager)
        self.assertTrue(entered.wait(2))
        manager.pause(job_id)
        release.set()
        deadline = time.monotonic() + 2
        while job_id in manager._workers and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(manager.job_detail(job_id)['job']['status'], 'paused')
        self.assertEqual(manager.data['jobs'][job_id]['automatic_retries'], [])
        self.assertEqual(len(self.client.calls), 1)
        manager.resume(job_id)
        self.wait_status(manager, job_id, {'failed'})
        self.assertEqual(len(self.client.calls), 2)

    def test_restart_does_not_reset_the_count_of_an_authorized_automatic_retry(self):
        manager = self.manager()
        job_id, _ = self.create(manager)
        exp = self.experiment(manager, job_id)
        exp.submit(0, self.client)
        exp.record(0)['status'] = 'failed'
        exp.save()
        with manager._lock:
            self.assertTrue(manager._automatic_retry(manager.data['jobs'][job_id], 0, exp.record(0), 'fixture failure'))
        manager.close()
        reject = lambda *_: dict(approval(), decision='reject', action_integrity='fail')
        again = self.manager(workers=True, reviewer=reject)
        self.wait_status(again, job_id, {'failed'})
        self.assertEqual(len(self.client.calls), 2)
        self.assertEqual(len(again.data['jobs'][job_id]['automatic_retries']), 1)

    def test_interrupted_second_check_does_not_gain_a_third_check_after_restart(self):
        manager = self.manager()
        job_id, _ = self.create(manager)
        exp = self.experiment(manager, job_id)
        exp.submit(0, self.client)
        exp.poll(0, self.client)
        manager.data['jobs'][job_id].update(status='reviewing', review_attempts=[
            {'index': 0, 'attempt': 1, 'check_attempt': 1, 'status': 'done', 'reserved_cny': 1, 'cost_cny': .1,
             'result': dict(approval(), decision='reject', audio_consistency='unverified')},
            {'index': 0, 'attempt': 1, 'check_attempt': 2, 'status': 'running', 'reserved_cny': 1, 'cost_cny': None}])
        manager._save()
        manager.close()
        checks = []
        again = self.manager(workers=True, reviewer=lambda *args: checks.append(args))
        detail = self.wait_status(again, job_id, {'failed'})
        self.assertEqual(checks, [])
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(detail['job']['reserved_cny'], 1)

    def test_paused_legacy_download_does_not_start_automatic_checks(self):
        manager = self.manager()
        job_id, _ = self.create(manager)
        exp = self.experiment(manager, job_id)
        exp.submit(0, self.client)
        exp.poll(0, self.client)
        manager.data['jobs'][job_id].update(status='awaiting_review', paused=True)
        manager._save()
        manager.close()
        checks = []
        again = self.manager(workers=True, reviewer=lambda *args: checks.append(args))
        time.sleep(.1)
        self.assertEqual(again.job_detail(job_id)['job']['status'], 'paused')
        self.assertEqual(checks, [])
        self.assertEqual(len(self.client.calls), 1)


if __name__ == '__main__':
    unittest.main()
