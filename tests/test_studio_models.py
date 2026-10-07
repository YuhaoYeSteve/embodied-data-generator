import importlib
import importlib.util
import unittest


class ModelCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('studio_models'), '需要共享的模型能力目录')
        self.models = importlib.import_module('studio_models')

    def test_each_model_has_its_own_resolution_and_duration_contract(self):
        older = self.models.get_model('seedance-2.0')
        newer = self.models.get_model('seedance-2.5')
        self.assertIn('4k', older['resolutions'])
        self.assertNotIn('4k', newer['resolutions'])
        self.assertEqual(max(older['durations']), 15)
        self.assertEqual(max(newer['durations']), 30)
        self.assertIn('1080p', newer['resolutions'])

    def test_cross_model_parameters_cannot_be_submitted(self):
        for config in [
            {'model_family':'seedance-2.0','segment_duration_seconds':30},
            {'model_family':'seedance-2.5','resolution':'4k'},
            {'model_family':'unknown'},
            {'model_family':'seedance-2.0','segment_duration_seconds':True},
            {'model_family':'seedance-2.0','model':'doubao-seedance-2-5-260628'},
        ]:
            with self.subTest(config=config), self.assertRaises(ValueError):
                self.models.resolve_selection(config)

    def test_valid_4k_selection_and_copy_safe_catalogue(self):
        selection=self.models.resolve_selection({'model_family':'seedance-2.0','resolution':'4k'})
        self.assertEqual(selection['segment_duration_seconds'],15)
        self.assertEqual(self.models.resolution_dimensions('4k'),(3840,2160))
        entries=self.models.model_catalog()
        entries[0]['resolutions'].clear()
        self.assertTrue(self.models.model_catalog()[0]['resolutions'])

    def test_plan_retains_total_and_borrows_for_short_last_segment(self):
        self.assertEqual(self.models.plan_segment_durations(60,29),[29,27,4])
        self.assertEqual(self.models.plan_segment_durations(120,15,'seedance-2.0'),[15]*8)
        self.assertEqual(self.models.plan_segment_durations(180,30),[30]*6)
        with self.assertRaises(ValueError):
            self.models.plan_segment_durations(180,4)

    def test_plan_handles_all_legal_segment_sizes(self):
        for total in [60,90,120,180]:
            for family in ['seedance-2.0','seedance-2.5']:
                for duration in self.models.get_model(family)['durations']:
                    if (total+duration-1)//duration>20: continue
                    parts=self.models.plan_segment_durations(total,duration,family)
                    self.assertEqual(sum(parts),total)
                    self.assertTrue(all(4<=d<=duration for d in parts))
                    self.assertTrue(2<=len(parts)<=20)

    def test_spec_revalidates_every_segment_and_total(self):
        spec={'target_duration_seconds':60,'segments':[{'duration':30},{'duration':30}]}
        with self.assertRaises(ValueError):
            self.models.validate_spec_model(spec,'seedance-2.0','720p')
        self.models.validate_spec_model(spec,'seedance-2.5','1080p')
        with self.assertRaises(ValueError):
            self.models.validate_spec_model(spec,'seedance-2.5','4k')


if __name__ == '__main__': unittest.main()
