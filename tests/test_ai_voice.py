"""Voice input service rules on fake storage: permissions, limits, fallback, secrets.

No database or Yandex account: SQL is routed by its text to an in-memory fake.
Where SQLAlchemy and the account module are not installed, minimal stand-ins
are registered so the service rules still run.
"""

import sys
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app" / "backend" / "src"))

try:  # the real stack, as in the app image and CI
    import center_messenger.accounts  # noqa: F401
except ImportError:  # a bare environment: stand-ins for what ai_voice imports
    sqlalchemy = types.ModuleType("sqlalchemy")
    sqlalchemy.text = lambda value: value
    sys.modules.setdefault("sqlalchemy", sqlalchemy)

    class AuthError(Exception):
        def __init__(self, status, detail, *, retry_after=None):
            super().__init__(detail)
            self.status, self.detail, self.retry_after = status, detail, retry_after

    security = types.ModuleType("center_messenger.auth_security")
    security.AuthError = AuthError
    sys.modules["center_messenger.auth_security"] = security
    accounts_stub = types.ModuleType("center_messenger.accounts")
    accounts_stub.audit = lambda db, action, **kwargs: db.audits.append(action)
    sys.modules["center_messenger.accounts"] = accounts_stub

from center_messenger import ai_voice as v  # noqa: E402
from center_messenger.ai_yandex import YandexError, silence, stt_cost, model_cost  # noqa: E402
from center_messenger.auth_security import AuthError  # noqa: E402

WS = uuid4()


class Result:
    def __init__(self, rows=None, scalar=None):
        self.rows, self.scalar = rows or [], scalar

    def mappings(self):
        return self

    def one_or_none(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return self.rows

    def first(self):
        return self.rows[0] if self.rows else None

    def scalar_one(self):
        return self.scalar

    def scalar_one_or_none(self):
        return self.scalar


class FakeDB:
    def __init__(self):
        self.settings, self.agents, self.usage, self.audits, self.spent = None, [], [], [], 0.0

    def execute(self, sql, params=None):
        sql, params = str(sql), params or {}
        if sql.startswith("SELECT * FROM ai_settings"):
            return Result([self.settings] if self.settings else [])
        if "coalesce(sum(u.cost_rub)" in sql:
            return Result(scalar=self.spent)
        if sql.startswith("INSERT INTO ai_settings"):
            self.settings = {"folder_id": params["f"], "key_ciphertext": params["c"], "key_hint": params["h"],
                             "voice_enabled": params["v"], "monthly_limit_rub": params["l"], "status": params["s"],
                             "last_error_code": None, "verified_at": None}
            return Result()
        if sql.startswith("INSERT INTO auth_events"):  # the real accounts.audit
            self.audits.append(params["action"])
            return Result()
        if sql.startswith("INSERT INTO ai_usage"):
            self.usage.append(params)
            return Result()
        if "FROM ai_agents WHERE workspace_id=:w AND task='voice_input'" in sql:
            wanted = params["id"]
            rows = [a for a in self.agents if a["enabled"] and (str(a["id"]) == wanted if wanted else a["is_default"])]
            return Result(rows[:1])
        raise AssertionError(f"unexpected SQL: {sql[:80]}")


class FakeAccounts:
    def __init__(self, db, role="operator"):
        self.db, self.role, self.throttled = db, role, 0

    @contextmanager
    def transaction(self, actor=None, *, snapshot=False, **_):
        yield self.db

    def _workspace(self, db, actor, workspace, *, manage=False):
        if manage and self.role not in {"owner", "admin"}:
            raise AuthError(403, "admin only")
        return {"role": self.role}

    def _workspace_chat(self, db, actor, workspace):
        return {"role": self.role}

    def throttle(self, label, *, limit, minutes=15):
        self.throttled += 1


class FakeVault:
    ready = True

    def seal(self, workspace, connection, payload, *, purpose="alfa"):
        assert purpose == "yandex-ai" and connection == v.SECRET_ID
        return "sealed:" + payload["api_key"]

    def open(self, workspace, connection, ciphertext, *, purpose="alfa"):
        assert purpose == "yandex-ai"
        return {"api_key": ciphertext.removeprefix("sealed:")}


class FakeClient:
    def __init__(self, recognized="ну это мы завтра придём", normalized="Мы придём завтра.", fail=None):
        self.recognized, self.normalized, self.fail, self.calls = recognized, normalized, fail, []

    def recognize(self, audio, *, api_key):
        self.calls.append(("recognize", api_key))
        if self.fail == "recognize":
            raise YandexError("unavailable")
        return self.recognized

    def complete(self, text, **kwargs):
        self.calls.append(("complete", kwargs["model"]))
        if self.fail == "complete":
            raise YandexError("network")
        return {"text": self.normalized, "input_tokens": 300, "output_tokens": 20, "truncated": False}


KEY = "AQVN" + "k" * 36
Actor = types.SimpleNamespace


def service(role="operator", client=None, configured=True, voice=True, limit=None, agent=True):
    db = FakeDB()
    if configured:
        db.settings = {"folder_id": "b1gfolder000000000000", "key_ciphertext": "sealed:" + KEY, "key_hint": KEY[-4:],
                       "voice_enabled": voice, "monthly_limit_rub": limit, "status": "active",
                       "last_error_code": None, "verified_at": None}
    if agent:
        db.agents.append({"id": uuid4(), "name": "Чистовик", "task": "voice_input", "model": "yandexgpt-5.1",
                          "prompt": "Убери паразитов", "temperature": 0.2, "enabled": True, "is_default": True})
    alfa = types.SimpleNamespace(accounts=FakeAccounts(db, role), vault=FakeVault())
    return v.AiVoice(alfa, client or FakeClient()), db


ACTOR = Actor(user_id=uuid4())


class TranscribeTest(unittest.TestCase):
    def test_recognized_text_is_normalized_and_billed(self):
        voice, db = service()
        result = voice.transcribe(ACTOR, WS, silence(4))
        self.assertEqual(result["raw_text"], "ну это мы завтра придём")
        self.assertEqual(result["text"], "Мы придём завтра.")
        self.assertFalse(result["fallback"])
        self.assertEqual(result["agent"]["name"], "Чистовик")
        self.assertEqual([c[0] for c in voice.client.calls], ["recognize", "complete"])
        self.assertEqual(voice.client.calls[0][1], KEY, "the decrypted key goes to Yandex")
        usage = db.usage[-1]
        self.assertEqual((usage["k"], usage["o"]), ("voice", "ok"))
        self.assertAlmostEqual(usage["c"], round(stt_cost(4) + model_cost("yandexgpt-5.1", 300, 20), 4))
        self.assertNotIn("text", " ".join(map(str, usage)), "no texts in the usage ledger")
        self.assertEqual(voice.accounts.throttled, 1)

    def test_model_failure_keeps_the_raw_text(self):
        voice, db = service(client=FakeClient(fail="complete"))
        result = voice.transcribe(ACTOR, WS, silence(2))
        self.assertEqual(result["text"], "ну это мы завтра придём")
        self.assertTrue(result["fallback"])
        self.assertIn("без обработки", result["warning"])
        self.assertEqual(db.usage[-1]["o"], "fallback")

    def test_unusable_model_answer_keeps_the_raw_text(self):
        voice, _ = service(client=FakeClient(normalized="x" * 500))
        result = voice.transcribe(ACTOR, WS, silence(2))
        self.assertEqual(result["text"], "ну это мы завтра придём")
        self.assertTrue(result["fallback"])

    def test_without_agent_the_recognized_text_is_returned(self):
        voice, _ = service(agent=False)
        result = voice.transcribe(ACTOR, WS, silence(2))
        self.assertEqual(result["text"], "ну это мы завтра придём")
        self.assertIsNone(result["agent"])
        self.assertEqual([c[0] for c in voice.client.calls], ["recognize"])

    def test_silence_is_not_sent_to_the_model(self):
        voice, _ = service(client=FakeClient(recognized=""))
        result = voice.transcribe(ACTOR, WS, silence(2))
        self.assertEqual(result["text"], "")
        self.assertIn("не распознана", result["warning"])
        self.assertEqual([c[0] for c in voice.client.calls], ["recognize"])

    def test_refusals_happen_before_any_paid_call(self):
        cases = [
            ({"role": "observer"}, silence(2), 403),
            ({"voice": False}, silence(2), 409),
            ({"configured": False}, silence(2), 409),
            ({"limit": 10.0}, silence(2), 429),
            ({}, silence(31), 413),
            ({}, b"\x00", 422),
        ]
        for options, audio, status in cases:
            with self.subTest(options=options, status=status):
                voice, db = service(**options)
                db.spent = 10.0
                with self.assertRaises(AuthError) as caught:
                    voice.transcribe(ACTOR, WS, audio)
                self.assertEqual(caught.exception.status, status)
                self.assertEqual(voice.client.calls, [])

    def test_recognition_failure_is_reported_and_logged(self):
        voice, db = service(client=FakeClient(fail="recognize"))
        with self.assertRaises(AuthError) as caught:
            voice.transcribe(ACTOR, WS, silence(2))
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual((db.usage[-1]["o"], db.usage[-1]["e"]), ("error", "unavailable"))


class SettingsTest(unittest.TestCase):
    def test_key_is_sealed_and_never_returned(self):
        voice, db = service(role="owner", configured=False)
        public = voice.save_settings(ACTOR, WS, {"folder_id": "b1gfolder000000000000", "api_key": KEY,
                                                 "voice_enabled": True, "monthly_limit_rub": 500})
        self.assertEqual(db.settings["key_ciphertext"], "sealed:" + KEY)
        self.assertEqual(public["key_hint"], KEY[-4:])
        self.assertNotIn(KEY, repr(public))
        self.assertTrue(public["voice_enabled"])
        self.assertEqual(public["monthly_limit_rub"], 500)
        self.assertIn("ai_settings_updated", db.audits)

    def test_validation(self):
        for data, status in (({"folder_id": "Bad Folder"}, 422),
                             ({"folder_id": "b1gfolder000000000000", "voice_enabled": True}, 422),
                             ({"folder_id": "b1gfolder000000000000", "api_key": "short"}, 422),
                             ({"folder_id": "", "monthly_limit_rub": -5}, 422)):
            with self.subTest(data=data):
                voice, _ = service(role="admin", configured=False)
                with self.assertRaises(AuthError) as caught:
                    voice.save_settings(ACTOR, WS, data)
                self.assertEqual(caught.exception.status, status)

    def test_only_administrators_change_settings(self):
        voice, _ = service(role="operator")
        with self.assertRaises(AuthError) as caught:
            voice.save_settings(ACTOR, WS, {"folder_id": ""})
        self.assertEqual(caught.exception.status, 403)


class AgentRulesTest(unittest.TestCase):
    def test_agent_fields_are_checked(self):
        ok = v._clean_agent({"name": "  Чистовик   ответа ", "model": "yandexgpt-5.1", "prompt": " Убери паразитов ",
                             "temperature": 0.25})
        self.assertEqual((ok["name"], ok["prompt"], ok["temperature"], ok["task"]),
                         ("Чистовик ответа", "Убери паразитов", 0.25, "voice_input"))
        for bad in ({"name": "", "model": "yandexgpt-5.1", "prompt": "p"},
                    {"name": "a", "model": "gpt-4", "prompt": "p"},
                    {"name": "a", "model": "yandexgpt-5.1", "prompt": ""},
                    {"name": "a", "model": "yandexgpt-5.1", "prompt": "p", "temperature": 2},
                    {"name": "a", "model": "yandexgpt-5.1", "prompt": "p", "task": "other"}):
            with self.subTest(bad=bad), self.assertRaises(AuthError):
                v._clean_agent(bad)


if __name__ == "__main__":
    unittest.main()
