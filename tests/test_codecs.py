"""Real codec tests: exported PCM, decoded MP3 duration, tempo and pitch."""
from pathlib import Path
import json
import subprocess
import tempfile
import unittest
import wave

import numpy as np

from app.codecs import capabilities, decode_audio, encode_mp3, encode_wav, process_audio


RATE = 44100


def tone(seconds=4.137, frequency=440):
    frames = round(seconds * RATE)
    time = np.arange(frames) / RATE
    envelope = np.minimum(np.minimum(time / 0.04, (seconds - time) / 0.04), 1)
    left = 0.35 * np.sin(2 * np.pi * frequency * time) * envelope
    right = 0.28 * np.sin(2 * np.pi * 660 * time) * envelope
    return np.column_stack([left, right]).astype(np.float32)


def peak_frequency(samples):
    segment = samples[RATE // 2:RATE * 2]
    spectrum = np.abs(np.fft.rfft(segment * np.hanning(len(segment))))
    return np.argmax(spectrum) * RATE / len(segment)


class CodecTests(unittest.TestCase):
    def test_wave_round_trip_and_resampling_keep_exact_duration(self):
        original = tone()
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            destination = encode_wav(original, RATE, Path(directory) / "original.wav")
            with wave.open(str(destination), "rb") as source:
                self.assertEqual(source.getnframes(), len(original))
                self.assertEqual(source.getframerate(), RATE)
                self.assertEqual(source.getnchannels(), 2)
                decoded = np.frombuffer(source.readframes(source.getnframes()), "<i2").reshape(-1, 2)
            np.testing.assert_allclose(decoded.astype(float) / 32768, original, atol=1 / 32768)
            if capabilities()["available"]:
                resampled = encode_wav(original, RATE, Path(directory) / "video.wav", rate=48000, bit_depth=24)
                with wave.open(str(resampled), "rb") as source:
                    self.assertEqual(source.getnframes(), round(len(original) * 48000 / RATE))
                    self.assertEqual(source.getframerate(), 48000)
                    self.assertEqual(source.getsampwidth(), 3)
                decoded = decode_audio(resampled)
                self.assertLess(abs(len(decoded) - len(original)), 2)
                self.assertGreater(np.corrcoef(original[:, 0], decoded[:len(original), 0])[0, 1], 0.999)

    @unittest.skipUnless(capabilities()["available"], "Audio resampler unavailable")
    def test_every_offered_wave_quality_has_correct_header_and_signal(self):
        original = tone(seconds=1.234)
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            for rate in (22050, 32000, 44100, 48000, 96000):
                for depth in (16, 24, 32):
                    destination = encode_wav(original, RATE, Path(directory) / f"{rate}-{depth}.wav", rate=rate, bit_depth=depth)
                    with wave.open(str(destination), "rb") as source:
                        expected = round(len(original) * rate / RATE)
                        self.assertEqual(source.getnframes(), expected)
                        self.assertEqual(source.getframerate(), rate)
                        self.assertEqual(source.getsampwidth(), depth // 8)
                    decoded = decode_audio(destination, sample_rate=rate)
                    self.assertEqual(len(decoded), expected)
                    self.assertGreater(np.sqrt(np.mean(decoded ** 2)), 0.1)
                    self.assertLess(float(np.abs(decoded).max()), 0.4)

    @unittest.skipUnless(capabilities()["available"], "Audio resampler unavailable")
    def test_resampling_real_full_scale_music_does_not_clip_transients(self):
        source_path = Path(__file__).resolve().parents[1] / "analysis/output/tasty_jazz_30s_experimental.wav"
        if not source_path.is_file():
            self.skipTest('Optional private music fixture is not installed')
        with wave.open(str(source_path), "rb") as source:
            rate = source.getframerate()
            original = np.frombuffer(source.readframes(source.getnframes()), "<i2").reshape(-1, 2)
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            for output_rate in (44100, 48000, 96000):
                destination = encode_wav(original, rate, Path(directory) / f"{output_rate}.wav", rate=output_rate)
                with wave.open(str(destination), "rb") as source:
                    data = np.frombuffer(source.readframes(source.getnframes()), "<i2").reshape(-1, 2)
                    self.assertEqual(len(data), round(len(original) * output_rate / rate))
                self.assertLessEqual(int(np.abs(data.astype(np.int32)).max()), 32736)
                self.assertFalse(np.any(data == -32768))
                self.assertFalse(np.any(data == 32767))

    @unittest.skipUnless(capabilities()["mp3"], "MP3 codec unavailable")
    def test_mp3_decoded_duration_removes_encoder_delay_and_padding(self):
        original = tone()
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            wav = encode_wav(original, RATE, Path(directory) / "source.wav")
            for quality in (128, 192, 320):
                mp3 = encode_mp3(wav, Path(directory) / f"audio-{quality}.mp3", quality,
                                 metadata={"title": "Test of exact duration"})
                decoded = decode_audio(mp3)
                self.assertEqual(len(decoded), len(original), f"Decoded {quality} kbps MP3 duration")
                self.assertGreater(np.corrcoef(original[:, 0], decoded[:, 0])[0, 1], 0.995)
                self.assertLess(np.sqrt(np.mean(decoded ** 2)), 0.4)
                self.assertGreater(np.sqrt(np.mean(decoded ** 2)), 0.1)
            wav48 = encode_wav(original, RATE, Path(directory) / "source48.wav", rate=48000)
            mp348 = encode_mp3(wav48, Path(directory) / "audio48.mp3")
            decoded48 = decode_audio(mp348, sample_rate=48000)
            self.assertEqual(len(decoded48), round(len(original) * 48000 / RATE))
            wav22 = encode_wav(original, RATE, Path(directory) / "source22.wav", rate=22050)
            mp322 = encode_mp3(wav22, Path(directory) / "audio22.mp3", bitrate=320)
            with wave.open(str(wav22), "rb") as source:
                expected22 = source.getnframes() * 2
            decoded22 = decode_audio(mp322)
            self.assertEqual(len(decoded22), expected22)
            probe = Path(__file__).resolve().parents[1] / "app/tools/ffprobe.exe"
            if probe.is_file():
                output = subprocess.check_output([str(probe), "-v", "error", "-show_streams", "-of", "json", str(mp322)],
                                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                stream = json.loads(output)["streams"][0]
                self.assertEqual(int(stream["bit_rate"]), 320000)
                self.assertEqual(int(stream["sample_rate"]), 44100)
            loud = original * (0.999 / 0.35)
            wav_loud = encode_wav(loud, RATE, Path(directory) / "loud.wav")
            mp3_loud = encode_mp3(wav_loud, Path(directory) / "loud.mp3", bitrate=320)
            decoded_loud = decode_audio(mp3_loud)
            self.assertEqual(len(decoded_loud), len(loud))
            self.assertLess(float(np.abs(decoded_loud).max()), 1)

    @unittest.skipUnless(capabilities()["tempo"], "Tempo codec unavailable")
    def test_tempo_changes_duration_without_transposing_audio(self):
        original = tone(seconds=5)
        for ratio in (0.8, 0.995, 1.005, 1.2):
            target = round(len(original) / ratio)
            transformed, sample_rate = process_audio(original, RATE, ratio, target_frames=target)
            self.assertEqual(sample_rate, RATE)
            self.assertEqual(len(transformed), target)
            self.assertLess(abs(peak_frequency(transformed[:, 0]) - 440), 2)
            self.assertTrue(np.isfinite(transformed).all())
        loud = original * (0.999 / 0.35)
        transformed, _ = process_audio(loud, RATE, tempo_ratio=0.995)
        self.assertLessEqual(float(np.abs(transformed).max()), 1)

    @unittest.skipUnless(capabilities()["pitch"], "Pitch codec unavailable")
    def test_pitch_changes_note_without_changing_duration(self):
        original = tone(seconds=4)
        for semitones in (-3, 3):
            transformed, sample_rate = process_audio(original, RATE, pitch_semitones=semitones)
            self.assertEqual(len(transformed), len(original))
            self.assertEqual(sample_rate, RATE)
            expected = 440 * 2 ** (semitones / 12)
            self.assertLess(abs(peak_frequency(transformed[:, 0]) - expected), 2)

    def test_identity_and_invalid_requests(self):
        original = np.array([[0, 32767], [-32768, 120]], dtype=np.int16)
        copied, rate = process_audio(original, RATE)
        np.testing.assert_allclose(copied, original.astype(np.float32) / 32768)
        self.assertEqual(rate, RATE)
        with self.assertRaises(ValueError):
            process_audio(tone(), RATE, tempo_ratio=0)
        with self.assertRaises(ValueError):
            process_audio(tone(), RATE, target_frames=100)


if __name__ == "__main__":
    unittest.main()
