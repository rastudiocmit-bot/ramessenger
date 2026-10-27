"""MiMo gateways: documented chat/Responses, WAV recognition and speech synthesis.

Only official HTTPS endpoints are accepted. Keys never go through a browser or
redirect, and provider errors are mapped to safe codes without their bodies.
"""
import base64
import io
import json
import math
import re
import time
import urllib.error
import urllib.request
import wave

from center_messenger.ai_yandex import FIXED_RULES, SAMPLE_RATE, YandexError, check_audio

PAYG_URL = "https://api.xiaomimimo.com/v1"
PLAN_URL = "https://token-plan-sgp.xiaomimimo.com/v1"
PLAN_URLS = (PLAN_URL, "https://token-plan-cn.xiaomimimo.com/v1")
VOICES = ("mimo_default", "Mia", "Chloe", "Milo", "Dean", "冰糖", "茉莉", "苏打", "白桦")
USD_RUB = 90.0  # budget estimate, not a claimed live exchange rate
MODELS = {
    "mimo-v2.6-flash": {"title": "MiMo V2.6 Flash", "input": .14, "output": .28},
    "mimo-v2.6-pro": {"title": "MiMo V2.6 Pro", "input": .435, "output": .87},
}
PLAN_MODELS = {key.replace("mimo-", "mimo-plan-", 1): value | {"title": value["title"] + " · Token Plan"}
               for key, value in MODELS.items()}


def valid_key(value, *, plan=False):
    return isinstance(value, str) and bool(re.fullmatch(r"(?:tp|ttp)-[A-Za-z0-9_-]{16,190}" if plan
                                                       else r"sk-[A-Za-z0-9_-]{16,190}", value.strip()))


def endpoint(value, *, plan=False):
    value = str(value).rstrip("/")
    if value not in (PLAN_URLS if plan else (PAYG_URL,)):
        raise YandexError("gateway_url")
    return value


def model_id(model):
    if model not in MODELS and model not in PLAN_MODELS:
        raise YandexError("model_unknown")
    return model.replace("mimo-plan-", "mimo-", 1)


def audio_cost(seconds, *, plan=False):
    return 0.0 if plan else math.ceil(seconds) / 3600 * .074 * USD_RUB


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class MimoClient:
    def __init__(self, opener=None, sleep=time.sleep):
        self.opener = opener or urllib.request.build_opener(NoRedirect()).open
        self.sleep = sleep

    def _post(self, body, *, api_key, base_url=PAYG_URL, protocol="chat", plan=False, limit=2_000_000):
        base_url = endpoint(base_url, plan=plan)
        if protocol not in ("chat", "responses"):
            raise YandexError("bad_request")
        request = urllib.request.Request(base_url + ("/responses" if protocol == "responses" else "/chat/completions"),
            data=json.dumps(body, ensure_ascii=False).encode(), method="POST",
            headers={"api-key": api_key.strip(), "Content-Type": "application/json"})
        for attempt in range(2):
            try:
                with self.opener(request, timeout=25) as response:
                    raw = response.read(limit + 1)
                break
            except urllib.error.HTTPError as error:
                if error.code == 429 and attempt == 0:
                    self.sleep(1)
                    continue
                code = ("unauthorized" if error.code in (401,403) else "no_balance" if error.code == 402
                        else "rate_limited" if error.code == 429 else "bad_request" if error.code in (400,413,422)
                        else "unavailable")
                raise YandexError(code, error.code) from None
            except (urllib.error.URLError, TimeoutError, OSError):
                raise YandexError("network") from None
        if len(raw) > limit:
            raise YandexError("bad_response")
        try:
            result = json.loads(raw)
            if not isinstance(result, dict) or result.get("error"):
                raise ValueError()
            return result
        except (ValueError, TypeError):
            raise YandexError("bad_response") from None

    @staticmethod
    def _chat_result(payload):
        try:
            choice = payload["choices"][0]
            text = choice["message"]["content"]
            finish = choice.get("finish_reason")
            usage = payload.get("usage") or {}
            incoming, outgoing = int(usage.get("prompt_tokens",0)), int(usage.get("completion_tokens",0))
            if not isinstance(text,str) or incoming < 0 or outgoing < 0 or finish not in (None, "stop", "length"):
                raise ValueError()
            return {"text":text, "input_tokens":incoming, "output_tokens":outgoing, "truncated":finish == "length"}
        except (ValueError, KeyError, IndexError, TypeError, AttributeError, OverflowError):
            raise YandexError("bad_response") from None

    def complete(self, text, *, prompt, model, temperature, api_key, max_tokens=1500,
                 base_url=PAYG_URL, protocol="chat", rules=True, **_):
        plan = model in PLAN_MODELS
        instructions = prompt.strip() + ("\n\n" + FIXED_RULES if rules else "")
        if protocol == "responses":
            body = {"model":model_id(model), "instructions":instructions, "input":text,
                    "max_output_tokens":int(max_tokens), "reasoning":{"effort":"none"},
                    "temperature":float(temperature), "stream":False}
            payload = self._post(body, api_key=api_key, base_url=base_url, protocol=protocol, plan=plan)
            try:
                output = "".join(part["text"] for item in payload["output"] if item.get("type") == "message"
                                 for part in item.get("content",[]) if part.get("type") == "output_text")
                usage = payload.get("usage") or {}
                incoming, outgoing = int(usage.get("input_tokens",0)), int(usage.get("output_tokens",0))
                if not output or incoming < 0 or outgoing < 0 or payload.get("status") in ("failed","cancelled"):
                    raise ValueError()
                return {"text":output,"input_tokens":incoming,"output_tokens":outgoing,
                        "truncated":payload.get("status") == "incomplete"}
            except (ValueError,KeyError,TypeError,AttributeError,OverflowError):
                raise YandexError("bad_response") from None
        body = {"model":model_id(model), "messages":[{"role":"system","content":instructions},{"role":"user","content":text}],
                "thinking":{"type":"disabled"}, "max_completion_tokens":int(max_tokens),
                "temperature":float(temperature), "stream":False}
        return self._chat_result(self._post(body,api_key=api_key,base_url=base_url,plan=plan))

    def recognize(self, audio, *, api_key, base_url=PAYG_URL, plan=False, language="auto"):
        check_audio(audio)
        if language not in ("ru","auto","en","zh"):
            raise YandexError("speech_language")
        # MiMo does not document a Russian ASR language tag. Let its auto mode
        # handle Russian recordings without sending an unsupported `ru` value.
        api_language = "auto" if language == "ru" else language
        output=io.BytesIO()
        with wave.open(output,"wb") as wav:
            wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(SAMPLE_RATE);wav.writeframes(audio)
        encoded=base64.b64encode(output.getvalue()).decode()
        if len(encoded)>10_000_000:
            raise YandexError("audio_long")
        body={"model":"mimo-v2.5-asr","messages":[{"role":"user","content":[{"type":"input_audio",
              "input_audio":{"data":"data:audio/wav;base64,"+encoded}}]}],
              "asr_options":{"language":api_language},"stream":False}
        result=self._chat_result(self._post(body,api_key=api_key,base_url=base_url,plan=plan))
        if result["truncated"]:
            raise YandexError("bad_response")
        return result

    def synthesize(self, text, *, api_key, base_url=PAYG_URL, plan=False, voice="Mia"):
        if voice not in VOICES or not isinstance(text,str) or not 1<=len(text.strip())<=500:
            raise YandexError("bad_request")
        body={"model":"mimo-v2.5-tts","messages":[{"role":"assistant","content":text.strip()}],
              "audio":{"format":"wav","voice":voice},"stream":False}
        payload=self._post(body,api_key=api_key,base_url=base_url,plan=plan,limit=12_000_000)
        try:
            choice=payload["choices"][0]
            if choice.get("finish_reason") not in (None,"stop"):
                raise ValueError()
            audio=base64.b64decode(choice["message"]["audio"]["data"],validate=True)
            with wave.open(io.BytesIO(audio),"rb") as wav:
                seconds=wav.getnframes()/wav.getframerate()
                if seconds<=0 or seconds>180:
                    raise ValueError()
            return {"audio":base64.b64encode(audio).decode(),"content_type":"audio/wav","seconds":seconds}
        except (ValueError,KeyError,IndexError,TypeError,AttributeError,wave.Error,EOFError,ZeroDivisionError):
            raise YandexError("bad_response") from None
