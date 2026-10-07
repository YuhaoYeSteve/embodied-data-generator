"""Offline checks for approved, locally bundled image references."""
import base64
import importlib.util
import json
import struct
import tempfile
import unittest
from pathlib import Path

if importlib.util.find_spec('studio_references'):
    import studio_references as references
else:
    references = None


ROOT = Path(__file__).resolve().parents[1]
IMAGE_ID = 'hotel_booking_screen'
IMAGE_PATH = Path('refs/reference_board.png')
BINDING = [{'id': IMAGE_ID, 'segment_ids': ['01_greeting_reservation']}]


class ReferenceTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(references, 'approved reference catalogue must be implemented')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.path = self.root / IMAGE_PATH
        self.path.parent.mkdir(parents=True)
        self.payload = b'\x89PNG\r\n\x1a\nOFFLINE_IMAGE'
        self.path.write_bytes(self.payload)

    def test_catalog_contains_preview_metadata_without_asset_bytes_or_local_paths(self):
        item, = references.catalog(self.root)
        self.assertEqual(item, {
            'id': IMAGE_ID, 'name': '物品分区 · 合成布局参考',
            'url': '/api/reference-images/hotel_booking_screen',
            'source_url': '/api/reference-images/hotel_booking_screen', 'purpose': 'object_layout',
            'note': '本项目绘制的虚构布局；仅提供颜色、形状和分区参考',
            'width': 960, 'height': 640})
        encoded = json.dumps(item)
        self.assertNotIn(str(self.root), encoded)
        self.assertNotIn('data:', encoded)
        self.assertNotIn('content', item)
        item['name'] = 'caller mutation'
        self.assertNotEqual(references.catalog(self.root)[0]['name'], item['name'])

    def test_preview_only_resolves_whitelisted_existing_regular_file(self):
        self.assertEqual(references.reference_path(self.root, IMAGE_ID), self.path)
        for invalid in ['unknown', '../hotel_booking_screen', str(self.path),
                        'https://example.invalid/x.png', None, [], {}]:
            with self.subTest(value=invalid), self.assertRaises(FileNotFoundError):
                references.reference_path(self.root, invalid)
        self.path.unlink()
        self.assertEqual(references.catalog(self.root), [])
        with self.assertRaises(FileNotFoundError):
            references.reference_path(self.root, IMAGE_ID)
        self.path.mkdir()
        with self.assertRaises(FileNotFoundError):
            references.reference_path(self.root, IMAGE_ID)

    def test_preview_rejects_symlink_file_and_parent_directory(self):
        other = self.root / 'other.png'
        other.write_bytes(self.payload)
        self.path.unlink()
        self.path.symlink_to(other)
        with self.assertRaises(FileNotFoundError):
            references.reference_path(self.root, IMAGE_ID)
        self.path.unlink()
        self.path.parent.rmdir()
        external = self.root / 'external'
        external.mkdir()
        (external / self.path.name).write_bytes(self.payload)
        self.path.parent.symlink_to(external, target_is_directory=True)
        with self.assertRaises(FileNotFoundError):
            references.reference_path(self.root, IMAGE_ID)

    def test_binding_validates_known_ids_scopes_and_copies_input(self):
        binding = json.loads(json.dumps(BINDING))
        actual = references.validate_reference_images(binding, ['01_greeting_reservation', 'later'], self.root)
        self.assertEqual(actual, BINDING)
        actual[0]['segment_ids'].append('later')
        self.assertEqual(binding, BINDING)
        self.assertEqual(references.validate_reference_images([], ['later'], self.root), [])

    def test_binding_rejects_types_unknowns_duplicates_unbounded_and_extra_fields(self):
        cases = [None, {}, 'hotel_booking_screen', [None], [{}],
                 [{'id': 'unknown', 'segment_ids': ['first']}],
                 [{'id': [], 'segment_ids': ['first']}],
                 [{'id': IMAGE_ID, 'segment_ids': []}],
                 [{'id': IMAGE_ID, 'segment_ids': 'first'}],
                 [{'id': IMAGE_ID, 'segment_ids': ['missing']}],
                 [{'id': IMAGE_ID, 'segment_ids': [True]}],
                 [{'id': IMAGE_ID, 'segment_ids': ['first', 'first']}],
                 [{'id': IMAGE_ID, 'segment_ids': ['first'] * 21}],
                 [{'id': IMAGE_ID, 'segment_ids': ['first'], 'url': 'file:///private'}],
                 [{'id': IMAGE_ID, 'segment_ids': ['first']}] * 2,
                 [{'id': IMAGE_ID, 'segment_ids': ['first']}] * 100]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                references.validate_reference_images(value, ['first'], self.root)
        self.path.unlink()
        with self.assertRaises(ValueError):
            references.validate_reference_images(BINDING, ['01_greeting_reservation'], self.root)

    def test_assets_embed_exact_bytes_with_explicit_role_and_segment_scope(self):
        spec = {'reference_images': BINDING,
                'segments': [{'id': '01_greeting_reservation'}, {'id': 'later'}]}
        asset, = references.build_reference_assets(self.root, spec)
        self.assertEqual(asset['id'], IMAGE_ID)
        self.assertEqual(asset['role'], 'reference_image')
        self.assertEqual(asset['segment_ids'], ['01_greeting_reservation'])
        self.assertEqual(asset['content'], {
            'type': 'image_url', 'role': 'reference_image',
            'image_url': {'url': 'data:image/png;base64,' + base64.b64encode(self.payload).decode()}})
        for term in ['@图片1', '布局', '颜色', '形状', '分区', '首帧']:
            self.assertIn(term, asset['description'])
        self.assertEqual(references.build_reference_assets(self.root, {'segments': []}), [])

    def test_synthetic_reference_is_available_at_expected_dimensions(self):
        path = references.reference_path(ROOT, IMAGE_ID)
        payload = path.read_bytes()
        self.assertEqual(payload[:8], b'\x89PNG\r\n\x1a\n')
        self.assertEqual(struct.unpack('>II', payload[16:24]), (960, 640))


class PublicTemplateTests(unittest.TestCase):
    def test_reference_template_has_explicit_binding_and_preserves_later_segments(self):
        original = json.loads((ROOT / 'scenarios' / 'hotel_120s_v4.json').read_text())
        spec = json.loads((ROOT / 'scenarios' / 'hotel_120s_phone_reference.json').read_text())
        self.assertEqual(spec['reference_images'], BINDING)
        self.assertEqual(spec['global_prompt'], original['global_prompt'])
        self.assertEqual(spec['segments'][1:], original['segments'][1:])
        self.assertEqual([s['duration'] for s in spec['segments']], [30, 30, 30, 30])
        self.assertEqual(spec['target_duration_seconds'], 120)
        first = spec['segments'][0]
        for term in ['@图片1', '布局', '颜色', '分区', '首帧']:
            self.assertIn(term, first['prompt'])
        self.assertTrue(first['expected_end_state'])
        self.assertTrue(first['required_actions'])

    def test_object_sorting_template_is_valid_for_both_model_families(self):
        from studio_models import validate_spec_model
        path = ROOT / 'scenarios' / 'object_sorting_60s.json'
        self.assertTrue(path.is_file(), 'a public generic starter template is required')
        spec = json.loads(path.read_text())
        self.assertEqual([segment['duration'] for segment in spec['segments']], [15] * 4)
        self.assertEqual(spec['target_duration_seconds'], 60)
        self.assertEqual(spec['dataset']['task_type'], 'manipulation')
        self.assertEqual(spec['dataset']['annotation_scope'], 'planned')
        self.assertNotIn('reference_images', spec)
        for family in ['seedance-2.0', 'seedance-2.5']:
            with self.subTest(family=family):
                validate_spec_model(spec, family, '720p')
        for previous, following in zip(spec['segments'], spec['segments'][1:]):
            self.assertEqual(previous['expected_end_state'], following['expected_start_state'])


if __name__ == '__main__':
    unittest.main()
