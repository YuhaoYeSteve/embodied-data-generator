"""Offline media-provider contracts; encoding is replaced to avoid large test videos."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import studio_simulation as simulation


class SimulationTests(unittest.TestCase):
    def test_high_resolution_simulation_uses_requested_dimensions_and_keeps_model(self):
        with tempfile.TemporaryDirectory() as folder:
            provider = simulation.SimulationClient(folder)
            for resolution, expected in (('480p', (864, 480)), ('720p', (1280, 720)),
                                         ('1080p', (1920, 1080)), ('4k', (3840, 2160))):
                body = {'model': 'model-fixture', 'duration': 15, 'resolution': resolution,
                        'content': [{'type': 'text', 'text': 'offline fixture'}]}
                created = provider.submit(body)
                path = Path(folder) / (created['id'] + '.json')
                stored = json.loads(path.read_text())
                self.assertEqual(stored.get('model'), 'model-fixture')
                stored['created_at'] = 0
                path.write_text(json.dumps(stored))
                with patch.object(simulation.Image, 'new') as image, \
                        patch.object(simulation.ImageDraw, 'Draw'), \
                        patch.object(simulation.subprocess, 'run') as encode:
                    result = provider.get(created['id'])
                self.assertEqual(image.call_args.args[1], expected)
                self.assertEqual(result['duration'], 15)
                self.assertEqual(result['resolution'], resolution)
                argv = encode.call_args.args[0]
                self.assertEqual(argv[argv.index('-t') + 1], '15')

    def test_unknown_resolution_fails_before_a_simulation_task_is_recorded(self):
        with tempfile.TemporaryDirectory() as folder:
            provider = simulation.SimulationClient(folder)
            with self.assertRaises(ValueError):
                provider.submit({'model': 'fixture', 'duration': 15, 'resolution': '8k', 'content': []})
            self.assertEqual(list(Path(folder).iterdir()), [])


if __name__ == '__main__':
    unittest.main()
