"""Explicit read-only access report for the Grok card, not an inference check.

No raw provider prose, keys, URLs or headers enter the report or logs. A fresh
request uses the saved workspace credential, outside the database transaction.
"""
from datetime import datetime, timezone
import json
import urllib.error
import urllib.request

from center_messenger import ai_grok
from center_messenger.auth_security import AuthError

MESSAGES = {
    'available': 'Сервер получил доступ к каталогу моделей xAI. Генерация текста проверяется отдельно кнопкой «Проверить подключение».',
    'unauthorized': 'xAI не принял сохранённый на сервере ключ. Вставьте ключ заново, сохраните и повторите проверку.',
    'forbidden': 'xAI запретил доступ с сервера. Это не подтверждает неверный ключ: проверьте права ключа и ограничения доступа в xAI.',
    'origin_restricted': 'В ответе xAI указано ограничение на источник запроса (origin/IP). Замена ключа сама по себе может не устранить этот запрет.',
    'region_restricted': 'В ответе xAI указано ограничение по региону. Создание нового ключа само по себе его не устраняет.',
    'no_balance': 'В ответе xAI указана проблема с балансом или оплатой API. Проверьте Billing в console.x.ai.',
    'rate_limited': 'xAI ограничил частоту запросов. Повторите проверку позже.',
    'model_unavailable': 'Доступ к каталогу есть, но поддерживаемые Grok 4.7 и Grok 4.6 не найдены для этого ключа.',
    'network': 'Не удалось связаться с xAI с сервера. Эта проверка не подтверждает ошибку ключа.',
    'redirect_refused': 'xAI вернул перенаправление. Проверка остановлена, ключ по другому адресу не отправлялся.',
    'bad_response': 'xAI вернул ответ, который не удалось разобрать. Ключ по этому ответу проверить нельзя.',
    'unavailable': 'xAI сейчас недоступен с сервера. Повторите проверку позже.',
    'stale': 'Во время проверки ключ изменился. Запустите проверку для нового сохранённого ключа.',
}

ACCOUNT_MESSAGES = {
    'recognized': 'xAI распознал сохранённый ключ; ключ и команда не отмечены заблокированными. Проверьте разрешение на каталог моделей в настройках ключа.',
    'team_blocked': 'xAI сообщает, что команда владельца ключа заблокирована. Обратитесь в поддержку xAI через console.x.ai.',
    'key_blocked': 'xAI сообщает, что сохранённый ключ заблокирован. Проверьте его состояние в console.x.ai.',
    'key_disabled': 'xAI сообщает, что сохранённый ключ отключён. Проверьте его состояние в console.x.ai.',
    'no_permissions': 'xAI распознал ключ, но список его разрешений пуст. Разрешите необходимые API и модели в настройках ключа.',
    'forbidden': 'Сведения о ключе тоже недоступны. Эти ответы не позволяют отличить ограничения ключа или команды от ограничения источника запросов.',
    'unauthorized': 'При запросе сведений xAI не подтвердил сохранённый ключ. Проверьте, какой ключ сохранён в настройках шлюза.',
    'unavailable': 'Дополнительную проверку состояния ключа выполнить не удалось. Первый ответ остаётся HTTP 403.',
}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def report(code, status=None, models=None):
    return {'ok': code == 'available', 'code': code, 'message': MESSAGES[code],
            'http_status': status, 'models': models or [],
            'checked_at': datetime.now(timezone.utc).isoformat(), 'inference_requested': False}


def denial(status, raw):
    # Read only bounded JSON for classification. Never return its message,
    # even when a provider echoes an API key, account name, IP or request body.
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        value = None
    message = json.dumps(value).casefold() if isinstance(value, dict) else ''
    if status in (401, 403) and any(x in message for x in ('unsupported country', 'unsupported region',
            'region restricted', 'region_restricted', 'country is not supported', 'geographic restriction')):
        return 'region_restricted'
    if status == 403 and any(x in message for x in ('origin is not allowed', 'ip address is not allowed',
            'source ip is not allowed')):
        return 'origin_restricted'
    if status in (401, 402, 403) and any(x in message for x in ('insufficient credit', 'no credit',
            'out of credit', 'does not have any credits', "doesn't have any credits", 'insufficient balance')):
        return 'no_balance'
    if status == 401: return 'unauthorized'
    if status == 403: return 'forbidden'
    if status == 402: return 'no_balance'
    if status == 429: return 'rate_limited'
    if 300 <= status < 400: return 'redirect_refused'
    return 'unavailable'


def read_access(key, path, opener):
    if path not in ('/language-models', '/api-key'):
        raise ValueError('Unsupported access diagnostic')
    request = urllib.request.Request(ai_grok.BASE_URL + path, method='GET',
        headers={'Authorization': 'Bearer ' + key, 'Accept': 'application/json'})
    try:
        with opener(request, timeout=7) as response:
            return response.status, response.read(2_000_001)
    except urllib.error.HTTPError as error:
        with error:
            try:
                raw = error.read(65536)
            except (OSError, TimeoutError):
                raw = b''
        return error.code, raw


def account_access(key, opener):
    status, code = None, 'unavailable'
    try:
        status, raw = read_access(key, '/api-key', opener)
        if status == 200 and len(raw) <= 2_000_000:
            value = json.loads(raw)
            flags = ('team_blocked', 'api_key_blocked', 'api_key_disabled')
            if (isinstance(value, dict) and not value.get('error')
                    and all(type(value.get(flag)) is bool for flag in flags)
                    and isinstance(value.get('acls'), list)
                    and all(isinstance(acl, str) for acl in value['acls'])):
                code = ('team_blocked' if value['team_blocked'] else 'key_blocked' if value['api_key_blocked']
                        else 'key_disabled' if value['api_key_disabled'] else 'no_permissions' if not value['acls']
                        else 'recognized')
        elif status in (401, 403):
            code = 'unauthorized' if status == 401 else 'forbidden'
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, TypeError):
        pass
    # Project fixed enums only: no IDs, ACL strings, redacted key, names or raw body.
    return {'http_status': status, 'code': code, 'message': ACCOUNT_MESSAGES[code]}


def probe(key, opener=None):
    opener = opener or urllib.request.build_opener(NoRedirect()).open
    try:
        status, raw = read_access(key, '/language-models', opener)
    except (urllib.error.URLError, TimeoutError, OSError):
        return report('network')
    if status != 200:
        code = denial(status, raw)
        result = report(code, status)
        if status == 403 and code == 'forbidden':
            result['account'] = account_access(key, opener)
        return result
    try:
        if len(raw) > 2_000_000:
            raise ValueError()
        value = json.loads(raw)
        rows = value['models']
        if value.get('error') or not isinstance(rows, list):
            raise ValueError()
        ids = set()
        for item in rows:
            if 'text' not in item.get('input_modalities', []) or 'text' not in item.get('output_modalities', []):
                continue
            ids.add(item['id']); ids.update(item.get('aliases', []))
        models = [name for name in ai_grok.MODELS if name in ids]
        return report('available' if models else 'model_unavailable', status, models)
    except (ValueError, KeyError, TypeError, AttributeError):
        return report('bad_response', status)


def diagnose(service, actor, workspace, *, opener=None, proxy_data=None):
    with service.accounts.transaction(actor, snapshot=True) as db:
        service.accounts._workspace(db, actor, workspace, manage=True)
        row = service._settings(db, workspace)
        original = ai_grok.connection_stamp(row)
        key = ai_grok.credentials(service, row, workspace, {'grok'})
        saved_proxy = ai_grok.proxy_credentials(service, row, workspace, {'grok'}) if proxy_data is None else None
    if not key:
        raise AuthError(409, 'Сначала сохраните API-ключ Grok.')
    service.accounts.throttle(f'grok-access:{workspace}:{actor.user_id}', limit=5, minutes=1)
    proxy = None
    if proxy_data is not None:
        from center_messenger.grok_proxy_probe import ProxyOpener, parse
        proxy = ProxyOpener(parse(proxy_data['proxy'], proxy_data.get('protocol', 'auto')))
        opener = proxy.open
    elif saved_proxy is not None:
        from center_messenger.grok_proxy_probe import ProxyOpener
        proxy = ProxyOpener(saved_proxy)
        opener = proxy.open
    result = probe(key, opener)
    if proxy is not None:
        result.update(route='proxy', proxy=proxy.public())
        if result['ok']:
            result['message'] = ('Через прокси получен каталог моделей xAI. Разовая проверка не меняет настройки агентов.'
                if proxy_data is not None else 'Каталог моделей xAI доступен через сохранённый прокси Grok.')
        elif result['http_status'] == 403:
            result['message'] = 'xAI вернул HTTP 403 и через прокси. Точная причина запрета этим ответом не раскрыта.'
        elif result['http_status'] is None:
            result['message'] = 'Не удалось выполнить проверку xAI через прокси.'
    with service.accounts.transaction(actor, snapshot=True) as db:
        service.accounts._workspace(db, actor, workspace, manage=True)
        if ai_grok.connection_stamp(service._settings(db, workspace)) != original:
            return report('stale')
    return result
