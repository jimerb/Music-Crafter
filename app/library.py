"""Read folder collections and their authored SmartSound block maps."""
from __future__ import annotations

from pathlib import Path
import copy
import hashlib
import heapq
import json
import math
import os
import re
import sqlite3
import struct
import stat
import threading
import xml.etree.ElementTree as ET

PROJECT = Path(__file__).resolve().parents[1]
MASK = bytes.fromhex('fa04fceaf9fbf2f6ff0efb0a01f9f0f2f1faf7fb07f403fafdfdf708f117f6e718ea3e0825e7fd1af4fbf3e6eafff7ebf6fafbf2fdf01c0213f504fc09e7f6fff708f907f60304fe0310120913fa360b0ce2f00cee1cef02efe8f7f6f1f4f50a031117191a0f0312eb1615ff16170125f20edf22e9f1f7fff10a060ef30cec23')


def audio_header(path: Path) -> dict:
    """SDSS has the same chunk layout as PCM RIFF; honor actual header rates."""
    with path.open('rb') as f:
        header = f.read(12)
        if len(header) != 12 or header[:4] not in (b'RIFF', b'SDSS') or header[8:] != b'WAVE':
            raise ValueError(f'Unsupported sound container: {path.name}')
        fmt = None
        payload = None
        while True:
            chunk = f.read(8)
            if len(chunk) != 8:
                break
            tag, size = struct.unpack('<4sI', chunk)
            position = f.tell()
            if tag == b'fmt ':
                b = f.read(min(size, 40))
                if len(b) < 16:
                    raise ValueError('Incomplete sound format header')
                encoding, channels, rate, byte_rate, align, bits = struct.unpack_from('<HHIIHH', b)
                if encoding != 1 or bits != 16 or channels not in (1, 2) or align != channels * 2:
                    raise ValueError(f'Unsupported PCM format in {path.name}')
                fmt = dict(sample_rate=rate, channels=channels, bits=bits, block_align=align)
            elif tag == b'data':
                payload = dict(data_offset=position, data_bytes=size)
            if fmt and payload:
                break
            f.seek(position + size + (size & 1))
    if not fmt or not payload:
        raise ValueError(f'Missing sound header or audio in {path.name}')
    if payload['data_offset'] + payload['data_bytes'] > path.stat().st_size:
        raise ValueError(f'Truncated sound file: {path.name}')
    frames = payload['data_bytes'] // fmt['block_align']
    return {**fmt, **payload, 'frames': frames, 'duration': frames / fmt['sample_rate']}


def parse_blocks(path: Path) -> dict[str, dict]:
    raw = path.read_bytes()
    if raw.lstrip(b'\xef\xbb\xbf \r\n\t').startswith(b'<'):
        tracks = {}
        for cue in ET.fromstring(raw).iter('Cue'):
            blocking = cue.find('Blocking')
            if blocking is None:
                continue
            nodes = blocking.findall('B')
            blocks = []
            for index, node in enumerate(nodes):
                flags = int(node.findtext('F', '0'))
                connections = sum((int(c.text or '0') & 0xffffffff) << (32 * i)
                                  for i, c in enumerate(node.findall('C')))
                block = dict(index=index, name=node.get('n', ''),
                             group='' if flags & (1 << 30) else '*',
                             offset_units=int(node.get('S')), length_units=int(node.get('L')),
                             successors=[i for i in range(len(nodes)) if connections & (1 << i)], flags=flags)
                if node.get('SD') is not None and node.get('LD') is not None:
                    block.update(start_seconds=float(node.get('SD')), length_seconds=float(node.get('LD')))
                blocks.append(block)
            tracks[cue.get('name')] = _validate_blocks(blocks, int(blocking.get('slop', '44100')))
        if not tracks:
            raise ValueError('No arrangement maps found in XML')
        return tracks
    pos = 0
    tracks = {}
    while pos < len(raw):
        if pos + 36 > len(raw):
            raise ValueError(f'Incomplete block header in {path.name}')
        name = raw[pos:pos + 24].split(b'\0')[0].decode('latin-1')
        # The count is a 16-bit field followed by two unspecified padding bytes.
        # Many discs left those bytes nonzero; they are not part of the count.
        reference_rate, reserved, count = struct.unpack_from('<IIH', raw, pos + 24)
        end = pos + 36 + count * 132
        if count < 1 or count > 224 or end > len(raw):
            raise ValueError(f'Unsupported block map for {name}')
        blocks = []
        for index in range(count):
            enc = raw[pos + 36 + index * 132:pos + 36 + (index + 1) * 132]
            decoded = bytes(byte ^ MASK[(index + i) % 128] for i, byte in enumerate(enc))
            offset, length = struct.unpack_from('<II', decoded)
            connections = int.from_bytes(decoded[88:116], 'little')
            flags = int.from_bytes(decoded[116:120], 'little')
            blocks.append(dict(index=index, name=decoded[8:24].split(b'\0')[0].decode('latin-1'),
                group=decoded[24:88].split(b'\0')[0].decode('latin-1'), offset_units=offset,
                length_units=length, successors=[i for i in range(count) if connections & (1 << i)], flags=flags))
        tracks[name] = _validate_blocks(blocks, reference_rate)
        pos = end
    return tracks


def _validate_blocks(blocks, reference_rate):
    if not blocks or any(b['offset_units'] < 0 or b['length_units'] <= 0 for b in blocks):
        raise ValueError('Invalid arrangement section bounds')
    # Authored gaps, lead-ins and rounding overlaps are legal. Preserve each
    # explicit start instead of assuming the lengths partition the recording.
    return dict(reference_rate=reference_rate, blocks=blocks,
                total_units=max(b['offset_units'] + b['length_units'] for b in blocks))


def minimum_duration(blocks: list[dict], start: int, sample_rate: int) -> float:
    todo = [(blocks[start]['length'], start)]
    best = {start: blocks[start]['length']}
    while todo:
        duration, index = heapq.heappop(todo)
        if duration != best[index]:
            continue
        if blocks[index]['flags'] & (1 << 31):
            return duration / sample_rate
        for nxt in blocks[index]['successors']:
            length = duration + blocks[nxt]['length']
            if length < best.get(nxt, float('inf')):
                best[nxt] = length
                heapq.heappush(todo, (length, nxt))
    return float('inf')


class Catalog:
    def __init__(self):
        db = PROJECT / 'analysis/metadata_sources/Master.ssdb'
        self.db = sqlite3.connect(db.as_uri() + '?mode=ro', uri=True) if db.is_file() else None
        if self.db:
            self.db.row_factory = sqlite3.Row
        snapshot = PROJECT / 'analysis/output/light_jazzy_ux_metadata.json'
        self.legacy = {t['CueName']: t for t in json.loads(snapshot.read_text())['tracks']} if snapshot.is_file() else {}

    def close(self):
        if self.db:
            self.db.close()

    def album(self, stem: str) -> dict:
        if self.db:
            result = self.db.execute('SELECT * FROM Libs WHERE VolName=? COLLATE NOCASE OR BlockingFilename=? COLLATE NOCASE OR LibName=? COLLATE NOCASE LIMIT 1', (stem, stem, stem)).fetchone()
            if result:
                return dict(result)
        return dict(LibID=None, LibName=stem, Company='', Description='')

    def cue(self, title: str, lib_id) -> dict:
        if not self.db:
            return self.legacy.get(title, {})
        result = self.db.execute('SELECT c.* FROM Cues c JOIN Links l ON l.LinkCueID=c.CueID WHERE c.CueName=? COLLATE NOCASE AND l.LinkLibID=? LIMIT 1', (title, lib_id)).fetchone()
        if not result:
            result = self.db.execute('SELECT * FROM Cues WHERE CueName=? COLLATE NOCASE LIMIT 1', (title,)).fetchone()
        if not result:
            return {}
        cue = dict(result)
        tags = {}
        for word in self.db.execute('SELECT w.Type,w.Word FROM Words w JOIN WordLinks l ON l.LinkWordID=w.WordID WHERE l.LinkCueID=? ORDER BY w.Type,w.Word', (cue['CueID'],)):
            tags.setdefault(word['Type'], []).append(word['Word'])
        cue['tags'] = tags
        cue['credits'] = [dict(x) for x in self.db.execute('SELECT c.FirstName,c.LastName,c.Type,l.Afil,l.Percent FROM Contribs c JOIN ContribLinks l ON l.LinkContribID=c.ContribID WHERE l.LinkCueID=?', (cue['CueID'],))]
        if title in self.legacy:
            cue['energy'] = self.legacy[title]['screenshot_intensity_plus_count']
        return cue


class ScanCancelled(Exception):
    """The pending discovery/index was discarded without changing the catalog."""


def _check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise ScanCancelled('Library scan cancelled. The previous catalog is still available.')


def _path_key(path):
    # Lexical normalization avoids contacting a network share merely to resolve
    # every discovered filename. Discovery never follows directory links.
    return os.path.normcase(os.path.abspath(os.fspath(path))).casefold()


class Library:
    def __init__(self, roots: list[str], scan: bool = True):
        self.lock = threading.RLock()
        self.roots = list(roots)
        self.tracks: dict[str, dict] = {}
        self.collections: list[dict] = []
        self.errors: list[str] = []
        if scan:
            self.scan()

    def scan(self, progress=None, cancel=None, roots=None):
        """Build off-lock and atomically replace the completed index.

        Supplied roots can bootstrap a subset without changing configured roots.
        Callbacks contain phase, message, currentPath, foldersScanned, mapsFound,
        completed, total and progress (None while discovery has no known total).
        """
        with self.lock:
            configured_roots = list(self.roots)
        scan_roots = list(configured_roots if roots is None else roots)
        tracks = {}
        collections = []
        errors = []
        seen_sources = set()
        discovered_maps = {}
        discovered_audio = {}
        visited_dirs = set()
        readable_dirs = set()
        unavailable_roots = []
        folders_scanned = 0
        status = dict(phase='discovering', message='Looking for music collections', currentPath='',
                      foldersScanned=0, mapsFound=0, completed=0, total=0, progress=None)

        def report(**changes):
            status.update(changes)
            if progress:
                progress(dict(status))

        skip_dirs = {'.git', '.snapshot', '.snapshots', '@recently-snapshot', '@recycle', '$recycle.bin'}
        _check_cancel(cancel)
        report()
        # A single walk collects maps and sound names together. The former
        # per-map rglob repeated the same large NAS tree for every collection.
        for raw_root in scan_roots:
            _check_cancel(cancel)
            root = Path(os.path.abspath(os.path.expanduser(os.fspath(raw_root))))
            root_key = _path_key(root)
            if root_key in visited_dirs:
                if root_key not in readable_dirs:
                    unavailable_roots.append(str(root))
                continue
            report(currentPath=str(root), message=f'Looking for collections in {root.name or root}')
            _check_cancel(cancel)
            try:
                linked_root = root.is_symlink()
            except OSError as exc:
                visited_dirs.add(root_key)
                unavailable_roots.append(str(root))
                errors.append(f'Folder is unavailable: {root}: {exc}')
                continue
            if linked_root:
                visited_dirs.add(root_key)
                unavailable_roots.append(str(root))
                errors.append(f'Skipped linked music folder: {root}')
                continue
            pending = [root]
            while pending:
                _check_cancel(cancel)
                directory = pending.pop()
                key = _path_key(directory)
                if key in visited_dirs:
                    continue
                visited_dirs.add(key)
                report(message=f'Looking for collections in {directory.name or directory}',
                       currentPath=str(directory), foldersScanned=folders_scanned,
                       mapsFound=len(discovered_maps))
                children = []
                try:
                    _check_cancel(cancel)
                    with os.scandir(directory) as entries:
                        readable_dirs.add(key)
                        for entry in entries:
                            _check_cancel(cancel)
                            try:
                                if entry.is_symlink():
                                    continue
                                _check_cancel(cancel)
                                if entry.is_dir(follow_symlinks=False):
                                    if entry.name.casefold() in skip_dirs:
                                        continue
                                    # Windows junctions can create recursion
                                    # loops too; their attributes are cached by
                                    # the directory enumeration on Windows.
                                    _check_cancel(cancel)
                                    attributes = getattr(entry.stat(follow_symlinks=False), 'st_file_attributes', 0)
                                    if attributes & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0):
                                        continue
                                    children.append(Path(entry.path))
                                elif entry.name.casefold().endswith(('.blk', '.blx', '.sds')):
                                    _check_cancel(cancel)
                                    if entry.is_file(follow_symlinks=False):
                                        path = Path(entry.path)
                                        destination = discovered_audio if path.suffix.casefold() == '.sds' else discovered_maps
                                        destination.setdefault(_path_key(path), path)
                            except OSError as exc:
                                errors.append(f'Could not inspect {entry.path}: {exc}')
                except OSError as exc:
                    errors.append(f'Folder is unavailable: {directory}: {exc}')
                    if key == root_key:
                        unavailable_roots.append(str(root))
                folders_scanned += 1
                report(foldersScanned=folders_scanned, mapsFound=len(discovered_maps))
                pending.extend(sorted(children, key=lambda p: str(p).casefold(), reverse=True))
            _check_cancel(cancel)
        if scan_roots and not any(_path_key(Path(os.path.abspath(os.path.expanduser(os.fspath(p))))) in readable_dirs for p in scan_roots):
            raise OSError('Music folders are unavailable: ' + ', '.join(unavailable_roots or map(str, scan_roots)))

        maps = sorted(discovered_maps.values(), key=lambda p: (str(p.parent).casefold(), p.suffix != '.blx', str(p).casefold()))
        parent_for = {}
        audio_indexes = {}
        for blk in maps:
            _check_cancel(cancel)
            parent = blk.parent.parent if blk.parent.name.casefold() in ('library enablers', 'encoding', 'libraries') else blk.parent
            parent_for[_path_key(blk)] = parent
            audio_indexes.setdefault(_path_key(parent), {})
        # Assign each discovered sound file to collection ancestors once. An
        # index preserves duplicate names in 22k/44k or copied subfolders.
        shared_audio = {}
        for source in discovered_audio.values():
            _check_cancel(cancel)
            name = re.sub(r'\s+\d+k$', '', source.stem, flags=re.IGNORECASE).casefold()
            shared_audio.setdefault(name, []).append(source)
            for ancestor in source.parents:
                index = audio_indexes.get(_path_key(ancestor))
                if index is not None:
                    index.setdefault(name, []).append(source)

        report(phase='indexing', message='Reading collection information',
               completed=0, total=len(maps), progress=0.0)
        _check_cancel(cancel)
        catalog = Catalog()
        try:
            for map_index, blk in enumerate(maps):
                _check_cancel(cancel)
                report(message=f'Reading {blk.stem}', currentPath=str(blk),
                       completed=map_index, progress=map_index/max(1, len(maps)))
                try:
                    base = re.sub(r'\s*[-_]?\s*\d+k$', '', blk.stem, flags=re.IGNORECASE)
                    parent = parent_for[_path_key(blk)]
                    sdi = blk.with_suffix('.sdi')
                    descriptions = {}
                    categories = {}
                    _check_cancel(cancel)
                    if sdi.is_file():
                        _check_cancel(cancel)
                        for line in sdi.read_text(encoding='latin-1').splitlines():
                            _check_cancel(cancel)
                            values = line.split('\t')
                            if len(values) > 2 and values[0] == 'CUES':
                                descriptions[values[1]] = values[2]
                            elif len(values) > 3 and values[0] == 'CATA':
                                categories.setdefault(values[3], []).append(values[2])
                    album = catalog.album(base)
                    cid = str(album['LibID']) if album['LibID'] else hashlib.sha256(str(parent).encode()).hexdigest()[:12]
                    _check_cancel(cancel)
                    block_map = parse_blocks(blk)
                    _check_cancel(cancel)
                    audio_files = audio_indexes[_path_key(parent)]
                    collection_tracks = []
                    for track_index, (title, encoded) in enumerate(block_map.items()):
                        _check_cancel(cancel)
                        report(message=f'Reading {title}', currentPath=str(blk),
                               progress=(map_index+track_index/max(1, len(block_map)))/max(1, len(maps)))
                        candidates = list(audio_files.get(title.casefold(), []))
                        if not candidates:
                            # Installed libraries may keep their recordings in a
                            # shared sound folder, separate from the album map.
                            candidates = list(shared_audio.get(title.casefold(), []))
                        if not candidates:
                            errors.append(f'{base}: sound file missing for {title}')
                            continue
                        candidates.sort(key=lambda p: ('44k' not in str(p).lower(), str(p)))
                        if any(_path_key(p) in seen_sources for p in candidates):
                            continue
                        source = None
                        for candidate in candidates:
                            _check_cancel(cancel)
                            try:
                                report(currentPath=str(candidate))
                                header = audio_header(candidate)
                                source = candidate
                                break
                            except (ValueError, OSError, struct.error) as exc:
                                errors.append(f'{candidate.name}: {exc}')
                        if source is None:
                            continue
                        _check_cancel(cancel)
                        ratio = header['frames'] / encoded['total_units']
                        blocks = []
                        for raw in encoded['blocks']:
                            if 'start_seconds' in raw:
                                start = max(0, round(raw['start_seconds'] * header['sample_rate']))
                                finish = round((raw['start_seconds'] + raw['length_seconds']) * header['sample_rate'])
                            else:
                                start = round(raw['offset_units'] * ratio)
                                finish = round((raw['offset_units'] + raw['length_units']) * ratio)
                            # Different 44.1/48k masters can differ by a few
                            # trailing samples (observed <2 ms). Keep authored
                            # timing, clipping only that tiny final overhang.
                            if finish > header['frames'] + round(header['sample_rate'] * .005) or start < 0 or finish <= start:
                                raise ValueError(f'{title}: arrangement section exceeds recording')
                            finish = min(finish, header['frames'])
                            blocks.append({**raw, 'start': start, 'length': finish - start})
                        meta = catalog.cue(title, album['LibID'])
                        ending_mode = 'authored'
                        if not any(b['flags'] & (1 << 31) for b in blocks):
                            # Newer maps can express their ending as a terminal
                            # section without the legacy high-bit marker.
                            terminals = [b for b in blocks if not b['successors']]
                            if meta.get('Type') == 'SFX':
                                ending_mode = 'fade'
                            elif not terminals:
                                errors.append(f'{title}: no authored ending found')
                                continue
                            for b in terminals:
                                b['endingMarker'] = 'terminal section'
                                b['flags'] |= 1 << 31
                        uid = meta.get('CueID') or hashlib.sha256(title.encode()).hexdigest()[:12]
                        tid = f'{cid}:{uid}'
                        if tid in tracks:
                            continue
                        tags = meta.get('tags', {})
                        intensity = meta.get('Intensity') or 500
                        energy = meta.get('energy', min(4, max(1, 1 + sum(intensity >= n for n in (450, 600, 670)))))
                        try:
                            bpm = float(meta.get('Tempo') or 0)
                        except ValueError:
                            bpm = 0
                        variations = []
                        for b in blocks:
                            _check_cancel(cancel)
                            if b['group'] != '*' and not b['flags'] & (1 << 31):
                                minimum = 4 if ending_mode == 'fade' else minimum_duration(blocks, b['index'], header['sample_rate'])
                                if minimum != float('inf'):
                                    variations.append(dict(id=str(b['index']), name=b['name'], start=b['index'], minDuration=round(minimum, 3)))
                        track = dict(id=tid, cueId=uid, title=title, collectionId=cid, album=album['LibName'],
                            description=meta.get('Description') or descriptions.get(title, ''), bpm=bpm,
                            energy=energy, intensity=intensity, instruments=tags.get('Instrument', []),
                            moods=tags.get('KeyMusic', []), style=tags.get('Style', categories.get(title, [])),
                            credits=meta.get('credits', []), source=meta.get('Company', album.get('Company', '')),
                            variations=variations, duration=header['duration'], sampleRate=header['sample_rate'],
                            channels=header['channels'], sample_rate=header['sample_rate'],
                            source_path=str(source), blocks=blocks, container_header=header,
                            endingMode=ending_mode, kind=meta.get('Type', 'Music'))
                        from .engine import variation_ranges
                        ranges = variation_ranges(track)
                        for variant in variations:
                            limits = ranges.get(variant['id'])
                            if limits:
                                variant['minDuration'] = math.ceil(limits['min'] * 1000) / 1000
                                variant['maxDuration'] = math.floor(limits['max'] * 1000) / 1000 if limits['max'] is not None else None
                        tracks[tid] = track
                        collection_tracks.append(tid)
                        seen_sources.add(_path_key(source))
                    if collection_tracks:
                        existing = next((x for x in collections if x['id'] == cid), None)
                        if existing:
                            existing['trackCount'] += len(collection_tracks)
                        else:
                            collections.append(dict(id=cid, name=album['LibName'], trackCount=len(collection_tracks), path=str(parent), description=album.get('Description', '')))
                except (ValueError, OSError, UnicodeError, struct.error, ET.ParseError) as exc:
                    errors.append(f'{blk.name}: {exc}')
                _check_cancel(cancel)
                report(completed=map_index+1, progress=(map_index+1)/max(1, len(maps)))
        finally:
            catalog.close()
        _check_cancel(cancel)
        with self.lock:
            _check_cancel(cancel)
            if self.roots != configured_roots:
                raise ScanCancelled('Folder settings changed during the scan. The previous catalog is still available.')
            self.tracks = tracks
            self.collections = sorted(collections, key=lambda x: x['name'].casefold())
            self.errors = list(dict.fromkeys(errors))
        report(message=f'Loaded {len(tracks)} tracks from {len(collections)} collections',
               completed=len(maps), total=len(maps), progress=1.0)
        return dict(foldersScanned=folders_scanned, mapsFound=len(maps), completed=len(maps),
                    total=len(maps), tracks=len(tracks), collections=len(collections), warnings=list(dict.fromkeys(errors)))

    def snapshot(self) -> dict:
        """Copy a serializable internal catalog without accessing source files."""
        with self.lock:
            return copy.deepcopy(dict(version=1, roots=list(self.roots), tracks=self.tracks,
                                      collections=self.collections, errors=self.errors))

    def restore(self, snapshot: dict):
        """Restore a cached catalog without probing local or network music."""
        if not isinstance(snapshot, dict) or snapshot.get('version', 1) != 1:
            raise ValueError('Unsupported cached music catalog.')
        cached = copy.deepcopy(snapshot)
        if (not isinstance(cached.get('roots'), list) or not all(isinstance(p, str) for p in cached['roots'])
                or not isinstance(cached.get('tracks'), dict) or not all(isinstance(v, dict) for v in cached['tracks'].values())
                or not isinstance(cached.get('collections'), list) or not all(isinstance(c, dict) for c in cached['collections'])
                or not isinstance(cached.get('errors'), list) or not all(isinstance(e, str) for e in cached['errors'])):
            raise ValueError('The cached music catalog is incomplete.')
        with self.lock:
            self.roots = cached['roots']
            self.tracks = cached['tracks']
            self.collections = cached['collections']
            self.errors = cached['errors']

    def public(self, capabilities: dict) -> dict:
        private = ('source_path', 'blocks', 'container_header', 'sample_rate')
        with self.lock:
            tracks = [{k: v for k, v in t.items() if k not in private} for t in self.tracks.values()]
            return dict(collections=self.collections, tracks=sorted(tracks, key=lambda t: t['title'].casefold()), roots=self.roots, capabilities=capabilities, warnings=self.errors)

    def get(self, identifier) -> dict:
        with self.lock:
            track = self.tracks.get(str(identifier))
            if not track:
                track = next((t for t in self.tracks.values() if str(t['cueId']) == str(identifier)), None)
            if not track:
                raise ValueError('This track is no longer available. Rescan the music folder.')
            return track
