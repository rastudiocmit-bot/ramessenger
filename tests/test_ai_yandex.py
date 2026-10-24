"""Yandex Cloud client for voice input: request shape, parsing, errors, guards.

Uses a fake transport; no network, no Yandex account and no database needed.
"""

import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app" / "backend" / "src"))

from center_messenger import ai_yandex as y  # noqa: E402

KEY = "AQVN" + "a" * 36
FOLDER = "b1gexamplefolder0000"


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class FakeOpener:
    def __init__(self, payload=None, error=None):
        self.payload, self.error, self.requests = payload, error, []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        if self.error:
            raise self.error
        body = self.payload if isinstance(self.payload, bytes) else json.dumps(self.payload).encode()
        return FakeResponse(body)


def http_error(status):
    return urllib.error.HTTPError("https://example.test", status, "x", {}, io.BytesIO(b"{}"))


class RecognizeTest(unittest.TestCase):
    def test_request_shape_and_result(self):
        opener = FakeOpener({"result": "  добрый   день  "})
        text = y.YandexClient(opener).recognize(y.silence(1), api_key=KEY)
        self.assertEqual(text, "добрый день")
        request, timeout = opener.requests[0]
        url = urlsplit(request.full_url)
        self.assertEqual(f"{url.scheme}://{url.netloc}{url.path}", y.STT_URL)
        query = parse_qs(url.query)
        self.assertEqual(query["format"], ["lpcm"])
        self.assertEqual(query["sampleRateHertz"], ["16000"])
        self.assertEqual(query["lang"], ["ru-RU"])
        self.assertEqual(query["profanityFilter"], ["true"])
        self.assertNotIn("folderId", query, "a service-account key must not send folderId")
        self.assertEqual(request.get_header("Authorization"), f"Api-Key {KEY}")
        self.assertEqual(request.get_header("X-data-logging-enabled"), "false")
        self.assertEqual(request.data, y.silence(1))
        self.assertEqual(timeout, y.STT_TIMEOUT)

    def test_empty_recognition_is_allowed(self):
        self.assertEqual(y.YandexClient(FakeOpener({"result": ""})).recognize(y.silence(), api_key=KEY), "")

    def test_errors_are_classified(self):
        for status, code in ((401, "unauthorized"), (403, "unauthorized"), (429, "rate_limited"),
                             (400, "bad_request"), (500, "unavailable"), (503, "unavailable")):
            with self.subTest(status=status), self.assertRaises(y.YandexError) as caught:
                y.YandexClient(FakeOpener(error=http_error(status))).recognize(y.silence(), api_key=KEY)
            self.assertEqual(caught.exception.code, code)
        with self.assertRaises(y.YandexError) as caught:
            y.YandexClient(FakeOpener(error=urllib.error.URLError("down"))).recognize(y.silence(), api_key=KEY)
        self.assertEqual(caught.exception.code, "network")
        for payload in (b"not json", {"oops": 1}, {"result": 5}):
            with self.subTest(payload=payload), self.assertRaises(y.YandexError) as caught:
                y.YandexClient(FakeOpener(payload)).recognize(y.silence(), api_key=KEY)
            self.assertEqual(caught.exception.code, "bad_response")


class CompleteTest(unittest.TestCase):
    answer = {"result": {"alternatives": [{"message": {"role": "assistant", "text": "Добрый день!"},
                                            "status": "ALTERNATIVE_STATUS_FINAL"}],
                         "usage": {"inputTextTokens": "120", "completionTokens": "8", "totalTokens": "128"}}}

    def test_request_shape_and_usage(self):
        opener = FakeOpener(self.answer)
        result = y.YandexClient(opener).complete("ну это добрый день", prompt="Убери паразитов", model="yandexgpt-5.1",
                                                 temperature=0.2, api_key=KEY, folder_id=FOLDER)
        self.assertEqual(result, {"text": "Добрый день!", "input_tokens": 120, "output_tokens": 8, "truncated": False})
        request, timeout = opener.requests[0]
        self.assertEqual(request.full_url, y.COMPLETION_URL)
        self.assertEqual(request.get_header("X-folder-id"), FOLDER)
        self.assertEqual(request.get_header("X-data-logging-enabled"), "false")
        body = json.loads(request.data)
        self.assertEqual(body["modelUri"], f"gpt://{FOLDER}/yandexgpt-5.1")
        self.assertEqual(body["completionOptions"]["temperature"], 0.2)
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])
        self.assertTrue(body["messages"][0]["text"].startswith("Убери паразитов"))
        self.assertIn(y.FIXED_RULES, body["messages"][0]["text"], "fixed rules always follow the owner's prompt")
        self.assertEqual(body["messages"][1]["text"], "ну это добрый день")
        self.assertEqual(timeout, y.COMPLETION_TIMEOUT)

    def test_unwrapped_response_and_statuses(self):
        unwrapped = self.answer["result"]
        self.assertEqual(y.YandexClient(FakeOpener(unwrapped)).complete(
            "x", prompt="p", model="aliceai-llm", temperature=0, api_key=KEY, folder_id=FOLDER)["text"], "Добрый день!")
        filtered = json.loads(json.dumps(unwrapped)); filtered["alternatives"][0]["status"] = "ALTERNATIVE_STATUS_CONTENT_FILTER"
        with self.assertRaises(y.YandexError) as caught:
            y.YandexClient(FakeOpener(filtered)).complete("x", prompt="p", model="yandexgpt-5.1", temperature=0,
                                                          api_key=KEY, folder_id=FOLDER)
        self.assertEqual(caught.exception.code, "content_filter")
        truncated = json.loads(json.dumps(unwrapped)); truncated["alternatives"][0]["status"] = "ALTERNATIVE_STATUS_TRUNCATED_FINAL"
        self.assertTrue(y.YandexClient(FakeOpener(truncated)).complete(
            "x", prompt="p", model="yandexgpt-5.1", temperature=0, api_key=KEY, folder_id=FOLDER)["truncated"])

    def test_unknown_model_is_refused_without_a_call(self):
        opener = FakeOpener(self.answer)
        with self.assertRaises(y.YandexError) as caught:
            y.YandexClient(opener).complete("x", prompt="p", model="gpt-4", temperature=0, api_key=KEY, folder_id=FOLDER)
        self.assertEqual(caught.exception.code, "model_unknown")
        self.assertEqual(opener.requests, [])


class GuardsTest(unittest.TestCase):
    def test_audio_limits(self):
        self.assertAlmostEqual(y.check_audio(y.silence(2)), 2.0)
        for audio, code in ((b"\x00" * 3, "audio_format"), (b"", "audio_short"),
                            (y.silence(0.05), "audio_short"), (bytes(y.MAX_AUDIO_BYTES + 2), "audio_long"),
                            ("text", "audio_format")):
            with self.subTest(code=code), self.assertRaises(y.YandexError) as caught:
                y.check_audio(audio)
            self.assertEqual(caught.exception.code, code)
        self.assertLessEqual(len(y.silence(30)), y.MAX_AUDIO_BYTES, "30 s of 16 kHz LPCM fits SpeechKit's 1 MB")

    def test_costs(self):
        self.assertAlmostEqual(y.stt_cost(30), 2 * y.STT_UNIT_RUB)
        self.assertAlmostEqual(y.stt_cost(15.1), 2 * y.STT_UNIT_RUB)
        self.assertAlmostEqual(y.stt_cost(1), y.STT_UNIT_RUB)
        self.assertAlmostEqual(y.model_cost("yandexgpt-5.1", 400, 150), 0.44)
        self.assertAlmostEqual(y.model_cost("aliceai-llm", 1000, 1000), 1.7)

    def test_normalized_text_guard(self):
        self.assertEqual(y.accept_normalized("ну привет", "  «Привет!»  "), "Привет!")
        self.assertIsNone(y.accept_normalized("привет", "   "))
        self.assertIsNone(y.accept_normalized("да", "x" * 200), "an answer far longer than the speech is rejected")
        self.assertIsNone(y.accept_normalized("да", None))

    def test_key_and_folder_validation(self):
        self.assertTrue(y.valid_key(KEY))
        self.assertFalse(y.valid_key("short"))
        self.assertFalse(y.valid_key("AQVN with spaces inside the key value"))
        self.assertTrue(y.valid_folder(FOLDER))
        self.assertFalse(y.valid_folder("B1G-UPPER"))
        self.assertEqual(y.key_hint(KEY), "aaaa")

class DeepSeekClientTest(unittest.TestCase):
    def test_request_and_answer(self):
        import json as _json
        from center_messenger.ai_deepseek import DeepSeekClient, valid_key
        seen = {}

        class Response:
            def __init__(self, body): self.body = body
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, n): return self.body

        def opener(request, timeout):
            seen["url"], seen["auth"] = request.full_url, request.headers["Authorization"]
            seen["body"] = _json.loads(request.data)
            return Response(_json.dumps({"choices": [{"message": {"content": "Готово."}, "finish_reason": "stop"}],
                                         "usage": {"prompt_tokens": 50, "completion_tokens": 3}}).encode())

        answer = DeepSeekClient(opener).complete("тест", prompt="P", model="deepseek-flash", temperature=0.2,
                                                 api_key="sk-" + "a" * 32, folder_id="ignored")
        self.assertEqual(answer, {"text": "Готово.", "input_tokens": 50, "output_tokens": 3, "truncated": False})
        self.assertEqual(seen["url"], "https://api.deepseek.com/chat/completions")
        self.assertEqual(seen["auth"], "Bearer sk-" + "a" * 32)
        self.assertEqual(seen["body"]["thinking"], {"type": "disabled"})
        self.assertEqual(seen["body"]["messages"][1], {"role": "user", "content": "тест"})
        self.assertTrue(valid_key("sk-" + "a" * 32)); self.assertFalse(valid_key("AQVN" + "a" * 32))

    def test_errors(self):
        import urllib.error
        from center_messenger.ai_deepseek import DeepSeekClient
        for status, code in ((401, "unauthorized"), (402, "no_balance"), (429, "rate_limited"), (500, "unavailable")):
            def opener(request, timeout, status=status):
                raise urllib.error.HTTPError(request.full_url, status, "x", {}, None)
            with self.subTest(status=status), self.assertRaises(y.YandexError) as caught:
                DeepSeekClient(opener).complete("t", prompt="p", model="deepseek-flash", temperature=0, api_key="sk-x")
            self.assertEqual(caught.exception.code, code)
        with self.assertRaises(y.YandexError) as caught:
            DeepSeekClient(lambda *a, **k: None).complete("t", prompt="p", model="gpt-4", temperature=0, api_key="k")
        self.assertEqual(caught.exception.code, "model_unknown")



if __name__ == "__main__":
    unittest.main()
