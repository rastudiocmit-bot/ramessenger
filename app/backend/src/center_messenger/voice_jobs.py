"""Bounded, short-lived voice jobs. Audio never goes to disk or a database."""
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from time import monotonic
from uuid import uuid4
import logging

from center_messenger.ai_gateways import ready
from center_messenger.ai_yandex import check_audio, MAX_SECONDS, YandexError
from center_messenger.auth_security import AuthError


class VoiceJobs:
    def __init__(self, service):
        self.service = service
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="voice")
        self.lock, self.jobs = Lock(), {}

    def access(self, actor, workspace):
        with self.service.accounts.transaction(actor, snapshot=True) as db:
            member = self.service.accounts._workspace_chat(db, actor, workspace)
            if member["role"] == "observer":
                raise AuthError(403, "Наблюдатель не может использовать голосовой ввод.")
            return self.service._settings(db, workspace)

    def prune(self):
        now = monotonic()
        for key, job in list(self.jobs.items()):
            if job["expires"] < now:
                job["future"].cancel()
                del self.jobs[key]

    def create(self, actor, workspace, audio, agent_id):
        row = self.access(actor, workspace)
        if not ready(row):
            raise AuthError(409, "Голосовой ввод не настроен.")
        try:
            check_audio(audio, row.get("voice_max_seconds", MAX_SECONDS))
        except YandexError as error:
            raise AuthError(413 if error.code == "audio_long" else 422, "Проверьте формат и длительность записи.") from None
        with self.lock:
            self.prune()
            if len(self.jobs) >= 8 or sum(j["owner"] == actor.user_id and j["workspace"] == workspace
                                         for j in self.jobs.values()) >= 2:
                raise AuthError(429, "Распознавание занято. Завершите текущую запись и повторите.")
            key = uuid4()
            job = {"owner": actor.user_id, "workspace": workspace, "expires": monotonic() + 300,
                   "status": "pending"}
            self.jobs[key] = job
            job["future"] = self.pool.submit(self.run, key, actor, workspace, audio, agent_id)
        return {"job_id": str(key), "status": "pending"}

    def run(self, key, actor, workspace, audio, agent_id):
        try:
            result = self.service.transcribe(actor, workspace, audio, agent_id)
            update = {"status": "done", "result": result}
        except AuthError as error:
            update = {"status": "error", "error": (error.status, error.detail)}
        except Exception as error:
            logging.getLogger(__name__).warning("Voice job failed cause=%s", type(error).__name__)
            update = {"status": "error", "error": (503, "Не удалось обработать запись. Повторите позже.")}
        with self.lock:
            if key in self.jobs:
                self.jobs[key].update(update, expires=monotonic() + 120)

    def get(self, actor, workspace, key, *, cancel=False):
        self.access(actor, workspace)
        with self.lock:
            self.prune()
            job = self.jobs.get(key)
            if not job or job["owner"] != actor.user_id or job["workspace"] != workspace:
                raise AuthError(404, "Запись больше недоступна. Запишите сообщение заново.")
            if cancel:
                job["future"].cancel()
                del self.jobs[key]
                return None
            if job["status"] == "error":
                raise AuthError(*job["error"])
            return {"status": job["status"], **({"result": job["result"]} if job["status"] == "done" else {})}
