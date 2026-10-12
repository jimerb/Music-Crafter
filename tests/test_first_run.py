"""Portable startup and HTTP checks without any proprietary library files."""
from pathlib import Path
import json
import tempfile
import threading
import unittest
from urllib.request import urlopen
from unittest.mock import patch

from app.server import Handler, MusicCrafter, ThreadingHTTPServer


class FirstRunTests(unittest.TestCase):
    def test_empty_checkout_serves_ui_and_health_without_ffmpeg(self):
        root = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory(prefix='first-run-', dir=root) as directory:
            self.assertTrue(Path(directory).resolve().is_relative_to(root))
            with patch('app.server.capabilities', return_value={'available': False, 'mp3': False, 'tempo': False, 'pitch': False}):
                app = MusicCrafter(Path(directory), background_scan=False)
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            server.app = app
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f'http://127.0.0.1:{server.server_port}'
            try:
                with urlopen(base + '/api/health', timeout=5) as response:
                    health = json.load(response)
                self.assertEqual(health['app'], 'Music Crafter')
                self.assertEqual(health['tracks'], 0)
                self.assertFalse(health['capabilities']['available'])
                with urlopen(base + '/api/library', timeout=5) as response:
                    catalog = json.load(response)
                self.assertEqual(catalog['roots'], [])
                self.assertEqual(catalog['tracks'], [])
                with urlopen(base, timeout=5) as response:
                    self.assertIn(b'Music Crafter', response.read())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                app.pool.shutdown(wait=True)
                app.scan_pool.shutdown(wait=True)

    def test_existing_library_configuration_is_preserved(self):
        root = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory(prefix='saved-config-', dir=root) as directory:
            self.assertTrue(Path(directory).resolve().is_relative_to(root))
            data = Path(directory)
            (data / 'music').mkdir()
            config = data / 'library.json'
            config.write_text(json.dumps({'roots': [str(data / 'music')]}))
            before = config.read_bytes()
            app = MusicCrafter(data, background_scan=False)
            try:
                self.assertEqual(app.library.roots, [str(data / 'music')])
                self.assertEqual(config.read_bytes(), before)
            finally:
                app.pool.shutdown(wait=True)
                app.scan_pool.shutdown(wait=True)
