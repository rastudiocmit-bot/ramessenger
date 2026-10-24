"""DeepSeek chat completions for AI agents (OpenAI-compatible API).

Standard library only, the same contract as YandexClient.complete so an agent
can use either provider. Thinking mode is switched off: the agent rewrites a
short message and must answer fast. Prices are in US dollars per million
tokens; they are converted to roubles at a fixed, deliberately cautious rate
and at the peak tariff, so the monthly limit errs on the safe side.
"""

import json
import re
import urllib.error
import urllib.request

from center_messenger.ai_yandex import FIXED_RULES, YandexError

URL = "https://api.deepseek.com/chat/completions"
TIMEOUT = 20
USD_RUB = 90.0
KEY_PATTERN = re.compile(r"sk-[A-Za-z0-9]{16,200}")

# Peak prices, USD per 1M tokens (cache miss), api-docs.deepseek.com/quick_start/pricing.
MODELS = {
    "deepseek-flash": {"title": "DeepSeek V4.1 Flash", "input_usd": 0.3, "output_usd": 1.2},
    "deepseek-v4-pro": {"title": "DeepSeek V4 Pro", "input_usd": 1.32, "output_usd": 3.96},
}


def valid_key(value):
    return isinstance(value, str) and KEY_PATTERN.fullmatch(value.strip()) is not None


def rub_per_1k(model):
    price = MODELS[model]
    return {"input": price["input_usd"] * USD_RUB / 1000, "output": price["output_usd"] * USD_RUB / 1000}


def _status_code(status):
    if status in (401, 403):
        return "unauthorized"
    if status == 402:
        return "no_balance"
    if status == 429:
        return "rate_limited"
    if status in (400, 413, 422):
        return "bad_request"
    return "unavailable"


class DeepSeekClient:
    def __init__(self, opener=None):
        self.opener = opener or urllib.request.urlopen

    def complete(self, text, *, prompt, model, temperature, api_key, max_tokens=1500, **_):
        if model not in MODELS:
            raise YandexError("model_unknown")
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": f"{prompt.strip()}\n\n{FIXED_RULES}"},
                {"role": "user", "content": text},
            ],
            "thinking": {"type": "disabled"},
            "temperature": float(temperature),
            "max_tokens": int(max_tokens),
            "stream": False,
        }
        request = urllib.request.Request(
            URL, data=json.dumps(body, ensure_ascii=False).encode(), method="POST",
            headers={"Authorization": f"Bearer {api_key.strip()}", "Content-Type": "application/json"},
        )
        try:
            with self.opener(request, timeout=TIMEOUT) as response:
                raw = response.read(2_000_001)
        except urllib.error.HTTPError as error:
            raise YandexError(_status_code(error.code), error.code) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise YandexError("network") from None
        if len(raw) > 2_000_000:
            raise YandexError("bad_response")
        try:
            payload = json.loads(raw)
            choice = payload["choices"][0]
            answer = choice["message"]["content"]
            finish = choice.get("finish_reason")
            usage = payload.get("usage") or {}
            input_tokens = int(usage.get("prompt_tokens") or 0)
            output_tokens = int(usage.get("completion_tokens") or 0)
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise YandexError("bad_response") from None
        if finish == "content_filter":
            raise YandexError("content_filter")
        if finish == "insufficient_system_resource":
            raise YandexError("unavailable")
        if not isinstance(answer, str):
            raise YandexError("bad_response")
        return {"text": answer, "input_tokens": input_tokens, "output_tokens": output_tokens,
                "truncated": finish == "length"}
