"""Local HTTP application, library index, generation jobs and real audio downloads."""
from __future__ import annotations

import argparse
import hashlib
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import re
import threading
import time
import traceback
from urllib.parse import parse_qs, unquote, urlsplit
import uuid

import numpy as np

from .codecs import capabilities, encode_mp3, encode_wav, process_audio
from .engine import render, variation_availability
from .library import Library, PROJECT, ScanCancelled

DATA = PROJECT / 'app/data'
STATIC = PROJECT / 'app/static'
DEFAULT_SETTINGS = dict(favorites=[], notes={}, defaultDuration=60, exportFormat='wav', bitrate=320)


def read_json(path: Path, fallback):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return fallback


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding='utf-8')
    temp.replace(path)


class MusicCrafter:
    def __init__(self, data_dir: Path = DATA, background_scan: bool = True):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.clips_dir = data_dir / 'clips'
        self.clips_dir.mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.export_lock = threading.RLock()
        self.preview_lock = threading.RLock()
        self.index_lock = threading.Lock()
        config = read_json(data_dir / 'library.json', {'roots': []})
        self.library = Library(config['roots'], scan=False)
        cached = read_json(data_dir / 'library-index.json', None)
        if cached:
            try:
                self.library.restore(cached)
                self.library.roots = list(config['roots'])
                if [self.root_key(root) for root in cached['roots']] != [self.root_key(root) for root in config['roots']]:
                    self._prune_removed_sources(self.library.roots)
            except (ValueError, KeyError, TypeError):
                cached = None
        if not cached:
            if not background_scan:
                self.library.scan()
            else:
                # A large network tree must never delay the HTTP server startup.
                local = str(PROJECT / 'Light Jazzy')
                if any(self.root_key(root) == self.root_key(local) for root in config['roots']):
                    self.library.scan(roots=[local])
        self.settings = {**DEFAULT_SETTINGS, **read_json(data_dir / 'settings.json', {})}
        self.jobs = {}
        self.clips = {}
        for manifest in self.clips_dir.glob('*/clip.json'):
            clip = read_json(manifest, None)
            if clip and (manifest.parent / 'audio.wav').is_file():
                self.clips[clip['id']] = clip
        self.features = {**capabilities(), 'loop': False}
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='music-render')
        self.scan_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='library-scan')
        self.scan_jobs = {}
        self.scan_id = None
        self.scan_cancel = None
        self.scan_started = None
        if background_scan:
            self.start_scan()

    def catalog(self):
        return {**self.library.public(self.features), 'scan': self.scan_status()}

    @staticmethod
    def root_key(value: str):
        return str(Path(value).expanduser().absolute()).rstrip('/\\').casefold()

    def scan_status(self, identifier=None):
        with self.lock:
            job = self.scan_jobs.get(identifier or self.scan_id)
            if not job:
                return dict(status='idle', phase='idle', message='Ready to scan', roots=list(self.library.roots),
                            progress=None, foldersScanned=0, mapsFound=0, completed=0, total=0,
                            trackCount=len(self.library.tracks), collectionCount=len(self.library.collections), warnings=[])
            value = {**job, 'roots': list(job['roots']), 'warnings': list(job.get('warnings', []))}
            if value['status'] in ('queued', 'running') and self.scan_started is not None:
                value['elapsedSeconds'] = round(time.monotonic() - self.scan_started, 1)
            return value

    def start_scan(self, action='rescan', root=None):
        with self.lock:
            current = self.scan_jobs.get(self.scan_id)
            if action == 'rescan' and current and current['status'] in ('queued', 'running'):
                return self.scan_status()
            roots = list(self.library.roots)
            if action in ('add', 'remove'):
                if not str(root or '').strip():
                    raise ValueError('Enter a music folder path.')
                absolute = str(Path(root.strip()).expanduser().absolute())
                key = self.root_key(absolute)
                if action == 'add':
                    if key not in [self.root_key(value) for value in roots]:
                        roots.append(absolute)
                else:
                    roots = [value for value in roots if self.root_key(value) != key]
                if self.scan_cancel:
                    self.scan_cancel.set()
                with self.library.lock:
                    self.library.roots = roots
                    if action == 'remove':
                        self._prune_removed_sources(roots)
                write_json(self.data_dir / 'library.json', {'roots': roots})
            if self.scan_cancel:
                self.scan_cancel.set()
            if current and current['status'] in ('queued', 'running'):
                current.update(status='cancelled', message='Replaced by the updated folder scan')
            identifier = uuid.uuid4().hex
            cancel = threading.Event()
            self.scan_id, self.scan_cancel = identifier, cancel
            self.scan_started = time.monotonic()
            self.scan_jobs[identifier] = dict(id=identifier, status='queued', phase='discovering',
                message='Starting folder scan', roots=roots, foldersScanned=0, mapsFound=0,
                completed=0, total=0, progress=None, trackCount=len(self.library.tracks),
                collectionCount=len(self.library.collections), warnings=[])
            self.scan_pool.submit(self._scan, identifier, roots, cancel)
            return self.scan_status(identifier)

    def _prune_removed_sources(self, roots):
        def retained(track):
            source = Path(track['source_path'])
            return any(source.is_relative_to(Path(value)) for value in roots)
        self.library.tracks = {key: value for key, value in self.library.tracks.items() if retained(value)}
        counts = {}
        for track in self.library.tracks.values():
            counts[track['collectionId']] = counts.get(track['collectionId'], 0) + 1
        self.library.collections = [{**value, 'trackCount': counts[value['id']]}
                                    for value in self.library.collections if value['id'] in counts]
        self.library.errors = []

    def _scan(self, identifier, roots, cancel):
        def progress(values):
            with self.lock:
                if cancel.is_set() or identifier != self.scan_id:
                    raise ScanCancelled('Scan replaced by a newer folder selection')
                self.scan_jobs[identifier].update(values, status='running')
        try:
            progress(dict(phase='discovering', message='Looking for music collections'))
            self.library.scan(progress=progress, cancel=cancel, roots=roots)
            # Cache I/O has its own lock and cannot hold up generation/status.
            with self.index_lock:
                snapshot = self.library.snapshot()
                with self.lock:
                    if cancel.is_set() or identifier != self.scan_id:
                        return
                write_json(self.data_dir / 'library-index.json', snapshot)
            with self.lock:
                if cancel.is_set() or identifier != self.scan_id:
                    return
                count, albums = len(self.library.tracks), len(self.library.collections)
                self.scan_jobs[identifier].update(status='complete', phase='complete', progress=1,
                    trackCount=count, collectionCount=albums, warnings=list(self.library.errors),
                    message=f'Scan complete: {count} tracks in {albums} collections')
        except ScanCancelled:
            with self.lock:
                self.scan_jobs[identifier].update(status='cancelled', message='Scan replaced by an updated folder selection')
        except Exception as exc:
            with self.lock:
                self.scan_jobs[identifier].update(status='error', phase='error', error=str(exc), message=str(exc))

    def variants(self, options: dict):
        track = self.library.get(options.get('trackId', [''])[0])
        duration = float(options.get('duration', [60])[0])
        bpm = float(options.get('tempo', [track['bpm']])[0] or track['bpm'])
        ratio = bpm / track['bpm'] if track['bpm'] else 1
        if not np.isfinite(duration) or not 4 <= duration <= 7200 or not .8 - 1e-8 <= ratio <= 1.2 + 1e-8:
            raise ValueError('Choose a valid clip length and tempo.')
        available = variation_availability(track, duration * ratio)
        return dict(variations=[{**v, 'available': available.get(v['id'], {}).get('available', False),
                                 'reason': available.get(v['id'], {}).get('reason', '')} for v in track['variations']])

    def add_root(self, value: str):
        return self.start_scan('add', value)

    def remove_root(self, value: str):
        return self.start_scan('remove', value)

    def preview(self, track_id: str, variation: str = 'auto'):
        track = self.library.get(track_id)
        header = track['container_header']
        start = 0
        if variation != 'auto':
            selected = next((v for v in track['variations'] if str(v['id']) == variation), None)
            if selected is None:
                raise ValueError('Choose one of this track’s variations.')
            start = track['blocks'][selected['start']]['start']
        frames = min(header['frames'] - start, 30 * header['sample_rate'])
        signature = hashlib.sha256((track['source_path'] + json.dumps(header, sort_keys=True) + f':excerpt-v2:{start}').encode()).hexdigest()[:24]
        output = self.data_dir / 'previews' / f'{signature}.wav'
        with self.preview_lock:
            if output.is_file():
                return output
            output.parent.mkdir(exist_ok=True)
            with Path(track['source_path']).open('rb') as source:
                source.seek(header['data_offset'] + start * header['block_align'])
                raw = source.read(frames * header['block_align'])
            if len(raw) != frames * header['block_align']:
                raise ValueError('The track preview could not be read. Rescan the folder and try again.')
            pcm = np.frombuffer(raw, '<i2').reshape(-1, header['channels']).copy()
            # Audition the recording directly; a brief fade avoids a cut-off click.
            fade = min(round(header['sample_rate'] * .03), len(pcm))
            pcm[-fade:] = np.rint(pcm[-fade:].astype(float) * np.linspace(1, 0, fade)[:, None]).astype(np.int16)
            encode_wav(pcm, header['sample_rate'], output)
            return output

    def update_settings(self, update: dict):
        allowed = ('favorites', 'notes', 'defaultDuration', 'exportFormat', 'bitrate')
        with self.lock:
            for key in allowed:
                if key in update:
                    self.settings[key] = update[key]
            write_json(self.data_dir / 'settings.json', self.settings)
            return self.settings.copy()

    def generate(self, request: dict):
        track = self.library.get(request.get('trackId'))
        duration = float(request.get('duration', 60))
        if not np.isfinite(duration) or not 4 <= duration <= 7200:
            raise ValueError('Choose a clip length between 4 seconds and 2 hours.')
        original_bpm = float(track['bpm'])
        tempo = float(request.get('tempo') or original_bpm)
        if not np.isfinite(tempo) or (original_bpm and not original_bpm * .8 - .01 <= tempo <= original_bpm * 1.2 + .01):
            raise ValueError('Choose a tempo between 80% and 120% of the original.')
        pitch = int(request.get('pitch', 0))
        if not -12 <= pitch <= 12:
            raise ValueError('Choose a pitch shift between -12 and +12 semitones.')
        loop = bool(request.get('loop', False))
        if loop and not self.features['loop']:
            raise ValueError('Looping exports are not available yet. Generate a clip with a musical ending instead.')
        variation = str(request.get('variation', 'auto'))
        if variation != 'auto' and variation not in [v['id'] for v in track['variations']] and variation not in [v['name'] for v in track['variations']]:
            raise ValueError('Choose one of this track’s variations.')
        normalized = dict(trackId=track['id'], duration=duration, variation=variation,
                          tempo=tempo, pitch=pitch, loop=loop)
        jid = uuid.uuid4().hex
        with self.lock:
            self.jobs[jid] = dict(id=jid, status='queued', progress=0, message='Queued for generation')
        self.pool.submit(self._render, jid, track, normalized)
        return dict(id=jid, status='queued')

    def _update_job(self, jid: str, **values):
        with self.lock:
            self.jobs[jid].update(values)

    def _render(self, jid: str, track: dict, request: dict):
        try:
            self._update_job(jid, status='running', progress=.02, message='Choosing musical sections')
            ratio = request['tempo'] / track['bpm'] if track['bpm'] else 1

            def progress(value):
                p = min(1, max(0, float(value)))
                self._update_job(jid, progress=.03 + p * .75,
                                 message='Arranging and smoothing the music' if p < .9 else 'Fitting the musical ending')

            result = render(track, request['duration'] * ratio, variation=request['variation'], progress=progress)
            pcm, rate = result['pcm'], result['sample_rate']
            sections = result['sections']
            if abs(ratio - 1) > 1e-9 or request['pitch']:
                self._update_job(jid, progress=.82, message='Applying tempo and pitch')
                original_frames = len(pcm)
                pcm, rate = process_audio(pcm, rate, tempo_ratio=ratio,
                                          pitch_semitones=request['pitch'], target_frames=round(request['duration'] * rate))
                scale = len(pcm) / original_frames
                sections = [{**s, 'start': s['start'] * scale, 'duration': s['duration'] * scale} for s in sections]
            expected = round(request['duration'] * rate)
            if len(pcm) != expected:
                raise RuntimeError('The generated music did not match the requested duration.')
            if not np.isfinite(pcm).all():
                raise RuntimeError('The generated audio contains invalid samples.')
            self._update_job(jid, progress=.9, message='Saving audio and preview')
            directory = self.clips_dir / jid
            directory.mkdir(exist_ok=True)
            encode_wav(pcm, rate, directory / 'audio.wav')
            scale = 32768.0 if np.issubdtype(pcm.dtype, np.integer) else 1.0
            waveform = [round(float(np.max(np.abs(chunk.astype(np.float32)))) / scale, 4)
                        for chunk in np.array_split(pcm, min(400, len(pcm))) if len(chunk)]
            chosen = next((v['name'] for v in track['variations'] if v['id'] == request['variation'] or v['name'] == request['variation']), 'Auto arrangement')
            diagnostics = {**result.get('diagnostics', {}), 'tempoRatio': ratio, 'pitchSemitones': request['pitch'], 'verifiedFrames': expected}
            diagnostics['baseArrangementDuration'] = diagnostics.get('duration', result['pcm'].shape[0] / result['sample_rate'])
            diagnostics['baseArrangementFrames'] = diagnostics.get('output_frames', result['pcm'].shape[0])
            diagnostics['duration'] = len(pcm) / rate
            diagnostics['output_frames'] = len(pcm)
            fit_ratio = float(diagnostics.get('fit_ratio', 1))
            diagnostics['timingAdjustmentPercent'] = (fit_ratio - 1) * 100
            estimated_tempo = request['tempo'] * fit_ratio
            clip = dict(id=jid, title=track['title'], trackId=track['id'], album=track['album'], duration=len(pcm) / rate,
                        variation=chosen, variationId=request['variation'], tempo=request['tempo'], pitch=request['pitch'], loop=request['loop'],
                        sampleRate=rate, channels=pcm.shape[1], estimatedTempo=estimated_tempo, waveform=waveform, sections=sections,
                        playbackUrl=f'/api/clips/{jid}/audio.wav', diagnostics=diagnostics,
                        request=request, createdAt=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
            write_json(directory / 'clip.json', clip)
            with self.lock:
                self.clips[jid] = clip
            self._update_job(jid, status='complete', progress=1, message='Clip ready', clip=clip)
        except Exception as exc:
            traceback.print_exc()
            self._update_job(jid, status='error', progress=1, message='Generation failed', error=str(exc))

    def export(self, clip_id: str, options: dict) -> tuple[Path, str]:
        with self.export_lock:
            return self._export(clip_id, options)

    def _export(self, clip_id: str, options: dict) -> tuple[Path, str]:
        clip = self.clips.get(clip_id)
        if not clip:
            raise ValueError('Generate a clip before exporting it.')
        fmt = options.get('format', ['wav'])[0].lower()
        directory = self.clips_dir / clip_id
        original = directory / 'audio.wav'
        if fmt == 'mp3':
            bitrate = int(options.get('bitrate', [320])[0])
            if bitrate not in (128, 192, 256, 320):
                raise ValueError('Choose a supported MP3 quality.')
            output = directory / f'audio-{bitrate}.mp3'
            if not output.is_file():
                encode_mp3(original, output, bitrate=bitrate, metadata={'title': clip['title'], 'album': clip['album']})
        elif fmt == 'wav':
            rate = options.get('rate', ['original'])[0]
            depth = int(options.get('bitDepth', [16])[0])
            if depth not in (16, 24, 32):
                raise ValueError('Choose a supported WAV bit depth.')
            if rate == 'original' and depth == 16:
                output = original
            else:
                target_rate = clip['sampleRate'] if rate == 'original' else int(rate)
                if target_rate not in (22050, 32000, 44100, 48000, 96000):
                    raise ValueError('Choose a supported WAV quality.')
                output = directory / f'audio-{target_rate}-{depth}.wav'
                if not output.is_file():
                    from .engine import read_audio
                    pcm, original_rate = read_audio(original)
                    encode_wav(pcm, original_rate, output, rate=target_rate, bit_depth=depth)
        else:
            raise ValueError('Choose WAV or MP3.')
        filename = options.get('filename', [f"{clip['title']}_{clip['duration']:g}s.{fmt}"])[0]
        filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', filename)
        if not filename.lower().endswith('.' + fmt):
            filename = str(Path(filename).stem) + '.' + fmt
        return output, filename


class Handler(BaseHTTPRequestHandler):
    server_version = 'MusicCrafter/1.0'

    @property
    def app(self) -> MusicCrafter:
        return self.server.app

    def log_message(self, fmt, *args):
        if self.path.startswith('/api/jobs/'):
            return
        print(f'{self.client_address[0]} {fmt % args}', flush=True)

    def json(self, value, status=200):
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def body(self):
        length = int(self.headers.get('Content-Length', 0))
        return json.loads(self.rfile.read(length)) if length else {}

    def file(self, path: Path, filename: str | None = None):
        size = path.stat().st_size
        start, end, status = 0, size - 1, 200
        byte_range = self.headers.get('Range', '')
        if byte_range:
            match = re.fullmatch(r'bytes=(\d*)-(\d*)', byte_range)
            if match:
                lo, hi = match.groups()
                if not lo:
                    start = max(0, size - int(hi))
                else:
                    start = int(lo)
                    end = min(end, int(hi)) if hi else end
                if start >= size or end < start:
                    self.send_response(416)
                    self.send_header('Content-Range', f'bytes */{size}')
                    self.end_headers()
                    return
                status = 206
        self.send_response(status)
        self.send_header('Content-Type', mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
        self.send_header('Content-Length', str(end - start + 1))
        self.send_header('Accept-Ranges', 'bytes')
        if status == 206:
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        if filename:
            from urllib.parse import quote
            self.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + quote(filename))
        self.end_headers()
        try:
            with path.open('rb') as f:
                f.seek(start)
                remaining = end - start + 1
                while remaining:
                    chunk = f.read(min(256 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def do_GET(self):
        url = urlsplit(self.path)
        path = unquote(url.path)
        try:
            if path in ('/api/health', '/health'):
                self.json(dict(status='ok', app='Music Crafter', tracks=len(self.app.library.tracks), capabilities=self.app.features))
            elif path == '/api/library':
                self.json(self.app.catalog())
            elif path == '/api/scan':
                self.json(self.app.scan_status())
            elif path.startswith('/api/scans/'):
                self.json(self.app.scan_status(path.split('/')[-1]))
            elif path.startswith('/api/tracks/') and path.endswith('/preview.wav'):
                self.file(self.app.preview(path.split('/')[3], parse_qs(url.query).get('variation', ['auto'])[0]))
            elif path == '/api/variations':
                self.json(self.app.variants(parse_qs(url.query)))
            elif path == '/api/settings':
                self.json(self.app.settings)
            elif path == '/api/clips':
                self.json(dict(clips=sorted(self.app.clips.values(), key=lambda c: c['createdAt'], reverse=True)))
            elif path.startswith('/api/jobs/'):
                with self.app.lock:
                    job = self.app.jobs.get(path.split('/')[-1])
                    self.json(job or dict(error='Generation job not found'), 200 if job else 404)
            elif path.startswith('/api/clips/'):
                pieces = path.split('/')
                cid = pieces[3]
                if cid not in self.app.clips:
                    self.json(dict(error='Clip not found'), 404)
                elif pieces[-1] == 'audio.wav':
                    self.file(self.app.clips_dir / cid / 'audio.wav')
                elif pieces[-1] == 'export':
                    output, filename = self.app.export(cid, parse_qs(url.query))
                    self.file(output, filename)
                else:
                    self.json(self.app.clips[cid])
            else:
                relative = 'index.html' if path == '/' else path.lstrip('/')
                file = (STATIC / relative).resolve()
                if file.is_relative_to(STATIC.resolve()) and file.is_file():
                    self.file(file)
                else:
                    self.json(dict(error='Page not found'), 404)
        except (ValueError, OSError) as exc:
            self.json(dict(error=str(exc)), 400)
        except Exception as exc:
            traceback.print_exc()
            self.json(dict(error=str(exc)), 500)

    def do_POST(self):
        try:
            path = urlsplit(self.path).path
            data = self.body()
            if path == '/api/generate':
                self.json(self.app.generate(data), 202)
            elif path == '/api/roots':
                self.json(self.app.add_root(str(data.get('path', ''))), 202)
            elif path == '/api/roots/remove':
                self.json(self.app.remove_root(str(data.get('path', ''))), 202)
            elif path == '/api/rescan':
                self.json(self.app.start_scan(), 202)
            else:
                self.json(dict(error='Operation not found'), 404)
        except (ValueError, OSError) as exc:
            self.json(dict(error=str(exc)), 400)

    def do_PATCH(self):
        try:
            if urlsplit(self.path).path == '/api/settings':
                self.json(self.app.update_settings(self.body()))
            else:
                self.json(dict(error='Operation not found'), 404)
        except (ValueError, OSError) as exc:
            self.json(dict(error=str(exc)), 400)


def main():
    parser = argparse.ArgumentParser(description='Music Crafter local web application')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8768)
    args = parser.parse_args()
    app = MusicCrafter()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.app = app
    print(f'Music Crafter ready at http://{args.host}:{args.port}/ — {len(app.library.tracks)} tracks', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if app.scan_cancel:
            app.scan_cancel.set()
        app.scan_pool.shutdown(wait=False, cancel_futures=True)
        app.pool.shutdown(wait=False, cancel_futures=True)


if __name__ == '__main__':
    main()
