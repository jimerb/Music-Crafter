from collections import Counter
from pathlib import Path
import json
import os
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch
import wave

from app.library import Library, MASK, ScanCancelled, parse_blocks


def make_collection(root, title='Fixture Track'):
    collection = Path(root)/'Demo Collection'
    maps = collection/'Library Enablers'
    sounds = collection/'Sound Files'/'44k'
    maps.mkdir(parents=True, exist_ok=True)
    sounds.mkdir(parents=True, exist_ok=True)
    encoded = []
    for index, (offset, length, name, flags, successors) in enumerate([
            (0, 44100, 'Start', 1 << 30, 1 << 1),
            (44100, 22050, 'Ending', 1 << 31, 0)]):
        block = bytearray(132)
        struct.pack_into('<II', block, 0, offset, length)
        block[8:24] = name.encode().ljust(16, b'\0')
        block[24:88] = (b'Start' if index == 0 else b'*').ljust(64, b'\0')
        block[88:116] = successors.to_bytes(28, 'little')
        struct.pack_into('<I', block, 116, flags)
        encoded.append(bytes(v ^ MASK[(index+i) % len(MASK)] for i, v in enumerate(block)))
    blocking = title.encode().ljust(24, b'\0')+struct.pack('<III', 44100, 0, 2)+b''.join(encoded)
    (maps/'Demo Collection 44k.blk').write_bytes(blocking)
    source = sounds/f'{title} 44k.sds'
    with wave.open(str(source), 'wb') as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(44100)
        output.writeframes(b'\0'*(66150*4))
    return collection, source


class LibraryTests(unittest.TestCase):
    def test_ambient_loop_uses_authored_connections_and_exact_length(self):
        from app.engine import render, variation_ranges, _read_audio_cached
        self.addCleanup(_read_audio_cached.cache_clear)
        import numpy as np
        track = next(iter(Library([str(self.root)]).tracks.values()))
        track['endingMode'] = 'fade'
        track['blocks'][0]['successors'] = [0, 1]
        result = render(track, 12)
        self.assertEqual(len(result['pcm']), 12 * 44100)
        self.assertFalse(result['diagnostics']['ending_preserved'])
        self.assertIn('fades out', result['diagnostics']['note'])
        path = result['diagnostics']['path']
        self.assertTrue(all(b in track['blocks'][a]['successors'] for a, b in zip(path, path[1:])))
        self.assertTrue(np.isfinite(result['pcm']).all())
        self.assertIsNone(variation_ranges(track)['0']['max'])

    def test_count_padding_and_explicit_gaps(self):
        path = self.collection/'Library Enablers'/'Demo Collection 44k.blk'
        data = bytearray(path.read_bytes())
        data[34:36] = b'\xff\xa5'
        offset = 36 + 132
        for i, value in enumerate(struct.pack('<I', 44102)):
            data[offset+i] = value ^ MASK[(1+i) % 128]
        path.write_bytes(data)
        parsed = parse_blocks(path)['Fixture Track']
        self.assertEqual(parsed['blocks'][1]['offset_units'], 44102)
        self.assertEqual(parsed['total_units'], 66152)
        self.assertEqual(len(Library([str(self.root)]).tracks), 1)

    def test_xml_maps_shared_audio_and_terminal_end(self):
        old = self.collection/'Library Enablers'/'Demo Collection 44k.blk'
        old.unlink()
        xml = old.with_suffix('.blx')
        xml.write_text('<Libraries><Library><Cue name="Fixture Track"><Blocking slop="44100">'
                       '<B n="Start" S="0" L="44100" SD="0" LD="1"><F>1073741824</F><C>2</C></B>'
                       '<B n="End" S="44100" L="22050" SD="1" LD="0.5"><F>4</F><C>0</C></B>'
                       '</Blocking></Cue></Library></Libraries>')
        self.source.rename(self.root/self.source.name)
        library = Library([str(self.root)])
        self.assertEqual(library.errors, [])
        track = next(iter(library.tracks.values()))
        self.assertEqual(track['blocks'][1]['start'], 44100)
        self.assertEqual(track['blocks'][1]['endingMarker'], 'terminal section')
        self.assertEqual(track['variations'][0]['name'], 'Start')

    def test_xml_connection_words_cross_32_bit_boundary(self):
        xml = self.root/'connections.blx'
        blocks = '<B n="Start" S="0" L="10"><F>1073741824</F><C>0</C><C>2</C></B>'
        blocks += ''.join(f'<B n="{i}" S="{i*10}" L="10"><F>2147483648</F><C>0</C></B>' for i in range(1, 34))
        xml.write_text('<Libraries><Library><Cue name="Wide"><Blocking>'+blocks+'</Blocking></Cue></Library></Libraries>')
        self.assertEqual(parse_blocks(xml)['Wide']['blocks'][0]['successors'], [33])

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='library-test-', dir=Path(__file__).resolve().parent)
        self.root = Path(self.folder.name)
        # Verify this fixture's generated deletion target is inside tests before
        # TemporaryDirectory performs its recursive cleanup on Windows.
        self.assertTrue(self.root.resolve().is_relative_to(Path(__file__).resolve().parent))
        self.addCleanup(self.folder.cleanup)
        self.collection, self.source = make_collection(self.root)

    def test_constructor_can_skip_scan(self):
        with patch('app.library.os.scandir', side_effect=AssertionError('Unexpected walk')):
            library = Library([str(self.root/'offline share')], scan=False)
        self.assertEqual(library.tracks, {})

    def test_progress_one_walk_overlap_and_snapshot_exclusions(self):
        for name in ('.snapshot', '@Recently-Snapshot', '@Recycle', '.git'):
            make_collection(self.root/name, title='Hidden Fixture')
        # A second map and duplicate configured parent must not retraverse audio.
        maps = self.collection/'Library Enablers'
        (maps/'Duplicate 44k.blk').write_bytes((maps/'Demo Collection 44k.blk').read_bytes())
        original_scandir = os.scandir
        calls = Counter()
        def counted(directory):
            calls[str(directory)] += 1
            return original_scandir(directory)
        events = []
        library = Library([str(self.root), str(self.collection)], scan=False)
        with patch('app.library.os.scandir', side_effect=counted):
            result = library.scan(progress=events.append)
        self.assertEqual(len(library.tracks), 1)
        self.assertEqual(len(library.collections), 1)
        self.assertEqual(library.collections[0]['trackCount'], 1)
        self.assertEqual(result['mapsFound'], 2)
        self.assertTrue(all(count == 1 for count in calls.values()))
        self.assertFalse(any(any(part in path for part in ('.snapshot', '@Recently-Snapshot', '@Recycle', '.git')) for path in calls))
        required = {'phase','message','currentPath','foldersScanned','mapsFound','completed','total','progress'}
        self.assertTrue(all(required <= e.keys() for e in events))
        self.assertTrue(all(e['progress'] is None for e in events if e['phase']=='discovering'))
        self.assertTrue(all(0 <= e['progress'] <= 1 for e in events if e['phase']=='indexing'))
        self.assertEqual(events[-1]['progress'], 1)
        self.assertEqual(events[-1]['completed'], 2)
        self.assertGreaterEqual(events[-1]['foldersScanned'], 5)

    def test_duplicate_audio_names_preserved_and_valid_fallback_selected(self):
        good = self.collection/'Sound Files'/'22k'/'Fixture Track 22k.sds'
        good.parent.mkdir()
        good.write_bytes(self.source.read_bytes())
        self.source.write_bytes(b'not a PCM music file')
        library = Library([str(self.root)])
        self.assertEqual(len(library.tracks), 1)
        self.assertEqual(Path(next(iter(library.tracks.values()))['source_path']), good)
        self.assertTrue(any('Unsupported sound container' in warning for warning in library.errors))

    def test_cancelled_scan_preserves_old_index(self):
        library = Library([str(self.root)])
        previous = library.snapshot()
        make_collection(self.root/'New', title='New Fixture')
        cancellation = threading.Event()
        def progress(event):
            if event['phase'] == 'indexing' and event['completed'] == 1:
                cancellation.set()
        with self.assertRaises(ScanCancelled):
            library.scan(progress=progress, cancel=cancellation)
        self.assertEqual(library.snapshot(), previous)
        with self.assertRaises(ScanCancelled):
            library.scan(cancel=cancellation)
        self.assertEqual(library.snapshot(), previous)
        discovery_cancel = threading.Event()
        def cancel_discovery(event):
            if event['phase']=='discovering' and event['foldersScanned'] >= 1:
                discovery_cancel.set()
        with self.assertRaises(ScanCancelled):
            library.scan(progress=cancel_discovery, cancel=discovery_cancel)
        self.assertEqual(library.snapshot(), previous)

    def test_reader_available_while_background_scan_paused(self):
        library = Library([str(self.root)])
        existing = next(iter(library.tracks))
        paused = threading.Event()
        release = threading.Event()
        errors = []
        def progress(event):
            if event['phase'] == 'discovering' and event['currentPath'] and not paused.is_set():
                paused.set()
                release.wait(3)
        def scan():
            try:
                library.scan(progress=progress)
            except Exception as error:
                errors.append(error)
        worker = threading.Thread(target=scan)
        worker.start()
        self.assertTrue(paused.wait(2))
        try:
            self.assertTrue(library.lock.acquire(timeout=.2))
            library.lock.release()
            self.assertEqual(library.get(existing)['title'], 'Fixture Track')
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])

    def test_root_subset_scan_preserves_configuration_and_change_discards_index(self):
        configured = [str(self.root), str(self.root/'future offline')]
        library = Library(configured, scan=False)
        library.scan(roots=[str(self.root)])
        self.assertEqual(library.roots, configured)
        previous = library.snapshot()
        def change_roots(event):
            if event['phase']=='indexing' and event['completed']==event['total']:
                with library.lock:
                    library.roots = ['new configuration']
        with self.assertRaises(ScanCancelled):
            library.scan(progress=change_roots, roots=[str(self.root)])
        after = library.snapshot()
        self.assertEqual(after['tracks'], previous['tracks'])
        self.assertEqual(after['collections'], previous['collections'])
        self.assertEqual(after['errors'], previous['errors'])
        self.assertEqual(library.roots, ['new configuration'])

    def test_unavailable_roots_preserve_index_mixed_roots_produce_warning(self):
        library = Library([str(self.root)])
        previous = library.snapshot()
        with self.assertRaisesRegex(OSError, 'Music folders are unavailable'):
            library.scan(roots=[str(self.root/'missing')])
        self.assertEqual(library.snapshot(), previous)
        library.scan(roots=[str(self.root/'missing'), str(self.root)])
        self.assertEqual(len(library.tracks), 1)
        self.assertTrue(any('Folder is unavailable' in warning for warning in library.errors))

    def test_inaccessible_child_is_warning_and_unsupported_map_does_not_stop_scan(self):
        unreadable = self.root/'blocked'
        unreadable.mkdir()
        bad = self.root/'unsupported.blk'
        bad.write_bytes(b'invalid blocking format')
        original_scandir = os.scandir
        def scan(directory):
            if Path(directory) == unreadable:
                raise PermissionError('Fixture directory access denied')
            return original_scandir(directory)
        library = Library([str(self.root)], scan=False)
        with patch('app.library.os.scandir', side_effect=scan):
            library.scan()
        self.assertEqual(len(library.tracks), 1)
        self.assertTrue(any('access denied' in warning for warning in library.errors))
        self.assertTrue(any('unsupported.blk' in warning for warning in library.errors))

    def test_snapshot_restore_without_filesystem_and_caller_mutation_isolated(self):
        library = Library([str(self.root)])
        snapshot = json.loads(json.dumps(library.snapshot()))
        restored = Library([], scan=False)
        with (patch('app.library.os.scandir', side_effect=AssertionError('Unexpected cached catalog walk')),
              patch('app.library.audio_header', side_effect=AssertionError('Unexpected sound-file probe'))):
            restored.restore(snapshot)
        self.assertEqual(restored.snapshot(), library.snapshot())
        snapshot['collections'][0]['name'] = 'Changed external copy'
        self.assertNotEqual(restored.collections[0]['name'], 'Changed external copy')
        copied = restored.snapshot()
        copied['tracks'].clear()
        self.assertEqual(len(restored.tracks), 1)
        before = restored.snapshot()
        with self.assertRaises(ValueError):
            restored.restore({'version': 1, 'tracks': {}})
        self.assertEqual(restored.snapshot(), before)

    def test_empty_subset_intentionally_publishes_empty_index(self):
        library = Library([str(self.root)])
        configured = list(library.roots)
        result = library.scan(roots=[])
        self.assertEqual(library.tracks, {})
        self.assertEqual(library.collections, [])
        self.assertEqual(library.roots, configured)
        self.assertEqual(result['tracks'], 0)


if __name__ == '__main__':
    unittest.main()
