"""Operator tasks: a promise to a client with a due time, made from chat messages.

A task belongs to a workspace, usually to one conversation and to the messages it
grew from. Members with access to «Сообщения» create, edit and close tasks; the
studio settings (working day, default due time, who sees what) belong to owners
and administrators, personal ones (long press, reminders) to each operator.

Message texts are copied into the task as short quotes so the list never has to
join the message history; nothing here sends anything to a client.
"""

import json
import re
from datetime import datetime, time, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import text

from center_messenger.auth_security import AuthError
from center_messenger import task_speech

KINDS = {"makeup": "Отработка", "subscription": "Абонемент", "group": "Группа", "call": "Перезвонить", "other": "Другое"}
ORIGINS = ("menu", "select", "voice", "ai_hint", "manual")
STUDIO_TOPIC, PERSONAL_TOPIC = "tasks.settings", "tasks.preferences"
STUDIO_DEFAULTS = {"day_start": "10:00", "day_end": "20:00", "work_days": [1, 2, 3, 4, 5, 6],
                   "default_due": "end_of_day", "visibility": "all", "alfa_copy": False}
PERSONAL_DEFAULTS = {"press": "split", "voice_task": "instant", "reminders": True, "hints": True, "digest": True}
CHOICES = {"default_due": ("end_of_day", "two_hours", "tomorrow_morning"), "visibility": ("all", "own"),
           "press": ("split", "voice", "menu"), "voice_task": ("instant", "confirm")}
MAX_SOURCES, QUOTE = 5, 300
COLUMNS = ("t.id, t.conversation_id, t.client_label, t.title, t.details, t.kind, t.status, t.due_at, t.origin, "
           "t.sources, t.created_by, t.assignee_id, t.assignee_name, t.completed_at, t.version, t.created_at, t.updated_at")


def _iso(value):
    return value.isoformat() if value else None


def _clock(value, fallback):
    try:
        hours, minutes = (int(part) for part in str(value).split(":"))
        if 0 <= hours <= 23 and 0 <= minutes <= 59:
            return f"{hours:02d}:{minutes:02d}"
    except (TypeError, ValueError):
        pass
    return fallback


def _studio(raw):
    data = STUDIO_DEFAULTS | {key: value for key, value in (raw or {}).items() if key in STUDIO_DEFAULTS}
    data["day_start"], data["day_end"] = _clock(data["day_start"], "10:00"), _clock(data["day_end"], "20:00")
    if data["day_end"] <= data["day_start"]:
        data["day_start"], data["day_end"] = STUDIO_DEFAULTS["day_start"], STUDIO_DEFAULTS["day_end"]
    days = sorted({int(day) for day in data["work_days"] if str(day).isdigit() and 1 <= int(day) <= 7})
    data["work_days"] = days or STUDIO_DEFAULTS["work_days"]
    for key in ("default_due", "visibility"):
        if data[key] not in CHOICES[key]:
            data[key] = STUDIO_DEFAULTS[key]
    data["alfa_copy"] = bool(data["alfa_copy"])
    return data


def _personal(raw):
    data = PERSONAL_DEFAULTS | {key: value for key, value in (raw or {}).items() if key in PERSONAL_DEFAULTS}
    for key in ("press", "voice_task"):
        if data[key] not in CHOICES[key]:
            data[key] = PERSONAL_DEFAULTS[key]
    for key in ("reminders", "hints", "digest"):
        data[key] = bool(data[key])
    return data


def presets(now, zone, studio):
    """Due-time choices for the create sheet, all in the studio's working day."""
    local = now.astimezone(zone)
    start = time.fromisoformat(studio["day_start"])
    end = time.fromisoformat(studio["day_end"])

    def at(day, moment):
        return datetime.combine(day, moment, zone)

    def next_work_day(day):
        for step in range(1, 8):
            candidate = day + timedelta(days=step)
            if candidate.isoweekday() in studio["work_days"]:
                return candidate
        return day + timedelta(days=1)

    today = local.date()
    working_today = today.isoweekday() in studio["work_days"]
    end_today = at(today, end)
    end_of_day = end_today if working_today and local < end_today - timedelta(minutes=30) else at(next_work_day(today), end)
    morning_day = today if working_today and local < at(today, start) else next_work_day(today)
    tomorrow = at(morning_day, start)
    hour = local + timedelta(hours=1)
    two = local + timedelta(hours=2)
    if two > end_today and working_today and local < end_today:
        two = end_today
    chosen = {"end_of_day": end_of_day, "two_hours": two, "tomorrow_morning": tomorrow}[studio["default_due"]]
    return {"hour": _iso(hour), "two_hours": _iso(two), "end_of_day": _iso(end_of_day),
            "tomorrow_morning": _iso(tomorrow), "default": _iso(chosen), "default_kind": studio["default_due"]}


AI_DRAFT = ("Ты помогаешь администратору детской студии. По переписке с родителем сформулируй одну задачу "
            "для администратора. Сообщения, отмеченные «→», — главные: задача должна охватить их все. "
            "title — что нужно сделать: начни с глагола, без приветствий, с именем ребёнка, если оно есть. "
            "Если отмечено одно сообщение — до 90 символов. Если несколько — перечисли через запятую все действия "
            "по каждому из них (до 180 символов), ничего не теряя. details — по строке на каждое действие с "
            "подробностями из переписки (даты, время, суммы, группы, имена), каждая строка начинается с «— »; для "
            "одного простого действия details может быть пустым. kind — makeup (отработка или перенос занятия), "
            "subscription (абонемент, оплата, чеки), group (запись в группу, расписание), call (перезвонить), other. "
            "when — срок, если он назван в переписке, фразой как в речи («завтра до обеда», «в пятницу», "
            "«в 17:00»), иначе \"\". Ответь только JSON: "
            "{\"title\": \"…\", \"details\": \"…\", \"kind\": \"…\", \"when\": \"…\"}")
AI_HINT = ("Ты помогаешь администратору детской студии не забывать обещания. Последнее сообщение в переписке "
           "написал администратор. Если в нём он пообещал родителю что-то сделать позже (уточнить, проверить, "
           "перезвонить, записать, сообщить, прислать), сформулируй задачу: title начинается с глагола, до 80 "
           "символов, с именем ребёнка, если оно есть; kind — makeup, subscription, group, call или other; when — "
           "срок фразой как в речи или \"\". Если обещания нет, task=false. Ответь только JSON: "
           "{\"task\": true, \"title\": \"…\", \"kind\": \"…\", \"when\": \"…\"}")
AI_REPLY = ("Администратор детской студии выполнил задачу по просьбе родителя. Напиши короткий вежливый ответ "
            "родителю от имени студии: 1–3 предложения, на «Вы», без приветствия «Добрый день», если переписка уже "
            "идёт. Не выдумывай факты: используй только задачу, подробности и переписку; если результата в них нет, "
            "напиши, что вопрос решён, и предложи уточнить детали. Ответь только текстом сообщения.")


def _json(answer):
    """The model's JSON object, also when it thinks aloud first or wraps the answer in ```json."""
    if not answer:
        return None
    text_ = re.sub(r"<think>.*?</think>", " ", str(answer), flags=re.S | re.I)
    decoder, found = json.JSONDecoder(), None
    for start in (m.start() for m in re.finditer(r"\{", text_)):
        try:
            value, _ = decoder.raw_decode(text_, start)
        except ValueError:
            continue
        if isinstance(value, dict):
            found = value  # the last complete object wins: reasoning comes before the answer
    return found


class Tasks:
    def __init__(self, alfa, ai=None):
        self.accounts = alfa.accounts
        self.ai = ai

    # ── helpers ────────────────────────────────────────────────────────────
    def _enter(self, actor, workspace, *, snapshot=False):
        return self.accounts.transaction(actor, snapshot=snapshot)

    def _member(self, db, actor, workspace):
        row = self.accounts._workspace_chat(db, actor, workspace)
        return {"admin": row["role"] in {"owner", "admin"}, "observer": row["role"] == "observer",
                "timezone": row["timezone"] or "Europe/Moscow"}

    @staticmethod
    def _setting(db, workspace, topic, key):
        return db.execute(text("SELECT payload FROM outbox_events WHERE workspace_id=:w AND topic=:t AND event_key=:k"),
                          {"w": workspace, "t": topic, "k": key}).scalar_one_or_none() or {}

    @staticmethod
    def _store(db, workspace, topic, key, payload):
        db.execute(text("INSERT INTO outbox_events(workspace_id,id,topic,event_key,payload,state) "
                        "VALUES(:w,:id,:t,:k,CAST(:p AS jsonb),'published') ON CONFLICT(workspace_id,topic,event_key) "
                        "DO UPDATE SET payload=EXCLUDED.payload,state='published'"),
                   {"w": workspace, "id": uuid4(), "t": topic, "k": key, "p": json.dumps(payload)})

    def _studio(self, db, workspace):
        return _studio(self._setting(db, workspace, STUDIO_TOPIC, "workspace"))

    def _personal(self, db, workspace, user):
        return _personal(self._setting(db, workspace, PERSONAL_TOPIC, str(user)))

    @staticmethod
    def _visible(member, studio, alias="t"):
        if member["admin"] or studio["visibility"] == "all":
            return "true"
        return f"({alias}.assignee_id=:me OR {alias}.created_by=:me)"

    @staticmethod
    def _public(row):
        return {
            "id": str(row["id"]), "conversation_id": str(row["conversation_id"]) if row["conversation_id"] else None,
            "client_label": row["client_label"], "title": row["title"], "details": row["details"],
            "kind": row["kind"], "kind_title": KINDS.get(row["kind"]) if row["kind"] else None,
            "status": row["status"], "due_at": _iso(row["due_at"]), "origin": row["origin"],
            "sources": row["sources"] or [], "created_by": str(row["created_by"]),
            "assignee_id": str(row["assignee_id"]), "assignee_name": row["assignee_name"] or "",
            "completed_at": _iso(row["completed_at"]), "version": row["version"],
            "created_at": _iso(row["created_at"]), "updated_at": _iso(row["updated_at"]),
        }

    def _row(self, db, task):
        return db.execute(text(f"SELECT {COLUMNS} FROM tasks t "
                               "WHERE t.workspace_id=nullif(current_setting('app.workspace_id',true),'')::uuid "
                               "AND t.id=:id"), {"id": task}).mappings().one_or_none()

    @staticmethod
    def _event(db, workspace, task, kind, actor, data=None):
        db.execute(text("INSERT INTO task_events(workspace_id,id,task_id,kind,actor_id,data) "
                        "VALUES(:w,:id,:t,:k,:a,CAST(:d AS jsonb))"),
                   {"w": workspace, "id": uuid4(), "t": task, "k": kind, "a": actor, "d": json.dumps(data or {})})

    @staticmethod
    def _assignee(db, workspace, user, actor, member):
        """Display name of the assignee. Only administrators see colleagues, so only they reassign."""
        if user == actor.user_id:
            return actor.display_name or actor.email
        if not member["admin"]:
            raise AuthError(403, "Назначить задачу другому сотруднику может администратор.")
        row = db.execute(text("SELECT coalesce(nullif(u.display_name,''), u.email) FROM workspace_memberships m "
                              "JOIN auth_users u ON u.id=m.user_id WHERE m.workspace_id=:w AND m.user_id=:u AND m.active "
                              "AND (m.role IN ('owner','admin') OR 'messages' = ANY(coalesce(m.sections, ARRAY[]::text[])))"),
                         {"w": workspace, "u": user}).scalar_one_or_none()
        if not row:
            raise AuthError(422, "Этому сотруднику нельзя назначить задачу: у него нет доступа к чатам.")
        return row

    def _editable(self, db, actor, workspace, task, member, studio):
        row = self._row(db, task)
        if row is None:
            raise AuthError(404, "Задача не найдена.")
        if member["observer"]:
            raise AuthError(403, "Роль «Наблюдатель» не может менять задачи.")
        if not self._may_see(actor, member, studio, row):
            raise AuthError(404, "Задача не найдена.")
        return row

    @staticmethod
    def _may_see(actor, member, studio, row):
        # «Только свои» narrows the lists; a dialog's tasks stay visible to whoever
        # answers that client, so nobody promises the same thing twice.
        return bool(member["admin"] or studio["visibility"] == "all" or row["conversation_id"]
                    or actor.user_id in (row["assignee_id"], row["created_by"]))

    @staticmethod
    def _due(value, *, required=True):
        if value is None:
            if required:
                raise AuthError(422, "Укажите срок задачи.")
            return None
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as error:
                raise AuthError(422, "Срок задачи указан неверно.") from error
        if value.tzinfo is None:
            raise AuthError(422, "Срок задачи указан без часового пояса.")
        return value

    @staticmethod
    def _title(value):
        title = " ".join(str(value or "").split())
        if not 1 <= len(title) <= 200:
            raise AuthError(422, "Напишите, что нужно сделать, — до 200 символов.")
        return title

    # ── context and settings ───────────────────────────────────────────────
    def context(self, actor, workspace):
        with self._enter(actor, workspace, snapshot=True) as db:
            member = self._member(db, actor, workspace)
            studio = self._studio(db, workspace)
            rows = [] if not member["admin"] else db.execute(text(
                "SELECT m.user_id, coalesce(nullif(u.display_name,''), u.email) AS name FROM workspace_memberships m "
                "JOIN auth_users u ON u.id=m.user_id WHERE m.workspace_id=:w AND m.active "
                "AND (m.role IN ('owner','admin') OR 'messages' = ANY(coalesce(m.sections, ARRAY[]::text[]))) "
                "ORDER BY name"), {"w": workspace}).mappings().all()
            now = db.execute(text("SELECT now()")).scalar_one()
            return {
                "me": str(actor.user_id), "admin": member["admin"], "read_only": member["observer"],
                "timezone": member["timezone"], "studio": studio,
                "personal": self._personal(db, workspace, actor.user_id),
                "presets": presets(now, ZoneInfo(member["timezone"]), studio),
                "kinds": [{"id": key, "title": value} for key, value in KINDS.items()],
                "assignees": [{"id": str(row["user_id"]), "name": row["name"]} for row in rows]
                or [{"id": str(actor.user_id), "name": actor.display_name or actor.email}],
            }

    def save_studio(self, actor, workspace, data):
        with self._enter(actor, workspace) as db:
            self.accounts._workspace(db, actor, workspace, manage=True)
            current = self._studio(db, workspace)
            merged = _studio(current | data)
            self._store(db, workspace, STUDIO_TOPIC, "workspace", merged)
            return merged

    def save_personal(self, actor, workspace, data):
        with self._enter(actor, workspace) as db:
            self._member(db, actor, workspace)
            merged = _personal(self._personal(db, workspace, actor.user_id) | data)
            self._store(db, workspace, PERSONAL_TOPIC, str(actor.user_id), merged)
            return merged

    # ── reading ────────────────────────────────────────────────────────────
    def list(self, actor, workspace, *, scope="mine", status="open", conversation=None, limit=200):
        with self._enter(actor, workspace, snapshot=True) as db:
            member = self._member(db, actor, workspace)
            studio = self._studio(db, workspace)
            where, params = ["t.workspace_id=:w"], {"w": workspace, "me": actor.user_id, "limit": limit}
            if conversation:
                where.append("t.conversation_id=:c"); params["c"] = conversation
            elif scope == "mine":
                where.append("t.assignee_id=:me")
            else:
                where.append(self._visible(member, studio))
            if status == "open":
                where.append("t.status='open'")
                order = "t.due_at, t.created_at"
            else:
                where.append("t.status<>'open' AND t.updated_at > now() - interval '7 days'")
                order = "t.updated_at DESC"
            rows = db.execute(text(f"SELECT {COLUMNS} FROM tasks t "
                                   f"WHERE {' AND '.join(where)} ORDER BY {order} LIMIT :limit"), params).mappings().all()
            return {"tasks": [self._public(row) for row in rows]}

    def summary(self, actor, workspace):
        """Counters for the badge, markers for the chat list and my upcoming reminders."""
        with self._enter(actor, workspace, snapshot=True) as db:
            member = self._member(db, actor, workspace)
            studio = self._studio(db, workspace)
            params = {"w": workspace, "me": actor.user_id, "tz": member["timezone"]}
            counts = db.execute(text(
                "SELECT count(*) FILTER (WHERE due_at < now()) AS overdue, "
                "count(*) FILTER (WHERE due_at >= now() AND (due_at AT TIME ZONE :tz)::date = (now() AT TIME ZONE :tz)::date) AS today, "
                "count(*) AS open FROM tasks WHERE workspace_id=:w AND status='open' AND assignee_id=:me"),
                params).mappings().one()
            markers = db.execute(text(
                "SELECT t.conversation_id, count(*) AS n, min(t.due_at) AS due_at FROM tasks t "
                f"WHERE t.workspace_id=:w AND t.status='open' AND t.conversation_id IS NOT NULL AND {self._visible(member, studio)} "
                "GROUP BY t.conversation_id"), params).mappings().all()
            upcoming = db.execute(text(
                f"SELECT {COLUMNS} FROM tasks t "
                "WHERE t.workspace_id=:w AND t.status='open' AND t.assignee_id=:me "
                "AND t.due_at < now() + interval '26 hours' ORDER BY t.due_at LIMIT 50"), params).mappings().all()
            now = db.execute(text("SELECT now()")).scalar_one()
            return {
                "now": _iso(now), "overdue": counts["overdue"], "today": counts["today"], "open": counts["open"],
                "badge": counts["overdue"] + counts["today"],
                "conversations": {str(row["conversation_id"]): {"count": row["n"], "due_at": _iso(row["due_at"])}
                                  for row in markers},
                "upcoming": [self._public(row) for row in upcoming],
                "studio": {"day_start": studio["day_start"], "day_end": studio["day_end"], "work_days": studio["work_days"]},
            }

    # ── writing ────────────────────────────────────────────────────────────
    def _sources(self, db, workspace, conversation, message_ids):
        ids = list(dict.fromkeys(message_ids or []))[:MAX_SOURCES]
        if not ids:
            return []
        if conversation is None:
            raise AuthError(422, "Сообщения можно привязать только к задаче из переписки.")
        rows = db.execute(text(
            "SELECT id, direction, text, occurred_at FROM messages WHERE workspace_id=:w AND conversation_id=:c "
            "AND id = ANY(:ids) ORDER BY occurred_at, id"), {"w": workspace, "c": conversation, "ids": ids}).mappings().all()
        if len(rows) != len(ids):
            raise AuthError(422, "Некоторые сообщения не найдены в этой переписке.")
        return [{"id": str(row["id"]), "direction": row["direction"], "at": _iso(row["occurred_at"]),
                 "text": " ".join(str(row["text"] or "").split())[:QUOTE]} for row in rows]

    def create(self, actor, workspace, data):
        with self._enter(actor, workspace) as db:
            member = self._member(db, actor, workspace)
            if member["observer"]:
                raise AuthError(403, "Роль «Наблюдатель» не может создавать задачи.")
            studio = self._studio(db, workspace)
            conversation = data.get("conversation_id")
            if conversation is not None and db.execute(text(
                    "SELECT 1 FROM conversations WHERE workspace_id=:w AND id=:c"),
                    {"w": workspace, "c": conversation}).scalar_one_or_none() is None:
                raise AuthError(404, "Переписка не найдена.")
            due = self._due(data.get("due_at"), required=False)
            if due is None:
                now = db.execute(text("SELECT now()")).scalar_one()
                due = datetime.fromisoformat(presets(now, ZoneInfo(member["timezone"]), studio)["default"])
            assignee = data.get("assignee_id") or actor.user_id
            assignee_name = self._assignee(db, workspace, assignee, actor, member)
            kind = data.get("kind")
            if kind is not None and kind not in KINDS:
                raise AuthError(422, "Неизвестный тип задачи.")
            origin = data.get("origin") or "manual"
            if origin not in ORIGINS:
                origin = "manual"
            task = uuid4()
            db.execute(text(
                "INSERT INTO tasks(workspace_id,id,conversation_id,client_label,title,details,kind,due_at,origin,"
                "sources,created_by,assignee_id,assignee_name) VALUES(:w,:id,:c,:label,:title,:details,:kind,:due,:origin,"
                "CAST(:sources AS jsonb),:me,:assignee,:assignee_name)"),
                {"w": workspace, "id": task, "c": conversation,
                 "label": " ".join(str(data.get("client_label") or "").split())[:200],
                 "title": self._title(data.get("title")), "details": str(data.get("details") or "").strip()[:2000],
                 "kind": kind, "due": due, "origin": origin,
                 "sources": json.dumps(self._sources(db, workspace, conversation, data.get("message_ids"))),
                 "me": actor.user_id, "assignee": assignee, "assignee_name": assignee_name[:200]})
            self._event(db, workspace, task, "created", actor.user_id, {"origin": origin})
            return self._public(self._row(db, task))

    def voice(self, actor, workspace, data):
        """A dictated task: fields from the phrase, sources from the dialog's last messages.

        With create=True a phrase of three words or more becomes a task at once
        (the operator can undo it); otherwise the draft fills the task sheet.
        """
        spoken = " ".join(str(data.get("text") or "").split())[:1000]
        with self._enter(actor, workspace, snapshot=True) as db:
            member = self._member(db, actor, workspace)
            if member["observer"]:
                raise AuthError(403, "Роль «Наблюдатель» не может создавать задачи.")
            studio, zone = self._studio(db, workspace), ZoneInfo(member["timezone"])
            now = db.execute(text("SELECT now()")).scalar_one()
            people = [{"id": actor.user_id, "name": actor.display_name or actor.email}]
            if member["admin"]:
                people = [{"id": row["user_id"], "name": row["name"]} for row in db.execute(text(
                    "SELECT m.user_id, coalesce(nullif(u.display_name,''), u.email) AS name FROM workspace_memberships m "
                    "JOIN auth_users u ON u.id=m.user_id WHERE m.workspace_id=:w AND m.active "
                    "AND (m.role IN ('owner','admin') OR 'messages' = ANY(coalesce(m.sections, ARRAY[]::text[])))"),
                    {"w": workspace}).mappings().all()] or people
            parsed = task_speech.parse(spoken, now, zone, studio, people)
            conversation, sources = data.get("conversation_id"), []
            if conversation is not None:
                recent = db.execute(text(
                    "SELECT id, direction, text, occurred_at FROM messages WHERE workspace_id=:w AND conversation_id=:c "
                    "AND occurred_at > now() - interval '24 hours' AND payload->>'superseded_by' IS NULL "
                    "AND actor_type <> 'automation' ORDER BY occurred_at DESC, id DESC LIMIT 10"),
                    {"w": workspace, "c": conversation}).mappings().all()
                sources = [{"id": str(row["id"]), "direction": row["direction"], "at": _iso(row["occurred_at"]),
                            "text": " ".join(str(row["text"] or "").split())[:QUOTE]} for row in task_speech.link(recent)]
        if not parsed["title"]:
            raise AuthError(422, "Не удалось разобрать задачу. Скажите, что нужно сделать.")
        due = parsed["due_at"] or datetime.fromisoformat(presets(now, zone, studio)["default"])
        draft = {"title": parsed["title"], "due_at": due.isoformat(), "kind": parsed["kind"],
                 "assignee_id": str(parsed["assignee_id"] or actor.user_id), "sources": sources, "spoken": spoken,
                 "conversation_id": str(conversation) if conversation else None,
                 "client_label": " ".join(str(data.get("client_label") or "").split())[:200]}
        if not data.get("create") or len(parsed["title"].split()) < 3:
            return {"draft": draft}
        created = self.create(actor, workspace, {
            "title": draft["title"], "details": "", "kind": draft["kind"], "due_at": due, "conversation_id": conversation,
            "client_label": draft["client_label"], "message_ids": [UUID(source["id"]) for source in sources],
            "assignee_id": parsed["assignee_id"] or actor.user_id, "origin": "voice"})
        return {"task": created}

    # ── AI: draft from messages, promise hint, reply on completion ─────────
    def _dialog(self, db, workspace, conversation, limit=10, before_ids=()):
        rows = db.execute(text(
            "SELECT id, direction, text, occurred_at FROM messages WHERE workspace_id=:w AND conversation_id=:c "
            "AND payload->>'superseded_by' IS NULL AND actor_type <> 'automation' AND coalesce(text,'') <> '' "
            "ORDER BY occurred_at DESC, id DESC LIMIT :n"), {"w": workspace, "c": conversation, "n": limit}).mappings().all()
        return list(rows)

    @staticmethod
    def _transcript(rows, marked=()):
        lines = []
        for row in sorted(rows, key=lambda r: r["occurred_at"]):
            who = "Администратор" if row["direction"] == "out" else "Родитель"
            mark = "→ " if str(row["id"]) in marked else ""
            lines.append(f"{mark}{who}: {' '.join(str(row['text']).split())[:500]}")
        return "\n".join(lines)

    def _ai(self, actor, workspace, instruction, content, **options):
        if self.ai is None or not content:
            return None
        try:
            return self.ai.assist(actor, workspace, instruction, content, **options)
        except AuthError:
            return None

    def _fields(self, answer, now, zone, studio):
        data = _json(answer) or {}
        title = " ".join(str(data.get("title") or "").split()).strip(" .")[:200].rstrip(" ,;")
        kind = data.get("kind") if data.get("kind") in KINDS else None
        when = str(data.get("when") or "").strip()
        due = task_speech.parse(when, now, zone, studio)["due_at"] if when else None
        return data, title, kind, due

    def draft(self, actor, workspace, data):
        """Model suggestion for the task sheet; {"ai": False} keeps the sheet as it is."""
        conversation, marked = data.get("conversation_id"), {str(i) for i in data.get("message_ids") or []}
        with self._enter(actor, workspace, snapshot=True) as db:
            member = self._member(db, actor, workspace)
            studio, zone = self._studio(db, workspace), ZoneInfo(member["timezone"])
            now = db.execute(text("SELECT now()")).scalar_one()
            rows = self._dialog(db, workspace, conversation, 14) if conversation else []
            missing = [UUID(i) for i in marked if i not in {str(row["id"]) for row in rows}]
            if conversation and missing:
                # Picked messages older than the recent window still go to the model.
                rows += list(db.execute(text(
                    "SELECT id, direction, text, occurred_at FROM messages WHERE workspace_id=:w AND conversation_id=:c "
                    "AND id = ANY(:ids) AND coalesce(text,'') <> ''"), {"w": workspace, "c": conversation, "ids": missing[:5]}).mappings().all())
        content = self._transcript(rows, marked)
        if data.get("client_label"):
            content = f"Клиент: {data['client_label']}\n{content}"
        answer = self._ai(actor, workspace, AI_DRAFT, content, max_tokens=5000, timeout=15)
        raw, title, kind, due = self._fields(answer, now, zone, studio)
        if not title:
            return {"ai": False}
        details = "\n".join(line.strip() for line in str(raw.get("details") or "").splitlines() if line.strip())[:2000]
        return {"ai": True, "title": title, "details": details, "kind": kind, "due_at": _iso(due)}

    def hint(self, actor, workspace, data):
        """After the operator's message: suggest a task when it reads like a promise."""
        spoken = " ".join(str(data.get("text") or "").split())[:1000]
        conversation = data.get("conversation_id")
        if not spoken or not task_speech.PROMISE.search(spoken) or conversation is None:
            return {"hint": None}
        with self._enter(actor, workspace, snapshot=True) as db:
            member = self._member(db, actor, workspace)
            if member["observer"]:
                return {"hint": None}
            if not self._personal(db, workspace, actor.user_id)["hints"]:
                return {"hint": None}
            studio, zone = self._studio(db, workspace), ZoneInfo(member["timezone"])
            now = db.execute(text("SELECT now()")).scalar_one()
            rows = self._dialog(db, workspace, conversation, 8)
            open_titles = [row["title"] for row in db.execute(text(
                "SELECT title FROM tasks WHERE workspace_id=:w AND conversation_id=:c AND status='open'"),
                {"w": workspace, "c": conversation}).mappings().all()]
        before = [row for row in rows if " ".join(str(row["text"]).split()) != spoken]
        sources = [{"id": str(row["id"]), "direction": row["direction"], "at": _iso(row["occurred_at"]),
                    "text": " ".join(str(row["text"] or "").split())[:QUOTE]}
                   for row in task_speech.link([r for r in before])]
        content = self._transcript(before) + f"\nАдминистратор: {spoken}"
        answer = self._ai(actor, workspace, AI_HINT, content, max_tokens=5000, timeout=10)
        data_ai, title, kind, due = self._fields(answer, now, zone, studio)
        if answer is not None and data_ai and data_ai.get("task") is False:
            return {"hint": None}
        if not title:
            # No model: the client's latest question is what was promised.
            last = next((row for row in before if row["direction"] == "in"), None)
            if last is None:
                return {"hint": None}
            client = " ".join(str(last["text"]).split())
            title = "Ответить: " + (client if len(client) <= 80 else client[:78].rsplit(" ", 1)[0] + "…")
            if not any(source["id"] == str(last["id"]) for source in sources):
                sources = [{"id": str(last["id"]), "direction": "in", "at": _iso(last["occurred_at"]), "text": client[:QUOTE]}]
        if title in open_titles:
            return {"hint": None}
        due = due or datetime.fromisoformat(presets(now, zone, studio)["default"])
        return {"hint": {"title": title, "kind": kind, "due_at": _iso(due), "sources": sources, "ai": bool(answer)}}

    def reply(self, actor, workspace, task):
        with self._enter(actor, workspace, snapshot=True) as db:
            member = self._member(db, actor, workspace)
            studio = self._studio(db, workspace)
            row = self._editable(db, actor, workspace, task, member, studio)
            rows = self._dialog(db, workspace, row["conversation_id"], 8) if row["conversation_id"] else []
        content = (f"Задача: {row['title']}\nПодробности: {row['details'] or 'нет'}\nКлиент: {row['client_label'] or 'родитель'}\n"
                   f"Переписка:\n{self._transcript(rows)}")
        answer = self._ai(actor, workspace, AI_REPLY, content, max_tokens=5000, timeout=10)
        reply = " ".join(str(answer or "").split()).strip('"«» ')
        return {"text": reply[:1000] or None}

    def update(self, actor, workspace, task, data):
        with self._enter(actor, workspace) as db:
            member = self._member(db, actor, workspace)
            studio = self._studio(db, workspace)
            row = self._editable(db, actor, workspace, task, member, studio)
            changes, params = [], {"w": workspace, "id": task}
            if "title" in data:
                changes.append("title=:title"); params["title"] = self._title(data["title"])
            if "details" in data:
                changes.append("details=:details"); params["details"] = str(data["details"] or "").strip()[:2000]
            if "kind" in data:
                if data["kind"] is not None and data["kind"] not in KINDS:
                    raise AuthError(422, "Неизвестный тип задачи.")
                changes.append("kind=:kind"); params["kind"] = data["kind"]
            if "due_at" in data:
                changes.append("due_at=:due"); params["due"] = self._due(data["due_at"])
            if "assignee_id" in data and data["assignee_id"] and data["assignee_id"] != row["assignee_id"]:
                params["assignee_name"] = self._assignee(db, workspace, data["assignee_id"], actor, member)[:200]
                changes.append("assignee_id=:assignee, assignee_name=:assignee_name"); params["assignee"] = data["assignee_id"]
                self._event(db, workspace, task, "assigned", actor.user_id, {"to": str(data["assignee_id"])})
            if "message_ids" in data:
                changes.append("sources=CAST(:sources AS jsonb)")
                params["sources"] = json.dumps(self._sources(db, workspace, row["conversation_id"], data["message_ids"]))
            if changes:
                db.execute(text(f"UPDATE tasks SET {', '.join(changes)}, version=version+1, updated_at=now() "
                                "WHERE workspace_id=:w AND id=:id"), params)
                self._event(db, workspace, task, "edited", actor.user_id, {"fields": sorted(data)})
            return self._public(self._row(db, task))

    def transition(self, actor, workspace, task, action, due_at=None):
        with self._enter(actor, workspace) as db:
            member = self._member(db, actor, workspace)
            studio = self._studio(db, workspace)
            row = self._editable(db, actor, workspace, task, member, studio)
            params = {"w": workspace, "id": task, "me": actor.user_id}
            if action == "complete":
                if row["status"] != "done":
                    db.execute(text("UPDATE tasks SET status='done', completed_at=now(), completed_by=:me, "
                                    "version=version+1, updated_at=now() WHERE workspace_id=:w AND id=:id"), params)
                    self._event(db, workspace, task, "done", actor.user_id)
            elif action == "cancel":
                if row["status"] != "cancelled":
                    db.execute(text("UPDATE tasks SET status='cancelled', completed_at=now(), completed_by=:me, "
                                    "version=version+1, updated_at=now() WHERE workspace_id=:w AND id=:id"), params)
                    self._event(db, workspace, task, "cancelled", actor.user_id)
            elif action == "reopen":
                if row["status"] != "open":
                    db.execute(text("UPDATE tasks SET status='open', completed_at=NULL, completed_by=NULL, "
                                    "version=version+1, updated_at=now() WHERE workspace_id=:w AND id=:id"), params)
                    self._event(db, workspace, task, "reopened", actor.user_id)
            elif action == "snooze":
                params["due"] = self._due(due_at)
                db.execute(text("UPDATE tasks SET due_at=:due, status='open', completed_at=NULL, completed_by=NULL, "
                                "version=version+1, updated_at=now() WHERE workspace_id=:w AND id=:id"), params)
                self._event(db, workspace, task, "snoozed", actor.user_id,
                            {"from": _iso(row["due_at"]), "to": _iso(params["due"])})
            else:
                raise AuthError(422, "Неизвестное действие с задачей.")
            return self._public(self._row(db, task))

    def history(self, actor, workspace, task):
        with self._enter(actor, workspace, snapshot=True) as db:
            member = self._member(db, actor, workspace)
            studio = self._studio(db, workspace)
            row = self._row(db, task)
            if row is None or not self._may_see(actor, member, studio, row):
                raise AuthError(404, "Задача не найдена.")
            events = db.execute(text(
                "SELECT e.kind, e.data, e.created_at, coalesce(nullif(u.display_name,''), u.email) AS actor "
                "FROM task_events e LEFT JOIN auth_users u ON u.id=e.actor_id "
                "WHERE e.workspace_id=:w AND e.task_id=:t ORDER BY e.created_at"),
                {"w": workspace, "t": task}).mappings().all()
            return {"task": self._public(row),
                    "events": [{"kind": e["kind"], "data": e["data"], "at": _iso(e["created_at"]), "actor": e["actor"] or ""}
                               for e in events]}


def as_uuid(value):
    return value if isinstance(value, UUID) or value is None else UUID(str(value))
