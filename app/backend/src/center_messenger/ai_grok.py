"""xAI text gateway. Fixed HTTPS origin, sealed key, no automatic paid retries."""
import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

from center_messenger.ai_yandex import FIXED_RULES, YandexError, key_hint

BASE_URL = "https://api.x.ai/v1"
SECRET_ID = uuid5(NAMESPACE_URL, "center-messenger:grok-ai")
# The vault's existing generic connection envelope binds workspace + connection
# identity. A distinct Grok identity and payload marker isolate this account
# without changing the pinned vault contract or the database schema.
PURPOSE = "alfa"
USD_RUB = 90.0  # conservative budget estimate, not a live exchange rate
MODELS = {name: {"title": title, "input": .18, "output": .54} for name, title in (
    ("grok-4.7", "Grok 4.7"), ("grok-4.6", "Grok 4.6"))}


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
            "base_url": BASE_URL, "protocol": "responses", "billing": "pay_as_you_go"}


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

    def _request(self, path, *, api_key, body=None, timeout=25):
        request = urllib.request.Request(BASE_URL + path,
            data=json.dumps(body, ensure_ascii=False).encode() if body is not None else None,
            method="POST" if body is not None else "GET",
            headers={"Authorization": "Bearer " + api_key.strip(), "Content-Type": "application/json"})
        try:
            with self.opener(request, timeout=timeout) as response:
                raw = response.read(2_000_001)
        except urllib.error.HTTPError as error:
            code = ("unauthorized" if error.code in (401, 403) else "no_balance" if error.code == 402
                    else "rate_limited" if error.code == 429 else "model_unknown" if error.code == 404
                    else "bad_request" if error.code in (400, 413, 422) else "unavailable")
            raise YandexError(code, error.code) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise YandexError("network") from None
        try:
            if len(raw) > 2_000_000:
                raise ValueError()
            value = json.loads(raw)
            if not isinstance(value, dict) or value.get("error"):
                raise ValueError()
            return value
        except (ValueError, TypeError):
            raise YandexError("bad_response") from None

    def models(self, *, api_key):
        payload = self._request("/language-models", api_key=api_key, timeout=10)
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

    def complete(self, text, *, prompt, model, temperature, api_key, max_tokens=1500,
                 rules=True, timeout=25, **_):
        if model not in MODELS:
            raise YandexError("model_unknown")
        body = {"model": model, "input": [
            {"role": "system", "content": prompt.strip() + ("\n\n" + FIXED_RULES if rules else "")},
            {"role": "user", "content": text}],
            "temperature": float(temperature), "max_output_tokens": int(max_tokens),
            "reasoning": {"effort": "low"}, "stream": False, "store": False}
        payload = self._request("/responses", api_key=api_key, body=body, timeout=timeout)
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
                item = {"key_ciphertext": service.vault.seal(workspace, SECRET_ID, {"api_key": key, "provider": "grok"}, purpose=PURPOSE),
                        "key_hint": key_hint(key)}
            if clear:
                item = {}
            saved["grok"] = item
            self.gateways._write(db, actor, workspace, saved)
            audit(db, "ai_gateway_updated", user_id=actor.user_id, workspace_id=workspace)
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
            original_cipher = stored(row).get("key_ciphertext")
            key = credentials(service, row, workspace, {"grok"})
        if not key:
            raise AuthError(409, "Сначала сохраните API-ключ Grok.")
        service.accounts.throttle(f"ai-grok-check:{workspace}:{actor.user_id}", limit=5, minutes=1)
        error, models, tokens, cost = None, [], (0, 0), 0.0
        try:
            models = service.grok.models(api_key=key)
            if not models:
                raise YandexError("model_unknown")
            answer = service.grok.complete("Ready?", prompt="Reply only: OK.", model=models[0],
                temperature=0, api_key=key, max_tokens=256, rules=False)
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
            if stored(row).get("key_ciphertext") != original_cipher:
                return service._public(db, workspace) | {"ok": False, "stale": True}
            saved, item = dict(row.get("gateway_config") or {}), stored(row)
            item.update(models=models, checks={"text": {"ok": error is None, "error_code": error,
                        "checked_at": datetime.now(timezone.utc).isoformat()}})
            saved["grok"] = item
            self.gateways._write(db, actor, workspace, saved)
            return service._public(db, workspace) | {"ok": error is None}
