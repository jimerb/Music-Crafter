"""Exercise real catalog, generation, streaming, exports and durable settings."""
from pathlib import Path
import hashlib
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen
import wave

import numpy as np

from app.codecs import decode_audio
from app.library import PROJECT
from app.server import Handler, MusicCrafter, ThreadingHTTPServer


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (PROJECT / 'analysis/output/source_hashes.json').is_file():
            raise unittest.SkipTest('Optional private library and source-hash fixtures are not installed')
        cls.temp_parent = (PROJECT / 'tests/.tmp').resolve()
        assert cls.temp_parent.is_relative_to(PROJECT.resolve())
        cls.temp_parent.mkdir(exist_ok=True)
        cls.temp = tempfile.TemporaryDirectory(prefix='music-crafter-api-', dir=cls.temp_parent)
        (Path(cls.temp.name) / 'library.json').write_text(json.dumps({'roots': [str(PROJECT / 'Light Jazzy')]}))
        cls.app = MusicCrafter(Path(cls.temp.name), background_scan=False)
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.server.app = cls.app
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = 'http://127.0.0.1:' + str(cls.server.server_address[1])
        cls.sources = json.loads((PROJECT / 'analysis/output/source_hashes.json').read_text())

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.app.pool.shutdown(wait=True)
        cls.app.scan_pool.shutdown(wait=True)
        assert Path(cls.temp.name).resolve().is_relative_to(cls.temp_parent)
        cls.temp.cleanup()
        for path, digest in cls.sources.items():
            assert hashlib.sha256((PROJECT / path).read_bytes()).hexdigest() == digest

    def api(self, path, data=None, method=None):
        req = Request(self.base + path, data=json.dumps(data).encode() if data is not None else None,
                      headers={'Content-Type': 'application/json'}, method=method)
        with urlopen(req) as response:
            return json.load(response)

    def wait_scan(self, job):
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            result = self.api('/api/scans/' + job['id'])
            if result['status'] in ('complete', 'error', 'cancelled'):
                self.assertEqual(result['status'], 'complete', result)
                return result
            time.sleep(.05)
        self.fail('The folder scan did not finish.')

    def test_catalog_and_variation_gaps(self):
        library = self.api('/api/library')
        self.assertEqual(len(library['tracks']), 16)
        self.assertEqual(library['collections'][0]['name'], 'Light & Jazzy')
        self.assertTrue(library['capabilities']['mp3'])
        self.assertEqual(library['warnings'], [])
        variants = self.api('/api/variations?trackId=55%3A2010&duration=30&tempo=82')['variations']
        gap = next(v for v in variants if v['name'] == 'Clear skies')
        self.assertFalse(gap['available'])
        self.assertTrue(gap['reason'])

    def test_length_limits_support_more_than_an_hour(self):
        variants = self.app.variants(dict(trackId=['55:2015'],duration=['3900'],tempo=['92']))
        self.assertTrue(any(v['available'] for v in variants['variations']))
        with self.assertRaisesRegex(ValueError, '2 hours'):
            self.app.generate(dict(trackId='55:2015',duration=7201))
        with self.assertRaises(ValueError):
            self.app.variants(dict(trackId=['55:2015'],duration=['nan']))

    def test_generation_streaming_and_exports(self):
        payload = dict(trackId='55:2015', duration=12.375, variation='auto', tempo=92, pitch=0, loop=False)
        job = self.api('/api/generate', payload)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            result = self.api('/api/jobs/' + job['id'])
            if result['status'] in ('complete', 'error'):
                break
            time.sleep(.1)
        self.assertEqual(result['status'], 'complete', result)
        clip = result['clip']
        self.assertEqual(clip['request'], payload)
        self.assertEqual(clip['variationId'], 'auto')
        self.assertTrue(clip['diagnostics']['ending_preserved'])
        with urlopen(self.base + clip['playbackUrl']) as response:
            wav_bytes = response.read()
        output = Path(self.temp.name) / 'download.wav'
        output.write_bytes(wav_bytes)
        with wave.open(str(output)) as audio:
            self.assertEqual(audio.getnframes(), round(payload['duration'] * audio.getframerate()))
            samples = np.frombuffer(audio.readframes(audio.getnframes()), '<i2')
            self.assertGreater(np.sqrt(np.mean(samples.astype(float) ** 2)), 500)
            self.assertTrue((samples[-2:] == 0).all())
        req = Request(self.base + clip['playbackUrl'], headers={'Range': 'bytes=44-1043'})
        with urlopen(req) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(len(response.read()), 1000)
        for fmt in ('wav', 'mp3'):
            with urlopen(self.base + f'/api/clips/{clip["id"]}/export?format={fmt}&bitrate=320&filename=Test.{fmt}') as response:
                self.assertIn('attachment;', response.headers['Content-Disposition'])
                export = Path(self.temp.name) / f'export.{fmt}'
                export.write_bytes(response.read())
            decoded = decode_audio(export, sample_rate=44100)
            self.assertEqual(len(decoded), round(payload['duration'] * 44100))
            self.assertGreater(np.sqrt(np.mean(decoded ** 2)), .01)
        reloaded = MusicCrafter(Path(self.temp.name), background_scan=False)
        self.assertIn(clip['id'], reloaded.clips)
        reloaded.pool.shutdown(wait=True)
        reloaded.scan_pool.shutdown(wait=True)

    def test_settings_and_parent_folder_rescan(self):
        settings = self.api('/api/settings', dict(favorites=['55:2015'], notes={'55:2015': 'Good for travel'}, exportFormat='mp3', bitrate=256), 'PATCH')
        self.assertEqual(settings['favorites'], ['55:2015'])
        self.assertEqual(self.api('/api/settings')['notes']['55:2015'], 'Good for travel')
        job = self.api('/api/roots', {'path': str(PROJECT)})
        self.wait_scan(job)
        library = self.api('/api/library')
        self.assertEqual(len(library['tracks']), 16)
        self.assertEqual(library['collections'][0]['trackCount'], 16)
        self.assertEqual(len(library['roots']), 2)
        self.wait_scan(self.api('/api/roots', {'path': str(PROJECT)}))
        self.assertEqual(len(self.api('/api/library')['roots']), 2)
        self.wait_scan(self.api('/api/roots/remove', {'path': str(PROJECT)}))
        remaining = self.api('/api/library')
        self.assertEqual(len(remaining['roots']), 1)
        self.assertEqual(len(remaining['tracks']), 16)
        self.assertTrue((PROJECT / 'Light Jazzy').is_dir())

    def test_preview_without_generation(self):
        before = len(self.api('/api/clips')['clips'])
        with urlopen(self.base + '/api/tracks/55%3A2015/preview.wav') as response:
            output = Path(self.temp.name) / 'preview.wav'
            output.write_bytes(response.read())
        with wave.open(str(output)) as audio:
            self.assertEqual(audio.getnframes(), audio.getframerate() * 30)
            self.assertEqual(audio.getnchannels(), 2)
            samples = np.frombuffer(audio.readframes(audio.getnframes()), '<i2')
            self.assertGreater(np.sqrt(np.mean(samples.astype(float) ** 2)), 500)
            self.assertTrue((samples[-2:] == 0).all())
        self.assertEqual(len(self.api('/api/clips')['clips']), before)

    def test_preview_follows_variation_and_caches_separately(self):
        track = self.app.library.get('55:2015')
        variants = [v for v in track['variations'] if track['blocks'][v['start']]['start'] > 0][:2]
        outputs = []
        for variant in variants:
            output = self.app.preview(track['id'], variant['id'])
            outputs.append(output)
            with wave.open(str(output), 'rb') as wav:
                actual = wav.readframes(100)
            with Path(track['source_path']).open('rb') as source:
                source.seek(track['container_header']['data_offset'] + track['blocks'][variant['start']]['start']*4)
                self.assertEqual(actual, source.read(400))
            self.assertEqual(self.app.preview(track['id'], variant['id']), output)
        self.assertNotEqual(outputs[0], outputs[1])
        with self.assertRaises(ValueError):
            self.app.preview(track['id'], 'invalid')

    def test_remove_folder_can_replace_a_slow_scan(self):
        entered, release = threading.Event(), threading.Event()
        slow_folder = Path(self.temp.name) / 'slow-folder'
        slow_folder.mkdir()
        original = self.app.library.scan
        def slow_scan(progress=None, cancel=None, roots=None):
            if str(slow_folder) in roots:
                progress(dict(phase='discovering', message='Waiting for a slow folder', progress=None))
                entered.set()
                release.wait(10)
            return original(progress=progress, cancel=cancel, roots=roots)
        with patch.object(self.app.library, 'scan', side_effect=slow_scan):
            try:
                first = self.api('/api/roots', {'path': str(slow_folder)})
                self.assertTrue(entered.wait(2))
                second = self.api('/api/roots/remove', {'path': str(slow_folder)})
                self.wait_scan(second)
                self.assertEqual(self.api('/api/scans/' + first['id'])['status'], 'cancelled')
                self.assertNotIn(str(slow_folder), self.api('/api/library')['roots'])
                self.assertEqual(len(self.api('/api/library')['tracks']), 16)
                self.assertTrue(slow_folder.is_dir())
            finally:
                release.set()

    def test_async_scan_does_not_block_generation(self):
        entered, release = threading.Event(), threading.Event()
        original = self.app.library.scan
        def slow_scan(progress=None, cancel=None, roots=None):
            progress(dict(phase='discovering', message='Looking through a slow test folder',
                          foldersScanned=1, mapsFound=0, total=0, completed=0, progress=None))
            entered.set()
            release.wait(10)
            return original(progress=progress, cancel=cancel, roots=roots)
        with patch.object(self.app.library, 'scan', side_effect=slow_scan):
            try:
                started = time.monotonic()
                scan = self.api('/api/rescan', {})
                self.assertLess(time.monotonic() - started, 1)
                self.assertTrue(entered.wait(2))
                self.assertEqual(self.api('/api/scan')['status'], 'running')
                self.assertEqual(self.api('/api/scan')['foldersScanned'], 1)
                self.assertEqual(self.api('/api/health')['status'], 'ok')
                started = time.monotonic()
                job = self.api('/api/generate', dict(trackId='55:2015', duration=4, variation='auto'))
                self.assertLess(time.monotonic() - started, 1)
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    result = self.api('/api/jobs/' + job['id'])
                    if result['status'] in ('complete', 'error'):
                        break
                    time.sleep(.05)
                self.assertEqual(result['status'], 'complete', result)
                self.assertEqual(self.api('/api/scan')['status'], 'running')
            finally:
                release.set()
                self.wait_scan(scan)

    def test_tempo_pitch_final_duration_and_diagnostics(self):
        payload = dict(trackId='55:2015', duration=10.375, variation='auto', tempo=110.4, pitch=3)
        job = self.api('/api/generate', payload)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            result = self.api('/api/jobs/' + job['id'])
            if result['status'] in ('complete', 'error'):
                break
            time.sleep(.1)
        self.assertEqual(result['status'], 'complete', result)
        clip = result['clip']
        diagnostics = clip['diagnostics']
        expected_frames = round(payload['duration'] * clip['sampleRate'])
        self.assertEqual(diagnostics['output_frames'], expected_frames)
        self.assertEqual(diagnostics['verifiedFrames'], expected_frames)
        self.assertAlmostEqual(diagnostics['duration'], clip['duration'])
        self.assertAlmostEqual(diagnostics['baseArrangementDuration'], payload['duration'] * 1.2, places=4)
        self.assertGreater(diagnostics['baseArrangementFrames'], diagnostics['output_frames'])
        self.assertAlmostEqual(clip['estimatedTempo'], payload['tempo'] * diagnostics['fit_ratio'])
        self.assertTrue(diagnostics['ending_preserved'])
        self.assertLessEqual(max(s['start'] + s['duration'] for s in clip['sections']), clip['duration'] + .001)


if __name__ == '__main__':
    unittest.main()
