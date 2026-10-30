"""Private control path for manual MAX login/OTP; queues and history stay in DB.

No generic RPC, retries, redirects or user-supplied destinations. Worker auth is
additional to the unchanged application session, workspace rights and CSRF.
"""
import hmac
import json
import re
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from center_messenger.auth_security import AuthError
from center_messenger.alfa_max_browser import AlfaBrowserError

ORIGIN = 'http://max-worker:8001'
KEY_HEADER = 'x-cm-max-worker-key'
MAX_REPLY = 262144


def control_key(config):
    path = config.max_worker_key_file
    if path is None:
        raise ValueError('MAX worker key is not configured')
    value = path.read_text().strip()
    if not re.fullmatch('[a-f0-9]{64}', value):
        raise ValueError('Invalid MAX worker key')
    return value


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class BrowserDisabled:
    """Fail closed if a public-API code path tries to run a native browser."""
    def __getattr__(self, name):
        def disabled(*args, **kwargs):
            raise AlfaBrowserError('browser_unavailable')
        return disabled


def forward_control(request, workspace, action, model):
    if action not in {'connect', 'verify'}:
        raise ValueError('Unsupported MAX worker operation')
    # Reject unauthorized operators before forwarding credentials. The worker
    # repeats these checks using the same session; do not trust actor IDs in JSON.
    from center_messenger.accounts_api import identity, service
    accounts = service(request)
    actor = identity(request, mutation=True)
    with accounts.transaction(actor, snapshot=True) as db:
        accounts._workspace(db, actor, workspace, manage=True)
    config = accounts.settings
    try:
        key = control_key(config)
    except (OSError, ValueError):
        raise AuthError(503, 'Подключение MAX временно недоступно.') from None
    payload = model.model_dump_json().encode()
    # Pydantic serializes SecretStr as stars; only this explicitly bounded
    # control request may carry the real candidate password to the worker.
    if action == 'connect':
        payload = json.dumps({'email': str(model.email).casefold(),
                              'password': model.password.get_secret_value(),
                              'auto_reconnect': model.auto_reconnect}).encode()
    if len(payload) > 8192:
        raise AuthError(413, 'Слишком большой запрос.')
    headers = {'Content-Type': 'application/json', 'Origin': config.public_url,
               'Cookie': f'{config.cookie_name}={actor.raw_session}',
               'X-CSRF-Token': request.headers.get('x-csrf-token', ''), KEY_HEADER: key}
    upstream = Request(f'{ORIGIN}/api/workspaces/{workspace}/integrations/alfa-max/{action}',
                       data=payload, headers=headers, method='POST')
    # Never send connector passwords/cookies to an environment HTTP proxy.
    opener = build_opener(ProxyHandler({}), NoRedirects())
    try:
        with opener.open(upstream, timeout=95) as response:
            data = response.read(MAX_REPLY + 1)
            if response.status != 200 or len(data) > MAX_REPLY:
                raise ValueError('Invalid control reply')
            result = json.loads(data)
            if not isinstance(result, dict):
                raise ValueError('Invalid control reply')
            return result
    except HTTPError as error:
        if 400 <= error.code <= 599:
            # Worker handlers produce only sanitized public AuthError/validation
            # details. Preserve their semantics (especially OTP vs app login),
            # never arbitrary HTML or validation inputs.
            try:
                body = error.read(MAX_REPLY + 1)
                value = json.loads(body) if len(body) <= MAX_REPLY else {}
                detail = value.get('detail') if isinstance(value, dict) else None
            except (OSError, ValueError):
                detail = None
            if isinstance(detail, str) and 1 <= len(detail) <= 512:
                raise AuthError(error.code, detail) from None
    except (URLError, OSError, ValueError):
        pass
    # No automatic replay of a POST whose result is unknown. The next status
    # read comes from the local DB and can show an already completed login.
    raise AuthError(503, 'Не удалось подтвердить результат подключения MAX. Обновите его состояние.')


class WorkerControlAuth:
    def __init__(self, app, settings):
        self.app, self.key = app, control_key(settings)

    async def __call__(self, scope, receive, send):
        from starlette.responses import JSONResponse
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        if scope['path'] not in {'/health/live', '/health/ready'}:
            supplied = dict(scope.get('headers', [])).get(KEY_HEADER.encode(), b'')
            if len(supplied) != 64 or not hmac.compare_digest(supplied, self.key.encode()):
                return await JSONResponse({'detail': 'Недоступно.'}, status_code=403)(scope, receive, send)
        return await self.app(scope, receive, send)
