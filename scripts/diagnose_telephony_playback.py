"""Read the last requested call recording and measure decoded signal, no writes.

Run on stdin inside telephony-worker. Reports only technical facts, never
provider URLs, credentials, recording bytes, transcript or contact details.
No Data/Call API calls, AI requests, settings changes or container restarts.
"""
from array import array
import http.client
import json
import math
import re
import subprocess
import sys
from uuid import UUID

WORKSPACE = UUID('4f7a27e9-9b19-486d-9700-2df7784a24cb')
LIMIT = 4 * 1024 * 1024
SECONDS = 90
RATE = 8000


def emit(stage, **facts):
    print(json.dumps({'stage': stage, **facts}, ensure_ascii=False), flush=True)


def signal_levels(raw, channels):
    if channels not in (1, 2) or len(raw) % (4 * channels):
        raise ValueError('invalid_pcm')
    values = array('f', raw)
    if sys.byteorder != 'little':
        values.byteswap()
    if any(not math.isfinite(value) for value in values):
        raise ValueError('invalid_pcm')

    def level(samples):
        if not samples:
            return {'samples': 0, 'peak_dbfs': None, 'rms_dbfs': None, 'all_zero': True}
        peak = max(abs(value) for value in samples)
        rms = math.sqrt(sum(value * value for value in samples) / len(samples))
        db = lambda value: round(20 * math.log10(value), 2) if value else None
        return {'samples': len(samples), 'peak_dbfs': db(peak), 'rms_dbfs': db(rms), 'all_zero': peak == 0}

    result = {'decoded_seconds': round(len(values) / channels / RATE, 3),
              'channels': [level(values[index::channels]) for index in range(channels)]}
    if channels == 2:
        result['mono_mix'] = level([(values[index] + values[index + 1]) / 2
                                    for index in range(0, len(values), 2)])
    return result


def inspect_audio(body):
    # pipe-only decoding: the recording cannot make ffmpeg fetch external URLs.
    result = subprocess.run(['ffprobe', '-v', 'error', '-protocol_whitelist', 'pipe',
        '-select_streams', 'a', '-show_entries', 'stream=codec_name,channels,sample_rate',
        '-of', 'json', 'pipe:0'], input=body, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, timeout=15, check=False)
    if result.returncode or len(result.stdout) > 4096:
        return {'decoded': False, 'code': 'probe_failed'}
    try:
        streams = json.loads(result.stdout)['streams']
        channels = streams[0]['channels']
        if len(streams) != 1 or type(channels) is not int or channels not in (1, 2):
            raise ValueError()
    except (ValueError, KeyError, IndexError, TypeError):
        return {'decoded': False, 'code': 'audio_layout_unsupported'}
    decoded = subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-threads', '1',
        '-protocol_whitelist', 'pipe', '-i', 'pipe:0', '-map', '0:a:0', '-vn', '-sn', '-dn',
        '-t', str(SECONDS), '-ar', str(RATE), '-ac', str(channels),
        '-filter_threads', '1', '-threads', '1', '-acodec', 'pcm_f32le', '-f', 'f32le', 'pipe:1'],
        input=body, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30, check=False)
    if decoded.returncode or not decoded.stdout or len(decoded.stdout) > SECONDS * RATE * channels * 4:
        return {'decoded': False, 'code': 'decode_failed', 'codec': streams[0].get('codec_name')}
    return {'decoded': True, 'codec': streams[0].get('codec_name'),
            'sample_rate': streams[0].get('sample_rate'), **signal_levels(decoded.stdout, channels)}


def last_call(config):
    from sqlalchemy import text
    from center_messenger.accounts import context
    from center_messenger.database import make_engine
    engine = make_engine(config)
    try:
        with engine.connect() as db:
            db.execute(text('BEGIN READ ONLY'))
            context(db, workspace_id=WORKSPACE, membership_admin='true')
            recent = db.execute(text("SELECT subject_id FROM auth_events WHERE workspace_id=:w "
                "AND action='telephony_recording_read' ORDER BY created_at DESC LIMIT 1"),
                {'w': WORKSPACE}).scalar_one_or_none()
            call = db.execute(text("SELECT c.id,c.started_at,c.talk_seconds,c.total_seconds FROM telephony_calls c "
                "JOIN telephony_state s ON s.workspace_id=c.workspace_id AND s.account_id=c.account_id "
                "WHERE c.workspace_id=:w AND s.connected AND c.recording_ciphertext IS NOT NULL "
                "ORDER BY (c.id=:id) DESC,c.started_at DESC LIMIT 1"),
                {'w': WORKSPACE, 'id': recent or UUID(int=0)}).mappings().one_or_none()
            return dict(call) | {'last_browser_request': bool(call['id'] == recent)} if call else None
    finally:
        engine.dispose()


def main():
    from center_messenger.config import Settings
    from center_messenger.telephony_audio import control_key
    config = Settings()
    if config.runtime_role != 'telephony':
        raise ValueError('telephony_worker_required')
    call = last_call(config)
    emit('lookup', found=bool(call), **({key: str(value) if key in ('id', 'started_at') else value
                                      for key, value in call.items()} if call else {}))
    if not call:
        return
    connection = http.client.HTTPConnection('telephony-worker', 8002, timeout=35)
    try:
        connection.request('POST', '/recording', json.dumps({'workspace': str(WORKSPACE), 'call': str(call['id'])}),
            {'Content-Type': 'application/json', 'X-CM-Telephony-Key': control_key(config),
             'Range': f'bytes=0-{LIMIT - 1}'})
        response = connection.getresponse()
        body = response.read(LIMIT + 1)
        content_range = response.getheader('Content-Range', '')
        match = re.fullmatch(r'bytes 0-(\d+)/(\d+)', content_range)
        emit('worker_response', status=response.status, content_type=response.getheader('Content-Type'),
             bytes_read=len(body), content_range=content_range if match else None)
        if response.status != 206 or len(body) > LIMIT or not match or int(match[1]) + 1 != len(body):
            emit('result', code='recording_response_unavailable')
            return
        emit('signal', partial_file=len(body) != int(match[2]), max_analyzed_seconds=SECONDS, **inspect_audio(body))
    finally:
        connection.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        emit('diagnostic_error', exception=type(error).__name__)
