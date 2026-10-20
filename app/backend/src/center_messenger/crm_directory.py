"""Pure, conservative identity projection; source cards are never discarded."""

import re
import unicodedata
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID, uuid5

from center_messenger.alfa_client import AlfaError


def normalized_name(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().replace("ё", "е").split())


def positive_id(value):
    if isinstance(value, bool) or not re.fullmatch(r"[1-9][0-9]{0,18}", str(value)):
        raise AlfaError("invalid_response")
    result = int(value)
    if result > 9223372036854775807:
        raise AlfaError("invalid_response")
    return result


def source_date(value):
    if value in (None, "", "0000-00-00", "00.00.0000"):
        return None
    for pattern in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(str(value), pattern).date()
        except ValueError:
            continue
    raise AlfaError("invalid_response")


def phone_number(value):
    if not isinstance(value, str) or not re.fullmatch(r"\+?[0-9\s().-]{7,40}", value.strip()):
        return None
    digits = re.sub(r"\D", "", value)
    if len(digits) == 10:
        digits = "7" + digits
    elif len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if not 8 <= len(digits) <= 15 or digits.startswith("0"):
        return None
    if not value.strip().startswith("+") and not (len(digits) == 11 and digits.startswith("7")):
        return None
    return "+" + digits


def names_list(value, normalizer):
    if value in (None, ""):
        return []
    if isinstance(value, str):
        value = re.split(r"[;,\n]", value)
    if not isinstance(value, list) or len(value) > 50:
        raise AlfaError("invalid_response")
    result = []
    for entry in value:
        normalized = normalizer(entry) if isinstance(entry, str) else None
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def email_address(value):
    value = value.strip().casefold()
    return value if len(value) <= 254 and re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value) else None


def source_name(value, *, optional=False):
    if optional and value in (None, ""):
        return ""
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        raise AlfaError("invalid_response")
    return " ".join(value.split())


def source_balance(value):
    """Money or lesson units from CRM; missing is distinct from a genuine zero."""
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise AlfaError("invalid_response")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise AlfaError("invalid_response") from None
    if not result.is_finite() or abs(result) > Decimal("999999999999.99"):
        raise AlfaError("invalid_response")
    return result


def source_lessons(value):
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not re.fullmatch(r"-?[0-9]{1,9}", str(value)):
        raise AlfaError("invalid_response")
    return int(value)


def customer_row(item, *, archive):
    if not isinstance(item, dict) or str(item.get("is_study")) not in {"0", "1", "True", "False"}:
        raise AlfaError("invalid_response")
    study = item.get("study_status_id")
    leads = item.get("lead_status_ids") or []
    if not isinstance(leads, list) or len(leads) > 100:
        raise AlfaError("invalid_response")
    born = source_date(item.get("dob"))
    if born and not date(1900, 1, 1) <= born <= date.today():
        born = None
    return {
        "external_id": positive_id(item.get("id")),
        "name": source_name(item.get("name")),
        "legal_name": source_name(item.get("legal_name"), optional=True),
        "legal_type": 2 if str(item.get("legal_type")) == "2" else 1,
        "dob": born,
        "balance": source_balance(item.get("balance")),
        "paid_lesson_count": source_lessons(item.get("paid_lesson_count")),
        "phones": names_list(item.get("phone"), phone_number),
        "emails": names_list(item.get("email"), email_address),
        "status": "archive"
        if archive
        else "client"
        if str(item["is_study"]) in {"1", "True"}
        else "lead",
        "study_status_id": positive_id(study) if study not in (None, "", 0, "0") else None,
        "lead_status_ids": [positive_id(value) for value in leads],
    }


def page_rows(data, page):
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise AlfaError("invalid_response")
    items, total = data["items"], data.get("total")
    if isinstance(total, bool) or not re.fullmatch(r"[0-9]+", str(total)):
        raise AlfaError("invalid_response")
    total = int(total)
    if total > 100000 or page >= 1000:
        raise AlfaError("import_limit")
    if len(items) > 500 or ("page" in data and str(data["page"]) != str(page)):
        raise AlfaError("invalid_response")
    if "count" in data and str(data["count"]) != str(len(items)):
        raise AlfaError("invalid_response")
    ids = [positive_id(item.get("id")) if isinstance(item, dict) else None for item in items]
    if None in ids or len(set(ids)) != len(ids):
        raise AlfaError("invalid_response")
    return items, total


def project_directory(connection: UUID, rows, previous=()):
    """One source customer across branches; separate children sharing a representative.

    Only a primary communication address is used, never chains of secondary numbers.
    Conflicting payer names sharing an address remain separate and require review.
    Cross-ID student matches require the same contact, full name AND valid birthday.
    """
    sources, address_names = defaultdict(list), defaultdict(set)
    for row in rows:
        sources[row["external_id"]].append(row)
    representatives = {
        key: min(values, key=lambda r: r["branch_id"]) for key, values in sources.items()
    }
    addresses = {}
    for key, row in representatives.items():
        address = (
            ("phone:" + row["phones"][0])
            if row["phones"]
            else ("email:" + row["emails"][0] if row["emails"] else "card:" + str(key))
        )
        addresses[key] = address
        address_names[address].update(
            normalized_name(r["legal_name"]) for r in sources[key] if r["legal_name"]
        )
    groups = defaultdict(list)
    for key, row in representatives.items():
        address, name = addresses[key], normalized_name(row["legal_name"])
        conflict = len(address_names[address]) > 1
        identity = (
            address + (("|payer:" + name) if name else f"|card:{key}") if conflict else address
        )
        groups[identity].append(key)
    previous_ids = {r["external_id"]: r["student_id"] for r in previous if r["student_id"]}
    previous_shapes = defaultdict(set)
    previous_owners = {}
    for key, row in sorted(representatives.items()):
        if key in previous_ids:
            old = previous_ids[key]
            shape = (normalized_name(row["name"]), row["dob"])
            previous_shapes[old].add(shape)
            previous_owners.setdefault(old, shape)
    changed_aliases = {old for old, shapes in previous_shapes.items() if len(shapes) > 1}
    contacts, students, bindings = [], {}, []
    for identity, keys in sorted(groups.items()):
        contact = uuid5(connection, "contact:" + identity)
        cards = [r for key in keys for r in sources[key]]
        legal_names = sorted(
            {r["legal_name"] for r in cards if r["legal_name"]}, key=normalized_name
        )
        phones = sorted({p for r in cards for p in r["phones"]})
        emails = sorted({p for r in cards for p in r["emails"]})
        review = any(
            len(address_names[addresses[key]]) > 1 or previous_ids.get(key) in changed_aliases
            for key in keys
        )
        label = (
            legal_names[0]
            if legal_names
            else phones[0]
            if phones
            else emails[0]
            if emails
            else representatives[keys[0]]["name"]
        )
        children = defaultdict(list)
        for key in keys:
            values, row = sources[key], representatives[key]
            signatures = {(normalized_name(r["name"]), r["dob"]) for r in values}
            if len(signatures) != 1:
                review = True
            signature = (
                (normalized_name(row["name"]), row["dob"])
                if row["dob"] and len(signatures) == 1
                else ("card", key)
            )
            children[signature].append(key)
        for child_keys in children.values():
            row = representatives[min(child_keys)]
            shape = (normalized_name(row["name"]), row["dob"])
            known = sorted(
                {
                    previous_ids[k]
                    for k in child_keys
                    if k in previous_ids and previous_owners[previous_ids[k]] == shape
                },
                key=str,
            )
            student = UUID(
                str(known[0] if known else uuid5(connection, "student:" + str(min(child_keys))))
            )
            if not known and student in previous_owners and previous_owners[student] != shape:
                student = uuid5(
                    connection,
                    f"student:{min(child_keys)}:identity:{shape[0]}:dob:{shape[1]}",
                )
            students[student] = {"id": student, "name": row["name"]}
            for key in child_keys:
                for source in sources[key]:
                    bindings.append(
                        {
                            "branch_id": source["branch_id"],
                            "external_id": key,
                            "contact_id": contact,
                            "student_id": student,
                            "needs_review": review,
                        }
                    )
        search = normalized_name(
            " ".join(
                [
                    label,
                    *legal_names,
                    *(p.lstrip("+") for p in phones),
                    *emails,
                    *(" ".join(re.findall(r"[^\W_]+", email)) for email in emails),
                    *(r["name"] for r in cards),
                ]
            )
        )
        contacts.append(
            {
                "id": contact,
                "name": label,
                "phones": phones,
                "emails": emails,
                "needs_review": review,
                "search_text": search,
            }
        )
    return contacts, list(students.values()), bindings
