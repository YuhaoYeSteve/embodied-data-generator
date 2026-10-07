import json
import contextlib
import copy
import hashlib
import inspect
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import longform_pipeline as lp


class FakeClient:
    def __init__(self):
        self.calls = []
    def submit(self, body):
        self.calls.append(body)
        return {'id': 'cgt-test-' + str(len(self.calls)), 'status': 'queued'}


def scenario():
    return {'name': 'test', 'version': 1, 'target_duration_seconds': 60,
            'global_prompt': '固定人物与自然ego视角',
            'segments': [dict(id=str(i), duration=30, prompt='动作'+str(i),
                              expected_start_state='开始', expected_end_state='结束',
                              required_actions=['交互']) for i in range(2)]}


# Synthetic test prices, not a provider or account quotation.
FIXTURE_PRICES = {'seedance-2.5': {
    '480p': {'nv2v': .1, 'v2v': .05},
    '720p': {'nv2v': .1, 'v2v': .05},
    '1080p': {'nv2v': .2, 'v2v': .1},
}}


class PriceFixture(unittest.TestCase):
    def setUp(self):
        patcher = patch('studio_models._PRICES', FIXTURE_PRICES)
        patcher.start()
        self.addCleanup(patcher.stop)


class PipelineTests(PriceFixture):
    def test_model_20_continuation_omits_unverified_25_request_options(self):
        spec=scenario()
        spec['segments']=[dict(spec['segments'][0],id=str(i),duration=15) for i in range(4)]
        body=lp.build_request(spec,1,'doubao-seedance-2-0-260128',resolution='4k',
            previous={'video_url':'https://example/prior.mp4','duration':15},observed_state='物体已放稳，双手空闲')
        self.assertNotIn('omni_reference_task_type',body)
        self.assertNotIn('output_format',body)
        self.assertIn('向后延长@视频1',body['content'][0]['text'])
        self.assertIn('物体已放稳',body['content'][0]['text'])

    def test_standalone_four_second_experiment_remains_supported(self):
        spec=scenario()
        spec['segments']=[dict(spec['segments'][0],duration=4)]
        spec['target_duration_seconds']=4
        with tempfile.TemporaryDirectory() as d:
            try:
                exp=lp.Experiment.create(d,spec,'profile','ep-test')
                client=FakeClient()
                exp.submit(0,client)
            except ValueError as exc:
                self.fail('工作台总时长限制不能破坏底层短实验: '+str(exc))
            self.assertEqual(client.calls[0]['duration'],4)

    def test_model_20_rejects_30_seconds_before_request(self):
        with self.assertRaisesRegex(ValueError, '15|时长'):
            lp.build_request(scenario(), 0, 'doubao-seedance-2-0-260128')

    def test_model_25_rejects_4k_before_request(self):
        with self.assertRaisesRegex(ValueError, '4k|分辨率'):
            lp.build_request(scenario(), 0, 'doubao-seedance-2-5-260628', resolution='4k')

    def test_model_20_rejects_overlong_reference(self):
        spec=scenario()
        spec['segments']=[dict(spec['segments'][0],id=str(i),duration=15) for i in range(4)]
        with self.assertRaisesRegex(ValueError, '15|参考'):
            lp.build_request(spec,1,'doubao-seedance-2-0-260128',
                previous={'video_url':'https://example/prior.mov','duration':30},observed_state='空手')

    def test_informational_budget_does_not_gate_submission_or_resume(self):
        self.assertIn('budget_enforced',inspect.signature(lp.Experiment.create).parameters)
        with tempfile.TemporaryDirectory() as d:
            exp=lp.Experiment.create(d,scenario(),'profile','doubao-seedance-2-5-260628',
                budget_cny=0,budget_enforced=False,model_family='seedance-2.5')
            client=FakeClient()
            task=exp.submit(0,client)
            again=lp.Experiment(d).submit(0,client)
            self.assertEqual(task['id'],again['id'])
            self.assertEqual(len(client.calls),1)
            self.assertGreater(exp.committed_cost(),0)

    def test_high_resolution_uses_its_own_price(self):
        self.assertIn('resolution',inspect.signature(lp.usage_cost).parameters)
        self.assertAlmostEqual(lp.usage_cost({'completion_tokens':1000},False,
            model_family='seedance-2.5',resolution='1080p'),.2)
        self.assertAlmostEqual(lp.usage_cost({'completion_tokens':1000},True,
            model_family='seedance-2.5',resolution='1080p'),.1)

    def test_unknown_price_stays_unknown_without_blocking(self):
        self.assertIn('model_family',inspect.signature(lp.Experiment.create).parameters)
        spec=scenario()
        spec['segments']=[dict(spec['segments'][0],id=str(i),duration=15) for i in range(4)]
        with tempfile.TemporaryDirectory() as d:
            exp=lp.Experiment.create(d,spec,'profile','doubao-seedance-2-0-260128',
                resolution='4k',budget_cny=0,budget_enforced=False,model_family='seedance-2.0')
            with patch('studio_models.price_rate',return_value=None):
                exp.submit(0,FakeClient())
            self.assertIsNone(exp.record(0)['reserved_cny'])
            self.assertIsNone(exp.committed_cost())
            self.assertEqual(exp.data['model_family'],'seedance-2.0')

    def test_image_reference_is_only_sent_to_its_bound_segment(self):
        spec = scenario()
        image = {'role': 'reference_image', 'segment_ids': ['0'],
                 'content': {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,cGhvbmU='},
                             'role': 'reference_image'},
                 'description': '@图片1仅参考手机订单页面布局。'}
        audio = {'role': 'reference_audio',
                 'content': {'type': 'audio_url', 'audio_url': {'url': 'data:audio/mpeg;base64,dm9pY2U='},
                             'role': 'reference_audio'}, 'description': '保持参考声线。'}
        first = lp.build_request(spec, 0, 'ep-test', assets=[audio, image])
        second = lp.build_request(spec, 1, 'ep-test', assets=[audio, image],
            previous={'output_url': 'https://example/previous.mov', 'duration': 30}, observed_state='手机已收回')
        self.assertEqual([item['type'] for item in first['content']], ['text', 'audio_url', 'image_url'])
        self.assertEqual(first['content'][-1]['role'], 'reference_image')
        self.assertIn('@图片1仅参考手机订单页面布局。', first['content'][0]['text'])
        self.assertEqual([item['type'] for item in second['content']], ['text', 'video_url', 'audio_url'])
        self.assertNotIn('@图片1', second['content'][0]['text'])
        self.assertIn('保持参考声线。', second['content'][0]['text'])
        self.assertEqual(image['segment_ids'], ['0'])

    def test_bad_reference_scope_is_rejected(self):
        for scope in ([], '0', ['absent'], ['0', '0'], [0], None):
            image = {'role': 'reference_image', 'segment_ids': scope,
                     'content': {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,cGhvbmU='},
                                 'role': 'reference_image'}, 'description': '手机界面'}
            with self.subTest(scope=scope), self.assertRaisesRegex(ValueError, 'segment_ids'):
                lp.build_request(scenario(), 0, 'ep-test', assets=[image])

    def test_unscoped_image_reference_remains_available_for_legacy_assets(self):
        image = {'role': 'reference_image',
                 'content': {'type': 'image_url', 'image_url': {'url': 'https://example/room.png'},
                             'role': 'reference_image'}, 'description': '空间参考'}
        result = lp.build_request(scenario(), 1, 'ep-test', assets=[image],
            previous={'output_url': 'https://example/previous.mov', 'duration': 30}, observed_state='柜台前')
        self.assertEqual(result['content'][-1], image['content'])

    def test_embodied_structured_edits_are_sent_to_video_model(self):
        spec = scenario()
        spec['dataset'] = {'annotation_scope': 'planned'}
        spec['segments'][0].update(
            required_actions=['抓起绿色杯子并放进左侧收纳格'],
            expected_start_state='绿色杯子在桌面中央，双手空闲',
            expected_end_state='绿色杯子在左侧收纳格，手已松开')
        prompt = lp.build_request(spec, 0, 'ep-test')['content'][0]['text']
        self.assertIn('抓起绿色杯子并放进左侧收纳格', prompt)
        self.assertIn('绿色杯子在桌面中央，双手空闲', prompt)
        self.assertIn('绿色杯子在左侧收纳格，手已松开', prompt)

    def test_embodied_continuation_uses_observed_start_and_edited_goal(self):
        spec = scenario()
        spec['dataset'] = {'annotation_scope': 'planned'}
        spec['segments'][1].update(
            required_actions=['将工具盒合上'],
            expected_start_state='过期的计划起始状态',
            expected_end_state='工具盒闭合，右手空闲')
        prompt = lp.build_request(spec, 1, 'ep-test',
            previous={'output_url': 'https://example/previous.mov', 'duration': 30},
            observed_state='工具盒打开，右手扶着盒盖')['content'][0]['text']
        self.assertIn('工具盒打开，右手扶着盒盖', prompt)
        self.assertIn('将工具盒合上', prompt)
        self.assertIn('工具盒闭合，右手空闲', prompt)
        self.assertNotIn('过期的计划起始状态', prompt)

    def test_extension_needs_accepted_observed_state(self):
        with self.assertRaisesRegex(ValueError, '实际结束状态'):
            lp.build_request(scenario(), 1, 'ep-test', previous={'content': {'video_url': 'https://example/a.mov'}, 'duration':30})

    def test_extension_uses_original_video_not_accumulated_movie(self):
        result = lp.build_request(scenario(), 1, 'ep-test',
                    previous={'content': {'video_url': 'https://example/a.mov'}, 'duration':30},
                    observed_state='证件已归还，双手空闲')
        self.assertEqual(result['omni_reference_task_type'], 'extend')
        self.assertEqual(result['ratio'], 'adaptive')
        self.assertEqual(result['content'][1]['role'], 'reference_video')
        self.assertIn('证件已归还', result['content'][0]['text'])

    def test_rejects_accumulated_sixty_second_reference(self):
        with self.assertRaisesRegex(ValueError, '30'):
            lp.build_request(scenario(), 1, 'ep-test', previous={'duration':60, 'content':{'video_url':'https://example/full.mov'}}, observed_state='已核验')

    def test_cost_counts_reference_once(self):
        self.assertAlmostEqual(lp.estimate_cost(30,30,1280,720),64.8)
        self.assertAlmostEqual(lp.usage_cost({'completion_tokens':1296000,'total_tokens':1296000}, True),64.8)

    def test_short_video_reference_has_twenty_second_floor_for_30s_output(self):
        self.assertAlmostEqual(lp.estimate_cost(30,4.064,1280,720),54.0)

    def test_budget_prevents_submission(self):
        with tempfile.TemporaryDirectory() as d:
            client=FakeClient()
            exp=lp.Experiment.create(Path(d),scenario(),'profile','ep-test',budget_cny=1)
            with self.assertRaisesRegex(ValueError,'预算'):
                exp.submit(0,client)
            self.assertEqual(client.calls,[])

    def test_resume_never_recreates_existing_task(self):
        with tempfile.TemporaryDirectory() as d:
            client=FakeClient()
            exp=lp.Experiment.create(Path(d),scenario(),'profile','ep-test',budget_cny=200)
            task=exp.submit(0,client)
            again=lp.Experiment(Path(d)).submit(0,client)
            self.assertEqual(task['id'],again['id'])
            self.assertEqual(len(client.calls),1)

    def test_downstream_blocked_until_review(self):
        with tempfile.TemporaryDirectory() as d:
            exp=lp.Experiment.create(Path(d),scenario(),'profile','ep-test',budget_cny=200)
            with self.assertRaisesRegex(ValueError,'评审'):
                exp.submit(1,FakeClient())

    def test_submitted_upstream_without_review_is_blocked(self):
        with tempfile.TemporaryDirectory() as d:
            exp=lp.Experiment.create(Path(d),scenario(),'profile','ep-test',budget_cny=200)
            exp.submit(0,FakeClient())
            with self.assertRaisesRegex(ValueError,'评审'):
                exp.submit(1,FakeClient())

    def test_actual_arkcli_output_url_is_usable(self):
        self.assertEqual(lp.video_url({'output_url':'https://example/result.mov'}),'https://example/result.mov')

    def test_unknown_submission_retains_reservation(self):
        class TimeoutClient:
            def submit(self,body):
                raise TimeoutError('response lost')
        with tempfile.TemporaryDirectory() as d:
            exp=lp.Experiment.create(Path(d),scenario(),'profile','ep-test',budget_cny=200)
            with self.assertRaises(TimeoutError): exp.submit(0,TimeoutClient())
            again=lp.Experiment(Path(d))
            self.assertGreater(again.committed_cost(),40)
            with self.assertRaisesRegex(ValueError,'提交状态'):
                again.submit(0,FakeClient())

    def test_review_requires_media_and_each_quality_dimension(self):
        with tempfile.TemporaryDirectory() as d:
            exp=lp.Experiment.create(Path(d),scenario(),'profile','ep-test',budget_cny=200)
            exp.submit(0,FakeClient())
            with self.assertRaisesRegex(ValueError,'成功|下载|检查'):
                exp.review(0,{'decision':'accept','observed_end_state':'空手'})

    def test_stale_instances_cannot_double_submit(self):
        with tempfile.TemporaryDirectory() as d:
            first=lp.Experiment.create(Path(d),scenario(),'profile','ep-test',budget_cny=200)
            second=lp.Experiment(Path(d)); client=FakeClient()
            first.submit(0,client); second.submit(0,client)
            self.assertEqual(len(client.calls),1)

    def test_media_duration_fallback_allows_normal_container_tail(self):
        self.assertEqual(lp.reference_result({'result':{'output_url':'https://example/a'},
            'media':{'video_duration':30.041667}},30)['duration'],30)

    def test_reject_short_output_during_acceptance(self):
        with tempfile.TemporaryDirectory() as d:
            exp=lp.Experiment.create(Path(d),scenario(),'profile','ep-test',budget_cny=200)
            exp.submit(0,FakeClient())
            exp.record(0).update(status='succeeded',media={'video_duration':5})
            exp.save()
            with self.assertRaisesRegex(ValueError,'时长'):
                exp.review(0,{'decision':'accept','observed_end_state':'空手','evidence':'test',
                    'visual_continuity':'pass','action_integrity':'pass','audio_consistency':'pass'})


class ResampleTests(PriceFixture):
    def experiment(self, budget=200):
        self.assertIn('resample', inspect.signature(lp.Experiment.submit).parameters,
                      'submit must expose explicit resample while preserving default resume')
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        return lp.Experiment.create(Path(temp.name), scenario(), 'profile', 'ep-test', budget_cny=budget)

    def test_default_submit_recovers_failed_task_id_without_new_charge(self):
        exp = self.experiment()
        client = FakeClient()
        original = exp.submit(0, client)
        exp.record(0)['status'] = 'failed'
        exp.save()
        again = exp.submit(0, client)
        self.assertEqual(again['id'], original['id'])
        self.assertEqual(len(client.calls), 1)

    def test_explicit_resample_requires_a_terminal_failure_or_rejected_success(self):
        allowed = [('failed', None), ('cancelled', None), ('canceled', None), ('expired', None), ('submission_rejected', None),
                   ('succeeded', {'decision': 'reject'})]
        for status, review in allowed:
            with self.subTest(status=status, review=review):
                exp = self.experiment()
                client = FakeClient()
                exp.submit(0, client)
                exp.record(0).update(status=status, review=review)
                exp.save()
                self.assertEqual(exp.submit(0, client, resample=True)['id'], 'cgt-test-2')
                self.assertEqual(len(client.calls), 2)

    def test_unknown_active_accepted_and_unreviewed_tasks_cannot_resample(self):
        blocked = [('submission_unknown', None), ('unknown', None), ('submitting', None),
                   ('queued', None), ('running', None), ('submitted', None),
                   ('succeeded', None),
                   ('succeeded', {'decision': 'accept'}), ('failed', {'decision': 'accept'})]
        for status, review in blocked:
            with self.subTest(status=status, review=review):
                exp = self.experiment()
                client = FakeClient()
                exp.submit(0, client)
                exp.record(0).update(status=status, review=review)
                exp.save()
                with self.assertRaises(ValueError):
                    exp.submit(0, client, resample=True)
                self.assertEqual(len(client.calls), 1)
                self.assertEqual(len(exp.data['segments']), 1)

    def test_definite_rejection_releases_reservation_and_retry_is_explicit(self):
        from longform_client import ArkCLIError
        exp = self.experiment(budget=80)
        class RejectedClient:
            def submit(self, body):
                raise ArkCLIError('Authentication rejected before task creation')
        with self.assertRaises(ArkCLIError):
            exp.submit(0, RejectedClient())
        entry = exp.record(0)
        self.assertEqual(entry['status'], 'submission_rejected')
        self.assertEqual(entry['actual_estimated_cny'], 0)
        self.assertEqual(entry['reserved_cny'], 0)
        self.assertIn('not accepted', entry.get('cost_basis', ''))
        client = FakeClient()
        with self.assertRaises(ValueError):
            exp.submit(0, client)
        self.assertEqual(client.calls, [])
        exp.submit(0, client, resample=True)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(exp.record(0)['attempt'], 2)

    def test_resample_does_not_create_first_candidate(self):
        exp = self.experiment()
        client = FakeClient()
        with self.assertRaises(ValueError):
            exp.submit(0, client, resample=True)
        self.assertEqual(client.calls, [])

    def test_resample_preserves_original_candidate_files_and_cost(self):
        exp = self.experiment()
        client = FakeClient()
        exp.submit(0, client)
        first = exp.record(0)
        first.update(status='succeeded', review={'decision': 'reject'}, actual_estimated_cny=21.5)
        original_file = exp.folder / first['directory'] / 'original.mov'
        original_file.write_bytes(b'original candidate bytes')
        request_file = original_file.with_name('request.json')
        original_request = request_file.read_bytes()
        exp.save()
        snapshot = copy.deepcopy(first)
        exp.submit(0, client, resample=True)
        latest = exp.record(0)
        self.assertEqual(exp.data['segments'][0], snapshot)
        self.assertEqual(original_file.read_bytes(), b'original candidate bytes')
        self.assertEqual(request_file.read_bytes(), original_request)
        self.assertEqual(latest['directory'], 'segment_01_attempt_02')
        self.assertEqual(latest['attempt'], 2)
        self.assertEqual(latest['reason'], 'explicit resample')
        self.assertEqual(first['attempt'], 1)
        self.assertEqual(len(exp.data['segments']), 2)
        self.assertAlmostEqual(exp.committed_cost(), 21.5 + latest['reserved_cny'])

    def test_budget_counts_previous_failed_attempt(self):
        exp = self.experiment(budget=90)
        client = FakeClient()
        exp.submit(0, client)
        exp.record(0).update(status='failed', actual_estimated_cny=35)
        exp.save()
        with self.assertRaisesRegex(ValueError, '预算'):
            exp.submit(0, client, resample=True)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(len(exp.data['segments']), 1)

    def test_new_attempt_records_code_hashes_and_reuses_immutable_snapshot(self):
        exp = self.experiment()
        client = FakeClient()
        exp.submit(0, client)
        first = exp.record(0)
        self.assertIn('code_sha256', first)
        self.assertIn('code_snapshot_directory', first)
        snapshot = exp.folder / first['code_snapshot_directory']
        mtimes = {}
        for name in ('longform_pipeline.py', 'longform_client.py'):
            source = Path(lp.__file__).with_name(name).read_bytes()
            self.assertEqual(first['code_sha256'][name], hashlib.sha256(source).hexdigest())
            self.assertEqual((snapshot / name).read_bytes(), source)
            mtimes[name] = (snapshot / name).stat().st_mtime_ns
        first['status'] = 'failed'
        exp.save()
        exp.submit(0, client, resample=True)
        self.assertEqual(exp.record(0)['code_snapshot_directory'], first['code_snapshot_directory'])
        for name, mtime in mtimes.items():
            self.assertEqual((snapshot / name).stat().st_mtime_ns, mtime)

    def test_second_segment_legacy_record_resamples_without_rewriting_history(self):
        exp = self.experiment(budget=300)
        client = FakeClient()
        exp.submit(0, client)
        exp.record(0).update(status='succeeded', media={'video_duration': 30},
                             result={'output_url': 'https://example/first.mov'},
                             review={'decision': 'accept', 'observed_end_state': '已核验'})
        exp.save()
        exp.submit(1, client)
        old = exp.record(1)
        old.update(status='failed')
        old.pop('attempt')
        old.pop('reason')
        old.pop('code_sha256', None)
        old.pop('code_snapshot_directory', None)
        snapshot = copy.deepcopy(old)
        exp.save()
        exp.submit(1, client, resample=True)
        self.assertEqual(exp.data['segments'][1], snapshot)
        self.assertEqual(exp.record(1)['directory'], 'segment_02_attempt_02')
        self.assertEqual(exp.record(1)['attempt'], 2)
        self.assertEqual(exp.record(1)['upstream_task_id'], 'cgt-test-1')

    def test_resample_blocked_if_any_downstream_candidate_exists(self):
        exp = self.experiment()
        client = FakeClient()
        exp.submit(0, client)
        exp.record(0).update(status='failed')
        exp.data['segments'].append({'index': 1, 'status': 'queued', 'task_id': 'cgt-downstream',
                                     'reserved_cny': 1, 'actual_estimated_cny': None})
        exp.save()
        with self.assertRaisesRegex(ValueError, '下游'):
            exp.submit(0, client, resample=True)
        self.assertEqual(len(client.calls), 1)

    def test_two_stale_retry_instances_create_only_one_new_candidate(self):
        exp = self.experiment()
        client = FakeClient()
        exp.submit(0, client)
        exp.record(0).update(status='failed')
        exp.save()
        stale = lp.Experiment(exp.folder)
        exp.submit(0, client, resample=True)
        with self.assertRaises(ValueError):
            stale.submit(0, client, resample=True)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(len(lp.Experiment(exp.folder).data['segments']), 2)

    def test_manifest_lists_all_candidates_and_marks_only_selected_attempts(self):
        exp = self.experiment()
        media = {'width': 1280, 'height': 720, 'fps': '24/1', 'video_codec': 'h264',
                 'audio_streams': [{'codec': 'aac'}], 'duration': 30, 'video_duration': 30}
        exp.data['segments'] = [
            {'index': 0, 'attempt': 1, 'directory': 'segment_01', 'task_id': 'cgt-rejected',
             'status': 'succeeded', 'review': {'decision': 'reject'}, 'media': media,
             'actual_estimated_cny': 3, 'reserved_cny': 4},
            {'index': 0, 'attempt': 2, 'directory': 'segment_01_attempt_02', 'task_id': 'cgt-selected-1',
             'status': 'succeeded', 'review': {'decision': 'accept'}, 'media': media,
             'actual_estimated_cny': 5, 'reserved_cny': 6},
            {'index': 1, 'attempt': 1, 'directory': 'segment_02', 'task_id': 'cgt-selected-2',
             'status': 'succeeded', 'review': {'decision': 'accept'}, 'media': media,
             'actual_estimated_cny': 7, 'reserved_cny': 8},
        ]
        exp.save()
        with patch.object(lp.subprocess, 'run'), patch.object(lp, 'probe', return_value=dict(media, duration=60, video_duration=60)):
            manifest = exp.assemble()
        self.assertEqual(manifest['cost_estimated_cny'], 15)
        self.assertEqual(len(manifest['cost_items']), 3)
        selected = {row['task_id']: row['selected'] for row in manifest['cost_items']}
        self.assertEqual(selected, {'cgt-rejected': False, 'cgt-selected-1': True, 'cgt-selected-2': True})
        self.assertEqual([row['task_id'] for row in manifest['boundaries']], ['cgt-selected-1', 'cgt-selected-2'])

    def test_cli_retry_calls_explicit_resample(self):
        exp = self.experiment()
        client = FakeClient()
        exp.submit(0, client)
        exp.record(0).update(status='failed')
        exp.save()
        with patch('sys.argv', ['longform_pipeline.py', 'retry', '--run-dir', str(exp.folder), '--segment', '0']), \
             patch('longform_client.ArkCLIClient', return_value=client), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                lp.main()
            except SystemExit as exc:
                self.fail('CLI retry must be supported: ' + str(exc))
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(lp.Experiment(exp.folder).record(0)['attempt'], 2)


if __name__ == '__main__': unittest.main()
