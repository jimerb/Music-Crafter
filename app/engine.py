"""Arrange authored SmartSound sections without cutting off musical endings.

The graph and PCM come from the user's files. Fitting whole musical sections to
an exact duration is our implementation, not a claim of Sonicfire equivalence.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import heapq
import math
import struct
import wave
from collections import deque

import numpy as np


class ArrangementError(ValueError):
    pass


@lru_cache(maxsize=24)
def _read_audio_cached(path: str, size: int, modified: int):
    # Import lazily: library scanning imports the arrangement helpers, while
    # the shared header reader needs only small seek-based metadata reads.
    from .library import audio_header

    source = Path(path)
    try:
        header = audio_header(source)
    except ValueError as error:
        raise ArrangementError(str(error)) from error
    if header["data_bytes"] % header["block_align"]:
        raise ArrangementError("The music file ends in an incomplete audio frame.")
    if header["frames"] <= 0:
        raise ArrangementError("The music file contains no audio samples.")
    # Mapping reserves an address range; it does not copy the source file into
    # RAM or into the project. Graph inspection touches boundary samples, and
    # assembly reads only the chosen sections. Windows/SMB may still read ahead.
    pcm = np.memmap(source, dtype="<i2", mode="r", offset=header["data_offset"],
                    shape=(header["frames"], header["channels"]))
    if header["channels"] == 1:
        # Both output channels share the same mapped samples. Avoid duplicating
        # an entire mono source merely to present the stereo reader contract.
        pcm = np.broadcast_to(pcm, (header["frames"], 2))
    pcm.flags.writeable = False
    return pcm, header["sample_rate"]


def read_audio(path):
    """Return a lazy read-only stereo int16 view and the actual sample rate."""
    source = Path(path).resolve()
    info = source.stat()
    return _read_audio_cached(str(source), info.st_size, info.st_mtime_ns)


def _graph(track, sample_rate):
    blocks = track["blocks"]
    if not blocks:
        raise ArrangementError("This track has no arrangement sections.")
    lengths = [int(b["length"]) for b in blocks]
    edges = [[int(j) for j in b["successors"] if 0 <= int(j) < len(blocks)] for b in blocks]
    ends = {i for i, b in enumerate(blocks) if int(b["flags"]) & (1 << 31)}
    if not ends:
        ends = {i for i, b in enumerate(blocks) if not edges[i]}
    if not ends or any(n <= 0 for n in lengths):
        raise ArrangementError("This track has no usable musical ending.")
    overlap = round(sample_rate * .005)
    previous = [[] for _ in blocks]
    for i, successors in enumerate(edges):
        for j in successors:
            # Adjacent source sections are already sample-continuous.
            previous[j].append((i, lengths[j] - (0 if j == i + 1 else overlap)))
    remaining = [math.inf] * len(blocks)
    next_shortest = [None] * len(blocks)
    queue = []
    for i in ends:
        remaining[i] = 0
        heapq.heappush(queue, (0, i))
    while queue:
        distance, j = heapq.heappop(queue)
        if distance != remaining[j]:
            continue
        for i, weight in previous[j]:
            if i in ends:
                continue
            candidate = distance + weight
            if candidate < remaining[i]:
                remaining[i] = candidate
                next_shortest[i] = j
                heapq.heappush(queue, (candidate, i))
    return lengths, edges, ends, overlap, remaining, next_shortest


def variation_minimums(track):
    """Shortest complete opening-to-ending route, in source-tempo seconds.

    The UI can disable named openings at shorter durations. Auto has a separate
    short-form arrangement which preserves the ending, and remains available.
    """
    rate = int(track["sample_rate"])
    lengths, _, _, _, remaining, _ = _graph(track, rate)
    return {str(v["id"]): math.ceil((lengths[int(v["start"])] + remaining[int(v["start"])]) / rate * 10) / 10
            for v in track["variations"] if math.isfinite(remaining[int(v["start"])])}


def _ambience_lengths(track):
    blocks = track['blocks']
    overlap = round(track['sample_rate'] * .005)
    memo = {}
    def longest(index, visiting):
        if index in visiting:
            return math.inf
        if index in memo:
            return memo[index]
        visiting = visiting | {index}
        memo[index] = blocks[index]['length'] + max((longest(j, visiting) - (0 if j == index + 1 else overlap)
                                                   for j in blocks[index]['successors']), default=0)
        return memo[index]
    # Ambience maps are small and predominantly linear, with authored loops.
    return [longest(i, set()) for i in range(len(blocks))]


def variation_ranges(track):
    """Valid named-opening lengths; a maximum of None denotes a musical loop.

    Closing passages cannot be extended indefinitely. The variation menu should
    reflect both boundaries rather than stretch a ten-second ending to minutes.
    """
    if track.get('endingMode') == 'fade':
        limits = _ambience_lengths(track)
        return {str(v['id']): {'min': 4, 'max': limits[v['start']]/track['sample_rate'] if math.isfinite(limits[v['start']]) else None}
                for v in track['variations']}
    rate = int(track["sample_rate"])
    lengths, edges, ends, overlap, remaining, _ = _graph(track, rate)
    def longest(index, visiting, memo):
        if index in ends:
            return lengths[index]
        if index in visiting:
            return math.inf
        if index in memo:
            return memo[index]
        visiting.add(index)
        usable = [j for j in edges[index] if math.isfinite(remaining[j])]
        value = max((longest(j, visiting, memo) - (0 if j == index+1 else overlap) for j in usable), default=-math.inf)
        visiting.remove(index)
        memo[index] = lengths[index]+value
        return memo[index]
    result = {}
    for variant in track.get("variations", []):
        start = int(variant["start"])
        maximum = longest(start, set(), {})
        minimum = lengths[start]+remaining[start]
        if math.isfinite(minimum):
            result[str(variant["id"])] = {"min": minimum/rate,
                                          "max": maximum/rate if math.isfinite(maximum) else None}
    return result


def _shortest_route(start, ends, next_shortest):
    path = [start]
    for _ in range(len(next_shortest) + 1):
        if path[-1] in ends:
            return tuple(path)
        j = next_shortest[path[-1]]
        if j is None:
            break
        path.append(j)
    raise ArrangementError("This variation does not connect to a musical ending.")


def _choose_path(track, pcm, rate, target, variation, progress):
    lengths, edges, ends, overlap, remaining, next_shortest = _graph(track, rate)
    blocks = track["blocks"]
    variations = track.get("variations", [])
    automatic = variation in (None, "", "auto")
    if automatic:
        # Auto may use every authored start, including short opening stings
        # which do not have a separate named group in the variation menu.
        starts = [i for i, b in enumerate(blocks) if int(b["flags"]) & (1 << 30) and i not in ends]
        starts += [int(v["start"]) for v in variations]
    else:
        selected = next((v for v in variations if str(v["id"]) == str(variation) or v["name"] == variation), None)
        if selected is None:
            raise ArrangementError("Choose one of this track's available variations.")
        starts = [int(selected["start"])]
    starts = [s for s in dict.fromkeys(starts) if math.isfinite(remaining[s])]
    if not starts:
        raise ArrangementError("No opening connects to a musical ending.")
    shortest_start = min(starts, key=lambda i: lengths[i] + remaining[i])
    shortest_frames = lengths[shortest_start] + remaining[shortest_start]
    if not automatic and target < shortest_frames - 1:
        minimum = (shortest_frames / rate)
        raise ArrangementError(f"This variation needs at least {minimum:.1f} seconds for its complete opening and ending. Choose Auto for a shorter clip.")
    if not automatic:
        bounds = variation_ranges(track)[str(selected["id"])]
        if bounds["max"] is not None and target > round(bounds["max"]*rate):
            raise ArrangementError(f"This closing variation works up to {bounds['max']:.1f} seconds. Choose Auto or another variation for a longer clip.")
    if automatic and target < shortest_frames:
        return _shortest_route(shortest_start, ends, next_shortest), True, overlap

    if target > 600 * rate:
        return _long_path(track, starts, ends, lengths, edges, overlap, remaining, next_shortest, target), False, overlap

    # Keep the final fit subtle. Search within a small musical timing window,
    # then use pitch-preserving time fitting if quiet-tail trimming cannot fit.
    window = max(round(rate * 1.2), round(target * .025))
    upper = target + window
    lower = target - window
    bucket = max(1, round(rate * .025))
    jump_cost = {}
    for i, successors in enumerate(edges):
        final_frame = int(blocks[i]["start"]) + lengths[i] - 1
        for j in successors:
            if j == i + 1:
                jump_cost[i, j] = 0.0
            else:
                jump = np.max(np.abs(pcm[final_frame].astype(np.int32) - pcm[int(blocks[j]["start"])].astype(np.int32)))
                jump_cost[i, j] = .18 + min(float(jump) / 32768, .5)
    layer = [(lengths[i], (i,), 0.0) for i in starts if lengths[i] + remaining[i] <= upper]
    best = None
    closest = None
    # Beam diversity by endpoint and elapsed time keeps long arrangements fast;
    # this is not a greedy nearest-block search, which often misses endings.
    maximum_depth = min(2200, int(upper / min(lengths)) + 2)
    for depth in range(maximum_depth):
        states = {}
        for frames, path, cost in layer:
            i = path[-1]
            if i in ends:
                distance = abs(frames-target)/rate
                fallback = (distance, cost/max(len(path), 1), frames, path)
                if closest is None or fallback < closest:
                    closest = fallback
                if lower <= frames <= upper:
                    difference = abs(frames - target) / rate
                    # Aim first for accurate musical time, then smooth/varied
                    # routes. Very short end stings have a small quality penalty.
                    fit_penalty = difference / max(target / rate, 10) * 42
                    score = cost / max(len(path), 1) + fit_penalty + (.08 if lengths[i] < rate else 0)
                    candidate = (score, frames, path)
                    if best is None or candidate < best:
                        best = candidate
                continue
            for j in edges[i]:
                nf = frames + lengths[j] - (0 if j == i + 1 else overlap)
                if nf + remaining[j] > upper:
                    continue
                repeats = path.count(j)
                nc = cost + jump_cost[i, j] + min(repeats, 8) * .15
                key = (j, round(nf / bucket))
                old = states.get(key)
                if old is None or nc < old[2]:
                    states[key] = (nf, path + (j,), nc)
        if not states:
            break
        # Retain both low-cost routes and routes close to the fitting window.
        layer = sorted(states.values(), key=lambda s: s[2] / len(s[1]) +
                       max(0, lower - s[0] - remaining[s[1][-1]]) / rate * .002)[:700]
        if progress and depth % 10 == 0:
            progress(min(.55, .08 + depth / max(maximum_depth, 1) * .5))
    if best is not None:
        return best[2], False, overlap
    if closest is not None:
        if not automatic and not .85 <= closest[2]/target <= 1.15:
            raise ArrangementError("This variation has no complete musical arrangement for that length. Choose Auto or another variation.")
        return closest[3], False, overlap
    # Rare sparse graphs can miss the timing window. Take the best complete
    # route and let duration fitting retain its ending rather than truncate it.
    if not automatic:
        raise ArrangementError("This variation has no complete musical arrangement for that length. Choose Auto or another variation.")
    return _shortest_route(shortest_start, ends, next_shortest), target < shortest_frames, overlap


def _long_path(track, starts, ends, lengths, edges, overlap, remaining, next_shortest, target):
    """Extend an authored phrase loop without a thousands-deep beam search."""
    def route(a, b):
        queue = deque([(a, (a,))]); seen = {a}
        while queue:
            node, path = queue.popleft()
            if node == b: return path
            for nxt in sorted(edges[node], key=lambda j: (j != node + 1, j)):
                if nxt not in seen and nxt not in ends and math.isfinite(remaining[nxt]):
                    seen.add(nxt); queue.append((nxt, path + (nxt,)))
        return None
    def frames(path):
        return sum(lengths[i] for i in path) - sum(overlap for a, b in zip(path, path[1:]) if b != a + 1)
    candidates = []
    for last, successors in enumerate(edges):
        if last in ends: continue
        for entry in successors:
            if entry > last or entry in ends or not math.isfinite(remaining[entry]): continue
            # Prefer the complete recorded phrase when its successive sections
            # connect, with the authored backward connection closing the loop.
            cycle = tuple(range(entry, last + 1))
            if any(b not in edges[a] or a in ends for a, b in zip(cycle, cycle[1:])):
                cycle = route(entry, last)
            if not cycle: continue
            step = frames(cycle) - (0 if entry == last + 1 else overlap)
            if step <= 0: continue
            ending = _shortest_route(entry, ends, next_shortest)
            for start in starts:
                opening = route(start, entry)
                if not opening: continue
                base = opening[:-1] + ending
                repeats = max(0, round((target - frames(base)) / step))
                if not repeats: continue
                path = opening[:-1] + cycle * repeats + ending
                distance = abs(frames(path) - target)
                # Long phrases avoid turning a tiny musical hit into a long bed.
                joins = sum(b != a + 1 for a, b in zip(path, path[1:]))
                candidates.append(((distance / target, joins / len(path), -step), path))
    if not candidates:
        raise ArrangementError('This variation has no repeatable musical phrase for that length. Choose Auto or another variation.')
    return min(candidates, key=lambda item: item[0])[1]


def variation_availability(track, duration):
    if track.get('endingMode') == 'fade':
        ranges = variation_ranges(track)
        return {key: {'available': bounds['max'] is None or duration <= bounds['max'], 'reason': 'Choose a shorter length for this ambience.'}
                for key, bounds in ranges.items()}
    """Duration-specific choice availability, including gaps in authored routes."""
    pcm, rate = read_audio(track["source_path"])
    target = round(float(duration)*rate)
    ranges = variation_ranges(track)
    answer = {}
    for variant in track.get("variations", []):
        key = str(variant["id"])
        bounds = ranges.get(key, {"min": None, "max": None})
        try:
            _choose_path(track, pcm, rate, target, key, None)
            answer[key] = {**bounds, "available": True, "reason": ""}
        except ArrangementError as error:
            answer[key] = {**bounds, "available": False, "reason": str(error)}
    return answer


def _assemble(track, pcm, rate, path, overlap):
    blocks = track["blocks"]
    frames = sum(int(blocks[i]["length"]) for i in path)
    frames -= sum(overlap for a, b in zip(path, path[1:]) if b != a + 1)
    result = np.empty((frames, 2), dtype=np.int16)
    position = 0
    sections = []
    for ordinal, index in enumerate(path):
        block = blocks[index]
        segment = pcm[int(block["start"]):int(block["start"]) + int(block["length"])]
        if len(segment) != int(block["length"]):
            raise ArrangementError("A musical section extends beyond its sound file.")
        crossfade = overlap if ordinal and index != path[ordinal - 1] + 1 else 0
        if crossfade:
            crossfade = min(crossfade, position, len(segment))
            blend = np.linspace(0, 1, crossfade, dtype=np.float64)[:, None]
            mixed = result[position-crossfade:position].astype(np.float64) * (1-blend) + segment[:crossfade] * blend
            result[position-crossfade:position] = np.rint(mixed).astype(np.int16)
        start = position-crossfade
        result[position:position + len(segment)-crossfade] = segment[crossfade:]
        position += len(segment)-crossfade
        sections.append({"name": block["name"], "start": start/rate, "duration": len(segment)/rate,
                         "block": index, "source_start": int(block["start"])/rate})
    return result[:position], sections


def _quiet_trim_allowance(pcm, rate, maximum_seconds=.8):
    """Find a measured quiet tail, keeping the attack and audible resolution."""
    maximum = min(len(pcm)-round(rate*.06), round(rate*maximum_seconds))
    hop = max(1, round(rate*.02))
    quiet = 0
    while quiet + hop <= maximum:
        window = pcm[len(pcm)-quiet-hop:len(pcm)-quiet].astype(np.float64)
        if np.sqrt(np.mean(window**2)) > 160 or np.max(np.abs(window)) > 1000:
            break
        quiet += hop
    return quiet


def _time_fit(pcm, rate, frames):
    # Imported here so PCM reading and graph inspection have no FFmpeg dependency.
    from .codecs import process_audio
    processed = process_audio(pcm, rate, tempo_ratio=len(pcm)/frames, target_frames=frames)
    if isinstance(processed, tuple):
        processed = processed[0]
    if isinstance(processed, dict):
        processed = processed["pcm"]
    output = np.asarray(processed)
    if np.issubdtype(output.dtype, np.floating):
        converted = np.empty(output.shape, dtype=np.int16)
        for start in range(0, len(output), 131072):
            converted[start:start+131072] = np.rint(np.clip(output[start:start+131072], -1, 1) * 32767).astype(np.int16)
        output = converted
    else:
        output = output.astype(np.int16)
    if len(output) != frames:
        raise ArrangementError("Audio processing did not produce the requested clip length.")
    return output


def _render_ambience(track, pcm, rate, target, variation, progress):
    """Follow authored ambience connections; fade at the requested duration."""
    selected = next((v for v in track['variations'] if str(v['id']) == str(variation)), None)
    if variation not in (None, '', 'auto') and selected is None:
        raise ArrangementError('Choose one of this track’s variations.')
    current = selected['start'] if selected else 0
    blocks = track['blocks']
    overlap = round(rate * .005)
    limits = _ambience_lengths(track)
    path = []
    frames = 0
    while frames < target:
        b = blocks[current]
        added = b['length'] - (overlap if path and current != path[-1] + 1 else 0)
        if added <= 0:
            raise ArrangementError('Ambience section is too short to join.')
        frames += added
        path.append(current)
        if frames >= target:
            break
        successors = [j for j in b['successors'] if limits[j] - (0 if j == current + 1 else overlap) >= target - frames]
        if not successors:
            raise ArrangementError('This ambience variation cannot extend to that length.')
        current = current + 1 if current + 1 in successors else successors[0]
    output, sections = _assemble(track, pcm, rate, path, overlap)
    output = output[:target].copy()
    fade = min(round(rate * .3), target // 2)
    output[:fade] = np.rint(output[:fade].astype(float) * np.linspace(0, 1, fade)[:, None]).astype(np.int16)
    output[-fade:] = np.rint(output[-fade:].astype(float) * np.linspace(1, 0, fade)[:, None]).astype(np.int16)
    sections = [{**s, 'duration': min(s['duration'], target/rate-s['start'])} for s in sections if s['start'] < target/rate]
    if progress: progress(1)
    return {'pcm': output, 'sample_rate': rate, 'sections': sections,
            'diagnostics': {'path': path, 'output_frames': target, 'duration': target/rate,
                            'fit_ratio': 1, 'ending_preserved': False,
                            'note': 'Ambient sound follows its recorded sections and fades out at your chosen length.'}}


def render(track, duration, variation="auto", tempo=None, pitch=0, loop=False, progress=None):
    """Return exact-duration stereo int16 audio plus its musical-section timeline.

    User tempo and pitch processing is owned by the server/codec layer. Render
    must be called with the duration before that user tempo adjustment. The
    engine's pitch-preserving fine fit is separate from the user's BPM choice.
    Progress callbacks receive fractions from 0 to 1.
    """
    seconds = float(duration)
    # Internal allowance accommodates 4 seconds..2 hours at 80..120% BPM.
    if not math.isfinite(seconds) or not 3.2 <= seconds <= 8640:
        raise ArrangementError("Choose a clip length between 4 seconds and 2 hours.")
    if tempo is not None or pitch:
        raise ArrangementError("Apply tempo and pitch through the audio-processing layer after arrangement.")
    pcm, rate = read_audio(track["source_path"])
    if rate != int(track["sample_rate"]):
        raise ArrangementError("The catalog sample rate does not match this music file.")
    target = round(seconds * rate)
    if progress:
        progress(.04)
    if track.get('endingMode') == 'fade':
        return _render_ambience(track, pcm, rate, target, variation, progress)
    path, short_form, overlap = _choose_path(track, pcm, rate, target, variation, progress)
    output, sections = _assemble(track, pcm, rate, path, overlap)
    source_frames = len(output)
    leading_removed = 0
    quiet_trim = 0
    if short_form and source_frames > target:
        # A flagged ending can be only the sustained decay AFTER a preceding
        # block's chord attack. Keeping the fade alone creates a weak, nearly
        # silent clip. Keep the complete shortest attack-to-ending phrase,
        # compact its measured quiet decay, then fit the whole remaining phrase.
        ending_frames = int(track["blocks"][path[-1]]["length"])
        allowance = _quiet_trim_allowance(output, rate, maximum_seconds=max(0, ending_frames/rate-.06))
        quiet_trim = min(source_frames-target, allowance)
        if quiet_trim:
            output = output[:-quiet_trim].copy()
            sections[-1]["duration"] -= quiet_trim/rate
    fit_ratio = 1.0
    delta = len(output)-target
    if delta > 0 and not short_form and delta <= _quiet_trim_allowance(output, rate):
        quiet_trim += delta
        output = output[:target].copy()
        if sections:
            sections[-1]["duration"] -= delta/rate
    elif delta:
        if progress:
            progress(.62)
        fit_ratio = len(output)/target
        output = _time_fit(output, rate, target)
        for section in sections:
            section["start"] /= fit_ratio
            section["duration"] /= fit_ratio
    else:
        output = output.copy()
    # Fades only suppress clicks at the entrance and quiet end. The recorded
    # ending remains part of the arrangement, with its full musical resolution.
    intro = min(round(rate*.005), len(output)//2)
    ending = min(round(rate*.06), len(output)//2)
    output[:intro] = np.rint(output[:intro].astype(float) * np.linspace(0,1,intro)[:,None]).astype(np.int16)
    output[-ending:] = np.rint(output[-ending:].astype(float) * np.linspace(1,0,ending)[:,None]).astype(np.int16)
    if loop:
        # A loop export keeps the authored ending and fades at both boundaries.
        # It can be replayed without a digital click; phrase-loop seam matching
        # is intentionally not claimed by this flag.
        pass
    if progress:
        progress(1.0)
    return {"pcm": output, "sample_rate": rate, "sections": sections,
            "diagnostics": {"path": list(path), "source_frames": source_frames,
                            "output_frames": len(output), "duration": len(output)/rate,
                            "short_form": short_form, "leading_removed_seconds": leading_removed/rate,
                            "quiet_tail_trim_seconds": quiet_trim/rate, "fit_ratio": fit_ratio,
                            "crossfade_seconds": overlap/rate,
                            "variation_minimums": variation_minimums(track),
                            "variation_ranges": variation_ranges(track),
                            "ending_block": path[-1], "ending_preserved": True}}
