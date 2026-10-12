"""Music Crafter's real audio export and tempo/pitch processing.

PCM entering or leaving this module is frames x channels. Signed integer PCM is
accepted on input; floating point PCM uses the usual -1..1 scale. FFmpeg can be
provided locally in app/tools, through MUSIC_CRAFTER_FFMPEG, or on PATH.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import math
import os
import shutil
import subprocess
import wave
import tempfile

import numpy as np


TOOLS = Path(__file__).resolve().parent / "tools"
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def find_ffmpeg() -> Path | None:
    """Find the project codec first, with a configurable override for deployment."""
    override = os.environ.get("MUSIC_CRAFTER_FFMPEG")
    candidates = [Path(override)] if override else []
    candidates += [TOOLS / "ffmpeg.exe", TOOLS / "ffmpeg" ]
    on_path = shutil.which("ffmpeg")
    if on_path:
        candidates.append(Path(on_path))
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def _require_ffmpeg() -> Path:
    executable = find_ffmpeg()
    if executable is None:
        raise RuntimeError("FFmpeg is missing. Add it to app/tools, set MUSIC_CRAFTER_FFMPEG, or put it on PATH. See README setup.")
    return executable


def _run(args: list[str], *, input: bytes | None = None, timeout: float = 120) -> bytes:
    command = [str(_require_ffmpeg()), "-hide_banner", "-loglevel", "error", "-nostdin", *args]
    result = subprocess.run(command, input=input, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=timeout,
                            creationflags=_CREATE_NO_WINDOW)
    if result.returncode:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Audio processing failed: {message or 'codec exited unsuccessfully'}")
    return result.stdout


@lru_cache(maxsize=4)
def _capabilities(executable: str) -> tuple[str, str]:
    outputs = []
    for kind in ("-filters", "-encoders"):
        result = subprocess.run([executable, "-hide_banner", kind],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=15, creationflags=_CREATE_NO_WINDOW)
        if result.returncode:
            raise RuntimeError("Music Crafter's audio codec could not be started.")
        outputs.append((result.stdout + result.stderr).decode("utf-8", errors="replace"))
    return outputs[0], outputs[1]


def capabilities() -> dict:
    executable = find_ffmpeg()
    if executable is None:
        return {"available": False, "mp3": False, "tempo": False, "pitch": False}
    filters, encoders = _capabilities(str(executable))
    rubberband = " rubberband " in filters
    return {"available": True, "mp3": " libmp3lame " in encoders,
            "tempo": rubberband or " atempo " in filters,
            "pitch": rubberband or (" asetrate " in filters and " atempo " in filters),
            "rubberband": rubberband}


def _pcm_float(pcm: np.ndarray) -> np.ndarray:
    data = np.asarray(pcm)
    if data.ndim == 1:
        data = data[:, None]
    if data.ndim != 2 or not 1 <= data.shape[1] <= 8 or not len(data):
        raise ValueError("Audio must contain one or more frames and 1 to 8 channels.")
    if np.issubdtype(data.dtype, np.signedinteger):
        scale = float(2 ** (np.iinfo(data.dtype).bits - 1))
        data = data.astype(np.float32) / scale
    elif data.dtype == np.uint8:
        data = (data.astype(np.float32) - 128.0) / 128.0
    elif np.issubdtype(data.dtype, np.floating):
        data = data.astype(np.float32)
    else:
        raise ValueError("Unsupported audio sample type.")
    if not np.isfinite(data).all():
        raise ValueError("Audio contains invalid samples.")
    if float(np.max(np.abs(data))) > 1.1:
        raise ValueError("Floating point audio must use the -1 to 1 sample scale.")
    return np.ascontiguousarray(data, dtype="<f4")


def _sample_rate(sample_rate: int) -> int:
    value = int(sample_rate)
    if value != sample_rate or not 8000 <= value <= 192000:
        raise ValueError("Sample rate must be a whole number between 8000 and 192000 Hz.")
    return value


def _raw_input(data: np.ndarray, sample_rate: int) -> list[str]:
    return ["-f", "f32le", "-ar", str(sample_rate), "-ac", str(data.shape[1]), "-i", "pipe:0"]


def _raw_output(channels: int, sample_rate: int) -> list[str]:
    return ["-ac", str(channels), "-ar", str(sample_rate), "-c:a", "pcm_f32le", "-f", "f32le", "pipe:1"]


def _from_bytes(raw: bytes, channels: int) -> np.ndarray:
    if len(raw) % (4 * channels):
        raise RuntimeError("Audio codec returned incomplete samples.")
    return np.frombuffer(raw, dtype="<f4").reshape(-1, channels).copy()


def _resample(data: np.ndarray, sample_rate: int, output_rate: int) -> np.ndarray:
    target_frames = round(len(data) * output_rate / sample_rate)
    filters = f"aresample={output_rate},apad=whole_len={target_frames},atrim=end_sample={target_frames}"
    raw = _run([*_raw_input(data, sample_rate), "-af", filters,
                *_raw_output(data.shape[1], output_rate)], input=data.tobytes())
    output = _from_bytes(raw, data.shape[1])
    peak = float(np.max(np.abs(output)))
    if peak > 0.999:
        # Interpolation can reveal a true peak between original samples.
        # Preserve its shape with a constant gain, rather than hard clipping.
        output *= 0.999 / peak
    return output


def encode_wav(pcm: np.ndarray, sample_rate: int, out_path: str | Path,
               rate: int | None = None, bit_depth: int = 16) -> Path:
    """Write actual PCM WAV, resampling when the export rate differs from source."""
    source_rate = _sample_rate(sample_rate)
    output_rate = source_rate if rate is None else _sample_rate(rate)
    if bit_depth not in (16, 24, 32):
        raise ValueError("WAV bit depth must be 16, 24, or 32.")
    original = np.asarray(pcm)
    if bit_depth == 16 and output_rate == source_rate and original.dtype == np.int16 and original.ndim == 2 and 1 <= original.shape[1] <= 8:
        # Native PCM needs no whole-recording float conversion or duplicate byte
        # buffer. This also makes hour-long clips practical to save.
        destination = Path(out_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(destination), 'wb') as output:
            output.setnchannels(original.shape[1]); output.setsampwidth(2); output.setframerate(output_rate)
            for start in range(0, len(original), 131072):
                output.writeframesraw(original[start:start+131072].astype('<i2', copy=False).tobytes())
        return destination
    data = _pcm_float(pcm)
    if output_rate != source_rate:
        data = _resample(data, source_rate, output_rate)
    scale = 2 ** (bit_depth - 1)
    samples = np.clip(np.rint(data.astype(np.float64) * scale), -scale, scale - 1)
    if bit_depth == 16:
        encoded = samples.astype("<i2").tobytes()
    elif bit_depth == 32:
        encoded = samples.astype("<i4").tobytes()
    else:
        words = samples.astype("<i4").reshape(-1).astype(np.uint32)
        packed = np.empty((len(words), 3), dtype=np.uint8)
        for index in range(3):
            packed[:, index] = (words >> (index * 8)) & 255
        encoded = packed.tobytes()
    destination = Path(out_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(destination), "wb") as output:
        output.setnchannels(data.shape[1])
        output.setsampwidth(bit_depth // 8)
        output.setframerate(output_rate)
        output.writeframes(encoded)
    return destination


def encode_mp3(wav_path: str | Path, out_path: str | Path, bitrate: int = 320,
               metadata: dict[str, str] | None = None) -> Path:
    """Encode a real MP3 with Xing/LAME gapless information for exact playback."""
    if int(bitrate) not in (96, 128, 160, 192, 224, 256, 320):
        raise ValueError("Choose an MP3 quality from 96 to 320 kbps.")
    source = Path(wav_path)
    if not source.is_file():
        raise FileNotFoundError(source)
    if not capabilities()["mp3"]:
        raise RuntimeError("Music Crafter's audio codec does not include MP3 encoding.")
    destination = Path(out_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(source), "rb") as wav:
        source_rate = wav.getframerate()
        sample_width = wav.getsampwidth()
        source_peak = 0.0
        while raw := wav.readframes(65536):
            if sample_width in (2, 4):
                samples = np.frombuffer(raw, dtype=f"<i{sample_width}").astype(np.int64)
            elif sample_width == 3:
                packed = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int64)
                words = packed[:, 0] | (packed[:, 1] << 8) | (packed[:, 2] << 16)
                samples = (words ^ 0x800000) - 0x800000
            elif sample_width == 1:
                samples = np.frombuffer(raw, dtype=np.uint8).astype(np.int64) - 128
            else:
                raise ValueError("Unsupported source WAV sample width.")
            source_peak = max(source_peak, float(np.abs(samples).max()) / 2 ** (sample_width * 8 - 1))
    # MPEG-2 MP3 at 22.05 kHz silently caps a 320 kbps request at 160 kbps.
    # Use a MPEG-1 rate so the quality chosen in the UI is the quality exported.
    mp3_rate = source_rate if source_rate in (32000, 44100, 48000) else (44100 if source_rate < 44100 else 48000)
    args = ["-y", "-i", str(source), "-vn", "-ar", str(mp3_rate), "-c:a", "libmp3lame", "-b:a", f"{int(bitrate)}k",
            "-write_xing", "1", "-id3v2_version", "3", "-map_metadata", "-1"]
    # Lossy reconstruction can overshoot full-scale peaks. A small, constant
    # gain adjustment keeps loud source transients clean without limiting them.
    if source_peak > 0.95:
        args += ["-af", f"volume={0.95 / source_peak:.12g}"]
    for key, value in (metadata or {}).items():
        args += ["-metadata", f"{key}={value}"]
    _run([*args, str(destination)])
    if not destination.is_file() or destination.stat().st_size < 100:
        raise RuntimeError("MP3 encoding did not produce an audio file.")
    return destination


def _atempo_chain(ratio: float) -> list[str]:
    filters = []
    while ratio < 0.5:
        filters.append("atempo=0.5")
        ratio /= 0.5
    while ratio > 2:
        filters.append("atempo=2")
        ratio /= 2
    filters.append(f"atempo={ratio:.12g}")
    return filters


def process_audio(pcm: np.ndarray, sample_rate: int, tempo_ratio: float = 1,
                  pitch_semitones: float = 0, target_frames: int | None = None
                  ) -> tuple[np.ndarray, int]:
    """Change tempo and pitch independently and return an exact sample count.

    A faster tempo shortens the supplied audio. The arranging engine must first
    choose enough musical blocks for the requested speed; target_frames corrects
    tiny processor boundary differences rather than substituting for arrangement.
    Rubber Band keeps stereo channels synchronized and retains transients.
    """
    rate = _sample_rate(sample_rate)
    long_audio = len(pcm) > 600 * rate
    data = np.asarray(pcm) if long_audio else _pcm_float(pcm)
    if long_audio and (data.ndim != 2 or not 1 <= data.shape[1] <= 8):
        raise ValueError('Audio must have one to eight channels.')
    ratio = float(tempo_ratio)
    pitch = float(pitch_semitones)
    if not math.isfinite(ratio) or not 0.25 <= ratio <= 4:
        raise ValueError("Tempo ratio must be between 0.25 and 4.")
    if not math.isfinite(pitch) or not -24 <= pitch <= 24:
        raise ValueError("Pitch must be between -24 and 24 semitones.")
    expected_frames = round(len(data) / ratio)
    frames = expected_frames if target_frames is None else int(target_frames)
    if frames <= 0 or (target_frames is not None and frames != target_frames):
        raise ValueError("Target duration must contain a positive whole number of frames.")
    if abs(frames - expected_frames) > max(1, round(0.25 * rate)):
        raise ValueError("Requested duration does not match the tempo. Arrange enough music first.")
    if abs(ratio - 1) < 1e-12 and abs(pitch) < 1e-12 and frames == len(data):
        return (_pcm_float(data) if long_audio else data.copy()), rate
    filters, _ = _capabilities(str(_require_ffmpeg()))
    if " rubberband " in filters:
        pitch_ratio = 2 ** (pitch / 12)
        chain = [f"rubberband=tempo={ratio:.12g}:pitch={pitch_ratio:.12g}:"
                 "pitchq=quality:channels=together:window=long"]
    else:
        pitch_ratio = 2 ** (pitch / 12)
        chain = []
        if abs(pitch) > 1e-12:
            chain += [f"asetrate={round(rate * pitch_ratio)}", f"aresample={rate}"]
        chain += _atempo_chain(ratio / pitch_ratio)
    chain += [f"apad=whole_len={frames}", f"atrim=end_sample={frames}"]
    if long_audio:
        work_root = (TOOLS.parent / 'data' / 'codec-work').resolve()
        work_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='long-clip-', dir=work_root) as directory:
            work = Path(directory).resolve()
            if not work.is_relative_to(work_root):
                raise RuntimeError('Unexpected audio working directory.')
            source, output = work/'input.f32', work/'output.f32'
            with source.open('wb') as f:
                for start in range(0, len(data), 131072):
                    f.write(_pcm_float(data[start:start+131072]).tobytes())
            _run(['-f','f32le','-ar',str(rate),'-ac',str(data.shape[1]),'-i',str(source),
                  '-af', ','.join(chain), '-ac',str(data.shape[1]),'-ar',str(rate),
                  '-c:a','pcm_f32le','-f','f32le',str(output)], timeout=max(120,len(data)/rate*6))
            if output.stat().st_size != frames * data.shape[1] * 4:
                raise RuntimeError('Processed audio did not have the requested duration.')
            processed = np.fromfile(output, dtype='<f4').reshape(-1, data.shape[1])
        peak = 0
        for start in range(0, len(processed), 131072):
            chunk = processed[start:start+131072]
            if not np.isfinite(chunk).all():
                raise RuntimeError('Processed audio contains invalid samples.')
            peak = max(peak, float(np.max(np.abs(chunk))))
        if peak > .999:
            processed *= .999/peak
        return processed, rate
    raw = _run([*_raw_input(data, rate), "-af", ",".join(chain),
                *_raw_output(data.shape[1], rate)], input=data.tobytes(),
               timeout=max(120, len(data) / rate * 6))
    processed = _from_bytes(raw, data.shape[1])
    if len(processed) != frames or not np.isfinite(processed).all():
        raise RuntimeError("Processed audio did not have the requested duration.")
    peak = float(np.max(np.abs(processed)))
    if peak > 0.999:
        # Time stretching can reconstruct a slightly louder transient. Scale
        # the entire clip uniformly rather than clipping individual samples.
        processed *= 0.999 / peak
    return processed, rate


def decode_audio(path: str | Path, sample_rate: int = 44100, channels: int = 2) -> np.ndarray:
    """Decode exported audio for duration and content validation, including MP3 padding."""
    rate = _sample_rate(sample_rate)
    if not 1 <= channels <= 8:
        raise ValueError("Choose between 1 and 8 channels.")
    raw = _run(["-i", str(Path(path)), "-vn", *_raw_output(channels, rate)])
    return _from_bytes(raw, channels)
