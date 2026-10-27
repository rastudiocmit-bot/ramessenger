"""Turn a dictated task («завтра до обеда перезвонить, поручи Ольге») into fields.

Plain rules, no model: the due time, the assignee and the kind are found by
phrases, then removed from the text, and what is left becomes «Что сделать».
Times are read in the studio's working day: «утром» is its start, «к вечеру»
18:00, «до конца дня» its end. Nothing here guesses a client.
"""

import re
from datetime import datetime, time, timedelta

TRIGGER = re.compile(r"^\s*(?:поставь\s+задачу|создай\s+задачу|новая\s+задача|задача|напомни(?:ть)?(?:\s+мне)?)\s*[:,.\-—]*\s*", re.I)
WEEKDAYS = {"понедельник": 1, "вторник": 2, "сред": 3, "четверг": 4, "пятниц": 5, "суббот": 6, "воскресень": 7}
WORD_NUMBERS = {"один": 1, "одну": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5, "шесть": 6}
KIND_WORDS = (("makeup", r"отработ"), ("subscription", r"абонемент|оплат|долг"), ("call", r"перезвон|позвон|созвон"),
              ("group", r"в\s+групп|групп[уы]|расписани|записать\s+на\s+(?:пробн|заняти)"))
PROMISE = re.compile(r"уточн|провер|посмотр|сообщ|перезвон|вернусь|напиш|узна|отвеч", re.I)


def _next_day(today, wanted, work_days):
    ahead = (wanted - today.isoweekday()) % 7 or 7
    return today + timedelta(days=ahead)


def _take(pattern, text, flags=re.I):
    match = re.search(pattern, text, flags)
    if not match:
        return None, text
    return match, (text[:match.start()] + " " + text[match.end():])


def parse(text, now, zone, studio, people=()):
    """Return {"title", "due_at" (aware datetime or None), "kind", "assignee_id"}."""
    local = now.astimezone(zone)
    body = TRIGGER.sub("", " ".join(str(text or "").split()))
    today, day, moment, delta = local.date(), None, None, None
    start, end = time.fromisoformat(studio["day_start"]), time.fromisoformat(studio["day_end"])

    match, body = _take(r"\bпоручи(?:ть)?\b|\bпередай\b|\bназначь(?:\s+на)?\b", body)
    assignee = None
    if match:
        name, rest = _take(r"^\s*([А-ЯЁа-яё]+)", body[match.start():])
        if name:
            token = name.group(1).lower()
            for person in people:
                first = str(person["name"]).split()[0].lower() if person.get("name") else ""
                if len(first) >= 3 and token[:max(3, len(first) - 2)] == first[:max(3, len(first) - 2)]:
                    assignee = person["id"]; break
            body = body[:match.start()] + rest

    match, body = _take(r"\bчерез\s+(полчаса|(\d+|[а-я]+)?\s*(час(?:а|ов)?|минут[уы]?))\b", body)
    if match:
        if match.group(1) == "полчаса":
            delta = timedelta(minutes=30)
        else:
            raw = (match.group(2) or "1").strip()
            amount = int(raw) if raw.isdigit() else WORD_NUMBERS.get(raw, 1)
            delta = timedelta(hours=amount) if match.group(3).startswith("час") else timedelta(minutes=amount)

    match, body = _take(r"\b(?:(?:до|к|на)\s+)?(послезавтра|завтра|сегодня)\b", body)
    if match:
        day = today + timedelta(days={"сегодня": 0, "завтра": 1, "послезавтра": 2}[match.group(1).lower()])
    else:
        match, body = _take(r"\b(?:во?|до|к|ко)\s+(понедельник\w*|вторник\w*|сред\w*|четверг\w*|пятниц\w*|суббот\w*|воскресень\w*)", body)
        if match:
            word = match.group(1).lower()
            wanted = next(value for key, value in WEEKDAYS.items() if word.startswith(key))
            day = _next_day(today, wanted, studio["work_days"])

    match, body = _take(r"\b(?:в|к|до|на)\s+(\d{1,2})(?:[:.](\d{2})|\s+час(?:а|ов)?)?(?:\s+(утра|дня|вечера))?\b(?!\s*(?:минут|час|дн|недел|занят|урок|раз))", body)
    if match:
        hours, minutes, part = int(match.group(1)), int(match.group(2) or 0), match.group(3)
        if part in ("дня", "вечера") and hours < 12:
            hours += 12
        elif part is None and hours < 8:
            hours += 12  # «в 5» in a studio means five in the evening
        if 0 <= hours <= 23 and 0 <= minutes <= 59:
            moment = time(hours, minutes)
    if moment is None:
        for pattern, value in ((r"\bдо\s+конца\s+(?:рабочего\s+)?дня\b", end), (r"\b(?:до|к)\s+обеда\b|\bв\s+обед\b", time(13, 0)),
                               (r"\bутром\b|\bс\s+утра\b", start), (r"\bднём\b|\bднем\b", time(14, 0)),
                               (r"\b(?:к\s+)?вечеру\b|\bвечером\b", min(time(18, 0), end))):
            match, body = _take(pattern, body)
            if match:
                moment = value; break

    due = None
    if delta is not None:
        due = local + delta
    elif day is not None or moment is not None:
        if day is None:
            day = today if datetime.combine(today, moment, zone) > local else today + timedelta(days=1)
        due = datetime.combine(day, moment or start, zone)
        if due <= local:
            due = local + timedelta(hours=1)

    kind = next((key for key, pattern in KIND_WORDS if re.search(pattern, body, re.I)), None)
    body = re.sub(r"\b(?:сделать|нужно|надо|пожалуйста)\b", " ", body, flags=re.I)
    title = re.sub(r"\s+([,.!?])", r"\1", " ".join(body.split())).strip(" ,.;:-—")
    title = re.sub(r"^(?:и|а|,)\s+", "", title)
    title = re.sub(r"(?:[,\s]+(?:и|а|до|к|в|на|по))+$", "", title, flags=re.I).strip(" ,.;:-—")
    title = title[:1].upper() + title[1:]
    return {"title": title[:200], "due_at": due, "kind": kind, "assignee_id": assignee}


def link(messages):
    """Pick the messages a dictated task is about, from the newest first.

    The client's messages after the operator's last answer; failing that, the
    operator's last message that sounds like a promise («уточню», «проверю»).
    """
    picked = []
    for message in messages:
        if message["direction"] == "out":
            break
        if message["text"]:
            picked.append(message)
    if picked:
        return list(reversed(picked[:3]))
    for message in messages:
        if message["direction"] == "out" and message["text"] and PROMISE.search(message["text"]):
            return [message]
    return []
