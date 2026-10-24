"""Voice input through Yandex Cloud and the workspace's AI agents.

Settings and agents belong to owners and administrators; recognition is open to
members who may write in chats. Network calls never run inside a database
transaction. Audio and texts are not stored or logged: the usage ledger keeps
only who, when, which agent, seconds, tokens, cost and the outcome.
"""

import logging
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import text

from center_messenger.accounts import audit
from center_messenger.ai_yandex import (
    DEFAULT_MODEL, DEFAULT_PROMPT, MODELS, YandexClient, YandexError, accept_normalized,
    check_audio, key_hint, model_cost, silence, stt_cost, valid_folder, valid_key,
)
from center_messenger.auth_security import AuthError

SECRET_ID = uuid5(NAMESPACE_URL, "center-messenger:yandex-ai")
PURPOSE = "yandex-ai"
TASKS = {"voice_input": "Голосовой ввод"}
VOICE_PER_HOUR = 60

ERRORS = {
    "unauthorized": (502, "Yandex Cloud отклонил ключ. Проверьте API-ключ и роли сервисного аккаунта в «Подключениях»."),
    "rate_limited": (429, "Yandex Cloud временно ограничил запросы. Повторите через минуту."),
    "bad_request": (422, "Yandex Cloud не принял запрос. Попробуйте ещё раз."),
    "unavailable": (503, "Yandex Cloud сейчас недоступен. Повторите позже."),
    "network": (503, "Не удалось связаться с Yandex Cloud. Повторите позже."),
    "bad_response": (502, "Yandex Cloud вернул непонятный ответ. Повторите позже."),
    "content_filter": (422, "Модель отказалась обрабатывать этот текст."),
    "model_unknown": (422, "Эта модель не поддерживается."),
    "audio_format": (422, "Запись повреждена. Попробуйте записать ещё раз."),
    "audio_short": (422, "Запись слишком короткая."),
    "audio_long": (413, "Запись длиннее 30 секунд."),
}


def _fail(code):
    status, detail = ERRORS.get(code, (502, "Не удалось обработать запрос к Yandex Cloud."))
    return AuthError(status, detail)


def _iso(value):
    return value.isoformat() if value else None


def _agent_public(row):
    return {
        "id": str(row["id"]), "name": row["name"], "task": row["task"],
        "task_title": TASKS.get(row["task"], row["task"]), "model": row["model"],
        "model_title": MODELS.get(row["model"], {}).get("title", row["model"]),
        "prompt": row["prompt"], "temperature": float(row["temperature"]),
        "enabled": bool(row["enabled"]), "is_default": bool(row["is_default"]),
        "version": row["version"], "updated_at": _iso(row["updated_at"]),
    }


def _clean_agent(data):
    name = " ".join(str(data.get("name") or "").split())
    prompt = str(data.get("prompt") or "").strip()
    task, model = data.get("task") or "voice_input", data.get("model") or DEFAULT_MODEL
    try:
        temperature = round(float(data.get("temperature", 0.2)), 2)
    except (TypeError, ValueError):
        temperature = -1
    if not 1 <= len(name) <= 120:
        raise AuthError(422, "Укажите название агента, до 120 символов.")
    if task not in TASKS:
        raise AuthError(422, "Неизвестная задача агента.")
    if model not in MODELS:
        raise AuthError(422, "Выберите модель из списка.")
    if not 1 <= len(prompt) <= 4000:
        raise AuthError(422, "Инструкция агента должна быть от 1 до 4000 символов.")
    if not 0 <= temperature <= 1:
        raise AuthError(422, "Точность — число от 0 до 1.")
    return {"name": name, "prompt": prompt, "task": task, "model": model, "temperature": temperature,
            "enabled": bool(data.get("enabled", True)), "is_default": bool(data.get("is_default", False))}


class AiVoice:
    def __init__(self, alfa, client=None):
        self.accounts, self.vault = alfa.accounts, alfa.vault
        self.client = client or YandexClient()

    # ── reading ────────────────────────────────────────────────────────────
    @staticmethod
    def _settings(db, workspace, lock=False):
        return db.execute(
            text("SELECT * FROM ai_settings WHERE workspace_id=:w" + (" FOR UPDATE" if lock else "")),
            {"w": workspace},
        ).mappings().one_or_none()

    @staticmethod
    def _month_spent(db, workspace):
        return float(db.execute(text(
            "SELECT coalesce(sum(u.cost_rub),0) FROM ai_usage u JOIN workspaces w ON w.id=u.workspace_id "
            "WHERE u.workspace_id=:w AND u.created_at >= "
            "(date_trunc('month', now() AT TIME ZONE w.timezone) AT TIME ZONE w.timezone)"
        ), {"w": workspace}).scalar_one())

    @staticmethod
    def voice_available(db, workspace):
        """Whether the chat composer shows the microphone; used by the chat context."""
        return bool(db.execute(text(
            "SELECT voice_enabled AND key_ciphertext IS NOT NULL AND folder_id<>'' "
            "FROM ai_settings WHERE workspace_id=:w"
        ), {"w": workspace}).scalar_one_or_none())

    def _public(self, db, workspace):
        row, spent = self._settings(db, workspace), self._month_spent(db, workspace)
        error = row["last_error_code"] if row else None
        return {
            "configured": bool(row and row["key_ciphertext"] and row["folder_id"]),
            "folder_id": row["folder_id"] if row else "",
            "key_hint": row["key_hint"] if row else "",
            "voice_enabled": bool(row and row["voice_enabled"]),
            "monthly_limit_rub": float(row["monthly_limit_rub"]) if row and row["monthly_limit_rub"] is not None else None,
            "status": row["status"] if row else "not_configured",
            "error": ERRORS.get(error, (0, "Проверка не прошла."))[1] if error else None,
            "verified_at": _iso(row["verified_at"]) if row else None,
            "month_spent_rub": round(spent, 2),
            "vault_ready": self.vault.ready,
            "models": [{"id": key, "title": value["title"]} for key, value in MODELS.items()],
            "tasks": [{"id": key, "title": value} for key, value in TASKS.items()],
            "default_model": DEFAULT_MODEL, "default_prompt": DEFAULT_PROMPT,
        }

    def settings(self, actor, workspace):
        with self.accounts.transaction(actor, snapshot=True) as db:
            self.accounts._workspace(db, actor, workspace, manage=True)
            return self._public(db, workspace)

    def _credentials(self, db, workspace):
        row = self._settings(db, workspace)
        if not row or not row["key_ciphertext"] or not row["folder_id"]:
            raise AuthError(409, "Сначала укажите API-ключ и ID каталога Yandex Cloud.")
        secret = self.vault.open(workspace, SECRET_ID, row["key_ciphertext"], purpose=PURPOSE)
        return row, secret["api_key"], row["folder_id"]

    def _usage(self, actor, workspace, *, kind, agent=None, seconds=0, tokens=(0, 0), cost=0.0,
               outcome="ok", error=None):
        with self.accounts.transaction(actor) as db:
            self.accounts._workspace_chat(db, actor, workspace)
            db.execute(text(
                "INSERT INTO ai_usage(workspace_id,id,user_id,agent_id,kind,audio_seconds,input_tokens,"
                "output_tokens,cost_rub,outcome,error_code) VALUES (:w,:id,:u,:a,:k,:s,:ti,:to,:c,:o,:e)"
            ), {"w": workspace, "id": uuid4(), "u": actor.user_id, "a": agent, "k": kind,
                "s": round(seconds, 2), "ti": tokens[0], "to": tokens[1], "c": round(cost, 4),
                "o": outcome, "e": error})

    # ── settings ───────────────────────────────────────────────────────────
    def save_settings(self, actor, workspace, data):
        with self.accounts.transaction(actor) as db:
            self.accounts._workspace(db, actor, workspace, manage=True)
            old = self._settings(db, workspace, lock=True)
            folder = str(data.get("folder_id") or "").strip()
            key = str(data.get("api_key") or "").strip()
            if folder and not valid_folder(folder):
                raise AuthError(422, "ID каталога — латинские строчные буквы и цифры, например b1g…")
            cipher, hint = (old["key_ciphertext"], old["key_hint"]) if old else (None, "")
            if data.get("clear_key"):
                cipher, hint = None, ""
            if key:
                if not valid_key(key):
                    raise AuthError(422, "API-ключ выглядит неверно: латинские буквы, цифры, «-» и «_».")
                cipher, hint = self.vault.seal(workspace, SECRET_ID, {"api_key": key}, purpose=PURPOSE), key_hint(key)
            voice = bool(data.get("voice_enabled"))
            if voice and not (cipher and folder):
                raise AuthError(422, "Чтобы включить голосовой ввод, укажите API-ключ и ID каталога.")
            limit = data.get("monthly_limit_rub")
            if limit in ("", None):
                limit = None
            else:
                try:
                    limit = round(float(limit), 2)
                except (TypeError, ValueError):
                    limit = -1
                if not 0 <= limit <= 1_000_000:
                    raise AuthError(422, "Лимит — сумма в рублях от 0 до 1 000 000.")
            reset = bool(key) or bool(data.get("clear_key")) or (old is not None and old["folder_id"] != folder)
            status = "not_configured" if reset or not old else old["status"]
            db.execute(text(
                "INSERT INTO ai_settings(workspace_id,folder_id,key_ciphertext,key_hint,voice_enabled,"
                "monthly_limit_rub,status,updated_by) VALUES (:w,:f,:c,:h,:v,:l,:s,:u) "
                "ON CONFLICT (workspace_id) DO UPDATE SET folder_id=EXCLUDED.folder_id,"
                "key_ciphertext=EXCLUDED.key_ciphertext,key_hint=EXCLUDED.key_hint,"
                "voice_enabled=EXCLUDED.voice_enabled,monthly_limit_rub=EXCLUDED.monthly_limit_rub,"
                "status=EXCLUDED.status,"
                "last_error_code=CASE WHEN :reset THEN NULL ELSE ai_settings.last_error_code END,"
                "verified_at=CASE WHEN :reset THEN NULL ELSE ai_settings.verified_at END,"
                "version=ai_settings.version+1,updated_at=now(),updated_by=EXCLUDED.updated_by"
            ), {"w": workspace, "f": folder, "c": cipher, "h": hint, "v": voice, "l": limit,
                "s": status, "u": actor.user_id, "reset": reset})
            audit(db, "ai_settings_updated", user_id=actor.user_id, workspace_id=workspace)
            return self._public(db, workspace)

    def verify(self, actor, workspace):
        with self.accounts.transaction(actor) as db:
            self.accounts._workspace(db, actor, workspace, manage=True)
            _, key, folder = self._credentials(db, workspace)
        cost, tokens, error = stt_cost(0.5), (0, 0), None
        try:
            self.client.recognize(silence(0.5), api_key=key)
            answer = self.client.complete("Проверка связи", prompt="Ответь одним словом: готово.",
                                          model=DEFAULT_MODEL, temperature=0, api_key=key,
                                          folder_id=folder, max_tokens=20)
            tokens = (answer["input_tokens"], answer["output_tokens"])
            cost += model_cost(DEFAULT_MODEL, *tokens)
        except YandexError as failure:
            error = failure.code
        with self.accounts.transaction(actor) as db:
            self.accounts._workspace(db, actor, workspace, manage=True)
            db.execute(text(
                "UPDATE ai_settings SET status=:s,last_error_code=:e,"
                "verified_at=CASE WHEN :e IS NULL THEN now() ELSE verified_at END WHERE workspace_id=:w"
            ), {"w": workspace, "s": "error" if error else "active", "e": error})
            db.execute(text(
                "INSERT INTO ai_usage(workspace_id,id,user_id,kind,audio_seconds,input_tokens,output_tokens,"
                "cost_rub,outcome,error_code) VALUES (:w,:id,:u,'verify',0.5,:ti,:to,:c,:o,:e)"
            ), {"w": workspace, "id": uuid4(), "u": actor.user_id, "ti": tokens[0], "to": tokens[1],
                "c": round(cost, 4), "o": "error" if error else "ok", "e": error})
            return self._public(db, workspace) | {"ok": error is None}

    # ── agents ─────────────────────────────────────────────────────────────
    @staticmethod
    def _agent(db, workspace, agent_id, *, lock=False):
        row = db.execute(text(
            "SELECT * FROM ai_agents WHERE workspace_id=:w AND id=:id AND deleted_at IS NULL"
            + (" FOR UPDATE" if lock else "")
        ), {"w": workspace, "id": agent_id}).mappings().one_or_none()
        if row is None:
            raise AuthError(404, "Агент не найден.")
        return row

    def agents(self, actor, workspace):
        with self.accounts.transaction(actor, snapshot=True) as db:
            self.accounts._workspace(db, actor, workspace, manage=True)
            rows = db.execute(text(
                "SELECT * FROM ai_agents WHERE workspace_id=:w AND deleted_at IS NULL "
                "ORDER BY is_default DESC, created_at, id"
            ), {"w": workspace}).mappings().all()
            return {"agents": [_agent_public(row) for row in rows]}

    @staticmethod
    def _lock_workspace(db, workspace):
        db.execute(text("SELECT id FROM workspaces WHERE id=:w FOR UPDATE"), {"w": workspace})

    @staticmethod
    def _version(db, workspace, agent_id, version, values, actor):
        db.execute(text(
            "INSERT INTO ai_agent_versions(workspace_id,agent_id,version,name,model,prompt,temperature,created_by) "
            "VALUES (:w,:a,:v,:n,:m,:p,:t,:u)"
        ), {"w": workspace, "a": agent_id, "v": version, "n": values["name"], "m": values["model"],
            "p": values["prompt"], "t": values["temperature"], "u": actor.user_id})

    def create_agent(self, actor, workspace, data):
        values = _clean_agent(data)
        with self.accounts.transaction(actor) as db:
            self.accounts._workspace(db, actor, workspace, manage=True)
            self._lock_workspace(db, workspace)
            has_default = db.execute(text(
                "SELECT 1 FROM ai_agents WHERE workspace_id=:w AND task=:t AND is_default AND deleted_at IS NULL"
            ), {"w": workspace, "t": values["task"]}).first() is not None
            values["is_default"] = values["is_default"] and values["enabled"]
            if values["is_default"]:
                db.execute(text("UPDATE ai_agents SET is_default=false WHERE workspace_id=:w AND task=:t"),
                           {"w": workspace, "t": values["task"]})
            elif not has_default and values["enabled"]:
                values["is_default"] = True
            agent_id = uuid4()
            db.execute(text(
                "INSERT INTO ai_agents(workspace_id,id,name,task,model,prompt,temperature,enabled,is_default,updated_by) "
                "VALUES (:w,:id,:n,:task,:m,:p,:t,:e,:d,:u)"
            ), {"w": workspace, "id": agent_id, "n": values["name"], "task": values["task"], "m": values["model"],
                "p": values["prompt"], "t": values["temperature"], "e": values["enabled"],
                "d": values["is_default"], "u": actor.user_id})
            self._version(db, workspace, agent_id, 1, values, actor)
            audit(db, "ai_agent_created", user_id=actor.user_id, workspace_id=workspace, subject_id=agent_id)
            return _agent_public(self._agent(db, workspace, agent_id))

    def update_agent(self, actor, workspace, agent_id, data):
        values = _clean_agent(data)
        with self.accounts.transaction(actor) as db:
            self.accounts._workspace(db, actor, workspace, manage=True)
            self._lock_workspace(db, workspace)
            old = self._agent(db, workspace, agent_id, lock=True)
            content = any(str(old[k]) != str(values[k]) for k in ("name", "model", "prompt")) or \
                float(old["temperature"]) != values["temperature"]
            version = old["version"] + (1 if content else 0)
            if values["is_default"]:
                db.execute(text("UPDATE ai_agents SET is_default=false WHERE workspace_id=:w AND task=:t AND id<>:id"),
                           {"w": workspace, "t": values["task"], "id": agent_id})
            db.execute(text(
                "UPDATE ai_agents SET name=:n,task=:task,model=:m,prompt=:p,temperature=:t,enabled=:e,"
                "is_default=:d,version=:v,updated_at=now(),updated_by=:u WHERE workspace_id=:w AND id=:id"
            ), {"w": workspace, "id": agent_id, "n": values["name"], "task": values["task"], "m": values["model"],
                "p": values["prompt"], "t": values["temperature"], "e": values["enabled"],
                "d": values["is_default"] and values["enabled"], "v": version, "u": actor.user_id})
            if content:
                self._version(db, workspace, agent_id, version, values, actor)
            audit(db, "ai_agent_updated", user_id=actor.user_id, workspace_id=workspace, subject_id=agent_id)
            return _agent_public(self._agent(db, workspace, agent_id))

    def delete_agent(self, actor, workspace, agent_id):
        with self.accounts.transaction(actor) as db:
            self.accounts._workspace(db, actor, workspace, manage=True)
            self._agent(db, workspace, agent_id, lock=True)
            db.execute(text(
                "UPDATE ai_agents SET deleted_at=now(),is_default=false,enabled=false,updated_at=now(),updated_by=:u "
                "WHERE workspace_id=:w AND id=:id"
            ), {"w": workspace, "id": agent_id, "u": actor.user_id})
            audit(db, "ai_agent_deleted", user_id=actor.user_id, workspace_id=workspace, subject_id=agent_id)

    def versions(self, actor, workspace, agent_id):
        with self.accounts.transaction(actor, snapshot=True) as db:
            self.accounts._workspace(db, actor, workspace, manage=True)
            self._agent(db, workspace, agent_id)
            rows = db.execute(text(
                "SELECT v.version,v.name,v.model,v.prompt,v.temperature,v.created_at,u.display_name "
                "FROM ai_agent_versions v LEFT JOIN auth_users u ON u.id=v.created_by "
                "WHERE v.workspace_id=:w AND v.agent_id=:a ORDER BY v.version DESC LIMIT 50"
            ), {"w": workspace, "a": agent_id}).mappings().all()
            return {"versions": [{"version": r["version"], "name": r["name"], "model": r["model"],
                                  "prompt": r["prompt"], "temperature": float(r["temperature"]),
                                  "created_at": _iso(r["created_at"]), "author": r["display_name"]}
                                 for r in rows]}

    def _normalize(self, raw, agent, key, folder):
        """(text, tokens, cost, fallback, error) — the raw text is kept whenever the model fails."""
        if not raw:
            return raw, (0, 0), 0.0, False, None
        try:
            answer = self.client.complete(raw, prompt=agent["prompt"], model=agent["model"],
                                          temperature=float(agent["temperature"]), api_key=key,
                                          folder_id=folder, max_tokens=min(2000, max(200, len(raw) * 2)))
        except YandexError as failure:
            return raw, (0, 0), 0.0, True, failure.code
        tokens = (answer["input_tokens"], answer["output_tokens"])
        cost = model_cost(agent["model"], *tokens)
        normalized = None if answer["truncated"] else accept_normalized(raw, answer["text"])
        return (normalized, tokens, cost, False, None) if normalized else (raw, tokens, cost, True, "unusable")

    def test_agent(self, actor, workspace, data):
        values = _clean_agent(data | {"name": data.get("name") or "Проверка"})
        sample = " ".join(str(data.get("text") or "").split())
        if not 1 <= len(sample) <= 2000:
            raise AuthError(422, "Введите проверочный текст, до 2000 символов.")
        with self.accounts.transaction(actor) as db:
            self.accounts._workspace(db, actor, workspace, manage=True)
            _, key, folder = self._credentials(db, workspace)
        normalized, tokens, cost, fallback, error = self._normalize(sample, values, key, folder)
        self._usage(actor, workspace, kind="test", tokens=tokens, cost=cost,
                    outcome="fallback" if fallback else "ok", error=error)
        if fallback and error not in (None, "unusable"):
            raise _fail(error)
        return {"text": normalized, "fallback": fallback, "cost_rub": round(cost, 4),
                "tokens": {"input": tokens[0], "output": tokens[1]}}

    # ── recognition ────────────────────────────────────────────────────────
    def transcribe(self, actor, workspace, audio, agent_id=None):
        try:
            seconds = check_audio(audio)
        except YandexError as failure:
            raise _fail(failure.code) from None
        with self.accounts.transaction(actor, snapshot=True) as db:
            member = self.accounts._workspace_chat(db, actor, workspace)
            if member["role"] == "observer":
                raise AuthError(403, "Наблюдатель не может писать сообщения.")
            row = self._settings(db, workspace)
            if not row or not row["voice_enabled"] or not row["key_ciphertext"] or not row["folder_id"]:
                raise AuthError(409, "Голосовой ввод не настроен. Обратитесь к администратору.")
            limit = row["monthly_limit_rub"]
            if limit is not None and self._month_spent(db, workspace) >= float(limit):
                raise AuthError(429, "Лимит голосового ввода на этот месяц исчерпан.")
            agent = db.execute(text(
                "SELECT * FROM ai_agents WHERE workspace_id=:w AND task='voice_input' AND enabled "
                "AND deleted_at IS NULL AND (id=CAST(:id AS uuid) OR (CAST(:id AS uuid) IS NULL AND is_default)) "
                "LIMIT 1"
            ), {"w": workspace, "id": str(agent_id) if agent_id else None}).mappings().one_or_none()
            if agent_id and agent is None:
                raise AuthError(404, "Агент не найден или выключен.")
            _, key, folder = self._credentials(db, workspace)
        try:
            self.accounts.throttle(f"ai-voice:{workspace}:{actor.user_id}", limit=VOICE_PER_HOUR, minutes=60)
        except AuthError as error:
            if error.status == 429:
                raise AuthError(429, "Слишком много голосовых сообщений за час. Повторите позже.",
                                retry_after=error.retry_after) from None
            raise
        cost = stt_cost(seconds)
        try:
            raw = self.client.recognize(audio, api_key=key)
        except YandexError as failure:
            self._usage(actor, workspace, kind="voice", seconds=seconds, cost=0.0, outcome="error",
                        error=failure.code)
            raise _fail(failure.code) from None
        result = {"raw_text": raw, "text": raw, "seconds": round(seconds, 1), "agent": None,
                  "fallback": False, "warning": None}
        tokens, error = (0, 0), None
        if agent:
            normalized, tokens, model_spent, fallback, error = self._normalize(raw, agent, key, folder)
            cost += model_spent
            result |= {"text": normalized, "fallback": fallback,
                       "agent": {"id": str(agent["id"]), "name": agent["name"]}}
            if fallback:
                result["warning"] = "Текст вставлен без обработки: агент сейчас недоступен."
        if not raw:
            result["warning"] = "Речь не распознана. Попробуйте говорить ближе к микрофону."
        self._usage(actor, workspace, kind="voice", agent=agent["id"] if agent else None, seconds=seconds,
                    tokens=tokens, cost=cost, outcome="fallback" if result["fallback"] else "ok", error=error)
        if error:
            logging.getLogger(__name__).warning("Voice normalization fallback workspace=%s code=%s", workspace, error)
        return result
