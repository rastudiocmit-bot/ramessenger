"""Reconcile CRM customer identity and journal mirrors without deleting source rows."""

import json
from collections import defaultdict
from datetime import UTC, datetime
from uuid import UUID, uuid5

from sqlalchemy import text

from center_messenger.chat_store import changed, refresh_unread
from center_messenger.crm_api import DirectoryReader
from center_messenger.alfa_history import RECHECK_INTERVAL, history_coverage

REPAIR_VERSION = 1
REPAIR_BATCH = 20


def customer_directory(db, workspace, snapshot, identities):
    if not snapshot or not identities:
        return {}
    rows = db.execute(text(
        "SELECT a.branch_id,a.external_id,a.contact_id,a.phones->>0 AS primary_phone,a.status,b.available FROM alfa_customers a "
        "LEFT JOIN alfa_branches b ON b.workspace_id=a.workspace_id AND b.connection_id=:crm "
        "AND b.external_id=a.branch_id WHERE a.workspace_id=:w AND a.run_id=:r "
        "AND a.external_id=ANY(CAST(:ids AS bigint[])) ORDER BY a.external_id,a.branch_id"
    ), {"w": workspace, "r": snapshot["run"], "crm": snapshot["connection_id"],
        "ids": sorted(set(identities))}).mappings()
    directory = defaultdict(list)
    for row in rows:
        directory[row["external_id"]].append(row)
    return directory


def resolve_customer(raw, directory):
    customer = dict(raw)
    matches = [row for row in directory.get(customer["customer_id"], []) if row["contact_id"]]
    contacts = {row["contact_id"] for row in matches}
    exact = next((row for row in matches if row["branch_id"] == customer["branch_id"]), None)
    customer["observed_branch_id"] = customer["branch_id"]
    customer.pop("contact_id", None)
    if len(contacts) == 1:
        # The journal URL is a viewing context. The published CRM directory
        # determines the customer's real membership, even from another route.
        chosen = min(matches, key=lambda row: (row.get("available") is False, row["branch_id"]))
        customer.update(contact_id=chosen["contact_id"], branch_id=chosen["branch_id"],
                        primary_phone=chosen["primary_phone"], status=chosen["status"],
                        global_id_verified=True)
    elif exact:
        # Reused IDs in different branches remain separate when the directory
        # does not prove a single contact for this customer ID.
        customer.update(contact_id=exact["contact_id"], primary_phone=exact["primary_phone"],
                        status=exact["status"])
    return customer


def reconcile_messages(service, db, workspace, channel, conversation):
    from center_messenger.chat_queue import materialize_legacy_handled
    from center_messenger import native_relink
    materialize_legacy_handled(db, workspace, conversation)
    # crm_order has always been the numeric CRM row ID in the Alfa projector.
    # Recover that exact identity for rows imported before crm_record_id existed.
    restored = db.execute(text(
        "UPDATE messages SET payload=jsonb_set(payload,'{crm_record_id}',"
        "to_jsonb(((payload->>'crm_order')::numeric)::text))||jsonb_build_object('crm_identity_restored',true),version=version+1 "
        "WHERE workspace_id=:w AND conversation_id=:c AND payload->>'crm_record_id' IS NULL "
        "AND payload->>'source' IN ('alfa_max','center_messenger_alfa_max') "
        "AND payload->>'crm_order' ~ '^[0-9]{1,19}$' AND payload->>'branch_id' ~ '^[0-9]+$' "
        "AND payload->>'superseded_by' IS NULL RETURNING id"
    ), {"w": workspace, "c": conversation}).scalars().all()
    for message in restored:
        changed(db, workspace, conversation, message)
    records = list(db.execute(text(
        "SELECT * FROM messages WHERE workspace_id=:w AND conversation_id=:c "
        "AND coalesce(payload->>'deleted','false')<>'true' "
        "AND (payload->>'superseded_by' IS NULL OR payload->>'crm_mirror'='true') "
        "ORDER BY occurred_at,id"
    ), {"w": workspace, "c": conversation}).mappings())
    exact = defaultdict(list)
    for row in records:
        payload = row["payload"]
        if payload.get("crm_mirror") and not payload.get("crm_global_verified"):
            payload = {key: value for key, value in payload.items() if key not in {"crm_mirror", "superseded_by"}}
            db.execute(text("UPDATE messages SET payload=CAST(:p AS jsonb),version=version+1 "
                            "WHERE workspace_id=:w AND id=:id"),
                       {"w": workspace, "id": row["id"], "p": json.dumps(payload, ensure_ascii=False)})
            changed(db, workspace, conversation, row["id"])
            row = dict(row) | {"payload": payload}
        if payload.get("crm_record_id") is not None and payload.get("branch_id") is not None:
            exact[(row["direction"], str(payload["branch_id"]), str(payload["crm_record_id"]))].append(row)
    hidden = set()
    for rows in exact.values():
        if len(rows) < 2 or sum(row["send_command_id"] is not None for row in rows) > 1:
            continue
        canonical = min(rows, key=lambda row: (row["send_command_id"] is None,
                                              row["payload"].get("crm_identity_restored", False),
                                              row["payload"].get("crm_mirror", False), str(row["id"])))
        for row in rows:
            if row["id"] != canonical["id"]:
                service._supersede(db, workspace, row["id"], canonical["id"])
                changed(db, workspace, conversation, row["id"])
                hidden.add(row["id"])
    groups = defaultdict(lambda: defaultdict(list))
    relinked_customers = native_relink.mirror_customers(db, workspace, channel,
        DirectoryReader._published(db, workspace), conversation)
    for row in records:
        payload = row["payload"]
        if (row["id"] in hidden or row["direction"] != "in" or row["send_command_id"]
                or not payload.get("crm_global_verified") or not str(payload.get("branch_id", "")).isdigit()):
            continue
        customers = tuple(payload.get("crm_customer_ids") or [payload.get("customer_id")])
        if not customers or None in customers or payload.get("crm_record_id") is None:
            continue
        # Only incoming branch fan-out for the same verified CRM customer(s).
        # Preserve multiplicity within each journal, including repeated text in
        # the same minute. Different customer IDs and outgoing sends stay apart.
        key = (native_relink.mirror_key(payload, relinked_customers), row["occurred_at"], row["text"], row["actor_type"],
               json.dumps(payload.get("attachments", []), sort_keys=True, ensure_ascii=False), bool(payload.get("failed")))
        groups[key][str(payload["branch_id"])].append(row)
    mirrors_updated = 0
    for branches in groups.values():
        for rows in branches.values():
            rows.sort(key=lambda row: (int(row["payload"]["crm_order"])
                                      if str(row["payload"].get("crm_order", "")).isdigit() else 0, str(row["id"])))
        for rank in range(max(map(len, branches.values()))):
            copies = [rows[rank] for rows in branches.values() if rank < len(rows)]
            keeper = min(copies, key=lambda row: (
                str(row["payload"]["branch_id"]) != str(row["payload"].get("crm_primary_branch")),
                int(row["payload"]["branch_id"]), str(row["id"])))
            receipt = next((row["payload"]["handled_receipt"] for row in copies if row["payload"].get("handled_receipt")), None)
            for row in copies:
                payload = dict(row["payload"])
                if row["id"] == keeper["id"] and receipt:
                    payload["handled_receipt"] = receipt
                if row["id"] == keeper["id"]:
                    payload.pop("crm_mirror", False)
                    payload.pop("superseded_by", None)
                else:
                    payload.update(crm_mirror=True, superseded_by=str(keeper["id"]))
                if payload != row["payload"]:
                    mirrors_updated += int(row["id"] != keeper["id"])
                    # Keep each journal row, original provider key and source
                    # reference for coverage and future reconciliation.
                    db.execute(text("UPDATE messages SET payload=CAST(:p AS jsonb),version=version+1 "
                                    "WHERE workspace_id=:w AND id=:id"),
                               {"w": workspace, "id": row["id"], "p": json.dumps(payload, ensure_ascii=False)})
                    changed(db, workspace, conversation, row["id"])
    for _ in range(3):
        moved = db.execute(text(
            "UPDATE conversations c SET handled_inbound_id=n.id FROM messages m JOIN messages n "
            "ON n.workspace_id=m.workspace_id AND n.id::text=m.payload->>'superseded_by' "
            "WHERE c.workspace_id=:w AND c.id=:c AND m.workspace_id=c.workspace_id "
            "AND m.id=c.handled_inbound_id AND n.conversation_id=c.id AND n.direction='in'"
        ), {"w": workspace, "c": conversation}).rowcount
        if not moved:
            break
    refresh_unread(db, workspace, conversation)
    return {"checked_messages": len(records), "restored_ids": len(restored),
            "hidden_duplicates": len(hidden), "mirrors_updated": mirrors_updated}


def repair_batch(service, db, workspace, channel, cursor, *, on_progress=None, only_contacts=None):
    snapshot = DirectoryReader._published(db, workspace)
    if not snapshot:
        return False
    shared_changed = False
    if only_contacts is None:
        from center_messenger.alfa_shared_parent import repair_batch as repair_shared
        shared_changed = repair_shared(service, db, workspace, channel, snapshot, cursor)
    previous = cursor.get("client_repair", {}) if only_contacts is None else {}
    if only_contacts is None and cursor.get("sync_policy") == 1 and previous.get("version") == REPAIR_VERSION and previous.get("complete"):
        return shared_changed
    run = str(snapshot["run"])
    # A refreshed CRM snapshot gets a new run UUID even when all identities
    # are unchanged. Resume by identity evidence so refreshes cannot starve
    # a long repair by resetting its checkpoint every thirty minutes.
    mapping = db.execute(text(
        "SELECT md5(coalesce(string_agg(jsonb_build_array(a.branch_id,a.external_id,a.contact_id,"
        "b.available)::text,',' ORDER BY a.branch_id,a.external_id),'')) AS fingerprint,"
        "count(DISTINCT a.contact_id) AS total FROM alfa_customers a LEFT JOIN alfa_branches b "
        "ON b.workspace_id=a.workspace_id AND b.connection_id=:crm AND b.external_id=a.branch_id "
        "WHERE a.workspace_id=:w AND a.run_id=:r"
    ), {"w": workspace, "r": snapshot["run"], "crm": snapshot["connection_id"]}).mappings().one()
    same_mapping = previous.get("version") == REPAIR_VERSION and (
        previous.get("mapping") == mapping["fingerprint"]
        or previous.get("run") == run and not previous.get("mapping"))
    if same_mapping:
        if previous.get("complete"):
            if previous.get("run") != run or previous.get("mapping") != mapping["fingerprint"] or "total" not in previous:
                cursor["client_repair"] = previous | {"run": run, "mapping": mapping["fingerprint"], "total": mapping["total"]}
                return True
            return shared_changed
        after, processed = previous.get("after"), previous.get("processed", 0)
    else:
        after, processed = None, 0
        exists = db.execute(text("SELECT 1 FROM messages m JOIN conversations c "
                                 "ON c.workspace_id=m.workspace_id AND c.id=m.conversation_id "
                                 "WHERE c.workspace_id=:w AND c.connection_id=:c LIMIT 1"),
                            {"w": workspace, "c": channel}).scalar()
        if not exists:
            cursor["client_repair"] = {"version": REPAIR_VERSION, "run": run,
                                       "processed": 0, "total": 0, "after": None, "complete": True,
                                       "mapping": mapping["fingerprint"], "updated_at": datetime.now(UTC).isoformat()}
            return True
    contacts = db.execute(text(
        "SELECT DISTINCT contact_id FROM alfa_customers WHERE workspace_id=:w AND run_id=:r "
        "AND contact_id IS NOT NULL " + ("AND contact_id=ANY(CAST(:only AS uuid[])) " if only_contacts is not None else "") +
        "AND (CAST(:after AS uuid) IS NULL OR contact_id>CAST(:after AS uuid)) "
        "ORDER BY contact_id LIMIT :limit"
    ), {"w": workspace, "r": snapshot["run"], "after": after, "limit": REPAIR_BATCH + 1, "only": only_contacts}).scalars().all()
    more = len(contacts) > REPAIR_BATCH
    contacts = contacts[:REPAIR_BATCH]
    if contacts:
        clients = defaultdict(list)
        for row in db.execute(text(
            "SELECT a.contact_id,a.external_id,a.name,a.branch_id,b.name AS branch_name,d.display_name "
            "FROM alfa_customers a LEFT JOIN alfa_branches b ON b.workspace_id=a.workspace_id "
            "AND b.connection_id=:crm AND b.external_id=a.branch_id LEFT JOIN alfa_directory d "
            "ON d.workspace_id=a.workspace_id AND d.run_id=a.run_id AND d.contact_id=a.contact_id "
            "WHERE a.workspace_id=:w AND a.run_id=:r AND a.contact_id=ANY(CAST(:ids AS uuid[])) "
            "ORDER BY a.branch_id,a.external_id"
        ), {"w": workspace, "r": snapshot["run"], "crm": snapshot["connection_id"], "ids": contacts}).mappings():
            clients[row["contact_id"]].append(dict(row))
        ids = db.execute(text("SELECT DISTINCT external_id FROM alfa_customers WHERE workspace_id=:w "
                              "AND run_id=:r AND contact_id=ANY(CAST(:ids AS uuid[]))"),
                         {"w": workspace, "r": snapshot["run"], "ids": contacts}).scalars().all()
        directory = customer_directory(db, workspace, snapshot, ids)
        aliases = db.execute(text(
            "SELECT id,external_chat_id FROM conversations WHERE workspace_id=:w AND connection_id=:c "
            "AND contact_id IS NULL AND NOT crm_link_manual AND external_chat_id ~ '^alfa-customer:[0-9]+:[0-9]+$' "
            "AND split_part(external_chat_id,':',3)::bigint=ANY(CAST(:ids AS bigint[]))"
        ), {"w": workspace, "c": channel, "ids": ids}).mappings().all()
        for alias in aliases:
            _, branch, customer = alias["external_chat_id"].split(":")
            resolved = resolve_customer({"branch_id": int(branch), "customer_id": int(customer)}, directory)
            if resolved.get("contact_id") in contacts:
                db.execute(text("UPDATE conversations SET contact_id=:contact WHERE workspace_id=:w AND id=:id"),
                           {"w": workspace, "id": alias["id"], "contact": resolved["contact_id"]})
        active_contacts = set(db.execute(text(
            "SELECT DISTINCT c.contact_id FROM conversations c WHERE c.workspace_id=:w AND c.connection_id=:channel "
            "AND c.contact_id=ANY(CAST(:contacts AS uuid[])) AND (EXISTS(SELECT 1 FROM messages m "
            "WHERE m.workspace_id=c.workspace_id AND m.conversation_id=c.id) OR EXISTS(SELECT 1 FROM send_commands s "
            "WHERE s.workspace_id=c.workspace_id AND s.conversation_id=c.id))"
        ), {"w": workspace, "channel": channel, "contacts": contacts}).scalars())
        for index, contact in enumerate(contacts):
            cards = clients[contact]
            client = {"name": next((c["display_name"] for c in cards if c["display_name"]),
                                   cards[0]["name"] if cards else "Клиент без имени"),
                      "contact_id": str(contact), "cards": [
                          {"customer_id": str(c["external_id"]), "name": c["name"],
                           "branch_id": str(c["branch_id"]), "branch_name": c["branch_name"]} for c in cards]}
            detail = {"phase": "repair", "client": client, "position": processed + index + 1,
                      "position_total": len(contacts) if only_contacts is not None else mapping["total"], "record_id": None, "operation": "check_client"}
            if on_progress:
                on_progress(detail)
            if contact not in active_contacts:
                if on_progress:
                    on_progress(detail | {"operation": "client_checked", "event": {
                        "kind": "repair", "client": client, "position": detail["position"],
                        "result": "no_history", "checked_messages": 0}})
                continue
            external = f"alfa-contact:{contact}"
            canonical = uuid5(UUID(str(channel)), external)
            db.execute(text("INSERT INTO conversations(workspace_id,id,connection_id,contact_id,external_chat_id) "
                            "VALUES (:w,:id,:channel,:contact,:external) ON CONFLICT "
                            "(workspace_id,connection_id,external_chat_id,external_topic_id) DO NOTHING"),
                       {"w": workspace, "id": canonical, "channel": channel, "contact": contact, "external": external})
            canonical = db.execute(text("SELECT id FROM conversations WHERE workspace_id=:w "
                                        "AND connection_id=:channel AND external_chat_id=:external AND external_topic_id=''"),
                                   {"w": workspace, "channel": channel, "external": external}).scalar_one()
            service._merge_contact_conversations(db, workspace, channel, contact, canonical)
            for identity, memberships in directory.items():
                verified = [row for row in memberships if row["contact_id"]]
                if {row["contact_id"] for row in verified} != {contact}:
                    db.execute(text("UPDATE messages SET payload=payload||jsonb_build_object('crm_global_verified',false),"
                                    "version=version+1 WHERE workspace_id=:w AND conversation_id=:c "
                                    "AND payload->>'customer_id'=:customer AND payload->>'crm_global_verified'='true'"),
                               {"w": workspace, "c": canonical, "customer": str(identity)})
                    continue
                primary = min(verified, key=lambda row: (row.get("available") is False, row["branch_id"]))["branch_id"]
                db.execute(text("UPDATE messages SET payload=jsonb_set(payload||CAST(:p AS jsonb),'{crm_customer_ids}',"
                                "coalesce(payload->'crm_customer_ids',CAST(:ids AS jsonb))),version=version+1 "
                                "WHERE workspace_id=:w AND conversation_id=:c AND payload->>'customer_id'=:customer "
                                "AND (NOT (payload @> CAST(:p AS jsonb)) OR payload->'crm_customer_ids' IS NULL)"),
                           {"w": workspace, "c": canonical, "customer": str(identity), "ids": json.dumps([identity]), "p": json.dumps({
                               "customer_branch_id": primary, "crm_global_verified": True,
                               "crm_primary_branch": primary})})
            summary = reconcile_messages(service, db, workspace, channel, canonical)
            if on_progress:
                on_progress(detail | {"operation": "client_checked", "event": {
                    "kind": "repair", "client": client, "position": detail["position"],
                    "result": "checked", **summary}})
    cursor["client_repair"] = {"version": REPAIR_VERSION, "run": run, "processed": processed + len(contacts),
                               "after": str(contacts[-1]) if contacts else after, "complete": not more,
                               "total": mapping["total"], "mapping": mapping["fingerprint"],
                               "updated_at": datetime.now(UTC).isoformat()}
    if not more:
        branches = db.execute(text("SELECT external_id FROM alfa_branches WHERE workspace_id=:w "
                                   "AND connection_id=:crm AND available ORDER BY external_id"),
                              {"w": workspace, "crm": snapshot["connection_id"]}).scalars().all()
        coverage = history_coverage(db, workspace, channel, cursor, branches)
        for stream in coverage["streams"]:
            key = f"{stream['branch_id']}:{stream['direction']}"
            finished, started = cursor.get(f"finished:{key}"), cursor.get(f"started:{key}")
            replay = bool(finished and started and
                          datetime.fromisoformat(finished) <= datetime.fromisoformat(started)
                          < datetime.fromisoformat(finished) + RECHECK_INTERVAL)
            gap = cursor.get(f"gap:{key}") or replay
            if gap and finished and stream["expected_rows"] is not None and not stream["missing_rows"] \
                    and not stream["catchup_page"] and stream["error_code"] in {None, "history_gap", "history_incomplete"}:
                cursor[key] = 0
                cursor[f"complete:{key}"] = True
                for prefix in ("gap", "paused", "pause_error", "error", "retries"):
                    cursor.pop(f"{prefix}:{key}", None)
    return True
