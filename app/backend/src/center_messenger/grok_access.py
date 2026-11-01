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
    if status in (401, 402, 403) and any(x in message for x in ('insufficient credit', 'no credit',
            'out of credit', 'does not have any credits', "doesn't have any credits", 'insufficient balance')):
        return 'no_balance'
    if status == 401: return 'unauthorized'
    if status == 403: return 'forbidden'
    if status == 402: return 'no_balance'
    if status == 429: return 'rate_limited'
    if 300 <= status < 400: return 'redirect_refused'
    return 'unavailable'


def probe(key, opener=None):
    opener = opener or urllib.request.build_opener(NoRedirect()).open
    request = urllib.request.Request(ai_grok.BASE_URL + '/language-models', method='GET',
        headers={'Authorization': 'Bearer ' + key, 'Accept': 'application/json'})
    try:
        with opener(request, timeout=10) as response:
            raw = response.read(2_000_001)
            status = response.status
    except urllib.error.HTTPError as error:
        try:
            raw = error.read(65536)
        except (OSError, TimeoutError):
            raw = b''
        return report(denial(error.code, raw), error.code)
    except (urllib.error.URLError, TimeoutError, OSError):
        return report('network')
    try:
        if status != 200 or len(raw) > 2_000_000:
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


def diagnose(service, actor, workspace, *, opener=None):
    with service.accounts.transaction(actor, snapshot=True) as db:
        service.accounts._workspace(db, actor, workspace, manage=True)
        row = service._settings(db, workspace)
        original = ai_grok.stored(row).get('key_ciphertext')
        key = ai_grok.credentials(service, row, workspace, {'grok'})
    if not key:
        raise AuthError(409, 'Сначала сохраните API-ключ Grok.')
    service.accounts.throttle(f'grok-access:{workspace}:{actor.user_id}', limit=5, minutes=1)
    result = probe(key, opener)
    with service.accounts.transaction(actor, snapshot=True) as db:
        service.accounts._workspace(db, actor, workspace, manage=True)
        if ai_grok.stored(service._settings(db, workspace)).get('key_ciphertext') != original:
            return report('stale')
    return result
