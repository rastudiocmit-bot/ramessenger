"""Short-lived validated recording cache and bounded mono/stereo PCM decoding.

Only the telephony worker uses this module. No remote URL is passed to ffmpeg;
network protocols, metadata output and unbounded audio output are disabled.
"""
from array import array
import hashlib
import json
import math
from pathlib import Path
import subprocess
import tempfile
from time import monotonic

from center_messenger.telephony_audio import fetch

MAX_COMPRESSED = 32 * 1024 * 1024
MAX_SECONDS = 4 * 3600
PART_SECONDS = 25
RATE = 16000


class MediaError(Exception):
    def __init__(self, code): self.code = code; super().__init__(code)


def complete_recording(url, reader=fetch):
    body, headers, status = reader(url, f'bytes=0-{MAX_COMPRESSED-1}', chunk_size=MAX_COMPRESSED)
    if status != 206 or headers.get('Content-Range') != f'bytes 0-{len(body)-1}/{len(body)}':
        raise MediaError('recording_too_large_or_incomplete')
    return body


def _run(args, timeout, path):
    try:
        body = Path(path).read_bytes()
        if not 0 < len(body) <= MAX_COMPRESSED: raise MediaError('recording_too_large_or_incomplete')
        result = subprocess.run(args, input=body, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise MediaError('audio_decoder_unavailable') from None
    if result.returncode:
        raise MediaError('audio_decode_failed')
    return result.stdout


def probe(path):
    body = _run(['ffprobe', '-v', 'error', '-protocol_whitelist', 'pipe',
                 '-select_streams', 'a', '-show_entries', 'stream=channels:format=duration',
                 '-of', 'json', 'pipe:0'], 15, path)
    try:
        if len(body) > 4096: raise ValueError()
        result = json.loads(body)
        streams = result['streams']
        channels = streams[0]['channels']
        # A pipe may not advertise duration; decoding EOF remains authoritative.
        duration = result.get('format', {}).get('duration')
        seconds = float(duration) if duration is not None else None
        if (len(streams) != 1 or type(channels) is not int or channels not in (1,2)
                or seconds is not None and (not math.isfinite(seconds) or not 0 < seconds <= MAX_SECONDS)): raise ValueError()
        return channels, seconds
    except (ValueError, TypeError, KeyError, IndexError):
        raise MediaError('audio_layout_unsupported') from None


def decode_part(path, channels, part):
    if type(part) is not int or not 0 <= part <= MAX_SECONDS // PART_SECONDS:
        raise MediaError('audio_duration_limit')
    if channels not in (1,2): raise MediaError('audio_layout_unsupported')
    # Accurate seeking remains enabled. EOF, rather than approximate container
    # duration, determines completion: no 180-second dictation cap or lost tail.
    body = _run(['ffmpeg', '-v', 'error', '-nostdin', '-threads', '1',
                 '-protocol_whitelist', 'pipe', '-i', 'pipe:0', '-ss', str(part * PART_SECONDS),
                 '-map', '0:a:0', '-vn', '-sn', '-dn',
                 '-t', str(PART_SECONDS), '-ar', str(RATE), '-ac', str(channels),
                 '-filter_threads', '1', '-threads', '1', '-acodec', 'pcm_s16le',
                 '-f', 's16le', 'pipe:1'], 25, path)
    if len(body) > PART_SECONDS * RATE * channels * 2 or len(body) % (channels*2):
        raise MediaError('audio_decode_failed')
    if not body: return []
    if part == MAX_SECONDS // PART_SECONDS:
        raise MediaError('audio_duration_limit')
    samples = array('h', body)
    # array slicing preserves both channels independently. These are channel
    # numbers, not claimed customer/operator identities.
    return [samples[channel::channels].tobytes() for channel in range(channels)]


class RecordingCache:
    """At most one compressed file; tmpfs is cleared on container restart."""
    def __init__(self, reader=complete_recording):
        self.reader, self.directory, self.key, self.value = reader, None, None, None
        self.created_at = 0

    def expire(self):
        if self.directory and monotonic()-self.created_at>3600:self.close()

    def close(self):
        if self.directory: self.directory.cleanup()
        self.directory, self.key, self.value = None, None, None

    def get(self, key, url, expected_hash=None):
        self.expire()
        if self.key != key:
            self.close()
            body = self.reader(url)
            if not body or len(body) > MAX_COMPRESSED:
                raise MediaError('recording_too_large_or_incomplete')
            digest = hashlib.sha256(body).hexdigest()
            if expected_hash and digest != expected_hash:
                raise MediaError('recording_changed')
            directory = tempfile.TemporaryDirectory(prefix='cm-phone-audio-')
            try:
                path = Path(directory.name) / 'source.audio'
                with path.open('xb') as file: file.write(body)
                path.chmod(0o600)
                channels, seconds = probe(path)
            except BaseException:
                directory.cleanup(); raise
            self.directory, self.key = directory, key
            self.created_at = monotonic()
            self.value = {'path': path, 'hash': digest, 'channels': channels, 'seconds': seconds}
        if expected_hash and self.value['hash'] != expected_hash:
            raise MediaError('recording_changed')
        return self.value
