"""Offline scenario planning contracts; no model or video request is sent."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

if importlib.util.find_spec('studio_scenarios'):
    import studio_scenarios as planner
else:
    planner = None


def config(**changes):
    return dict(dict(description='在工作台上完成物品整理并确认结果。', task_type='manipulation',
                     duration_seconds=60, quantity=2, diversity='改变物品和动作顺序'), **changes)


def variant(index):
    item, action = [('收纳盒', '打开收纳盒并放入螺母'), ('折叠布', '铺平折叠布并包住木块')][index]
    return {
        'name': item + '整理', 'version': '1', 'global_prompt': '连续第一人称视角，双手和物体保持可见。',
        'target_duration_seconds': 60,
        'segments': [
            {'id': 's01', 'title': '操作', 'duration': 30,
             'prompt': '0–15秒：' + action + '。15–30秒：检查接触位置。',
             'required_actions': [action], 'expected_start_state': '双手空闲，' + item + '在桌上。',
             'expected_end_state': item + '已经完成第一次操作。', 'action_labels': ['manipulate']},
            {'id': 's02', 'title': '确认', 'duration': 30,
             'prompt': '0–15秒：转动' + item + '检查边缘。15–30秒：将其摆回桌面。',
             'required_actions': ['转动' + item, '放回' + item],
             'expected_start_state': item + '已经完成第一次操作。',
             'expected_end_state': item + '在桌面，双手空闲。', 'action_labels': ['inspect', 'place']},
        ],
        'dataset': {'source_scenario': '模型编造的来源', 'task_type': 'interaction',
                    'variant_id': 'untrusted-model-id', 'variation': '使用' + item + '完成不同动作',
                    'task_label': item + '整理', 'objects': [item], 'annotation_scope': 'observed'},
    }


def completed(payload=None, *, code=0, text=None):
    raw = {'id': 'resp-fixture', 'model': 'planner-model-fixture',
           'content': text if text is not None else json.dumps(payload or {'variants': [variant(0), variant(1)]}, ensure_ascii=False),
           'usage': {'prompt_tokens': 123, 'completion_tokens': 456, 'total_tokens': 579}}
    return subprocess.CompletedProcess(['arkcli'], code, json.dumps(raw, ensure_ascii=False), '')


def timed_variants(durations):
    result = []
    for index in range(2):
        spec = variant(index)
        source = spec['segments'][0]
        start = source['expected_start_state']
        segments = []
        for position, seconds in enumerate(durations):
            end = source['expected_end_state'] + str(position + 1)
            segments.append(dict(source, id='s%02d' % (position + 1), duration=seconds,
                                 prompt='0–%d秒：%s；步骤%d。' % (seconds, source['prompt'], position + 1),
                                 expected_start_state=start, expected_end_state=end))
            start = end
        spec.update(target_duration_seconds=sum(durations), segments=segments)
        result.append(spec)
    return {'variants': result}


class ScenarioTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(planner, 'studio_scenarios planner must be implemented')
        for name, value in {'PLANNER_MODEL': 'ep-planner-fixture',
                            'PLANNER_PROFILE': 'planner-profile-fixture'}.items():
            configured = patch.object(planner, name, value)
            configured.start()
            self.addCleanup(configured.stop)

    def test_missing_planner_model_fails_before_cli(self):
        with patch.object(planner, 'PLANNER_MODEL', ''), \
                patch.object(planner, '_run_owned_process') as caller:
            with self.assertRaisesRegex(planner.ScenarioError, 'planner_model'):
                planner.expand_scenario(config())
        caller.assert_not_called()

    def test_unconfigured_planner_still_allows_local_simulation(self):
        with patch.object(planner, 'PLANNER_MODEL', ''), \
                patch.object(planner, '_run_owned_process') as caller:
            result = planner.expand_scenario(config(), simulation=True)
        caller.assert_not_called()
        self.assertTrue(result['planner']['simulation'])

    def test_real_call_returns_validated_specs_with_application_owned_provenance(self):
        calls = []
        def caller(argv, **kwargs):
            calls.append((argv, kwargs))
            schema_path = Path(argv[argv.index('--text-schema') + 1])
            self.assertTrue(schema_path.is_file())
            schema = json.loads(schema_path.read_text())
            self.assertEqual(schema['properties']['variants']['minItems'], 2)
            self.assertEqual(schema['properties']['variants']['maxItems'], 2)
            return completed()
        request = config()
        original = copy.deepcopy(request)
        result = planner.expand_scenario(request, caller=caller)
        self.assertEqual(request, original)
        self.assertEqual(len(calls), 1)
        argv, kwargs = calls[0]
        self.assertEqual(argv[:2], ['arkcli', '+chat'])
        self.assertEqual(argv[argv.index('--model') + 1], 'ep-planner-fixture')
        self.assertEqual(argv[argv.index('--profile') + 1], 'planner-profile-fixture')
        for flag in ('--text-strict', '--no-progress'):
            self.assertIn(flag, argv)
        self.assertIs(kwargs['shell'], False)
        self.assertEqual(kwargs['env']['ARKCLI_SKILL_NAME'], 'arkcli-chat')
        self.assertFalse(Path(argv[argv.index('--text-schema') + 1]).exists())
        self.assertFalse(result['planner']['simulation'])
        self.assertEqual(result['planner']['response_id'], 'resp-fixture')
        self.assertEqual(result['planner']['usage']['total_tokens'], 579)
        for index, spec in enumerate(result['variants']):
            self.assertEqual(spec['dataset']['source_scenario'], request['description'])
            self.assertEqual(spec['dataset']['task_type'], 'manipulation')
            self.assertEqual(spec['dataset']['variant_id'], 'variant-%02d' % (index + 1))
            self.assertEqual(spec['dataset']['annotation_scope'], 'planned')
            self.assertEqual(set(spec['dataset']), {'source_scenario', 'task_type', 'variant_id',
                                                   'variation', 'task_label', 'objects', 'annotation_scope'})

    def test_simulation_never_calls_model_and_is_deterministic_and_explicit(self):
        with patch('studio_scenarios.subprocess.Popen', side_effect=AssertionError('network forbidden')):
            for task_type in ('manipulation', 'interaction'):
                for duration in (60, 90, 120, 180):
                    request = config()
                    request.update(task_type=task_type, duration_seconds=duration, quantity=6)
                    first = planner.expand_scenario(request, simulation=True)
                    self.assertEqual(first, planner.expand_scenario(request, simulation=True))
                    self.assertTrue(first['planner']['simulation'])
                    self.assertEqual(len(first['variants']), 6)
                    action_chains = set()
                    for spec in first['variants']:
                        self.assertIn('模拟', spec['name'])
                        self.assertIn(request['description'], spec['global_prompt'])
                        self.assertEqual(len(spec['segments']), duration // 30)
                        action_chains.add(tuple(action for segment in spec['segments'] for action in segment['required_actions']))
                        for index, segment in enumerate(spec['segments']):
                            self.assertEqual(segment['duration'], 30)
                            if index:
                                self.assertEqual(segment['expected_start_state'], spec['segments'][index - 1]['expected_end_state'])
                    self.assertEqual(len(action_chains), 6)

    def test_seedance20_schema_instructions_and_result_use_selected_segment_duration(self):
        observed = {}
        def caller(argv, **kwargs):
            observed['schema'] = json.loads(Path(argv[argv.index('--text-schema') + 1]).read_text())
            observed['instructions'] = argv[argv.index('--instructions') + 1]
            observed['prompt'] = argv[-1]
            observed['model'] = argv[argv.index('--model') + 1]
            return completed(timed_variants([15] * 4))
        result = planner.expand_scenario(config(model_family='seedance-2.0', resolution='720p',
                                                segment_duration_seconds=15), caller=caller)
        segments = observed['schema']['properties']['variants']['items']['properties']['segments']
        self.assertEqual((segments['minItems'], segments['maxItems']), (4, 4))
        self.assertEqual(segments['items']['properties']['duration']['enum'], [15])
        self.assertIn('0–5秒', observed['instructions'])
        self.assertNotIn('每段固定30秒', observed['instructions'])
        self.assertIn('seedance-2.0', observed['prompt'])
        self.assertEqual(observed['model'], planner.PLANNER_MODEL)
        self.assertEqual([row['duration'] for row in result['variants'][0]['segments']], [15] * 4)
        self.assertEqual(result['planner']['video_model_family'], 'seedance-2.0')
        self.assertEqual(result['planner']['segment_durations'], [15] * 4)

    def test_short_tail_is_rebalanced_and_each_segment_must_match_its_schedule(self):
        request = config(model_family='seedance-2.5', resolution='720p', segment_duration_seconds=29)
        expected = [29, 27, 4]
        observed = {}
        def caller(argv, **kwargs):
            observed['schema'] = json.loads(Path(argv[argv.index('--text-schema') + 1]).read_text())
            observed['instructions'] = argv[argv.index('--instructions') + 1]
            return completed(timed_variants(expected))
        result = planner.expand_scenario(request, caller=caller)
        self.assertEqual([row['duration'] for row in result['variants'][0]['segments']], expected)
        schema = observed['schema']['properties']['variants']['items']['properties']['segments']
        self.assertEqual(schema['items']['properties']['duration']['enum'], [4, 27, 29])
        self.assertIn('第3段：4秒', observed['instructions'])
        with self.assertRaises(planner.ScenarioError):
            planner.expand_scenario(request, caller=lambda *a, **k: completed(timed_variants([27, 29, 4])))

    def test_incompatible_video_model_options_fail_before_any_planning_call(self):
        for changes in ({'model_family': 'seedance-2.0', 'segment_duration_seconds': 30},
                        {'model_family': 'seedance-2.5', 'resolution': '4k'},
                        {'model_family': 'not-a-model'},
                        {'model_family': 'seedance-2.0', 'segment_duration_seconds': 4,
                         'duration_seconds': 180}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                planner.expand_scenario(config(**changes),
                                        caller=lambda *a, **k: self.fail('invalid video contract reached planner'))

    def test_seedance20_cannot_accept_a_30_second_model_response(self):
        with self.assertRaises(planner.ScenarioError):
            planner.expand_scenario(config(model_family='seedance-2.0'),
                                    caller=lambda *a, **k: completed())

    def test_simulation_handles_both_models_and_up_to_twenty_continuous_segments(self):
        with patch('studio_scenarios.subprocess.Popen', side_effect=AssertionError('network forbidden')):
            for task_type in ('manipulation', 'interaction'):
                for family, seconds in (('seedance-2.0', 15), ('seedance-2.5', 30)):
                    for total in (60, 90, 120, 180):
                        request = config(model_family=family, task_type=task_type,
                                         duration_seconds=total, quantity=6)
                        result = planner.expand_scenario(request, simulation=True)
                        self.assertEqual(result, planner.expand_scenario(request, simulation=True))
                        for spec in result['variants']:
                            self.assertEqual([s['duration'] for s in spec['segments']], [seconds] * (total // seconds))
                            self._assert_continuous_simulation(spec)
                result = planner.expand_scenario(config(model_family='seedance-2.0', task_type=task_type,
                                                         duration_seconds=180, segment_duration_seconds=9,
                                                         quantity=6), simulation=True)
                for spec in result['variants']:
                    self.assertEqual(len(spec['segments']), 20)
                    self.assertEqual(sum(s['duration'] for s in spec['segments']), 180)
                    self._assert_continuous_simulation(spec)

    def _assert_continuous_simulation(self, spec):
        for index, segment in enumerate(spec['segments']):
            self.assertTrue(segment['prompt'])
            self.assertTrue(segment['required_actions'])
            if index:
                self.assertEqual(segment['expected_start_state'], spec['segments'][index - 1]['expected_end_state'])
            if set(segment['action_labels']) <= {'inspect_state', 'hold_state'}:
                self.assertEqual(segment['expected_start_state'], segment['expected_end_state'])

    def test_invalid_requests_fail_before_caller(self):
        invalid = [('description', '太短'), ('description', 'x' * 4001), ('description', None),
                   ('task_type', 'other'), ('duration_seconds', 61), ('duration_seconds', True),
                   ('quantity', 0), ('quantity', 7), ('quantity', True), ('quantity', 2.0),
                   ('diversity', 'x' * 2001), ('diversity', []), ('description', 'valid\x00text')]
        for field, value in invalid:
            request = config()
            request[field] = value
            with self.subTest(field=field, value=str(value)[:30]):
                with self.assertRaises(ValueError) as error:
                    planner.expand_scenario(request, caller=lambda *a, **k: self.fail('must validate before call'))
                self.assertNotIsInstance(error.exception, planner.ScenarioError)

    def test_malformed_or_incomplete_content_is_rejected(self):
        for content in ('', '```json\n{}\n```', '{"variants":[],"variants":[]}',
                        '{"variants":NaN}', '{"variants":Infinity}', '{"variants":[]} trailing', '[]'):
            with self.subTest(content=content), self.assertRaises(planner.ScenarioError):
                planner.expand_scenario(config(), caller=lambda *a, **k: completed(text=content))

    def test_invalid_specs_are_rejected(self):
        mutations = [
            lambda p: p['variants'].pop(),
            lambda p: p['variants'][0]['segments'].pop(),
            lambda p: p['variants'][0]['segments'][0].update(duration=True),
            lambda p: p['variants'][0]['segments'][0].update(duration=29),
            lambda p: p['variants'][0]['segments'][1].update(id='s01'),
            lambda p: p['variants'][0]['segments'][1].update(expected_start_state='unconnected'),
            lambda p: p['variants'][0]['segments'][0].update(required_actions=[]),
            lambda p: p['variants'][0]['segments'][0].update(prompt='x' * 8001),
            lambda p: p['variants'][0]['segments'][0].update(action_labels=['']),
            lambda p: p['variants'][0]['dataset'].update(variation={'location': 'table'}),
            lambda p: p['variants'][0]['dataset'].update(objects='box'),
            lambda p: p['variants'][0]['dataset'].update(unexpected='field'),
            lambda p: p['variants'][0].update(target_duration_seconds=90),
            lambda p: p['variants'][0].update(global_prompt=''),
        ]
        for change in mutations:
            payload = {'variants': [variant(0), variant(1)]}
            change(payload)
            with self.subTest(mutation=mutations.index(change)), self.assertRaises(planner.ScenarioError):
                planner.expand_scenario(config(), caller=lambda *a, **k: completed(payload))

    def test_variants_with_only_cosmetic_changes_are_rejected(self):
        payload = {'variants': [variant(0), variant(0)]}
        payload['variants'][1]['name'] = 'a different title'
        payload['variants'][1]['global_prompt'] += ' 换成下午。'
        payload['variants'][1]['dataset']['variation'] = '下午拍摄'
        with self.assertRaises(planner.ScenarioError):
            planner.expand_scenario(config(), caller=lambda *a, **k: completed(payload))

    def test_response_id_does_not_turn_nonzero_exit_into_success(self):
        calls = []
        def caller(*args, **kwargs):
            calls.append(1)
            return completed(code=1)
        with self.assertRaises(planner.ScenarioError):
            planner.expand_scenario(config(), caller=caller)
        self.assertEqual(len(calls), 1)

    def test_timeout_is_safe_not_retried_and_has_no_simulation_fallback(self):
        calls = []
        def caller(argv, **kwargs):
            calls.append(1)
            raise subprocess.TimeoutExpired(argv + ['Bearer SECRET'], 120, stderr='api_key=SECRET')
        with self.assertRaises(planner.ScenarioError) as error:
            planner.expand_scenario(config(), caller=caller)
        self.assertNotIn('SECRET', str(error.exception))
        self.assertIn('超时', str(error.exception))
        self.assertEqual(len(calls), 1)

    def test_only_safe_usage_and_response_id_are_returned(self):
        response = completed()
        raw = json.loads(response.stdout)
        raw.update(id='Bearer SECRET', unexpected='SECRET')
        raw['usage'].update(api_key='SECRET', nested={'token': 'SECRET'}, input_tokens=float('nan'))
        response.stdout = json.dumps(raw)
        with self.assertRaises(planner.ScenarioError):
            planner.expand_scenario(config(), caller=lambda *a, **k: response)
        raw['usage'].pop('input_tokens')
        response.stdout = json.dumps(raw)
        result = planner.expand_scenario(config(), caller=lambda *a, **k: response)
        self.assertNotIn('SECRET', json.dumps(result))

    def test_overflowing_json_number_is_rejected(self):
        response = completed()
        response.stdout = response.stdout.replace('579', '1e400')
        with self.assertRaises(planner.ScenarioError):
            planner.expand_scenario(config(), caller=lambda *a, **k: response)

    def test_cli_response_must_be_flat_complete_and_single_json(self):
        for alteration in (
            lambda raw: raw.update(status='incomplete'),
            lambda raw: raw.update(error={'message': 'Bearer SECRET'}),
            lambda raw: raw.pop('content'),
        ):
            raw = json.loads(completed().stdout)
            alteration(raw)
            response = subprocess.CompletedProcess(['arkcli'], 0, json.dumps(raw), 'Bearer SECRET')
            with self.assertRaises(planner.ScenarioError) as error:
                planner.expand_scenario(config(), caller=lambda *a, **k: response)
            self.assertNotIn('SECRET', str(error.exception))
        for text in ('{"content":"{}","content":"{}"}', completed().stdout + '\n{}'):
            response = subprocess.CompletedProcess(['arkcli'], 0, text, '')
            with self.assertRaises(planner.ScenarioError):
                planner.expand_scenario(config(), caller=lambda *a, **k: response)

    def test_missing_cli_and_invalid_mode_do_not_fall_back_to_simulation(self):
        def absent(*args, **kwargs):
            raise FileNotFoundError('Bearer SECRET')
        with self.assertRaises(planner.ScenarioError) as error:
            planner.expand_scenario(config(), caller=absent)
        self.assertNotIn('SECRET', str(error.exception))
        with self.assertRaises(ValueError) as error:
            planner.expand_scenario(config(), simulation='false', caller=absent)
        self.assertNotIsInstance(error.exception, planner.ScenarioError)

    @unittest.skipUnless(os.name == 'posix' and shutil.which('node'), 'requires POSIX process groups and Node')
    def test_real_timeout_stops_native_child_of_node_wrapper(self):
        self._check_process_tree_timeout(leader_exits=False)

    @unittest.skipUnless(os.name == 'posix' and shutil.which('node'), 'requires POSIX process groups and Node')
    def test_real_timeout_stops_child_after_wrapper_has_already_exited(self):
        self._check_process_tree_timeout(leader_exits=True)

    def test_owned_runner_preserves_successful_output_and_exit_code(self):
        result = planner._run_owned_process(
            [sys.executable, '-c', 'import sys; print("已完成"); print("diagnostic", file=sys.stderr)'],
            timeout=10, capture_output=True, text=True, encoding='utf-8', shell=False, check=False)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), '已完成')
        self.assertEqual(result.stderr.strip(), 'diagnostic')

    def _check_process_tree_timeout(self, *, leader_exits):
        with tempfile.TemporaryDirectory(prefix='planner-process-test-') as folder:
            root = Path(folder)
            leader_file, child_file = root / 'leader.pid', root / 'child.pid'
            child = root / 'native.js'
            child.write_text(
                "process.on('SIGTERM', () => {});\n"
                "require('fs').writeFileSync(" + json.dumps(str(child_file)) + ", String(process.pid));\n"
                "setInterval(() => {}, 1000);\n")
            wrapper = root / 'arkcli'
            invoke = ("require('child_process').spawn(process.execPath, [" + json.dumps(str(child)) +
                      "], {stdio: 'inherit'}); process.exit(0);" if leader_exits else
                      "require('child_process').execFileSync(process.execPath, [" + json.dumps(str(child)) +
                      "], {stdio: 'inherit'});")
            wrapper.write_text("#!/usr/bin/env node\nrequire('fs').writeFileSync(" +
                               json.dumps(str(leader_file)) + ", String(process.pid));\n" + invoke + '\n')
            wrapper.chmod(0o700)
            real_popen = subprocess.Popen

            def ready_process(*args, **kwargs):
                # Return the real Popen only once the actual child has installed
                # its TERM handler. The tested communicate timeout starts after
                # this handshake, independent of Node startup speed or host load.
                process = real_popen(*args, **kwargs)
                deadline = time.monotonic() + 10
                while not child_file.is_file():
                    if time.monotonic() >= deadline:
                        process.kill()
                        process.wait(timeout=2)
                        self.fail('fixture child did not report readiness')
                    time.sleep(0.01)
                return process

            try:
                with patch.dict(os.environ, {'PATH': str(root) + os.pathsep + os.environ.get('PATH', '')}), \
                        patch.object(planner.subprocess, 'Popen', side_effect=ready_process), \
                        patch.object(planner, 'TIMEOUT_SECONDS', 0.05), \
                        patch.object(planner, 'TERMINATE_GRACE_SECONDS', 0.2):
                    with self.assertRaises(planner.ScenarioError) as error:
                        planner.expand_scenario(config())
                self.assertIn('超时', str(error.exception))
                self.assertTrue(child_file.is_file(), 'fixture child must start before timeout')
                for path in (leader_file, child_file):
                    pid = int(path.read_text())
                    result = subprocess.run(['ps', '-p', str(pid), '-o', 'stat='],
                                            capture_output=True, text=True, check=False)
                    self.assertTrue(not result.stdout.strip() or result.stdout.strip().startswith('Z'),
                                     'planner returned while fixture PID %s still runs (%s)' % (pid, result.stdout.strip()))
            finally:
                # A failed regression must not leave its deliberately stubborn fixture alive.
                for path in (child_file, leader_file):
                    if path.is_file():
                        try:
                            os.kill(int(path.read_text()), signal.SIGKILL)
                        except ProcessLookupError:
                            pass


if __name__ == '__main__':
    unittest.main()
