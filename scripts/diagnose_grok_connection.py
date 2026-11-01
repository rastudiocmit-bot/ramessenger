"""Run on stdin inside API. Read-only xAI connectivity check; no generation.

Reads only this workspace's sealed Grok account with the existing tenant role.
Never prints credentials, raw provider responses, SQL/errors or proxy URLs.
No DB writes, provider retries, account changes or application startup.
"""
import hashlib
import json
import os
from types import SimpleNamespace
import urllib.error
import urllib.request
from uuid import UUID

WORKSPACE = UUID('4f7a27e9-9b19-486d-9700-2df7784a24cb')
BASE_URL = 'https://api.x.ai/v1'
LIMIT = 65536
SUPPORTED = ('grok-4.7', 'grok-4.6')
PROVIDER_CODES = frozenset(('unauthorized', 'forbidden', 'permission-denied',
    'permission_denied', 'invalid_api_key', 'invalid-api-key', 'insufficient_quota',
    'insufficient_credits', 'resource-exhausted', 'rate_limit_exceeded', 'not-found'))


def emit(stage, **values):
    print(json.dumps({'stage': stage, **values}, ensure_ascii=False), flush=True)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def refusal(status, raw):
    """Only enumerated signals leave this function, never provider prose."""
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError()
    except (ValueError, TypeError):
        value = {}
    error = value.get('error')
    code = (error.get('code') or error.get('type')) if isinstance(error, dict) else value.get('code')
    message = json.dumps(value).casefold()
    signals = {name: any(token in message for token in tokens) for name, tokens in (
        ('balance', ('credit', 'balance', 'billing', 'fund')),
        ('region', ('region', 'country', 'geograph', 'location')),
        ('permission', ('permission', 'access denied', 'not allowed', 'blocked')),
        ('invalid_key', ('invalid api key', 'invalid_api_key', 'incorrect api key')))}
    return {'http_status': status, 'json_response': bool(value),
            'provider_code': code if isinstance(code, str) and code in PROVIDER_CODES else None,
            'reason_signals': signals}


def probe(path, key, opener=None):
    if path not in ('/language-models', '/models'):
        raise ValueError('Unsupported diagnostic endpoint')
    opener = opener or urllib.request.build_opener(NoRedirect()).open
    request = urllib.request.Request(BASE_URL + path,
        headers={'Authorization': 'Bearer ' + key, 'Accept': 'application/json'}, method='GET')
    try:
        with opener(request, timeout=10) as response:
            raw = response.read(LIMIT + 1)
            status = response.status
    except urllib.error.HTTPError as error:
        return refusal(error.code, error.read(LIMIT))
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return {'transport_error': type(error).__name__}
    try:
        if len(raw) > LIMIT:
            return {'http_status': status, 'response_too_large': True}
        value = json.loads(raw)
        rows = value['models' if path == '/language-models' else 'data']
        if not isinstance(rows, list):
            raise ValueError()
        ids = set()
        for item in rows:
            ids.add(item['id'])
            ids.update(item.get('aliases', []))
        return {'http_status': status, 'supported_models': [name for name in SUPPORTED if name in ids]}
    except (ValueError, TypeError, KeyError, AttributeError):
        return {'http_status': status, 'catalogue_unreadable': True}


def main():
    from sqlalchemy import text
    from center_messenger.accounts import context
    from center_messenger.config import Settings
    from center_messenger.database import make_engine
    from center_messenger.connector_vault import ConnectorVault
    from center_messenger import ai_grok
    config = Settings()
    if config.runtime_role != 'api':
        raise ValueError('Run only in API')
    emit('runtime', build_sha=os.environ.get('CM_BUILD_SHA'),
         proxy_configured=bool(urllib.request.getproxies()))
    engine = make_engine(config)
    try:
        with engine.connect() as db:
            db.execute(text('BEGIN READ ONLY'))
            context(db, workspace_id=WORKSPACE, membership_admin='true')
            item = db.execute(text("SELECT gateway_config->'grok' FROM ai_settings WHERE workspace_id=:w"),
                              {'w': WORKSPACE}).scalar_one_or_none()
    finally:
        engine.dispose()
    if not item or not item.get('key_ciphertext'):
        emit('saved_key', configured=False)
        return
    vault = ConnectorVault(config.connection_master_key_file)
    key = ai_grok.credentials(SimpleNamespace(vault=vault), {'gateway_config': {'grok': item}}, WORKSPACE, {'grok'})
    emit('saved_key', configured=True, key_length=len(key), key_hint=key[-4:],
         fingerprint=hashlib.sha256(key.encode()).hexdigest()[:12], valid_format=ai_grok.valid_key(key))
    result = probe('/language-models', key)
    emit('language_models', **result)
    if result.get('http_status') in (401, 403):
        # A distinct read endpoint diagnoses catalogue ACLs; no retry or inference.
        emit('models', **probe('/models', key))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        emit('diagnostic_error', exception=type(error).__name__)
