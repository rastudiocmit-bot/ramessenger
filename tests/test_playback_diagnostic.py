"""Technical audio measurements use synthetic signals, never private recordings."""
from array import array
import importlib.util
import io
import math
from pathlib import Path
import unittest
import wave

spec = importlib.util.spec_from_file_location('playback_diagnostic', Path(__file__).resolve().parents[1] / 'scripts/diagnose_telephony_playback.py')
diag = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diag)


class PlaybackDiagnosticTests(unittest.TestCase):
    def test_silence_and_stereo_phase_cancellation_are_distinct(self):
        samples = array('f', [0.25, -0.25] * 8000)
        signal = diag.signal_levels(samples.tobytes(), 2)
        self.assertFalse(signal['channels'][0]['all_zero'])
        self.assertTrue(signal['mono_mix']['all_zero'])
        self.assertEqual(signal['decoded_seconds'], 1)
        silent = diag.signal_levels(array('f', [0.] * 8000).tobytes(), 1)
        self.assertTrue(silent['channels'][0]['all_zero'])
        self.assertIsNone(silent['channels'][0]['rms_dbfs'])

    def test_decoder_measures_a_real_tone_and_rejects_non_audio(self):
        stream = io.BytesIO()
        with wave.open(stream, 'wb') as wav:
            wav.setparams((1, 2, diag.RATE, 0, 'NONE', 'not compressed'))
            wav.writeframes(array('h', [int(12000 * math.sin(2 * math.pi * 440 * index / diag.RATE))
                                       for index in range(diag.RATE)]).tobytes())
        result = diag.inspect_audio(stream.getvalue())
        self.assertTrue(result['decoded'])
        self.assertEqual(result['codec'], 'pcm_s16le')
        self.assertFalse(result['channels'][0]['all_zero'])
        self.assertGreater(result['channels'][0]['rms_dbfs'], -20)
        self.assertEqual(result['decoded_seconds'], 1)
        self.assertFalse(diag.inspect_audio(b'<html>unavailable</html>')['decoded'])

    def test_truncated_or_non_finite_pcm_is_rejected(self):
        for body in (b'bad', array('f', [float('nan')]).tobytes()):
            with self.assertRaises(ValueError):
                diag.signal_levels(body, 1)


if __name__ == '__main__':
    unittest.main()
