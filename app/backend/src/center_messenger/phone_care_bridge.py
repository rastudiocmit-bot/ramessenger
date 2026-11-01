"""API-side authenticated proxy. Business processing remains in telephony-worker."""
import http.client
import json
from center_messenger.auth_security import AuthError
from center_messenger.telephony_audio import control_key

MAX_RESPONSE = 1024*1024


class CareBridge:
    def __init__(self, accounts, config, *, connection_factory=http.client.HTTPConnection):
        self.accounts, self.config, self.connection_factory = accounts, config, connection_factory

    def call(self, actor, workspace, action, data):
        # The browser never supplies actor claims or the private worker key.
        with self.accounts.transaction(actor, snapshot=True) as db:
            self.accounts._workspace_chat(db, actor, workspace)
        payload = {'contract':'phone-family-care-v1','workspace': str(workspace), 'actor': {key: str(getattr(actor,key)) if key != 'auth_epoch' else actor.auth_epoch for key in ('user_id','session_id','auth_epoch')},
                   'action': action, 'data': data}
        body = json.dumps(payload, default=str, ensure_ascii=False).encode()
        if len(body) > 32768: raise AuthError(413, 'Слишком большой запрос.')
        connection = self.connection_factory('telephony-worker', 8002, timeout=20)
        try:
            connection.request('POST', '/care', body, {'Content-Type':'application/json', 'X-CM-Telephony-Key':control_key(self.config)})
            response = connection.getresponse(); raw = response.read(MAX_RESPONSE+1)
            if len(raw) > MAX_RESPONSE: raise AuthError(502, 'Ответ телефонии слишком большой. Уточните фильтры.')
            try: value = json.loads(raw)
            except (ValueError, UnicodeError): raise AuthError(502, 'Не удалось получить ответ телефонии.') from None
            if response.status != 200:
                status = response.status if response.status in (401,403,404,409,413,422,429,503) else 502
                detail = value.get('detail') if isinstance(value,dict) else None
                if response.status==404 and detail=='Not Found': raise AuthError(503, 'Контейнер телефонии ещё не обновлён для сопровождения семей.')
                raise AuthError(status, detail if isinstance(detail,str) else 'Телефония пока недоступна.')
            if not isinstance(value, dict): raise AuthError(502, 'Не удалось получить ответ телефонии.')
            if value.get('contract')!='phone-family-care-v1': raise AuthError(503, 'Нужно согласованное обновление API и контейнера телефонии.')
            return value
        except (OSError, http.client.HTTPException):
            raise AuthError(503, 'Сервис телефонии недоступен. Проверьте состояние контейнера телефонии.') from None
        finally: connection.close()
