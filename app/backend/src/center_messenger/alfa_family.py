"""Read bounded customer finance and lessons from documented AlfaCRM v2api endpoints."""

from datetime import date, timedelta
from decimal import Decimal

from center_messenger.alfa_client import AlfaError
from center_messenger.crm_directory import (
    page_rows,
    positive_id,
    source_balance,
    source_date,
    source_lessons,
)


def pages(client, path, filters, *, maximum=1000):
    found, seen = [], set()
    for page in range(10):
        data = client.call(path, {**filters, "page": page, "pageSize": 500})
        items, total = page_rows(data, page)
        for item in items:
            identifier = positive_id(item["id"])
            if identifier in seen:
                raise AlfaError("invalid_response")
            seen.add(identifier)
        found.extend(items)
        if len(found) > maximum or len(found) > total:
            raise AlfaError("import_limit")
        if len(found) == total:
            return found
        if not items:
            raise AlfaError("invalid_response")
    raise AlfaError("import_limit")


def _ids(value):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 100:
        raise AlfaError("invalid_response")
    return [positive_id(item) for item in value]


def _attendance(value):
    if value is None:
        return None
    if value in (True, 1, "1"):
        return True
    if value in (False, 0, "0"):
        return False
    raise AlfaError("invalid_response")


def _lesson(raw, customer_id):
    day = source_date(raw.get("date"))
    if not day:
        raise AlfaError("invalid_response")
    groups = _ids(raw.get("group_ids"))
    customers = _ids(raw.get("customer_ids"))
    details = raw.get("details") or []
    if not isinstance(details, list) or len(details) > 500:
        raise AlfaError("invalid_response")
    attendance, subscription_id = None, None
    for detail in details:
        if not isinstance(detail, dict) or str(detail.get("customer_id")) != str(customer_id):
            continue
        attendance = _attendance(detail.get("is_attend"))
        if detail.get("ctt_id") not in (None, "", 0, "0"):
            subscription_id = positive_id(detail["ctt_id"])
        break
    # Individual lessons have customer_ids; group lessons may list only group_ids.
    if customers and customer_id not in customers:
        raise AlfaError("invalid_response")
    time = raw.get("time_from")
    if time is not None and not isinstance(time, str):
        raise AlfaError("invalid_response")
    return {
        "id": positive_id(raw["id"]),
        "date": day.isoformat(),
        "time": time.rsplit(" ", 1)[-1][:5] if time and len(time) >= 5 else None,
        "group_ids": groups,
        "attended": attendance,
        "subscription_id": subscription_id,
    }


def fetch_family_card(client, branch_id, customer_id, *, source_customer, today=None):
    """Read bounded child facts while using the latest synchronized source customer row."""
    today = today or date.today()
    root = f"/v2api/{positive_id(branch_id)}"
    customer_id = positive_id(customer_id)
    if (
        not isinstance(source_customer, dict)
        or positive_id(source_customer.get("id")) != customer_id
        or positive_id(source_customer.get("branch_id")) != branch_id
    ):
        raise AlfaError("invalid_response")
    raw_subscriptions = pages(
        client,
        f"{root}/customer-tariff/index?customer_id={customer_id}",
        {"customer_id": customer_id, "dead": False},
        maximum=500,
    )
    archived_ids = {
        positive_id(item["id"])
        for item in pages(
            client,
            f"{root}/customer-tariff/index?customer_id={customer_id}",
            {"customer_id": customer_id, "dead": True},
            maximum=500,
        )
    }
    subscriptions = []
    for raw in raw_subscriptions:
        if str(raw.get("customer_id")) != str(customer_id):
            raise AlfaError("invalid_response")
        tariff_id = positive_id(raw["tariff_id"])
        amount = source_balance(raw.get("balance"))
        begin, end = source_date(raw.get("b_date")), source_date(raw.get("e_date"))
        subscriptions.append(
            {
                "id": positive_id(raw["id"]),
                "tariff_id": tariff_id,
                "balance": str(amount) if amount is not None else None,
                "begin": begin.isoformat() if begin else None,
                "end": end.isoformat() if end else None,
                "archived": positive_id(raw["id"]) in archived_ids
                or raw.get("dead") in (True, 1, "1"),
            }
        )
    names = {}
    if subscriptions:
        tariffs = pages(client, f"{root}/tariff/index", {"is_archive": False}, maximum=5000)
        names = {
            positive_id(item["id"]): {"name": item.get("name"), "type": item.get("type")}
            for item in tariffs
        }
    for item in subscriptions:
        item.update(
            names.get(item["tariff_id"], {"name": f"Абонемент №{item['tariff_id']}", "type": None})
        )
    lessons = []
    for status, start, end in (
        (3, today - timedelta(days=365), today),
        (1, today, today + timedelta(days=90)),
    ):
        for raw in pages(
            client,
            f"{root}/lesson/index",
            {
                "customer_id": customer_id,
                "status": status,
                "date_from": start.isoformat(),
                "date_to": end.isoformat(),
            },
            maximum=1000,
        ):
            lesson = _lesson(raw, customer_id)
            lesson["status"] = status
            lessons.append(lesson)
    money = source_balance(source_customer.get("balance"))
    return {
        "today": today.isoformat(),
        "balance": money,
        "paid_lesson_count": source_lessons(source_customer.get("paid_lesson_count")),
        "subscriptions": subscriptions,
        "lessons": lessons,
    }


def student_balance(cards):
    """Sum distinct CRM customer accounts, never copies of one account in branches."""
    accounts = {}
    for card in cards:
        identifier = str(card["id"])
        amount = source_balance(card.get("balance"))
        previous = accounts.get(identifier)
        if previous is not None and amount is not None and previous != amount:
            # Conflicting snapshots cannot safely be treated as separate accounts.
            return None
        accounts[identifier] = amount if amount is not None else previous
    if not accounts or any(value is None for value in accounts.values()):
        return None
    return str(sum(accounts.values(), Decimal("0")))


def group_facts(groups, cards, *, today=None):
    """A lesson links to a group by branch and ID; ctt_id is the tariff link."""
    today = today or date.today()
    for group in groups:
        matched, linked = [], {}
        for card in cards:
            if card["branch_id"] != group["branch_id"]:
                continue
            if group.get("customer_ids") and str(card.get("id")) not in group["customer_ids"]:
                continue
            tariffs = {item["id"]: item for item in card.get("subscriptions", [])}
            for lesson in card.get("lessons", []):
                if int(group["id"]) in lesson["group_ids"]:
                    matched.append(lesson)
                    if lesson["subscription_id"] in tariffs:
                        tariff = tariffs[lesson["subscription_id"]]
                        linked[(str(card.get("id", "")), tariff["id"])] = tariff
        before = [
            item for item in matched if item["status"] == 3 and item["date"] <= today.isoformat()
        ]
        after = [
            item for item in matched if item["status"] == 1 and item["date"] >= today.isoformat()
        ]

        def order(item):
            return (item["date"], item["time"] or "")

        group["previous_lesson"] = max(before, key=order) if before else None
        group["next_lesson"] = min(after, key=order) if after else None
        group["subscriptions"] = [linked[key] for key in sorted(linked)]
