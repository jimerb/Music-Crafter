"""Lazy source reading preserves PCM and does not copy complete music files."""
from pathlib import Path
import gc
import io
import struct
import tempfile
import unittest
from unittest.mock import patch
import wave

import numpy as np

from app.engine import ArrangementError, _read_audio_cached, read_audio, render
from app.library import Library, audio_header


PROJECT = Path(__file__).resolve().parents[1]
SOUNDS = PROJECT / "Light Jazzy/Sound Files/44k"


def reference_slice(path, start, frames):
    """Independent small seek/read reference; never read the complete source."""
    header = audio_header(path)
    with path.open("rb") as source:
        source.seek(header["data_offset"] + start * header["block_align"])
        raw = source.read(frames * header["block_align"])
    return np.frombuffer(raw, "<i2").reshape(-1, header["channels"])


def write_fixture(path, samples, rate, signature=b"RIFF"):
    with wave.open(str(path), "wb") as target:
        target.setnchannels(samples.shape[1])
        target.setsampwidth(2)
        target.setframerate(rate)
        target.writeframes(samples.astype("<i2").tobytes())
    if signature != b"RIFF":
        with path.open("r+b") as target:
            target.write(signature)


class AudioReaderTests(unittest.TestCase):
    def tearDown(self):
        _read_audio_cached.cache_clear()
        gc.collect()

    def test_real_22k_riff_and_44k_sdss_slices_match_original_bytes(self):
        if not SOUNDS.is_dir():
            self.skipTest('Optional private audio fixtures are not installed')
        # Actual Light & Jazzy filenames say "44k" even where the header is 22k.
        for title, expected_rate, expected_signature in (
            ("Tasty Jazz", 22050, b"RIFF"),
            ("In the Zone", 44100, b"SDSS"),
        ):
            with self.subTest(track=title):
                path = SOUNDS / f"{title} 44k.sds"
                header = audio_header(path)
                with path.open("rb") as source:
                    self.assertEqual(source.read(4), expected_signature)
                expected = [(start, reference_slice(path, start, 113))
                            for start in (0, header["frames"] // 2, header["frames"] - 113)]
                _read_audio_cached.cache_clear()
                with patch.object(Path, "read_bytes", side_effect=AssertionError("Whole-file reading is forbidden")):
                    pcm, rate = read_audio(path)
                    self.assertIsInstance(pcm, np.memmap)
                    self.assertEqual(pcm.mode, "r")
                    self.assertEqual(pcm.filename, path.resolve())
                    self.assertFalse(pcm.flags.writeable)
                    self.assertFalse(pcm.flags.owndata)
                    self.assertEqual(pcm.shape, (header["frames"], 2))
                    self.assertEqual(rate, expected_rate)
                    for start, samples in expected:
                        np.testing.assert_array_equal(pcm[start:start + len(samples)], samples)
                    with self.assertRaises(ValueError):
                        pcm[0, 0] = 0

    def test_metadata_inspection_reads_only_small_header(self):
        if not SOUNDS.is_dir():
            self.skipTest('Optional private audio fixtures are not installed')
        path = SOUNDS / "Tasty Jazz 44k.sds"
        _read_audio_cached.cache_clear()
        real_open = Path.open
        bytes_read = []

        class TrackedHeader:
            def __init__(self, source):
                self.source = source

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.source.close()

            def read(self, size=-1):
                self_size = size
                if self_size < 0 or self_size > 40:
                    raise AssertionError("Header lookup attempted a bulk audio read")
                raw = self.source.read(size)
                bytes_read.append(len(raw))
                return raw

            def seek(self, *args):
                return self.source.seek(*args)

            def tell(self):
                return self.source.tell()

        def track_open(candidate, *args, **kwargs):
            opened = real_open(candidate, *args, **kwargs)
            if candidate.resolve() == path.resolve():
                return TrackedHeader(opened)
            return opened

        with patch.object(Path, "open", track_open):
            pcm, rate = read_audio(path)
        self.assertIsInstance(pcm, np.memmap)
        self.assertEqual(rate, 22050)
        self.assertLessEqual(sum(bytes_read), 128)

    def test_mono_is_a_read_only_zero_copy_stereo_view(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            path = Path(directory) / "mono.sds"
            source = np.arange(-1000, 1000, dtype=np.int16)[:, None]
            write_fixture(path, source, 22050, signature=b"SDSS")
            with patch.object(Path, "read_bytes", side_effect=AssertionError("Whole-file reading is forbidden")):
                pcm, rate = read_audio(path)
                self.assertEqual(rate, 22050)
                self.assertEqual(pcm.shape, (len(source), 2))
                self.assertEqual(pcm.strides[1], 0)
                self.assertTrue(np.shares_memory(pcm[:, 0], pcm[:, 1]))
                self.assertFalse(pcm.flags.writeable)
                np.testing.assert_array_equal(pcm[:, 0], source[:, 0])
                np.testing.assert_array_equal(pcm[:, 1], source[:, 0])
            del pcm
            _read_audio_cached.cache_clear()
            gc.collect()

    def test_sdss_22k_and_riff_44k_headers_are_also_supported(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            source = np.column_stack([np.arange(256), -np.arange(256)]).astype(np.int16)
            for signature, rate in ((b"SDSS", 22050), (b"RIFF", 44100)):
                path = Path(directory) / f"{signature.decode()}-{rate}.sds"
                write_fixture(path, source, rate, signature)
                with patch.object(Path, "read_bytes", side_effect=AssertionError("Whole-file reading is forbidden")):
                    pcm, returned_rate = read_audio(path)
                    self.assertEqual(returned_rate, rate)
                    np.testing.assert_array_equal(pcm, source)
                    self.assertFalse(pcm.flags.writeable)
                del pcm
                _read_audio_cached.cache_clear()
                gc.collect()

    def test_generated_named_and_short_clips_work_without_source_bulk_reads(self):
        if not SOUNDS.is_dir():
            self.skipTest('Optional private audio fixtures are not installed')
        library = Library([str(PROJECT / "Light Jazzy")])
        tasty = next(track for track in library.tracks.values() if track["title"] == "Tasty Jazz")
        sleek = next(track for track in library.tracks.values() if track["title"] == "Sleeky and Stealthy")
        source_paths = {Path(track["source_path"]).resolve() for track in library.tracks.values()}
        original_read = Path.read_bytes

        def prevent_source_copy(path):
            if path.resolve() in source_paths:
                raise AssertionError("An entire source sound file was copied into memory")
            return original_read(path)

        _read_audio_cached.cache_clear()
        with patch.object(Path, "read_bytes", prevent_source_copy):
            named = render(tasty, 30, variation=tasty["variations"][0]["id"])
            short = render(sleek, 4)
        for result, seconds in ((named, 30), (short, 4)):
            self.assertEqual(result["pcm"].shape, (round(seconds * result["sample_rate"]), 2))
            self.assertEqual(result["pcm"].dtype, np.int16)
            self.assertTrue(np.all(result["pcm"][0] == 0))
            self.assertTrue(np.all(result["pcm"][-1] == 0))
        self.assertEqual(named["diagnostics"]["path"][0], tasty["variations"][0]["start"])
        normalized = short["pcm"].astype(float) / 32768
        initial = normalized[:round(short["sample_rate"] * .2)]
        self.assertGreater(float(np.abs(normalized).max()), .2)
        self.assertGreater(float(np.sqrt(np.mean(initial ** 2))), .04)
        self.assertEqual(short["diagnostics"]["leading_removed_seconds"], 0)

    def test_incomplete_frame_is_rejected_without_bulk_reading(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            path = Path(directory) / "broken.wav"
            content = io.BytesIO()
            with wave.open(content, "wb") as target:
                target.setnchannels(2)
                target.setsampwidth(2)
                target.setframerate(44100)
                target.writeframes(b"\0\0\0\0")
            raw = bytearray(content.getvalue())
            struct.pack_into("<I", raw, 40, 3)
            path.write_bytes(raw)
            with patch.object(Path, "read_bytes", side_effect=AssertionError("Whole-file reading is forbidden")):
                with self.assertRaisesRegex(ArrangementError, "incomplete audio frame"):
                    read_audio(path)


if __name__ == "__main__":
    unittest.main()
