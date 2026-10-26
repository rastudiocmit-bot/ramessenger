"""Voice input through Yandex Cloud and the workspace's AI agents.

Recognition, text processing and synthesis use independent gateway routes.
Each provider and billing account has its own sealed key; there is no paid fallback.

Settings and agents belong to owners and administrators; recognition is open to
members who may write in chats. Network calls never run inside a database
transaction. Audio and texts are not stored or logged: the usage ledger keeps
only who, when, which agent, seconds, tokens, cost and the outcome.
"""

import logging
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import text

from center_messenger import ai_deepseek, ai_mimo, ai_gateways
from center_messenger.accounts import audit
from center_messenger.ai_deepseek import DeepSeekClient
from center_messenger.ai_yandex import (
    DEFAULT_MODEL, DEFAULT_PROMPT, MODELS, YandexClient, YandexError, accept_normalized,
    check_audio, key_hint, silence, stt_cost, audio_cost, MAX_SECONDS, valid_folder, valid_key,
)
from center_messenger.auth_security import AuthError

SECRET_ID = uuid5(NAMESPACE_URL, "center-messenger:yandex-ai")
PURPOSE = "yandex-ai"
DEEPSEEK_SECRET_ID = uuid5(NAMESPACE_URL, "center-messenger:deepseek-ai")
DEEPSEEK_PURPOSE = "deepseek-ai"
PROVIDERS = {"yandex": "Yandex Cloud", "deepseek": "DeepSeek", "mimo": "MiMo", "mimo_plan": "MiMo Token Plan"}

# Every model an agent may use: provider, title and price in roubles per 1000 tokens.
CATALOG = {key: {"provider": "yandex", "title": value["title"], "input": value["input"], "output": value["output"]}
           for key, value in MODELS.items()} | {
    key: {"provider": "deepseek", "title": value["title"], **ai_deepseek.rub_per_1k(key)}
    for key, value in ai_deepseek.MODELS.items()}

CATALOG.update({key: {"provider": "mimo", "title": value["title"],
    "input": value["input"] * ai_mimo.USD_RUB / 1000, "output": value["output"] * ai_mimo.USD_RUB / 1000}
    for key, value in ai_mimo.MODELS.items()})
CATALOG.update({key: {"provider": "mimo_plan", "title": value["title"], "input": 0, "output": 0}
    for key, value in ai_mimo.PLAN_MODELS.items()})


def provider_of(model):
    return CATALOG.get(model, {}).get("provider", "yandex")


def model_cost(model, input_tokens, output_tokens):
    price = CATALOG.get(model, CATALOG[DEFAULT_MODEL])
    return (input_tokens * price["input"] + output_tokens * price["output"]) / 1000
TASKS = {"voice_input": "Голосовой ввод", "text_processing": "Обработка текста"}
VOICE_PER_HOUR = 60
# Uvicorn's configured INFO handler also captures successful/slow attempts;
# the unconfigured application root logger otherwise drops INFO events.
VOICE_LOG = logging.getLogger("uvicorn.error.voice")

ERRORS = {
    "unauthorized": (502, "{name} отклонил ключ. Проверьте API-ключ в «Подключениях»."),
    "rate_limited": (429, "{name} временно ограничил запросы. Повторите через минуту."),
    "bad_request": (422, "{name} не принял запрос. Попробуйте ещё раз."),
    "unavailable": (503, "{name} сейчас недоступен. Повторите позже."),
    "network": (503, "Не удалось связаться с {name}. Повторите позже."),
    "bad_response": (502, "{name} вернул непонятный ответ. Повторите позже."),
    "no_balance": (502, "На счёте {name} закончились средства. Пополните баланс."),
    "not_connected": (409, "{name} не подключён: добавьте API-ключ в «Подключениях»."),
    "content_filter": (422, "Модель отказалась обрабатывать этот текст."),
    "model_unknown": (422, "Эта модель не поддерживается."),
    "audio_format": (422, "Запись повреждена. Попробуйте записать ещё раз."),
    "audio_short": (422, "Запись слишком короткая."),
    "audio_long": (413, "Запись превышает допустимую длительность голосового ввода."),
}


def _message(code, provider="yandex"):
    status, detail = ERRORS.get(code, (502, "Не удалось обработать запрос к {name}."))
    return status, detail.format(name=PROVIDERS[provider])


def _fail(code, provider="yandex"):
    return AuthError(*_message(code, provider))


def _iso(value):
    return value.isoformat() if value else None


def _agent_public(row):
    return {
        "id": str(row["id"]), "name": row["name"], "task": row["task"],
        "task_title": TASKS.get(row["task"], row["task"]), "model": row["model"],
        "model_title": CATALOG.get(row["model"], {}).get("title", row["model"]),
        "provider": provider_of(row["model"]),
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
    if model not in CATALOG:
        raise AuthError(422, "Выберите модель из списка.")
    if not 1 <= len(prompt) <= 4000:
        raise AuthError(422, "Инструкция агента должна быть от 1 до 4000 символов.")
    if not 0 <= temperature <= 1:
        raise AuthError(422, "Точность — число от 0 до 1.")
    return {"name": name, "prompt": prompt, "task": task, "model": model, "temperature": temperature,
            "enabled": bool(data.get("enabled", True)), "is_default": bool(data.get("is_default", False))}


class AiVoice:
    def __init__(self, alfa, client=None, deepseek=None, mimo=None):
        self.accounts, self.vault = alfa.accounts, alfa.vault
        self.client = client or YandexClient()
        self.deepseek = deepseek or DeepSeekClient()
        self.mimo = mimo or ai_mimo.MimoClient()
        self.gateways = ai_gateways.Gateways(self)
        from center_messenger.voice_jobs import VoiceJobs
        self.jobs = VoiceJobs(self)

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
            "SELECT voice_enabled AND CASE coalesce(gateway_config->>'recognition_provider','yandex') "
            "WHEN 'mimo' THEN mimo_key_ciphertext IS NOT NULL "
            "WHEN 'mimo_plan' THEN mimo_plan_key_ciphertext IS NOT NULL "
            "ELSE key_ciphertext IS NOT NULL AND folder_id<>'' END "
            "FROM ai_settings WHERE workspace_id=:w"
        ), {"w": workspace}).scalar_one_or_none())

    @staticmethod
    def voice_duration(db, workspace):
        return db.execute(text("SELECT voice_max_seconds FROM ai_settings WHERE workspace_id=:w"),
                          {"w": workspace}).scalar_one_or_none() or MAX_SECONDS

    def _public(self, db, workspace):
        row, spent = self._settings(db, workspace), self._month_spent(db, workspace)
        error = row["last_error_code"] if row else None
        return {
            "gateways": ai_gateways.public(row),
            "configured": bool(row and row["key_ciphertext"] and row["folder_id"]),
            "folder_id": row["folder_id"] if row else "",
            "key_hint": row["key_hint"] if row else "",
            "voice_enabled": bool(row and row["voice_enabled"]),
            "voice_max_seconds": row.get("voice_max_seconds", MAX_SECONDS) if row else MAX_SECONDS,
            "monthly_limit_rub": float(row["monthly_limit_rub"]) if row and row["monthly_limit_rub"] is not None else None,
            "status": row["status"] if row else "not_configured",
            "error": _message(error)[1] if error else None,
            "verified_at": _iso(row["verified_at"]) if row else None,
            "month_spent_rub": round(spent, 2),
            "vault_ready": self.vault.ready,
            "deepseek": {
                "configured": bool(row and row["deepseek_key_ciphertext"]),
                "key_hint": row["deepseek_key_hint"] if row else "",
                "status": row["deepseek_status"] if row else "not_configured",
                "error": _message(row["deepseek_error_code"], "deepseek")[1]
                if row and row["deepseek_error_code"] else None,
            },
            "models": [{"id": key, "title": value["title"], "provider": value["provider"],
                        "provider_title": PROVIDERS[value["provider"]]} for key, value in CATALOG.items()],
            "tasks": [{"id": key, "title": value} for key, value in TASKS.items()],
            "default_model": DEFAULT_MODEL, "default_prompt": DEFAULT_PROMPT,
        }

    def settings(self, actor, workspace):
        with self.accounts.transaction(actor, snapshot=True) as db:
            self.accounts._workspace(db, actor, workspace, manage=True)
            return self._public(db, workspace)

    def _credentials(self, db, workspace, *, yandex=True, selected=None):
        """Decrypted keys: {"yandex", "folder", "deepseek"}; a missing provider is None."""
        row = self._settings(db, workspace)
        if yandex and (not row or not row["key_ciphertext"] or not row["folder_id"]):
            raise AuthError(409, "Сначала укажите API-ключ и ID каталога Yandex Cloud.")
        creds = {"yandex": None, "folder": row["folder_id"] if row else "", "deepseek": None}
        if row and row["key_ciphertext"] and (selected is None or "yandex" in selected):
            creds["yandex"] = self.vault.open(workspace, SECRET_ID, row["key_ciphertext"], purpose=PURPOSE)["api_key"]
        if row and row["deepseek_key_ciphertext"] and (selected is None or "deepseek" in selected):
            creds["deepseek"] = self.vault.open(workspace, DEEPSEEK_SECRET_ID, row["deepseek_key_ciphertext"],
                                                purpose=DEEPSEEK_PURPOSE)["api_key"]
        return creds | ai_gateways.credentials(self, row, workspace, selected)

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
            folder = str(data.get("folder_id", old["folder_id"] if old else "") or "").strip()
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
            voice = bool(data.get("voice_enabled", old["voice_enabled"] if old else False))
            duration = data.get("voice_max_seconds")
            if duration is None:
                duration = old.get("voice_max_seconds", MAX_SECONDS) if old else MAX_SECONDS
            if type(duration) is not int or not 15 <= duration <= MAX_SECONDS:
                raise AuthError(422, "Длительность записи — целое число от 15 до 180 секунд.")
            recognizer = ai_gateways.config(old).get("recognition_provider", "yandex")
            if voice and not ((cipher and folder) if recognizer == "yandex" else old and old.get(recognizer + "_key_ciphertext")):
                raise AuthError(422, "Чтобы включить голосовой ввод, укажите API-ключ и ID каталога.")
            limit = data.get("monthly_limit_rub", old["monthly_limit_rub"] if old else None)
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
                "monthly_limit_rub,status,voice_max_seconds,updated_by) VALUES (:w,:f,:c,:h,:v,:l,:s,:duration,:u) "
                "ON CONFLICT (workspace_id) DO UPDATE SET folder_id=EXCLUDED.folder_id,"
                "key_ciphertext=EXCLUDED.key_ciphertext,key_hint=EXCLUDED.key_hint,"
                "voice_enabled=EXCLUDED.voice_enabled,monthly_limit_rub=EXCLUDED.monthly_limit_rub,"
                "voice_max_seconds=EXCLUDED.voice_max_seconds,"
                "status=EXCLUDED.status,"
                "last_error_code=CASE WHEN :reset THEN NULL ELSE ai_settings.last_error_code END,"
                "verified_at=CASE WHEN :reset THEN NULL ELSE ai_settings.verified_at END,"
                "version=ai_settings.version+1,updated_at=now(),updated_by=EXCLUDED.updated_by"
            ), {"w": workspace, "f": folder, "c": cipher, "h": hint, "v": voice, "l": limit,
                "s": status, "u": actor.user_id, "reset": reset, "duration": duration})
            audit(db, "ai_settings_updated", user_id=actor.user_id, workspace_id=workspace)
            return self._public(db, workspace)

    def verify(self, actor, workspace):
        with self.accounts.transaction(actor) as db:
            self.accounts._workspace(db, actor, workspace, manage=True)
            creds = self._credentials(db, workspace, selected={"yandex"})
        key, folder = creds["yandex"], creds["folder"]
        cost, tokens, error = stt_cost(0.5), (0, 0), None
        try:
            self.client.recognize(silence(0.5), api_key=key)
        except YandexError as failure:
            error = failure.code
        with self.accounts.transaction(actor) as db:
            self.accounts._workspace(db, actor, workspace, manage=True)
            db.execute(text(
                "UPDATE ai_settings SET status=:s,last_error_code=:e,"
                "verified_at=CASE WHEN CAST(:e AS text) IS NULL THEN now() ELSE verified_at END WHERE workspace_id=:w"
            ), {"w": workspace, "s": "error" if error else "active", "e": error})
            db.execute(text(
                "INSERT INTO ai_usage(workspace_id,id,user_id,kind,audio_seconds,input_tokens,output_tokens,"
                "cost_rub,outcome,error_code) VALUES (:w,:id,:u,'verify',0.5,:ti,:to,:c,:o,:e)"
            ), {"w": workspace, "id": uuid4(), "u": actor.user_id, "ti": tokens[0], "to": tokens[1],
                "c": round(cost, 4), "o": "error" if error else "ok", "e": error})
            return self._public(db, workspace) | {"ok": error is None}

    def save_deepseek(self, actor, workspace, data):
        key = str(data.get("api_key") or "").strip()
        clear = bool(data.get("clear_key"))
        if not key and not clear:
            raise AuthError(422, "Вставьте API-ключ DeepSeek.")
        if key and not ai_deepseek.valid_key(key):
            raise AuthError(422, "Ключ DeepSeek начинается с «sk-», дальше латинские буквы и цифры.")
        with self.accounts.transaction(actor) as db:
            self.accounts._workspace(db, actor, workspace, manage=True)
            cipher, hint = (None, "") if clear else (
                self.vault.seal(workspace, DEEPSEEK_SECRET_ID, {"api_key": key}, purpose=DEEPSEEK_PURPOSE), key_hint(key))
            db.execute(text(
                "INSERT INTO ai_settings(workspace_id,deepseek_key_ciphertext,deepseek_key_hint,updated_by) "
                "VALUES (:w,:c,:h,:u) ON CONFLICT (workspace_id) DO UPDATE SET "
                "deepseek_key_ciphertext=EXCLUDED.deepseek_key_ciphertext,deepseek_key_hint=EXCLUDED.deepseek_key_hint,"
                "deepseek_status='not_configured',deepseek_error_code=NULL,"
                "version=ai_settings.version+1,updated_at=now(),updated_by=EXCLUDED.updated_by"
            ), {"w": workspace, "c": cipher, "h": hint, "u": actor.user_id})
            audit(db, "ai_deepseek_updated", user_id=actor.user_id, workspace_id=workspace)
            return self._public(db, workspace)

    def verify_deepseek(self, actor, workspace):
        with self.accounts.transaction(actor) as db:
            self.accounts._workspace(db, actor, workspace, manage=True)
            key = self._credentials(db, workspace, yandex=False, selected={"deepseek"})["deepseek"]
        if not key:
            raise AuthError(409, "Сначала сохраните API-ключ DeepSeek.")
        model = next(iter(ai_deepseek.MODELS))
        tokens, cost, error = (0, 0), 0.0, None
        try:
            answer = self.deepseek.complete("Проверка связи", prompt="Ответь одним словом: готово.",
                                            model=model, temperature=0, api_key=key, max_tokens=20)
            tokens = (answer["input_tokens"], answer["output_tokens"])
            cost = model_cost(model, *tokens)
        except YandexError as failure:
            error = failure.code
        with self.accounts.transaction(actor) as db:
            self.accounts._workspace(db, actor, workspace, manage=True)
            db.execute(text(
                "UPDATE ai_settings SET deepseek_status=:s,deepseek_error_code=CAST(:e AS text) WHERE workspace_id=:w"
            ), {"w": workspace, "s": "error" if error else "active", "e": error})
            db.execute(text(
                "INSERT INTO ai_usage(workspace_id,id,user_id,kind,input_tokens,output_tokens,"
                "cost_rub,outcome,error_code) VALUES (:w,:id,:u,'verify',:ti,:to,:c,:o,CAST(:e AS text))"
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
            "INSERT INTO ai_agent_versions(workspace_id,agent_id,version,name,model,prompt,temperature,task,created_by) "
            "VALUES (:w,:a,:v,:n,:m,:p,:t,:task,:u)"
        ), {"w": workspace, "a": agent_id, "v": version, "n": values["name"], "m": values["model"],
            "p": values["prompt"], "t": values["temperature"], "task": values["task"], "u": actor.user_id})

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
            content = any(str(old[k]) != str(values[k]) for k in ("name", "model", "prompt", "task")) or \
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
                "SELECT v.version,v.name,v.model,v.prompt,v.temperature,v.task,v.created_at,u.display_name "
                "FROM ai_agent_versions v LEFT JOIN auth_users u ON u.id=v.created_by "
                "WHERE v.workspace_id=:w AND v.agent_id=:a ORDER BY v.version DESC LIMIT 50"
            ), {"w": workspace, "a": agent_id}).mappings().all()
            return {"versions": [{"version": r["version"], "name": r["name"], "model": r["model"],
                                  "task": r["task"], "prompt": r["prompt"], "temperature": float(r["temperature"]),
                                  "created_at": _iso(r["created_at"]), "author": r["display_name"]}
                                 for r in rows]}

    def _normalize(self, raw, agent, creds):
        """(text, tokens, cost, fallback, error) — the raw text is kept whenever the model fails."""
        if not raw:
            return raw, (0, 0), 0.0, False, None
        provider = provider_of(agent["model"])
        client = self.mimo if provider in ai_gateways.PROVIDERS else self.deepseek if provider == "deepseek" else self.client
        key = creds.get(provider)
        extra = {}
        if provider in ai_gateways.PROVIDERS:
            extra = ai_gateways.options(creds, provider)
            extra.pop("api_key")
            extra["protocol"] = creds.get("gateways", {}).get(provider, {}).get("protocol", "chat")
        voice_task = agent.get("task", "voice_input") == "voice_input"
        if not voice_task:
            extra["rules"] = False
        if not key:
            return raw, (0, 0), 0.0, True, "not_connected"
        try:
            answer = client.complete(raw, prompt=agent["prompt"], model=agent["model"],
                                     temperature=float(agent["temperature"]), api_key=key,
                                     folder_id=creds["folder"], max_tokens=min(6000, max(200, len(raw) * 2)) if voice_task else 3000, **extra)
        except YandexError as failure:
            return raw, (0, 0), 0.0, True, failure.code
        tokens = (answer["input_tokens"], answer["output_tokens"])
        cost = model_cost(agent["model"], *tokens)
        normalized = None if answer["truncated"] else (accept_normalized(raw, answer["text"]) if voice_task else answer["text"].strip())
        return (normalized, tokens, cost, False, None) if normalized else (raw, tokens, cost, True, "unusable")

    def test_agent(self, actor, workspace, data):
        values = _clean_agent(data | {"name": data.get("name") or "Проверка"})
        sample = " ".join(str(data.get("text") or "").split())
        if not 1 <= len(sample) <= 2000:
            raise AuthError(422, "Введите проверочный текст, до 2000 символов.")
        with self.accounts.transaction(actor) as db:
            self.accounts._workspace(db, actor, workspace, manage=True)
            row = self._settings(db, workspace)
            if row and row["monthly_limit_rub"] is not None and self._month_spent(db, workspace) >= float(row["monthly_limit_rub"]):
                raise AuthError(429, "Месячный лимит исчерпан.")
            creds = self._credentials(db, workspace, yandex=False, selected={provider_of(values["model"])})
        self.accounts.throttle(f"ai-test:{workspace}:{actor.user_id}", limit=20, minutes=1)
        normalized, tokens, cost, fallback, error = self._normalize(sample, values, creds)
        if error != "not_connected":
            self._usage(actor, workspace, kind="test", tokens=tokens, cost=cost,
                        outcome="fallback" if fallback else "ok", error=error)
        if fallback and error not in (None, "unusable"):
            raise _fail(error, provider_of(values["model"]))
        return {"text": normalized, "fallback": fallback, "cost_rub": round(cost, 4),
                "billing": "subscription" if provider_of(values["model"]) == "mimo_plan" else "pay_as_you_go",
                "tokens": {"input": tokens[0], "output": tokens[1]}}

    # ── recognition ────────────────────────────────────────────────────────
    def diagnostic(self, actor, workspace, data):
        with self.accounts.transaction(actor, snapshot=True) as db:
            member = self.accounts._workspace_chat(db, actor, workspace)
            if member["role"] == "observer":
                raise AuthError(403, "Наблюдатель не может писать сообщения.")
        self.accounts.throttle(f"voice-diagnostic:{workspace}:{actor.user_id}", limit=30, minutes=1)
        # Whitelisted by VoiceDiagnosticInput; no device labels, audio or text.
        VOICE_LOG.info(
            "Voice capture workspace=%s attempt=%s outcome=%s stage=%s code=%s audio_state=%s "
            "elapsed_ms=%s first_frame_ms=%s captured_ms=%s",
            workspace, data["attempt_id"], data["outcome"], data["stage"], data["code"], data["audio_state"],
            data["elapsed_ms"], data["first_frame_ms"], data["captured_ms"],
        )

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
            if not ai_gateways.ready(row):
                raise AuthError(409, "Голосовой ввод не настроен. Обратитесь к администратору.")
            check_limit = row.get("voice_max_seconds", MAX_SECONDS)
            if seconds > check_limit:
                raise AuthError(413, f"Допустимая длительность записи — {check_limit} секунд.")
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
            recognizer = ai_gateways.config(row).get("recognition_provider", "yandex")
            selected = {recognizer} | ({provider_of(agent["model"])} if agent else set())
            creds = self._credentials(db, workspace, yandex=False, selected=selected)
        recognizer = ai_gateways.config(row).get("recognition_provider", "yandex")
        key = creds[recognizer]
        try:
            self.accounts.throttle(f"ai-voice:{workspace}:{actor.user_id}", limit=VOICE_PER_HOUR, minutes=60)
        except AuthError as error:
            if error.status == 429:
                raise AuthError(429, "Слишком много голосовых сообщений за час. Повторите позже.",
                                retry_after=error.retry_after) from None
            raise
        cost = audio_cost(audio) if recognizer == "yandex" else ai_mimo.audio_cost(seconds, plan=recognizer == "mimo_plan")
        try:
            if recognizer == "yandex":
                raw = self.client.recognize(audio, api_key=key)
            else:
                raw = self.mimo.recognize(audio, **ai_gateways.options(creds, recognizer),
                    plan=recognizer == "mimo_plan", language=ai_gateways.config(row).get("recognition_language", "auto"))["text"]
        except YandexError as failure:
            self._usage(actor, workspace, kind="voice", seconds=seconds, cost=getattr(failure, "billed_cost", 0.0), outcome="error",
                        error=failure.code)
            raise _fail(failure.code, recognizer) from None
        result = {"raw_text": raw, "text": raw, "seconds": round(seconds, 1), "agent": None,
                  "fallback": False, "warning": None}
        tokens, error = (0, 0), None
        if agent:
            normalized, tokens, model_spent, fallback, error = self._normalize(raw, agent, creds)
            cost += model_spent
            result |= {"text": normalized, "fallback": fallback,
                       "agent": {"id": str(agent["id"]), "name": agent["name"]}}
            if fallback:
                result["warning"] = ("Текст вставлен без обработки: " + PROVIDERS[provider_of(agent["model"])]
                                     + " не подключён." if error == "not_connected"
                                     else "Текст вставлен без обработки: агент сейчас недоступен.")
        if not raw:
            result["warning"] = "Речь не распознана. Попробуйте говорить ближе к микрофону."
        self._usage(actor, workspace, kind="voice", agent=agent["id"] if agent else None, seconds=seconds,
                    tokens=tokens, cost=cost, outcome="fallback" if result["fallback"] else "ok", error=error)
        if error:
            logging.getLogger(__name__).warning("Voice normalization fallback workspace=%s code=%s", workspace, error)
        return result
