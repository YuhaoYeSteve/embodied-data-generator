import importlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import wave
from unittest.mock import patch

from longform_pipeline import Experiment


PROFILE = 'video-profile-fixture'
MODEL = 'review-model-fixture'
VISUAL = {
    'visual_continuity': 'pass',
    'action_integrity': 'pass',
    'actual_observed_end_state': '客人双手空闲，手机在包内，镜头朝向客人。',
    'evidence': '手机取出、展示、放回连续可见。',
}
AUDIO = {'audio_consistency': 'pass', 'evidence': '对白清晰，句尾完整，角色声线稳定。'}


def response(payload, *, audio_tokens=0, input_tokens=1000, output_tokens=200):
    return {
        'id': 'resp_offline_test', 'status': 'completed', 'model': MODEL,
        'output': [{'type': 'message', 'content': [
            {'type': 'output_text', 'text': json.dumps(payload, ensure_ascii=False)}]}],
        'usage': {'input_tokens': input_tokens, 'output_tokens': output_tokens,
                  'input_tokens_details': {'cached_tokens': 0, 'audio_tokens': audio_tokens}},
    }


class StudioReviewTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('studio_review'),
                             'studio_review must implement guarded segment review')
        self.sr = importlib.import_module('studio_review')
        for name, value in {
                'DEFAULT_MODEL': 'ep-review-fixture',
                '_REVIEW_PRICES': {MODEL: {'input': 2, 'audio': 5, 'output': 3,
                                          'max_input_tokens': 16384}}}.items():
            configured = patch.object(self.sr, name, value, create=True)
            configured.start()
            self.addCleanup(configured.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        spec = {'name': 'test', 'target_duration_seconds': 60, 'global_prompt': '固定人物，无配乐。',
                'segments': [dict(duration=30, prompt='手机取出、展示、放回。',
                                  required_actions=['手机取出', '展示', '放回']) for _ in range(2)]}
        self.exp = Experiment.create(Path(self.temp.name).resolve(), spec, PROFILE, 'ep-generation')
        for index in range(2):
            directory = 'segment_%02d' % (index + 1)
            folder = self.exp.folder / directory
            folder.mkdir()
            (folder / 'original.mov').write_bytes(b'offline video fixture')
            self.exp.data['segments'].append({
                'index': index, 'directory': directory, 'status': 'succeeded',
                'result': {'output_url': 'https://example.invalid/%s.mov?signature=PRIVATE' % index},
                'media': {'video_duration': 30.0},
                'review': {'decision': 'accept', 'observed_end_state': '上一段已核实的末状态'},
            })
        self.exp.save()
        self.calls = []
        self.outputs = [response(VISUAL), response(AUDIO, audio_tokens=200)]

        def run(video_urls, prompt, output_path, audio_paths=None, **kwargs):
            self.calls.append({'video_urls': video_urls, 'prompt': prompt,
                               'output_path': Path(output_path), 'audio_paths': list(audio_paths or []),
                               'kwargs': kwargs})
            raw = self.outputs.pop(0)
            if isinstance(raw, Exception):
                raise raw
            Path(output_path).write_text(json.dumps({'response': raw}), encoding='utf-8')
            return raw

        def extract(source, output):
            Path(output).write_bytes(b'offline audio fixture')
            return Path(output)

        def contact(source, output):
            Path(output).write_bytes(b'offline contact fixture')
            return Path(output)

        self.run_patch = patch.object(self.sr, 'run_review', side_effect=run)
        self.extract_patch = patch.object(self.sr, 'extract_audio', side_effect=extract)
        self.contact_patch = patch.object(self.sr, 'contact', side_effect=contact)
        self.run_mock = self.run_patch.start()
        self.extract_mock = self.extract_patch.start()
        self.contact_mock = self.contact_patch.start()
        for patched in (self.run_patch, self.extract_patch, self.contact_patch):
            self.addCleanup(patched.stop)

    def test_all_quality_checks_pass_and_observed_state_is_saved(self):
        self.exp.data['segments'] = self.exp.data['segments'][:1]
        self.exp.save()
        result = self.sr.review_segment(self.exp, 0)
        self.assertEqual(result['decision'], 'accept')
        for field in ('visual_continuity', 'action_integrity', 'audio_consistency'):
            self.assertEqual(result[field], 'pass')
        self.assertEqual(result['observed_end_state'], VISUAL['actual_observed_end_state'])
        self.assertTrue(result['automatic'])
        self.assertFalse(result['requires_manual_review'])
        self.assertTrue(result['evidence'])
        self.assertEqual(json.loads((self.exp.folder / 'segment_01' / 'studio_review.json').read_text()), result)
        self.exp.review(0, result)

    def test_visual_and_audio_inputs_are_separate_and_only_current_video_is_sent(self):
        result = self.sr.review_segment(self.exp, 1)
        self.assertEqual(result['decision'], 'accept')
        self.assertEqual(len(self.calls), 2)
        visual, audio = self.calls
        self.assertEqual(visual['video_urls'], [self.exp.record(1)['result']['output_url']])
        self.assertEqual(visual['audio_paths'], [])
        self.assertEqual(audio['video_urls'], [])
        self.assertEqual(audio['audio_paths'], [self.exp.folder / 'segment_01' / 'review_audio.wav',
                                               self.exp.folder / 'segment_02' / 'review_audio.wav'])
        self.assertIn('独立', audio['prompt'])
        self.assertIn('上一段', audio['prompt'])
        self.assertIn('当前段', audio['prompt'])
        self.assertIn('上一段已核实的末状态', visual['prompt'])
        self.assertIn('手机取出', visual['prompt'])
        self.assertNotIn('signature=', visual['prompt'])
        for call in self.calls:
            self.assertEqual(call['output_path'].parent.parent, self.exp.folder / 'segment_02' / 'automatic_reviews')
            self.assertEqual(call['kwargs']['model'], self.sr.DEFAULT_MODEL)

    def test_opening_audio_has_exactly_one_audio_clip(self):
        self.sr.review_segment(self.exp, 0)
        self.assertEqual(self.calls[1]['audio_paths'], [self.exp.folder / 'segment_01' / 'review_audio.wav'])

    def test_contact_sheet_is_local_manual_review_artifact(self):
        result = self.sr.review_segment(self.exp, 0)
        self.assertEqual(result['contact_sheet'], 'review_contact.jpg')
        self.assertTrue((self.exp.folder / 'segment_01' / result['contact_sheet']).is_file())

    def test_json_fence_is_accepted_without_prose(self):
        self.outputs[0]['output'][0]['content'][0]['text'] = '```json\n' + json.dumps(VISUAL) + '\n```'
        self.assertEqual(self.sr.review_segment(self.exp, 0)['decision'], 'accept')

    def test_complete_json_is_required_and_no_substring_is_salvaged(self):
        for text in ('先给结论：' + json.dumps(VISUAL), json.dumps(VISUAL) + ' trailing',
                     '{"visual_continuity":"pass"', '[]', 'null',
                     json.dumps(VISUAL) + '\n' + json.dumps(VISUAL)):
            with self.subTest(text=text[:30]):
                self.outputs = [response(VISUAL), response(AUDIO)]
                self.outputs[0]['output'][0]['content'][0]['text'] = text
                result = self.sr.review_segment(self.exp, 0)
                self.assertEqual(result['decision'], 'reject')
                self.assertEqual(result['visual_continuity'], 'unverified')
                self.assertTrue(result['automatic'])
                self.assertFalse(result['requires_manual_review'])
                self.assertTrue(result['summary'])
                self.assertTrue(result['observed_end_state'])

    def test_unknown_missing_or_invalid_visual_fields_cannot_accept(self):
        variants = []
        for key in VISUAL:
            value = dict(VISUAL)
            del value[key]
            variants.append(value)
        variants.extend([dict(VISUAL, surprise='pass'), dict(VISUAL, action_integrity=True),
                         dict(VISUAL, actual_observed_end_state='  '), dict(VISUAL, evidence=[]),
                         dict(VISUAL, visual_continuity='PASS')])
        for payload in variants:
            with self.subTest(payload=payload):
                self.outputs = [response(payload), response(AUDIO)]
                result = self.sr.review_segment(self.exp, 0)
                self.assertEqual(result['decision'], 'reject')
                self.assertEqual(result['action_integrity'], 'unverified')

    def test_duplicate_json_fields_are_rejected(self):
        text = json.dumps(VISUAL)[:-1] + ', "visual_continuity":"fail"}'
        self.outputs[0]['output'][0]['content'][0]['text'] = text
        result = self.sr.review_segment(self.exp, 0)
        self.assertEqual(result['visual_continuity'], 'unverified')
        self.assertEqual(result['decision'], 'reject')

    def test_audio_unknown_fields_fail_closed(self):
        self.outputs[1] = response(dict(AUDIO, visual_continuity='pass'))
        result = self.sr.review_segment(self.exp, 0)
        self.assertEqual(result['audio_consistency'], 'unverified')
        self.assertEqual(result['decision'], 'reject')

    def test_each_failed_or_unverified_dimension_requests_the_correct_automatic_action(self):
        for field in ('visual_continuity', 'action_integrity', 'audio_consistency'):
            for status in ('fail', 'unverified'):
                with self.subTest(field=field, status=status):
                    visual, audio = dict(VISUAL), dict(AUDIO)
                    (audio if field == 'audio_consistency' else visual)[field] = status
                    self.outputs = [response(visual), response(audio)]
                    result = self.sr.review_segment(self.exp, 0)
                    self.assertEqual(result['decision'], 'reject')
                    self.assertEqual(result[field], status)
                    self.assertFalse(result['requires_manual_review'])
                    self.assertEqual(result['retry_kind'], 'generation' if status == 'fail' else 'review')

    def test_generation_profile_can_differ_from_configured_review_profile(self):
        self.exp.data['profile'] = 'another-video-profile'
        result = self.sr.review_segment(self.exp, 0)
        self.assertEqual(self.run_mock.call_count, 2)
        self.assertEqual(result['decision'], 'accept')
        for call in self.calls:
            self.assertEqual(call['kwargs']['model'], 'ep-review-fixture')

    def test_missing_review_model_makes_no_model_call(self):
        with patch.object(self.sr, 'DEFAULT_MODEL', ''):
            result = self.sr.review_segment(self.exp, 0)
        self.run_mock.assert_not_called()
        self.contact_mock.assert_not_called()
        self.extract_mock.assert_not_called()
        self.assertEqual(result['review_cost_cny'], 0.0)
        self.assertEqual(result['decision'], 'reject')
        self.assertIn('review_model', result['evidence'])
        self.assertTrue(all(result[field] == 'unverified' for field in
                            ('visual_continuity', 'action_integrity', 'audio_consistency')))

    def test_missing_media_makes_no_model_call(self):
        (self.exp.folder / 'segment_01' / 'original.mov').unlink()
        result = self.sr.review_segment(self.exp, 0)
        self.run_mock.assert_not_called()
        self.assertEqual(result['decision'], 'reject')
        self.assertEqual(result['review_cost_cny'], 0.0)

    def test_audio_extraction_failure_does_not_invent_audio_pass(self):
        self.extract_mock.side_effect = subprocess.CalledProcessError(1, ['ffmpeg'])
        self.outputs = [response(VISUAL)]
        result = self.sr.review_segment(self.exp, 0)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(result['audio_consistency'], 'unverified')
        self.assertEqual(result['decision'], 'reject')

    def test_model_failure_preserves_reservation_and_hides_exception_details(self):
        self.outputs[0] = RuntimeError('api_key=SECRET https://example.invalid/a?secret=yes')
        result = self.sr.review_segment(self.exp, 0)
        self.assertEqual(result['visual_continuity'], 'unverified')
        self.assertEqual(result['decision'], 'reject')
        self.assertIsNone(result['review_cost_cny'])
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertNotIn('secret=yes', json.dumps(result))

    def test_returned_text_contains_no_urls_credentials_or_inline_media(self):
        self.outputs[0] = response(dict(VISUAL, evidence=
            '证据 https://example.invalid/a?signature=SECRET data:audio/wav;base64,U0VDUkVU '
            'Bearer sk-private-example-key api_key=another-private-key'))
        result = self.sr.review_segment(self.exp, 0)
        text = json.dumps(result)
        for secret in ('https://', 'signature=', 'base64,', 'U0VDUkVU', 'sk-private-example-key',
                       'another-private-key'):
            self.assertNotIn(secret, text)

    def test_usage_estimate_counts_audio_separately_and_exports_reserve(self):
        result = self.sr.review_segment(self.exp, 0)
        expected = (1000 * 2 + 200 * 3 + 800 * 2 + 200 * 5 + 200 * 3) / 1_000_000
        self.assertAlmostEqual(result['review_cost_cny'], expected)
        self.assertEqual(self.sr.REVIEW_RESERVE_CNY, 1.0)

    def test_unconfigured_review_prices_stay_unknown(self):
        with patch.object(self.sr, '_REVIEW_PRICES', {}):
            result = self.sr.review_segment(self.exp, 0)
        self.assertEqual(result['decision'], 'accept')
        self.assertIsNone(result['review_cost_cny'])

    def test_review_price_input_tier_is_configured(self):
        raw = response(VISUAL, input_tokens=16385)
        self.assertIsNone(self.sr._usage_cost(raw))

    def test_unknown_usage_or_model_does_not_release_reservation(self):
        variants = []
        raw = response(VISUAL); raw.pop('usage'); variants.append(raw)
        raw = response(VISUAL); raw['model'] = 'unpriced-model'; variants.append(raw)
        raw = response(VISUAL); raw['usage']['input_tokens_details']['cached_tokens'] = 10; variants.append(raw)
        raw = response(VISUAL); raw['usage']['input_tokens'] = True; variants.append(raw)
        raw = response(VISUAL); raw['usage']['input_tokens_details']['audio_tokens'] = 2000; variants.append(raw)
        for raw in variants:
            with self.subTest(raw=raw):
                self.outputs = [raw, response(AUDIO)]
                self.assertIsNone(self.sr.review_segment(self.exp, 0)['review_cost_cny'])

    def test_incomplete_response_cannot_be_accepted(self):
        self.outputs[0]['status'] = 'incomplete'
        result = self.sr.review_segment(self.exp, 0)
        self.assertEqual(result['decision'], 'reject')
        self.assertEqual(result['visual_continuity'], 'unverified')

    def test_repeated_checks_preserve_previous_raw_evidence_without_requesting_manual_review(self):
        self.outputs = [response(dict(VISUAL, action_integrity='fail')), response(AUDIO)]
        first = self.sr.review_segment(self.exp, 0)
        self.assertFalse(first['requires_manual_review'])
        self.assertEqual(first['retry_kind'], 'generation')
        first_visual = self.calls[0]['output_path'].read_bytes()
        self.outputs = [response(VISUAL), response(AUDIO)]
        second = self.sr.review_segment(self.exp, 0)
        self.assertEqual(second['decision'], 'accept')
        self.assertEqual(self.calls[0]['output_path'].read_bytes(), first_visual)
        self.assertNotEqual(self.calls[0]['output_path'], self.calls[2]['output_path'])
        self.assertTrue((self.calls[0]['output_path'].parent / 'studio_review.json').is_file())

    def test_unverified_check_requests_automatic_recheck_not_manual_acceptance(self):
        self.outputs = [response(VISUAL), response(dict(AUDIO, audio_consistency='unverified'))]
        result = self.sr.review_segment(self.exp, 0)
        self.assertFalse(result['requires_manual_review'])
        self.assertEqual(result['retry_kind'], 'review')
        self.assertNotIn('人工', result['summary'])

    def test_audio_pcm_preserves_sample_rate_channels_and_sample_count(self):
        source = self.exp.folder / 'pcm_source.mov'
        samples = b'\x01\x00\x02\x00' * 1600
        with wave.open(str(source), 'wb') as stream:
            stream.setnchannels(2)
            stream.setsampwidth(2)
            stream.setframerate(16000)
            stream.writeframes(samples)
        # Remove only the helper patch, retaining the paid-API patch throughout.
        self.extract_patch.stop()
        output = self.sr.extract_audio(source, self.exp.folder / 'pcm_output.wav')
        with wave.open(str(output), 'rb') as stream:
            self.assertEqual(stream.getnchannels(), 2)
            self.assertEqual(stream.getframerate(), 16000)
            self.assertEqual(stream.getnframes(), 1600)
            self.assertEqual(stream.readframes(1600), samples)


if __name__ == '__main__':
    unittest.main()
