"""Real hour-long rendering without reading or copying the network library."""
from pathlib import Path
import tempfile
import unittest
import wave
import numpy as np

from app.engine import render, _read_audio_cached
from app.codecs import encode_wav


class LongClipTests(unittest.TestCase):
    def test_61_minute_arrangement_retains_opening_connections_and_ending(self):
        root = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory(prefix='long-audio-', dir=root) as directory:
            work = Path(directory).resolve()
            self.assertTrue(work.is_relative_to(root))
            source = work/'phrase.wav'
            rate = 8000
            samples = np.rint(np.sin(np.arange(rate*8)*2*np.pi*220/rate)*4000).astype(np.int16)
            pcm = np.column_stack([samples,samples])
            pcm[-rate:] = (pcm[-rate:].astype(float)*np.linspace(1,0,rate)[:,None]).astype(np.int16)
            encode_wav(pcm, rate, source)
            blocks = [dict(index=0,name='Opening',start=0,length=rate,successors=[1],flags=1<<30),
                      dict(index=1,name='Phrase A',start=rate,length=rate*2,successors=[2],flags=0),
                      dict(index=2,name='Phrase B',start=rate*3,length=rate*2,successors=[1,3],flags=0),
                      dict(index=3,name='Ending',start=rate*5,length=rate*3,successors=[],flags=1<<31)]
            track = dict(source_path=str(source),sample_rate=rate,blocks=blocks,
                         variations=[dict(id='0',name='Opening',start=0)])
            try:
                result = render(track, 61*60, '0')
                self.assertEqual(len(result['pcm']), 61*60*rate)
                self.assertTrue(result['diagnostics']['ending_preserved'])
                self.assertLess(abs(result['diagnostics']['fit_ratio']-1), .001)
                path = result['diagnostics']['path']
                self.assertEqual(path[0],0); self.assertEqual(path[-1],3)
                self.assertTrue(all(b in blocks[a]['successors'] for a,b in zip(path,path[1:])))
                self.assertGreater(np.count_nonzero(result['pcm'][:rate]), rate)
                self.assertTrue((result['pcm'][-1]==0).all())
                output = work/'61-minutes.wav'
                encode_wav(result['pcm'], rate, output)
                with wave.open(str(output)) as audio:
                    self.assertEqual(audio.getnframes(), 61*60*rate)
                    self.assertEqual(audio.getsampwidth(), 2)
            finally:
                _read_audio_cached.cache_clear()
