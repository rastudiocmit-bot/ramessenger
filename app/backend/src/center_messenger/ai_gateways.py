"""Workspace-scoped gateway configuration. No decrypted key enters public settings."""
import json
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import text

from center_messenger import ai_mimo
from center_messenger.accounts import audit
from center_messenger.ai_yandex import YandexError, key_hint, silence, TTS_VOICES, tts_cost
from center_messenger.auth_security import AuthError

PROVIDERS = ('mimo', 'mimo_plan')


def config(row):
    return dict(row.get('gateway_config') or {}) if row else {}


def ready(row):
    if not row or not row['voice_enabled']:
        return False
    provider = config(row).get('recognition_provider', 'yandex')
    return bool(row.get(provider + '_key_ciphertext')) if provider in PROVIDERS else bool(row['key_ciphertext'] and row['folder_id'])


def check_summary(configured, checks):
    """Derive current state from persisted checks, never from key presence alone."""
    known = [checks.get(key, {}) for key in ('text', 'recognition', 'synthesis')] if configured else []
    passed = sum(item.get('ok') is True for item in known)
    failed = sum(item.get('ok') is False for item in known)
    status = 'error' if failed else 'active' if passed == 3 else 'partial' if passed else 'not_configured'
    return {'status': status, 'passed_checks': passed, 'failed_checks': failed, 'total_checks': 3}


def public(row):
    saved = config(row)
    result = {key: saved.get(key, default) for key, default in (
        ('recognition_provider', 'yandex'), ('recognition_language', 'ru'),
        ('synthesis_provider', 'disabled'), ('synthesis_voice', 'Mia'))}
    for provider in PROVIDERS:
        item = saved.get(provider, {})
        result[provider] = {
            **check_summary(bool(row and row.get(provider + '_key_ciphertext')), item.get('checks', {})),
            'configured': bool(row and row.get(provider + '_key_ciphertext')),
            'key_hint': item.get('key_hint', ''), 'checks': item.get('checks', {}),
            'protocol': item.get('protocol', 'chat'),
            'base_url': item.get('base_url', ai_mimo.PLAN_URL if provider == 'mimo_plan' else ai_mimo.PAYG_URL),
            'billing': 'subscription' if provider == 'mimo_plan' else 'pay_as_you_go',
        }
    result['yandex'] = {'configured': bool(row and row.get('key_ciphertext') and row.get('folder_id')),
                        'checks': saved.get('yandex', {}).get('checks', {})}
    result['synthesis_voices'] = list(ai_mimo.VOICES)
    result['synthesis_voice_catalog'] = {
        'yandex': [{'id': key, 'title': title} for key, title in TTS_VOICES.items()],
        **{provider: [{'id': voice, 'title': voice} for voice in ai_mimo.VOICES] for provider in PROVIDERS}}
    return result


def credentials(service, row, workspace, selected=None):
    result = {'gateways': config(row)}
    for provider in PROVIDERS:
        cipher = row.get(provider + '_key_ciphertext') if row else None
        result[provider] = service.vault.open(workspace, uuid5(NAMESPACE_URL, 'center-messenger:' + provider),
            cipher, purpose=provider)['api_key'] if cipher and (selected is None or provider in selected) else None
    return result


def options(creds, provider):
    item = creds.get('gateways', {}).get(provider, {})
    return {'api_key': creds.get(provider),
            'base_url': item.get('base_url', ai_mimo.PLAN_URL if provider == 'mimo_plan' else ai_mimo.PAYG_URL)}


class Gateways:
    def __init__(self, service):
        self.service = service

    def _write(self, db, actor, workspace, value):
        db.execute(text("INSERT INTO ai_settings(workspace_id,gateway_config,updated_by) "
                        "VALUES (:w,CAST(:g AS jsonb),:u) ON CONFLICT (workspace_id) DO UPDATE SET "
                        "gateway_config=EXCLUDED.gateway_config,version=ai_settings.version+1,"
                        "updated_at=now(),updated_by=EXCLUDED.updated_by"),
                   {'w': workspace, 'g': json.dumps(value), 'u': actor.user_id})

    def save(self, actor, workspace, provider, data):
        if provider not in PROVIDERS:
            raise AuthError(422, 'Неизвестный шлюз.')
        service = self.service
        with service.accounts.transaction(actor) as db:
            service.accounts._workspace(db, actor, workspace, manage=True)
            row = service._settings(db, workspace, lock=True)
            saved = config(row)
            previous = saved.get(provider, {})
            item = dict(previous)
            plan = provider == 'mimo_plan'
            try:
                item['base_url'] = ai_mimo.endpoint(data.get('base_url') or item.get('base_url') or
                    (ai_mimo.PLAN_URL if plan else ai_mimo.PAYG_URL), plan=plan)
            except YandexError:
                raise AuthError(422, 'Выберите официальный адрес MiMo.') from None
            item['protocol'] = data.get('protocol') or item.get('protocol', 'chat')
            if item['protocol'] not in ('chat', 'responses'):
                raise AuthError(422, 'Неизвестный протокол.')
            key = str(data.get('api_key') or '').strip()
            clear = bool(data.get('clear_key'))
            if key and clear:
                raise AuthError(422, 'Сохранение и удаление ключа — разные действия.')
            cipher = row.get(provider + '_key_ciphertext') if row else None
            if key:
                if not ai_mimo.valid_key(key, plan=plan):
                    raise AuthError(422, 'Проверьте формат ключа выбранного подключения MiMo.')
                cipher = service.vault.seal(workspace, uuid5(NAMESPACE_URL, 'center-messenger:' + provider),
                                             {'api_key': key}, purpose=provider)
                item['key_hint'] = key_hint(key)
            if clear:
                cipher = None
                item['key_hint'] = ''
            if key or clear or item != previous:
                item['checks'] = {}
            saved[provider] = item
            self._write(db, actor, workspace, saved)
            # Identifier is chosen from the fixed internal whitelist above.
            db.execute(text(f'UPDATE ai_settings SET {provider}_key_ciphertext=:c '
                            'WHERE workspace_id=:w'), {'w': workspace, 'c': cipher})
            if clear and saved.get('recognition_provider') == provider:
                db.execute(text('UPDATE ai_settings SET voice_enabled=false WHERE workspace_id=:w'), {'w': workspace})
            if clear and saved.get('synthesis_provider') == provider:
                saved['synthesis_provider'] = 'disabled'
                self._write(db, actor, workspace, saved)
            audit(db, 'ai_gateway_updated', user_id=actor.user_id, workspace_id=workspace)
            return service._public(db, workspace)

    def routes(self, actor, workspace, data):
        service = self.service
        with service.accounts.transaction(actor) as db:
            service.accounts._workspace(db, actor, workspace, manage=True)
            row = service._settings(db, workspace, lock=True)
            saved = config(row)
            provider = data['recognition_provider']
            language = data['recognition_language']
            synthesis = data['synthesis_provider']
            if provider not in ('yandex', *PROVIDERS) or synthesis not in ('disabled', 'yandex', *PROVIDERS):
                raise AuthError(422, 'Неизвестный шлюз для действия.')
            if language not in (('ru',) if provider == 'yandex' else ('auto', 'en', 'zh')):
                raise AuthError(422, 'MiMo ASR поддерживает китайский и английский; для русского выберите Yandex.')
            if data['synthesis_voice'] not in (TTS_VOICES if synthesis == 'yandex' else ai_mimo.VOICES):
                raise AuthError(422, 'Неизвестный голос.')
            for selected in (provider, synthesis):
                if selected in PROVIDERS and not (row and row.get(selected + '_key_ciphertext')):
                    raise AuthError(422, 'Сначала сохраните ключ выбранного шлюза.')
            if synthesis == 'yandex' and not (row and row['key_ciphertext'] and row['folder_id']):
                raise AuthError(422, 'Сначала сохраните ключ Yandex Cloud и ID каталога.')
            if provider == 'yandex' and row and row['voice_enabled'] and not (row['key_ciphertext'] and row['folder_id']):
                raise AuthError(422, 'Сначала подключите Yandex Cloud или выключите голосовой ввод.')
            saved.update({key: data[key] for key in ('recognition_provider', 'recognition_language',
                                                     'synthesis_provider', 'synthesis_voice')})
            self._write(db, actor, workspace, saved)
            audit(db, 'ai_action_routes_updated', user_id=actor.user_id, workspace_id=workspace)
            return service._public(db, workspace)

    def verify(self, actor, workspace, provider, capability):
        if provider == 'yandex' and capability == 'synthesis':
            return self.verify_yandex_synthesis(actor, workspace)
        if provider not in PROVIDERS or capability not in ('text', 'recognition', 'synthesis'):
            raise AuthError(422, 'Неизвестная возможность шлюза.')
        service = self.service
        with service.accounts.transaction(actor, snapshot=True) as db:
            service.accounts._workspace(db, actor, workspace, manage=True)
            row = service._settings(db, workspace)
            if row and row['monthly_limit_rub'] is not None and service._month_spent(db, workspace) >= float(row['monthly_limit_rub']):
                raise AuthError(429, 'Месячный лимит исчерпан.')
            creds = credentials(service, row, workspace, {provider})
            original_cipher = row.get(provider + '_key_ciphertext') if row else None
        kwargs = options(creds, provider)
        if not kwargs['api_key']:
            raise AuthError(409, 'Сначала сохраните ключ шлюза.')
        service.accounts.throttle(f'ai-gateway:{workspace}:{actor.user_id}', limit=10, minutes=1)
        error, tokens, cost = None, (0, 0), 0.0
        plan = provider == 'mimo_plan'
        try:
            if capability == 'text':
                model = next(iter(ai_mimo.PLAN_MODELS if plan else ai_mimo.MODELS))
                answer = service.mimo.complete('Ready?', prompt='Reply: ready.', model=model, temperature=0,
                    max_tokens=32, protocol=creds['gateways'].get(provider, {}).get('protocol', 'chat'), rules=False, **kwargs)
                tokens = (answer['input_tokens'], answer['output_tokens'])
                from center_messenger.ai_voice import model_cost
                cost = model_cost(model, *tokens)
                if answer['truncated']:
                    raise YandexError('bad_response')
            elif capability == 'recognition':
                service.mimo.recognize(silence(.5), plan=plan, language='auto', **kwargs)
                cost = ai_mimo.audio_cost(.5, plan=plan)
            else:
                service.mimo.synthesize('Ready.', plan=plan, **kwargs)
        except YandexError as failure:
            error = failure.code
        service._usage(actor, workspace, kind='verify', tokens=tokens, cost=cost,
                       outcome='error' if error else 'ok', error=error)
        with service.accounts.transaction(actor) as db:
            service.accounts._workspace(db, actor, workspace, manage=True)
            row = service._settings(db, workspace, lock=True)
            saved = config(row)
            # A late test must not verify a replaced key or changed endpoint/protocol.
            same_route = all(saved.get(provider, {}).get(key) == creds['gateways'].get(provider, {}).get(key)
                             for key in ('base_url', 'protocol'))
            if row.get(provider + '_key_ciphertext') == original_cipher and same_route:
                item = dict(saved.get(provider, {}))
                item['checks'] = dict(item.get('checks', {})) | {capability: {
                    'ok': error is None, 'error_code': error, 'checked_at': datetime.now(timezone.utc).isoformat()}}
                saved[provider] = item
                self._write(db, actor, workspace, saved)
            return service._public(db, workspace) | {'ok': error is None}

    def synthesize(self, actor, workspace, sample):
        service = self.service
        with service.accounts.transaction(actor, snapshot=True) as db:
            service.accounts._workspace(db, actor, workspace, manage=True)
            row = service._settings(db, workspace)
            saved = config(row)
            provider = saved.get('synthesis_provider', 'disabled')
            if provider not in ('yandex', *PROVIDERS):
                raise AuthError(409, 'Сначала выберите шлюз синтеза речи.')
            if row['monthly_limit_rub'] is not None and service._month_spent(db, workspace) >= float(row['monthly_limit_rub']):
                raise AuthError(429, 'Месячный лимит исчерпан.')
            creds = service._credentials(db, workspace, yandex=False, selected={provider})
        kwargs = {'api_key': creds['yandex']} if provider == 'yandex' else options(creds, provider)
        if not kwargs['api_key']:
            raise AuthError(409, 'Шлюз синтеза не подключён.')
        service.accounts.throttle(f'ai-tts:{workspace}:{actor.user_id}', limit=10, minutes=1)
        try:
            if provider == 'yandex':
                result = service.client.synthesize(sample, voice=saved.get('synthesis_voice', 'marina'), **kwargs)
            else:
                result = service.mimo.synthesize(sample, plan=provider == 'mimo_plan',
                    voice=saved.get('synthesis_voice', 'Mia'), **kwargs)
        except YandexError as failure:
            service._usage(actor, workspace, kind='synthesis', outcome='error', error=failure.code)
            from center_messenger.ai_voice import _fail
            raise _fail(failure.code, provider) from None
        cost = tts_cost(sample) if provider == 'yandex' else 0
        service._usage(actor, workspace, kind='synthesis', seconds=result['seconds'], cost=cost)
        return result | {'billing': 'pay_as_you_go' if provider == 'yandex' else 'subscription' if provider == 'mimo_plan' else 'promotional',
                         'cost_rub': round(cost, 4), 'provider': provider}


    def verify_yandex_synthesis(self, actor, workspace):
        service = self.service
        with service.accounts.transaction(actor, snapshot=True) as db:
            service.accounts._workspace(db, actor, workspace, manage=True)
            row = service._settings(db, workspace)
            if row and row['monthly_limit_rub'] is not None and service._month_spent(db, workspace) >= float(row['monthly_limit_rub']):
                raise AuthError(429, 'Месячный лимит исчерпан.')
            key = service._credentials(db, workspace, selected={'yandex'})['yandex']
            cipher, folder = row['key_ciphertext'], row['folder_id']
        service.accounts.throttle(f'ai-tts-check:{workspace}:{actor.user_id}', limit=10, minutes=1)
        error, cost = None, 0
        try:
            service.client.synthesize('Проверка синтеза речи.', api_key=key, voice='marina')
            cost = tts_cost('Проверка синтеза речи.')
        except YandexError as failure:
            error = failure.code
        service._usage(actor, workspace, kind='verify', cost=cost, outcome='error' if error else 'ok', error=error)
        with service.accounts.transaction(actor) as db:
            service.accounts._workspace(db, actor, workspace, manage=True)
            row = service._settings(db, workspace, lock=True)
            saved = config(row)
            if row['key_ciphertext'] == cipher and row['folder_id'] == folder:
                saved['yandex'] = {'checks': {'synthesis': {'ok': error is None, 'error_code': error,
                    'checked_at': datetime.now(timezone.utc).isoformat()}}}
                self._write(db, actor, workspace, saved)
            return service._public(db, workspace) | {'ok': error is None}
