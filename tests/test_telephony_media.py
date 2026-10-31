"""Actual bounded decoder and complete-file checks with synthetic audio only."""
import io
from pathlib import Path
import tempfile
import unittest
import wave
from unittest.mock import Mock,patch

from center_messenger.telephony_media import (RecordingCache,MediaError,complete_recording,
    probe,decode_part,PART_SECONDS,RATE)


def wav(seconds=1,channels=2):
    output=io.BytesIO()
    with wave.open(output,'wb') as file:
        file.setnchannels(channels);file.setsampwidth(2);file.setframerate(RATE)
        frame=b'\1\0' if channels==1 else b'\1\0\2\0'
        file.writeframes(frame*round(RATE*seconds))
    return output.getvalue()


class TelephoneMediaTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.path=Path(self.directory.name)/'synthetic.wav'
    def test_stereo_channels_are_preserved_without_assigning_customer_or_operator(self):
        self.path.write_bytes(wav());self.assertEqual(probe(self.path)[0],2)
        first,second=decode_part(self.path,2,0)
        self.assertEqual(first,b'\1\0'*RATE);self.assertEqual(second,b'\2\0'*RATE)
    def test_call_longer_than_three_minutes_keeps_its_tail_and_stops_at_eof(self):
        self.path.write_bytes(wav(205))
        parts=decode_part(self.path,2,8)
        self.assertEqual([len(value) for value in parts],[5*RATE*2]*2)
        self.assertEqual(decode_part(self.path,2,9),[])
    def test_complete_recording_rejects_a_valid_but_incomplete_http_range(self):
        reader=Mock(return_value=(b'RIFF',{'Content-Range':'bytes 0-3/100'},206))
        with self.assertRaises(MediaError):complete_recording('synthetic-url',reader)
    def test_complete_recording_requests_a_bounded_whole_file(self):
        body=wav();reader=Mock(return_value=(body,{'Content-Range':f'bytes 0-{len(body)-1}/{len(body)}'},206))
        self.assertEqual(complete_recording('synthetic-url',reader),body)
        self.assertEqual(reader.call_args.kwargs['chunk_size'],32*1024*1024)
    def test_cache_is_content_bound_private_temporary_and_cleared(self):
        cache=RecordingCache(reader=Mock(return_value=wav()));self.addCleanup(cache.close)
        media=cache.get('one','synthetic-url');path=media['path']
        self.assertEqual(path.stat().st_mode&0o777,0o600)
        self.assertEqual(cache.get('one','synthetic-url')['hash'],media['hash'])
        self.assertEqual(cache.reader.call_count,1)
        with self.assertRaises(MediaError):cache.get('one','synthetic-url','a'*64)
        cache.close();self.assertFalse(path.exists())
    def test_cache_expires_instead_of_becoming_a_permanent_recording_store(self):
        cache=RecordingCache(reader=Mock(return_value=wav()));self.addCleanup(cache.close)
        media=cache.get('one','synthetic-url');cache.created_at-=3601;cache.expire()
        self.assertFalse(media['path'].exists())
    def test_decoder_never_receives_the_provider_url_or_access_to_file_network_protocols(self):
        self.path.write_bytes(wav())
        with patch('center_messenger.telephony_media.subprocess.run') as run:
            run.return_value=Mock(returncode=0,stdout=b'')
            self.assertEqual(decode_part(self.path,2,0),[])
            args=run.call_args.args[0]
            self.assertEqual(args[args.index('-protocol_whitelist')+1],'pipe')
            self.assertNotIn(str(self.path),args);self.assertEqual(args[args.index('-i')+1],'pipe:0')
            self.assertLessEqual(run.call_args.kwargs['timeout'],25)


if __name__=='__main__':unittest.main()
