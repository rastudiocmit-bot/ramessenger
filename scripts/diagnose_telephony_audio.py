"""Run on stdin inside telephony-worker: one private audio read, no DB writes.

Never print bearer URLs, credentials, audio bytes, raw SQL/errors or phone lists.
Does not call Novofon Data/Call API, initiate calls or change loader settings.
"""
import http.client
import json
from uuid import UUID

from sqlalchemy import text
from center_messenger.accounts import context
from center_messenger.config import Settings
from center_messenger.database import make_engine
from center_messenger.novofon_client import audio_kind
from center_messenger.telephony_audio import control_key

WORKSPACE = UUID('4f7a27e9-9b19-486d-9700-2df7784a24cb')


def main():
    config = Settings()
    if config.runtime_role != 'telephony':
        raise ValueError('Run only in the telephony worker')
    engine = make_engine(config)
    try:
        with engine.connect() as db:
            db.execute(text('BEGIN READ ONLY'))
            context(db, workspace_id=WORKSPACE, membership_admin='true')
            call = db.execute(text("""SELECT DISTINCT c.id,c.started_at FROM telephony_calls c
                JOIN telephony_state s ON s.workspace_id=c.workspace_id AND s.account_id=c.account_id
                JOIN alfa_sync_control a ON a.workspace_id=c.workspace_id
                JOIN alfa_directory d ON d.workspace_id=a.workspace_id AND d.run_id=a.published_id
                    AND d.phones ? c.phone
                WHERE c.workspace_id=:w AND c.recording_ciphertext IS NOT NULL
                    AND d.display_name ILIKE :name
                ORDER BY c.started_at DESC LIMIT 1"""),
                {'w': WORKSPACE, 'name': 'Глазкова Лидия%'}).mappings().one_or_none()
    finally:
        engine.dispose()
    if not call:
        print(json.dumps({'stage': 'lookup', 'found': False}), flush=True)
        return
    print(json.dumps({'stage': 'lookup', 'found': True, 'call_id': str(call['id']),
                      'started_at': str(call['started_at'])}), flush=True)
    connection = http.client.HTTPConnection('telephony-worker', 8002, timeout=35)
    try:
        connection.request('POST', '/recording',
            json.dumps({'workspace': str(WORKSPACE), 'call': str(call['id'])}),
            {'Content-Type': 'application/json', 'X-CM-Telephony-Key': control_key(config), 'Range': 'bytes=0-65535'})
        response = connection.getresponse()
        body = response.read(65536 if response.status == 206 else 2048)
        result = {'stage': 'worker_response', 'status': response.status,
                  'content_type': response.getheader('Content-Type'),
                  'content_range': response.getheader('Content-Range'), 'bytes_read': len(body)}
        if response.status == 206:
            result['audio_kind'] = audio_kind(body)
        else:
            try:
                detail = json.loads(body).get('detail')
                if isinstance(detail, str):result['detail'] = detail[:200]
            except (ValueError, UnicodeError, AttributeError):
                pass
        print(json.dumps(result, ensure_ascii=False), flush=True)
    finally:
        connection.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'stage': 'diagnostic_error', 'exception': type(error).__name__}), flush=True)
