"""xAI text and telephone ASR. Fixed origin, sealed key, no paid retries."""
import io
import json
import http.client
import math
import re
import urllib.error
import urllib.request
import wave
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid4, uuid5

from center_messenger.ai_yandex import FIXED_RULES, YandexError, key_hint

BASE_URL = "https://api.x.ai/v1"
SECRET_ID = uuid5(NAMESPACE_URL, "center-messenger:grok-ai")
PROXY_SECRET_ID = uuid5(NAMESPACE_URL, "center-messenger:grok-proxy")
# The vault's existing generic connection envelope binds workspace + connection
# identity. A distinct Grok identity and payload marker isolate this account
# without changing the pinned vault contract or the database schema.
PURPOSE = "alfa"
USD_RUB = 90.0  # conservative budget estimate, not a live exchange rate
ASR_MODEL = "grok-voice-transcribe-2.0"
ASR_SAMPLE_RATE = 16000
ASR_MAX_SECONDS = 25  # one bounded telephone channel fragment
MODELS = {name: {"title": title, "input": .18, "output": .54} for name, title in (
    ("grok-4.7", "Grok 4.7"), ("grok-4.6", "Grok 4.6"))}


def audio_cost(seconds):
    # REST STT: $0.10/hour. Round up for the budget reservation, not an invoice.
    return math.ceil(seconds) / 3600 * .10 * USD_RUB


def valid_key(value):
    return isinstance(value, str) and re.fullmatch(r"xai-[A-Za-z0-9_-]{16,196}", value) is not None


def stored(row):
    return dict((row.get("gateway_config") or {}).get("grok") or {}) if row else {}


def public(row):
    item = stored(row)
    configured = bool(item.get("key_ciphertext"))
    checks = item.get("checks", {}) if configured else {}
    check = checks.get("text", {})
    return {"configured": configured, "key_hint": item.get("key_hint", "") if configured else "",
            "status": "active" if check.get("ok") is True else "error" if check.get("ok") is False else "not_configured",
            "checks": checks, "models": item.get("models", []) if configured else [],
            "base_url": BASE_URL, "protocol": "responses", "billing": "pay_as_you_go",
            "proxy": {"configured": bool(item.get("proxy_ciphertext")), "enabled": item.get("proxy_enabled") is True,
                      "endpoint": item.get("proxy_endpoint", ""), "protocol": item.get("proxy_protocol", "socks5")}}


def connection_stamp(row):
    item = stored(row)
    if item.get("proxy_enabled") is True:
        return {"key": item.get("key_ciphertext"), "proxy": item.get("proxy_ciphertext"),
                "protocol": item.get("proxy_protocol")}
    return item.get("key_ciphertext")


def proxy_credentials(service, row, workspace, selected=None):
    item = stored(row)
    if item.get("proxy_enabled") is not True or selected is not None and "grok" not in selected:
        return None
    from center_messenger.auth_security import AuthError
    from center_messenger.grok_proxy_probe import parse
    if not item.get("proxy_ciphertext"):
        raise AuthError(503, "Не удалось прочитать сохранённый прокси Grok.")
    payload = service.vault.open(workspace, PROXY_SECRET_ID, item["proxy_ciphertext"], purpose=PURPOSE)
    if payload.get("provider") != "grok_proxy" or payload.get("protocol") not in ("http", "socks5"):
        raise AuthError(503, "Не удалось прочитать сохранённый прокси Grok.")
    return parse(payload.get("api_key"), payload["protocol"])


def credentials(service, row, workspace, selected=None):
    cipher = stored(row).get("key_ciphertext")
    if not cipher or (selected is not None and "grok" not in selected):
        return None
    payload = service.vault.open(workspace, SECRET_ID, cipher, purpose=PURPOSE)
    if payload.get("provider") != "grok":
        from center_messenger.auth_security import AuthError
        raise AuthError(503, "Не удалось прочитать ключ Grok.")
    return payload["api_key"]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class GrokClient:
    def __init__(self, opener=None):
        self.opener = opener or urllib.request.build_opener(NoRedirect()).open

    def _request(self, path, *, api_key, body=None, timeout=25, proxy=None, content_type="application/json"):
        request = urllib.request.Request(BASE_URL + path,
            data=body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode() if body is not None else None,
            method="POST" if body is not None else "GET",
            headers={"Authorization": "Bearer " + api_key.strip(), "Content-Type": content_type})
        def failure(status):
            return ("unauthorized" if status == 401 else "forbidden" if status == 403 else "no_balance" if status == 402
                    else "rate_limited" if status == 429 else "model_unknown" if status == 404
                    else "bad_request" if status in (400, 413, 422) else "unavailable")
        opener = self.opener
        if proxy is not None:
            from center_messenger.grok_proxy_probe import ProxyOpener
            opener = ProxyOpener(proxy, allow_inference=True).open
        try:
            with opener(request, timeout=timeout) as response:
                raw = response.read(2_000_001)
                status = getattr(response, "status", 200)
                if status != 200:
                    raise YandexError(failure(status), status)
        except urllib.error.HTTPError as error:
            raise YandexError(failure(error.code), error.code) from None
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as error:
            from center_messenger.grok_proxy_probe import ProxyFailure
            raise YandexError(error.code if isinstance(error, ProxyFailure) else "network") from None
        try:
            if len(raw) > 2_000_000:
                raise ValueError()
            value = json.loads(raw)
            if not isinstance(value, dict) or value.get("error"):
                raise ValueError()
            return value
        except (ValueError, TypeError):
            raise YandexError("bad_response") from None

    def models(self, *, api_key, proxy=None):
        payload = self._request("/language-models", api_key=api_key, timeout=10, proxy=proxy)
        try:
            models = payload["models"]
            if not isinstance(models, list):
                raise ValueError()
            available = set()
            for item in models:
                if "text" not in item.get("input_modalities", []) or "text" not in item.get("output_modalities", []):
                    continue
                available.update([item["id"], *item.get("aliases", [])])
            return [name for name in MODELS if name in available]
        except (ValueError, KeyError, TypeError, AttributeError):
            raise YandexError("bad_response") from None

    def recognize(self, audio, *, api_key, proxy=None, language="ru"):
        # Only decoded mono PCM16/16kHz is accepted, never a recording URL.
        if not isinstance(audio, bytes) or not 3200 <= len(audio) <= ASR_MAX_SECONDS * ASR_SAMPLE_RATE * 2 or len(audio) % 2:
            raise YandexError("bad_audio")
        if language != "ru":
            raise YandexError("speech_language")
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(ASR_SAMPLE_RATE)
            wav.writeframes(audio)
        boundary = "cm-grok-" + uuid4().hex
        parts = []
        # xAI requires the file to follow ALL option fields in the form.
        for name, value in (("model", ASR_MODEL), ("language", language),
                            ("format", "false"), ("filler_words", "true"), ("diarize", "false")):
            parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n').encode())
        parts.extend([(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="call.wav"\r\n'
                       'Content-Type: audio/wav\r\n\r\n').encode(), output.getvalue(),
                      f'\r\n--{boundary}--\r\n'.encode()])
        payload = self._request("/stt", api_key=api_key, body=b"".join(parts), proxy=proxy,
                                content_type="multipart/form-data; boundary=" + boundary)
        raw = payload.get("text")
        if not isinstance(raw, str) or len(raw) > 20000:
            raise YandexError("bad_response")
        return {"text": raw}

    def complete(self, text, *, prompt, model, temperature, api_key, max_tokens=1500,
                 rules=True, timeout=25, proxy=None, **_):
        if model not in MODELS:
            raise YandexError("model_unknown")
        body = {"model": model, "input": [
            {"role": "system", "content": prompt.strip() + ("\n\n" + FIXED_RULES if rules else "")},
            {"role": "user", "content": text}],
            "temperature": float(temperature), "max_output_tokens": int(max_tokens),
            "reasoning": {"effort": "low"}, "stream": False, "store": False}
        payload = self._request("/responses", api_key=api_key, body=body, timeout=timeout, proxy=proxy)
        try:
            usage = payload["usage"]
            # xAI Responses exposes prompt/completion token counters. Accept the
            # standard Responses spellings too, without silently billing zero.
            incoming = int(usage["input_tokens"] if "input_tokens" in usage else usage["prompt_tokens"])
            outgoing = int(usage["output_tokens"] if "output_tokens" in usage else usage["completion_tokens"])
            if "total_tokens" in usage:
                outgoing = max(outgoing, int(usage["total_tokens"]) - incoming)
            output = "".join(part["text"] for item in payload["output"] if item.get("type") == "message"
                             and item.get("role") == "assistant" for part in item.get("content", [])
                             if part.get("type") == "output_text")
            status = payload["status"]
            if status not in ("completed", "incomplete") or incoming < 0 or outgoing < 0:
                raise ValueError()
            if not output.strip() and status != "incomplete":
                raise ValueError()
            return {"text": output, "input_tokens": incoming, "output_tokens": outgoing,
                    "truncated": status == "incomplete"}
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
            raise YandexError("bad_response") from None


class GrokGateway:
    def __init__(self, gateways):
        self.gateways, self.service = gateways, gateways.service

    def save(self, actor, workspace, data):
        from sqlalchemy import text
        from center_messenger.accounts import audit
        from center_messenger.auth_security import AuthError
        key, clear = str(data.get("api_key") or "").strip(), bool(data.get("clear_key"))
        if key and clear:
            raise AuthError(422, "Сохранение и удаление ключа — разные действия.")
        if key and not valid_key(key):
            raise AuthError(422, "Проверьте API-ключ xAI: он должен начинаться с xai- и не содержать пробелов.")
        if data.get("base_url") not in (None, BASE_URL) or data.get("protocol") not in (None, "responses"):
            raise AuthError(422, "Для Grok используется официальный API xAI и протокол Responses.")
        service = self.service
        with service.accounts.transaction(actor) as db:
            service.accounts._workspace(db, actor, workspace, manage=True)
            db.execute(text("INSERT INTO ai_settings(workspace_id) VALUES (:w) ON CONFLICT DO NOTHING"), {"w": workspace})
            row = service._settings(db, workspace, lock=True)
            saved, item = dict(row.get("gateway_config") or {}), stored(row)
            if key:
                item = {k: v for k, v in item.items() if k.startswith("proxy_")} | {"key_ciphertext": service.vault.seal(workspace, SECRET_ID, {"api_key": key, "provider": "grok"}, purpose=PURPOSE),
                        "key_hint": key_hint(key)}
            if clear:
                item = {}
            saved["grok"] = item
            self.gateways._write(db, actor, workspace, saved)
            audit(db, "ai_gateway_updated", user_id=actor.user_id, workspace_id=workspace)
            return service._public(db, workspace)

    def save_proxy(self, actor, workspace, data):
        from center_messenger.accounts import audit
        from center_messenger.auth_security import AuthError
        from center_messenger.grok_proxy_probe import parse
        enabled, clear = data['enabled'], data.get('clear_proxy', False)
        value, protocol = (data.get('proxy') or '').strip(), data['protocol']
        if protocol not in ('http', 'socks5'):
            raise AuthError(422, 'Для постоянного подключения выберите HTTP CONNECT или SOCKS5.')
        if clear and (value or enabled):
            raise AuthError(422, 'Сохранение и удаление прокси — разные действия.')
        parsed = parse(value, protocol) if value else None
        with self.service.accounts.transaction(actor) as db:
            service = self.service
            service.accounts._workspace(db, actor, workspace, manage=True)
            row = service._settings(db, workspace, lock=True)
            item = stored(row)
            if not item.get('key_ciphertext'):
                raise AuthError(409, 'Сначала сохраните API-ключ Grok.')
            original = connection_stamp(row)
            if clear:
                item = {k: v for k, v in item.items() if not k.startswith('proxy_')}
            else:
                if not value and item.get('proxy_ciphertext') and item.get('proxy_protocol') != protocol:
                    payload = service.vault.open(workspace, PROXY_SECRET_ID, item['proxy_ciphertext'], purpose=PURPOSE)
                    if payload.get('provider') != 'grok_proxy':
                        raise AuthError(503, 'Не удалось прочитать сохранённый прокси Grok.')
                    value = payload.get('api_key')
                    parsed = parse(value, protocol)
                if value:
                    item.update(proxy_ciphertext=service.vault.seal(workspace, PROXY_SECRET_ID,
                        {'provider': 'grok_proxy', 'api_key': value, 'protocol': protocol}, purpose=PURPOSE),
                        proxy_protocol=protocol, proxy_endpoint=f'{parsed.host}:{parsed.port}')
                if enabled and not item.get('proxy_ciphertext'):
                    raise AuthError(422, 'Введите данные прокси перед включением.')
                item['proxy_enabled'] = enabled
            if connection_stamp({'gateway_config': {'grok': item}}) != original:
                item.pop('checks', None); item.pop('models', None)
            saved = dict(row.get('gateway_config') or {}); saved['grok'] = item
            self.gateways._write(db, actor, workspace, saved)
            audit(db, 'ai_grok_proxy_updated', user_id=actor.user_id, workspace_id=workspace)
            return service._public(db, workspace)

    def verify(self, actor, workspace, capability):
        from center_messenger.auth_security import AuthError
        if capability != "text":
            raise AuthError(422, "В этом подключении Grok используется только для обработки текста.")
        service = self.service
        with service.accounts.transaction(actor, snapshot=True) as db:
            service.accounts._workspace(db, actor, workspace, manage=True)
            row = service._settings(db, workspace)
            if row and row["monthly_limit_rub"] is not None and service._month_spent(db, workspace) >= float(row["monthly_limit_rub"]):
                raise AuthError(429, "Месячный лимит исчерпан.")
            original_connection = connection_stamp(row)
            key = credentials(service, row, workspace, {"grok"})
            proxy = proxy_credentials(service, row, workspace, {"grok"})
        if not key:
            raise AuthError(409, "Сначала сохраните API-ключ Grok.")
        service.accounts.throttle(f"ai-grok-check:{workspace}:{actor.user_id}", limit=5, minutes=1)
        error, models, tokens, cost = None, [], (0, 0), 0.0
        try:
            models = service.grok.models(api_key=key, proxy=proxy)
            if not models:
                raise YandexError("model_unknown")
            answer = service.grok.complete("Ready?", prompt="Reply only: OK.", model=models[0],
                temperature=0, api_key=key, max_tokens=256, rules=False, proxy=proxy)
            tokens = answer["input_tokens"], answer["output_tokens"]
            price = MODELS[models[0]]
            cost = (tokens[0] * price["input"] + tokens[1] * price["output"]) / 1000
            if answer["truncated"] or not answer["text"].strip():
                raise YandexError("bad_response")
        except YandexError as failure:
            error = failure.code
        service._usage(actor, workspace, kind="verify", tokens=tokens, cost=cost,
                       outcome="error" if error else "ok", error=error)
        with service.accounts.transaction(actor) as db:
            service.accounts._workspace(db, actor, workspace, manage=True)
            row = service._settings(db, workspace, lock=True)
            if connection_stamp(row) != original_connection:
                return service._public(db, workspace) | {"ok": False, "stale": True}
            saved, item = dict(row.get("gateway_config") or {}), stored(row)
            item.update(models=models, checks={"text": {"ok": error is None, "error_code": error,
                        "checked_at": datetime.now(timezone.utc).isoformat()}})
            saved["grok"] = item
            self.gateways._write(db, actor, workspace, saved)
            return service._public(db, workspace) | {"ok": error is None}
