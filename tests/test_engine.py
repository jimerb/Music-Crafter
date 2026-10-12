"""Exercise actual authored graphs and PCM, not a mocked arranger."""
from pathlib import Path
import json
import math
import unittest

import numpy as np

from app.engine import ArrangementError, _assemble, read_audio, render, variation_minimums, variation_ranges, variation_availability
from app.library import Library


PROJECT = Path(__file__).resolve().parents[1]


class EngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (PROJECT / 'Light Jazzy').is_dir():
            raise unittest.SkipTest('Optional private Light Jazzy integration fixture is not installed')
        cls.library = Library([str(PROJECT / "Light Jazzy")])
        cls.tracks = list(cls.library.tracks.values())
        assert len(cls.tracks) == 16, cls.library.errors

    def assert_arrangement(self, track, seconds, result, selected=None):
        pcm = result["pcm"]
        rate = result["sample_rate"]
        self.assertEqual(pcm.dtype, np.int16)
        self.assertEqual(pcm.shape, (round(seconds * rate), 2))
        self.assertTrue(np.all(pcm[0] == 0))
        self.assertTrue(np.all(pcm[-1] == 0))
        self.assertGreater(float(np.sqrt(np.mean(pcm.astype(np.float64)**2))), 1)
        diag = result["diagnostics"]
        path = diag["path"]
        self.assertTrue(track["blocks"][path[-1]]["flags"] & (1 << 31))
        self.assertTrue(diag["ending_preserved"])
        for first, second in zip(path, path[1:]):
            self.assertIn(second, track["blocks"][first]["successors"])
        for section in result["sections"]:
            self.assertGreater(section["duration"], 0)
            self.assertGreaterEqual(section["start"], 0)
            self.assertLessEqual(section["start"] + section["duration"], seconds + .006)
        if selected:
            self.assertEqual(path[0], selected["start"])
            self.assertFalse(diag["short_form"])
            self.assertEqual(result["sections"][0]["name"], track["blocks"][selected["start"]]["name"])

    def test_pcm_and_normalized_ranges_all_tracks(self):
        signatures = set()
        for track in self.tracks:
            with self.subTest(track=track["title"]):
                pcm, rate = read_audio(track["source_path"])
                signatures.add(Path(track["source_path"]).read_bytes()[:4])
                self.assertEqual(rate, track["sample_rate"])
                self.assertEqual(len(pcm), track["blocks"][-1]["start"] + track["blocks"][-1]["length"])
                self.assertEqual(sum(b["length"] for b in track["blocks"]), len(pcm))
                self.assertEqual(track["blocks"][-1]["start"] + track["blocks"][-1]["length"], len(pcm))
                self.assertLess(np.sqrt(np.mean(pcm[:rate].astype(np.float64)**2)), 12000)
        self.assertEqual(signatures, {b"RIFF", b"SDSS"})

    def test_auto_matrix_all_tracks_fractional_and_short(self):
        for track in self.tracks:
            for seconds in (4, 10.375, 30.125, 90):
                with self.subTest(track=track["title"], duration=seconds):
                    self.assert_arrangement(track, seconds, render(track, seconds))

    def test_named_openings_across_every_track(self):
        for track in self.tracks:
            minimums = variation_minimums(track)
            # First/middle/last variations include long intros and short stings.
            variations = track["variations"]
            ranges = variation_ranges(track)
            for variant in [variations[0], variations[len(variations)//2], variations[-1]]:
                seconds = max(30.125, minimums[variant["id"]] + .125)
                bounds = ranges[variant["id"]]
                if bounds["max"] is not None:
                    seconds = (bounds["min"] + bounds["max"])/2
                if not variation_availability(track, seconds)[variant["id"]]["available"]:
                    seconds = bounds["min"] + .0001
                    if bounds["max"] is not None:
                        seconds = min(seconds, bounds["max"])
                    if seconds < 3.2:
                        seconds = 45.125
                with self.subTest(track=track["title"], variation=variant["name"]):
                    result = render(track, seconds, variant["id"])
                    self.assert_arrangement(track, seconds, result, variant)

    def test_ten_minutes_and_previous_demo_lengths(self):
        for track in self.tracks:
            with self.subTest(track=track["title"], duration=600):
                result = render(track, 600)
                self.assert_arrangement(track, 600, result)
                self.assertLess(abs(result["diagnostics"]["fit_ratio"]-1), .03)
        tasty = next(track for track in self.tracks if track["title"] == "Tasty Jazz")
        for seconds in (30, 60, 180):
            self.assert_arrangement(tasty, seconds, render(tasty, seconds))

    def test_short_named_variation_explains_available_alternative(self):
        tasty = next(track for track in self.tracks if track["title"] == "Tasty Jazz")
        early = next(v for v in tasty["variations"] if v["name"] == "Early")
        with self.assertRaisesRegex(ArrangementError, "Choose Auto"):
            render(tasty, 4, early["id"])
        with self.assertRaises(ArrangementError):
            render(tasty, float("nan"))
        with self.assertRaises(ArrangementError):
            render(tasty, 30, "nonexistent opening")
        bittersweet = next(track for track in self.tracks if track["title"] == "Bittersweet")
        memories = next(v for v in bittersweet["variations"] if v["name"] == "Memories")
        with self.assertRaisesRegex(ArrangementError, "Choose Auto"):
            render(bittersweet, 180, memories["id"])
        newview = next(track for track in self.tracks if track["title"] == "New View")
        clear = next(v for v in newview["variations"] if v["name"] == "Clear skies")
        availability = variation_availability(newview, 30)
        self.assertFalse(availability[clear["id"]]["available"])
        self.assertTrue(any(v["available"] for v in availability.values()))

    def test_short_stings_retain_chord_attack_and_avoid_dead_air(self):
        # These two recorded ending blocks contain a fading sustain; their
        # musical attack is in the preceding block. Merely keeping the flagged
        # ending previously produced quiet four-second clips with lost attacks.
        for title in ("Sleeky and Stealthy", "Dreamy Piano"):
            track = next(t for t in self.tracks if t["title"] == title)
            with self.subTest(track=title):
                result = render(track, 4)
                pcm = result["pcm"].astype(np.float64)/32768
                rate = result["sample_rate"]
                self.assertGreater(np.max(np.abs(pcm)), .20)
                self.assertGreater(np.sqrt(np.mean(pcm[:round(.2*rate)]**2)), .04)
                hop = round(rate*.05)
                windows = pcm[:len(pcm)//hop*hop].reshape(-1, hop, 2)
                quiet_time = np.count_nonzero(np.sqrt(np.mean(windows**2, axis=(1,2))) < 10**(-50/20))*.05
                self.assertLess(quiet_time, .5)
                self.assertEqual(result["diagnostics"]["leading_removed_seconds"], 0)
                self.assertTrue(result["diagnostics"]["short_form"])
                self.assertLess(result["diagnostics"]["fit_ratio"], 1.25)

    def test_source_adjacent_sections_are_sample_exact(self):
        for track in self.tracks:
            pcm, rate = read_audio(track["source_path"])
            blocks = track["blocks"]
            output, timeline = _assemble(track, pcm, rate, (0, 1), round(rate*.005))
            self.assertTrue(np.array_equal(output, pcm[:blocks[1]["start"]+blocks[1]["length"]]))
            self.assertEqual(timeline[1]["start"], blocks[1]["start"]/rate)


if __name__ == "__main__":
    unittest.main()
