"""Read bounded customer finance and lessons from documented AlfaCRM v2api endpoints."""

from datetime import UTC, date, datetime, timedelta

from center_messenger.alfa_client import AlfaError
from center_messenger.crm_directory import (
    page_rows,
    positive_id,
    source_balance,
    source_date,
    source_lessons,
)


def pages(client, path, filters, *, maximum=1000):
    found, seen, expected = [], set(), None
    for page in range(10):
        data = client.call(path, {**filters, "page": page, "pageSize": 500})
        items, total = page_rows(data, page)
        if expected is not None and total != expected:
            raise AlfaError("invalid_response")
        expected = total
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


def _optional_id(value):
    return None if value in (None, "", 0, "0") else positive_id(value)


def _lesson(raw, customer_id):
    day = source_date(raw.get("date"))
    if not day:
        raise AlfaError("invalid_response")
    groups = _ids(raw.get("group_ids"))
    customers = _ids(raw.get("customer_ids"))
    details = raw.get("details") or []
    if not isinstance(details, list) or len(details) > 500:
        raise AlfaError("invalid_response")
    attendance, subscription_id, own_detail = None, None, False
    for detail in details:
        if not isinstance(detail, dict) or str(detail.get("customer_id")) != str(customer_id):
            continue
        own_detail = True
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
        "branch_id": _optional_id(raw.get("branch_id")),
        "subject_id": _optional_id(raw.get("subject_id")),
        "lesson_type_id": _optional_id(raw.get("lesson_type_id")),
        "customer_id": customer_id,
        "customer_verified": own_detail or customer_id in customers,
        "date": day.isoformat(),
        "time": time.rsplit(" ", 1)[-1][:5] if time and len(time) >= 5 else None,
        "group_ids": groups,
        "attended": attendance,
        "subscription_id": subscription_id,
    }


def fetch_family_finance(client, branch_id, customer_id, *, source_customer):
    """Read live totals/subscriptions without waiting for lesson history."""
    branch_id = positive_id(branch_id)
    root = f"/v2api/{positive_id(branch_id)}"
    customer_id = positive_id(customer_id)
    if (
        not isinstance(source_customer, dict)
        or positive_id(source_customer.get("id")) != customer_id
        or positive_id(source_customer.get("branch_id")) != branch_id
    ):
        raise AlfaError("invalid_response")
    # Customer.balance is AlfaCRM's total money balance (base account and
    # subscriptions). CustomerTariff.balance is only one component; adding it
    # again would double-count money. Never reuse the directory snapshot when
    # the operator explicitly refreshes this card.
    customer = None
    removed = (2, 0) if source_customer.get("status") == "archive" else (0, 2)
    for state in removed:
        rows = pages(client, f"{root}/customer/index",
                     {"id": customer_id, "is_study": 2, "removed": state}, maximum=1)
        if rows:
            if positive_id(rows[0]["id"]) != customer_id:
                raise AlfaError("invalid_response")
            customer = rows[0]
            break
    if customer is None:
        raise AlfaError("not_found")
    money = source_balance(customer.get("balance"))
    paid = source_lessons(customer.get("paid_lesson_count"))
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
                "subject_ids": _ids(raw.get("subject_ids")),
                "lesson_type_ids": _ids(raw.get("lesson_type_ids")),
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
            positive_id(item["id"]): {"name": item.get("name"), "type": item.get("type"),
                "tariff_branch_ids": _ids(item.get("branch_ids"))}
            for item in tariffs
        }
    for item in subscriptions:
        item.update(
            names.get(item["tariff_id"], {"name": f"Абонемент №{item['tariff_id']}", "type": None})
        )
    return {"balance": money, "paid_lesson_count": paid, "subscriptions": subscriptions}


def fetch_family_lessons(client, branch_id, customer_id, *, today=None, full=False, history_only=False):
    """Replace a date window, so removed/cancelled lessons do not linger.

    AlfaCRM documents date_from/date_to for lesson/index. A short complete
    window also detects deletions, unlike a changed-since-only request.
    """
    today = today or date.today()
    root = f"/v2api/{positive_id(branch_id)}"
    customer_id = positive_id(customer_id)
    lessons, seen = [], set()
    windows = (
        (3, today - timedelta(days=365 if full else 30), today),
        (1, today, today + timedelta(days=90)),
    )
    if history_only:
        if not full:
            raise ValueError("Only a daily check can reuse the recent window")
        windows = ((3, today - timedelta(days=365), today - timedelta(days=31)),)
    for status, start, end in windows:
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
            if not start.isoformat() <= lesson["date"] <= end.isoformat() or (
                raw.get("status") is not None and str(raw["status"]) != str(status)
            ) or lesson["id"] in seen:
                raise AlfaError("invalid_response")
            seen.add(lesson["id"])
            lesson["status"] = status
            lessons.append(lesson)
    return lessons


def merge_family_lessons(saved, incoming, *, today, full=False, history_only=False):
    """Retain older proof of branch/subject/purchase; replace the recent window."""
    first = (today - timedelta(days=365)).isoformat()
    recent = (today - timedelta(days=30)).isoformat()
    if history_only:
        # The half-hour task owns recent cancellations and mutable future dates.
        # It may have published while the larger historical task was reading.
        retained = [item for item in saved if item["date"] >= recent]
        current_ids = {item["id"] for item in retained}
        incoming = [item for item in incoming if first <= item["date"] < recent and item["id"] not in current_ids]
    else:
        replacement_ids = {item["id"] for item in incoming}
        retained = [] if full else [item for item in saved
            if first <= item["date"] < recent and item["id"] not in replacement_ids]
    return sorted(retained + incoming, key=lambda item: (item["date"], item.get("time") or "", item["id"]))


def fetch_family_card(client, branch_id, customer_id, *, source_customer, today=None):
    """Explicit synchronous full refresh, also used by the legacy card endpoint."""
    today = today or date.today()
    observed_at = datetime.now(UTC).isoformat()
    data = fetch_family_finance(client, branch_id, customer_id, source_customer=source_customer)
    return {
        **data,
        "today": today.isoformat(),
        "lessons": [dict(item, observed_at=observed_at) for item in
                    fetch_family_lessons(client, branch_id, customer_id, today=today, full=True)],
    }


def _customer_total(cards, field, parse):
    customers = {}
    for card in cards:
        customers.setdefault(positive_id(card["id"]), []).append(card)
    total = None
    for observations in customers.values():
        latest = max(card.get("balance_updated_at") or "" for card in observations)
        amounts = {parse(card.get(field)) for card in observations
                   if (card.get("balance_updated_at") or "") == latest}
        amounts.discard(None)
        if len(amounts) != 1:
            return None
        amount = amounts.pop()
        total = amount if total is None else total + amount
    return total


def customer_balance(cards):
    """Sum authoritative totals once per CRM customer, never subscription rows.

    The same global Customer ID is observed in several branches. Prefer the
    latest live observation; equally recent conflicting values are unknown,
    not an arbitrary or duplicated amount. Distinct financial cards of the
    selected child are additive; another child's money is not included.
    """
    total = _customer_total(cards, "balance", source_balance)
    return str(total) if total is not None else None


def customer_paid_lessons(cards):
    """Customer.paid_lesson_count is shared by its branch observations too."""
    return _customer_total(cards, "paid_lesson_count", source_lessons)


def mapping_pending(card):
    """Older persistent caches lack the facts needed to prove branch relations."""
    return ("subscriptions" not in card or "lessons" not in card
        or ("lessons_refreshed_at" in card and card["lessons_refreshed_at"] is None)
        or any("subject_ids" not in item or "lesson_type_ids" not in item for item in card.get("subscriptions", []))
        or any(any(field not in item for field in ("branch_id", "subject_id", "lesson_type_id", "customer_id", "customer_verified"))
               for item in card.get("lessons", [])))


def _observed_at(value):
    try:
        return datetime.fromisoformat(value).astimezone(UTC) if value else None
    except (ValueError, TypeError):
        return None


def _coverage_windows(card):
    """Only a complete date window can prove absence from the actual branch."""
    windows = []
    for field, days, local_field in (("lessons_refreshed_at", 30, "lessons_recent_day"),
                                     ("lessons_full_refreshed_at", 365, "lessons_full_day")):
        at = _observed_at(card.get(field))
        if at is None:
            continue
        checked_day = source_date(card.get(local_field)) or at.date()
        through = source_date(card.get("lessons_history_through")) if field == "lessons_full_refreshed_at" else None
        windows.append((checked_day - timedelta(days=days), through or checked_day + timedelta(days=90), at))
    return windows


def current_lessons(cards):
    """Resolve mirrored lesson versions before computing dates or branch links."""
    available = {(str(c["id"]), str(c["branch_id"])) for c in cards}
    branches = {}
    for card in cards:
        key = (str(card["id"]), str(card["branch_id"]))
        identifiers = {str(item["id"]) for item in card.get("lessons", []) if str(item.get("branch_id")) == key[1]}
        branches.setdefault(key, []).append((identifiers, _coverage_windows(card)))
    observations = {}
    for card in cards:
        for lesson in card.get("lessons", []):
            branch, customer = str(lesson.get("branch_id")), str(lesson.get("customer_id"))
            if (customer, branch) not in available or customer != str(card["id"]) or not lesson.get("date"):
                continue
            # Updating the recent window does not make retained old lessons a
            # new observation. Legacy rows use their last complete read time.
            observed_at = _observed_at(lesson.get("observed_at") or card.get("lessons_full_refreshed_at",
                card.get("lessons_refreshed_at", card.get("refreshed_at")))) or datetime.min.replace(tzinfo=UTC)
            key = (customer, branch, str(lesson["id"]))
            observations.setdefault(key, []).append((observed_at, lesson))
    result = []
    for (customer, branch, identifier), values in sorted(observations.items()):
        latest = max(at for at, _ in values)
        rows = [lesson for at, lesson in values if at == latest]
        fields = ("date", "time", "status", "subject_id", "lesson_type_id", "group_ids", "subscription_id", "customer_verified", "attended")
        if any(any(row.get(f) != rows[0].get(f) for f in fields) for row in rows[1:]):
            continue
        lesson = rows[0]
        day = source_date(lesson["date"])
        absent = any(identifier not in identifiers and any(first <= day <= last and at > latest for first, last, at in windows)
                     for identifiers, windows in branches.get((customer, branch), []))
        if not absent and lesson.get("status") in (1, 3):
            result.append(lesson)
    return result


def _member_at(group, customer, day):
    if "memberships" in group:
        return any(str(m["customer_id"]) == customer and (not m.get("begin") or m["begin"] <= day)
            and (not m.get("end") or m["end"] >= day) for m in group["memberships"])
    return customer in group.get("customer_ids", [])


def _subscription_usage(subscriptions, cards, groups):
    """ctt_id proves a purchase; subject/type/date prove an unambiguous fallback."""
    for lesson in current_lessons(cards):
        branch, customer = str(lesson["branch_id"]), str(lesson["customer_id"])
        member = any(str(g["branch_id"]) == branch and int(g["id"]) in lesson.get("group_ids", [])
            and _member_at(g, customer, lesson["date"]) for g in groups)
        if not lesson.get("customer_verified") and not member:
            continue
        candidates = []
        explicit = lesson.get("subscription_id")
        for item in subscriptions:
            if item["needs_review"]:
                continue
            if str(item["customer_id"]) != str(lesson["customer_id"]):
                continue
            if explicit is not None and str(item["id"]) != str(explicit):
                continue
            subject = lesson.get("subject_id")
            subjects, types = item.get("subject_ids", []), item.get("lesson_type_ids", [])
            if subjects and subject not in subjects:
                continue
            if explicit is None and (not subjects or subject not in subjects):
                continue
            if types and lesson.get("lesson_type_id") not in types:
                continue
            if item.get("tariff_branch_ids") and int(branch) not in item["tariff_branch_ids"]:
                continue
            if (item.get("begin") and lesson["date"] < item["begin"]) or (item.get("end") and lesson["date"] > item["end"]):
                continue
            candidates.append(item)
        if len(candidates) == 1:
            candidates[0]["usage"].append({"branch_id":branch,"lesson_id":str(lesson["id"]),
                "subject_id":lesson.get("subject_id"),"lesson_type_id":lesson.get("lesson_type_id"),
                "group_ids":[str(g) for g in lesson.get("group_ids", [])],"date":lesson["date"],
                "method":"ctt_id" if explicit is not None else "subject"})


def customer_subscriptions(cards, *, groups=None):
    """Present one CustomerTariff ID per Customer within the scoped connection.

    A branch is an observation context, not another purchased subscription.
    Keep raw branch caches and group links intact; never merge by tariff/name.
    """
    observed = {}
    customer_cards = {}
    for card in cards:
        customer = positive_id(card["id"])
        if "subscriptions" in card:
            customer_cards.setdefault(customer, []).append(card)
        for item in card.get("subscriptions", []):
            key = (customer, positive_id(item["id"]))
            observed.setdefault(key, []).append((card, item))
    result = []
    for (customer, subscription), observations in sorted(observed.items()):
        snapshots = customer_cards[customer]
        latest = max(card.get("refreshed_at") or "" for card in snapshots)
        current_cards = [card for card in snapshots if (card.get("refreshed_at") or "") == latest]
        inventories = [{positive_id(s["id"]) for s in card["subscriptions"]} for card in current_cards]
        if not any(subscription in ids for ids in inventories):
            continue
        current = [item for card, item in observations if (card.get("refreshed_at") or "") == latest]
        item = dict(current[0], customer_id=str(customer), id=subscription)
        # Equally recent contradictory facts are not a second subscription or a
        # trustworthy amount. Keep the identity visible and ask for a refresh.
        item["needs_review"] = any(ids != inventories[0] for ids in inventories[1:]) or any(any(other.get(field) != item.get(field)
            for field in ("tariff_id", "balance", "begin", "end", "archived", "subject_ids", "lesson_type_ids")) for other in current)
        if item["needs_review"]:
            item.update(balance=None, begin=None, end=None, archived=all(other.get("archived", False) for other in current))
        branches = {str(card["branch_id"]): card.get("branch_name") or f"Филиал {card['branch_id']}" for card, _ in observations}
        item["observed_branches"] = [{"id": branch, "name": branches[branch]} for branch in sorted(branches)]
        item["usage"] = []
        result.append(item)
    _subscription_usage(result, cards, groups or [])
    names = {str(card["branch_id"]):card.get("branch_name") or f"Филиал {card['branch_id']}" for card in cards}
    for item in result:
        branches = sorted({use["branch_id"] for use in item["usage"]})
        item["branches"] = [{"id":branch,"name":names[branch]} for branch in branches]
        item["branch_name"] = " · ".join(branch["name"] for branch in item["branches"]) or "Филиал по занятиям не подтверждён"
        item["relation_status"] = "confirmed" if branches else "pending" if any(mapping_pending(card) for card in cards) else "unconfirmed"
    return result


def group_facts(groups, cards, *, today=None, subscriptions=None):
    """A lesson links to a group by branch and ID; ctt_id is the tariff link."""
    today = today or date.today()
    subscriptions = customer_subscriptions(cards, groups=groups) if subscriptions is None else subscriptions
    lessons = current_lessons(cards)
    for group in groups:
        matched, linked = [], {}
        for lesson in lessons:
            customer = str(lesson["customer_id"])
            if (str(lesson["branch_id"]) == str(group["branch_id"]) and int(group["id"]) in lesson.get("group_ids", [])
                and (not group.get("customer_ids") or customer in group["customer_ids"])
                and (lesson.get("customer_verified") or _member_at(group, customer, lesson["date"]))):
                matched.append(lesson)
        for item in subscriptions:
            if (not group.get("customer_ids") or str(item["customer_id"]) in group["customer_ids"]) and any(
                use["branch_id"] == str(group["branch_id"]) and str(group["id"]) in use["group_ids"] for use in item["usage"]):
                linked[(item["customer_id"],item["id"])] = item
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
