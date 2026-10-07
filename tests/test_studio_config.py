"""Provider configuration tests use synthetic settings, never local accounts."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from studio_config import load_settings


class StudioConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'settings.json'

    def write(self, settings):
        self.path.write_text(json.dumps(settings))
        return load_settings(self.path)

    def test_missing_config_has_no_private_endpoint_or_assumed_account_price(self):
        value = load_settings(self.path)
        self.assertEqual(value['video_profile'], 'default')
        self.assertEqual(value['planner_model'], '')
        self.assertEqual(value['review_model'], '')
        self.assertTrue(all(model.startswith('doubao-') for model in value['video_models'].values()))
        self.assertEqual(value['video_prices_cny_per_1k_tokens'], {})
        self.assertEqual(value['review_prices_cny_per_1m_tokens'], {})

    def test_explicit_profile_and_endpoint_override_preserves_other_model(self):
        value = self.write({'video_profile': 'fixture-platform', 'review_profile': 'fixture-review',
                            'video_models': {'seedance-2.5': 'ep-fixture-video'}})
        self.assertEqual(value['planner_profile'], 'fixture-platform')
        self.assertEqual(value['review_profile'], 'fixture-review')
        self.assertEqual(value['video_models']['seedance-2.5'], 'ep-fixture-video')
        self.assertEqual(value['video_models']['seedance-2.0'], 'doubao-seedance-2-0-260128')

    def test_price_units_and_supported_combinations_are_validated(self):
        good = {'video_prices_cny_per_1k_tokens': {'seedance-2.5': {'720p': {'nv2v': .1, 'v2v': .05}}},
                'review_prices_cny_per_1m_tokens': {'review-fixture': {'input': 1, 'audio': 10, 'output': 2, 'max_input_tokens': 1000}}}
        self.assertEqual(self.write(good)['video_prices_cny_per_1k_tokens'], good['video_prices_cny_per_1k_tokens'])
        for rates in ({'seedance-2.5': {'4k': {'nv2v': 1}}},
                      {'seedance-2.5': {'720p': {'nv2v': -1}}},
                      {'seedance-2.5': {'720p': {'nv2v': True}}},
                      {'seedance-2.5': {'720p': {'nv2v': float('nan')}}}):
            with self.subTest(rates=rates), self.assertRaises(ValueError):
                self.write({'video_prices_cny_per_1k_tokens': rates})

    def test_secrets_and_invalid_input_are_never_echoed_in_errors(self):
        secret = 'fixture-secret-do-not-echo'
        for content in ('{"api_key":"' + secret + '"}', '{"broken":"' + secret):
            self.path.write_text(content)
            with self.assertRaises(ValueError) as error:
                load_settings(self.path)
            self.assertNotIn(secret, str(error.exception))

    def test_environment_path_override_and_deep_copy(self):
        self.write({'video_profile': 'fixture-override'})
        with patch.dict('os.environ', {'STUDIO_CONFIG_PATH': str(self.path)}):
            first = load_settings()
            first['video_models'].clear()
            self.assertTrue(load_settings()['video_models'])
            self.assertEqual(load_settings()['video_profile'], 'fixture-override')

    def test_duplicate_model_mapping_is_rejected(self):
        with self.assertRaises(ValueError):
            self.write({'video_models': {'seedance-2.0': 'ep-fixture', 'seedance-2.5': 'ep-fixture'}})

    def test_missing_review_configuration_keeps_video_submission_guard_active(self):
        from studio_server import create_app
        with patch('studio_review.DEFAULT_MODEL', ''), patch('studio_backend.StudioManager') as factory:
            factory.return_value.data_dir = Path(self.temp.name)
            create_app(data_dir=self.temp.name)
            self.assertIsNone(factory.call_args.kwargs['reviewer'])
