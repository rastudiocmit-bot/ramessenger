"""Cached CRM directory, restricted to admins until branch ACL is implemented."""

import json
import re
from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query, Request
from sqlalchemy import text

from center_messenger.accounts_api import identity, service
from center_messenger.alfa_client import AlfaError
from center_messenger.alfa_family import fetch_family_card, group_facts, customer_balance, customer_paid_lessons, customer_subscriptions, mapping_pending
from center_messenger.alfa_sync import SyncOptions
from center_messenger.auth_security import AuthError
from center_messenger.crm_directory import normalized_name, phone_number
from center_messenger.family_cache import enqueue, cache_status
from center_messenger.group_links import TOPIC as GROUPS_TOPIC
from center_messenger import card_preference

router = APIRouter(prefix="/api/workspaces/{workspace_id}/directory")
STATUS = {3: "client", 2: "lead", 1: "archive"}
WEIGHT = "CASE c.status WHEN 'client' THEN 3 WHEN 'lead' THEN 2 ELSE 1 END"
MAX_CONNECTED = (
    "EXISTS (SELECT 1 FROM conversations mxconv "
    "JOIN connections mxchannel ON mxchannel.workspace_id=mxconv.workspace_id "
    "AND mxchannel.id=mxconv.connection_id "
    "JOIN alfa_max_settings mxsettings ON mxsettings.workspace_id=mxconv.workspace_id "
    "AND mxsettings.connection_id=mxconv.connection_id "
    "WHERE mxconv.workspace_id=d.workspace_id AND mxconv.contact_id=d.contact_id "
    "AND mxconv.last_inbound_at IS NOT NULL "
    "AND mxchannel.kind='alfa_max_browser' "
    "AND mxsettings.crm_connection_id=:conn)"
)
MAX_BOT_CONNECTED = (
    "EXISTS (SELECT 1 FROM conversations botconv "
    "JOIN connections botchannel ON botchannel.workspace_id=botconv.workspace_id "
    "AND botchannel.id=botconv.connection_id "
    "JOIN max_settings botsettings ON botsettings.workspace_id=botconv.workspace_id "
    "AND botsettings.connection_id=botconv.connection_id "
    "WHERE botconv.workspace_id=d.workspace_id AND botconv.contact_id=d.contact_id "
    "AND botconv.peer_user_id IS NOT NULL AND botchannel.kind='max_bot' "
    "AND botchannel.status='active' AND botsettings.enabled "
    "AND botsettings.subscription_confirmed)"
)


def search_query(value):
    value = (phone_number(value) or normalized_name(value)).lstrip("+")
    tokens = re.findall(r"[^\W_]+", value, flags=re.UNICODE)[:8]
    return " & ".join(token + ":*" for token in tokens)


class DirectoryReader:
    def __init__(self, connections):
        self.connections = connections
        from center_messenger.chat_card_view import CardViews
        self.card_views = CardViews()

    @staticmethod
    def _published(db, workspace):
        return (
            db.execute(
                text(
                    "SELECT c.published_id AS run,c.connection_id,a.origin,a.version,r.finished_at,w.timezone,"
                    "CAST(now() AT TIME ZONE w.timezone AS date) AS today "
                    "FROM alfa_sync_control c JOIN alfa_settings a ON "
                    "a.workspace_id=c.workspace_id "
                    "AND a.connection_id=c.connection_id JOIN alfa_sync_runs r "
                    "ON r.workspace_id=c.workspace_id AND r.id=c.published_id "
                    "AND r.connection_id=a.connection_id JOIN workspaces w ON w.id=c.workspace_id "
                    "WHERE c.workspace_id=:w AND a.secret_ciphertext IS NOT NULL AND "
                    "r.state='succeeded'"
                ),
                {"w": workspace},
            )
            .mappings()
            .one_or_none()
        )

    @staticmethod
    def _filter(branch, status):
        return (" AND c.branch_id=:branch" if branch else "") + (
            " AND c.status=:status" if status else ""
        )

    @staticmethod
    def _branches(db, workspace):
        rows = db.execute(
            text(
                "SELECT b.external_id,b.name FROM alfa_branches b JOIN alfa_settings a "
                "ON a.workspace_id=b.workspace_id AND a.connection_id=b.connection_id "
                "WHERE b.workspace_id=:w AND b.available AND a.secret_ciphertext IS NOT NULL "
                "ORDER BY b.name,b.external_id"
            ),
            {"w": workspace},
        ).mappings()
        return [{"id": str(r["external_id"]), "name": r["name"]} for r in rows]

    def listing(
        self,
        actor,
        workspace,
        *,
        query="",
        branch=None,
        status=None,
        max_status="all",
        max_bot_status="connected_first",
        page=0,
        chat_only=False,
    ):
        transaction = self.connections.chat_transaction if chat_only else self.connections._transaction
        with transaction(actor, workspace, snapshot=True) as db:
            if chat_only and self.connections.accounts._workspace(db, actor, workspace)["role"] == "operator":
                raise AuthError(403, "Откройте карточку семьи из её диалога.")
            snapshot = self._published(db, workspace)
            result = {
                "contacts": [],
                "total": 0,
                "page": page,
                "has_more": False,
                "branches": self._branches(db, workspace),
                "snapshot": None,
            }
            if snapshot is None:
                return result
            params = {
                "w": workspace,
                "r": snapshot["run"],
                "conn": snapshot["connection_id"],
                "branch": branch,
                "status": status,
                "search": search_query(query),
                "offset": page * 30,
                "today": snapshot["today"],
            }
            scope = (
                "c.workspace_id=:w AND c.run_id=:r AND EXISTS (SELECT 1 FROM alfa_branches b "
                "WHERE b.workspace_id=c.workspace_id AND b.connection_id=:conn "
                "AND b.external_id=c.branch_id AND b.available)" + self._filter(branch, status)
            )
            where = (
                "d.workspace_id=:w AND d.run_id=:r AND EXISTS "
                "(SELECT 1 FROM alfa_customers c WHERE " + scope + " AND c.contact_id=d.contact_id)"
            )
            if chat_only:
                where += (" AND EXISTS(SELECT 1 FROM conversations accessible JOIN connections channel "
                          "ON channel.workspace_id=accessible.workspace_id AND channel.id=accessible.connection_id "
                          "WHERE accessible.workspace_id=d.workspace_id AND accessible.contact_id=d.contact_id "
                          "AND accessible.kind='direct' AND ((channel.kind='max_bot' AND EXISTS("
                          "SELECT 1 FROM max_settings bot WHERE bot.workspace_id=accessible.workspace_id "
                          "AND bot.connection_id=accessible.connection_id)) OR (channel.kind='alfa_max_browser' AND EXISTS("
                          "SELECT 1 FROM alfa_max_settings native WHERE native.workspace_id=accessible.workspace_id "
                          "AND native.connection_id=accessible.connection_id))))")
            if query.strip():
                where += (
                    " AND d.search_vector @@ to_tsquery('simple',:search)"
                    if params["search"]
                    else " AND false"
                )
            if max_status == "connected":
                where += " AND " + MAX_CONNECTED
            elif max_status == "disconnected":
                where += " AND NOT (" + MAX_CONNECTED + ")"
            if max_bot_status == "connected":
                where += " AND " + MAX_BOT_CONNECTED
            elif max_bot_status == "disconnected":
                where += " AND NOT (" + MAX_BOT_CONNECTED + ")"
            result.update(
                snapshot=str(snapshot["run"]),
                total=db.execute(
                    text("SELECT count(*) FROM alfa_directory d WHERE " + where), params
                ).scalar_one(),
            )
            order_by = []
            if max_bot_status == "connected_first":
                order_by.append("(" + MAX_BOT_CONNECTED + ") DESC")
            elif max_bot_status == "disconnected_first":
                order_by.append("(" + MAX_BOT_CONNECTED + ") ASC")
            if max_status == "connected_first":
                order_by.append("(" + MAX_CONNECTED + ") DESC")
            order_by.append("d.display_name")
            order_by.append("d.contact_id")
            records = list(
                db.execute(
                    text(
                        "SELECT d.*,("
                        + MAX_CONNECTED
                        + ") AS max_connected,("
                        + MAX_BOT_CONNECTED
                        + ") AS max_bot_connected FROM alfa_directory d WHERE "
                        + where
                        + " ORDER BY "
                        + ",".join(order_by)
                        + " LIMIT 30 OFFSET :offset"
                    ),
                    params,
                ).mappings()
            )
            if not records:
                return result
            params["ids"] = [r["contact_id"] for r in records]
            summaries = {
                r["contact_id"]: r
                for r in db.execute(
                    text(
                        "SELECT c.contact_id,count(DISTINCT c.student_id) AS student_count,"
                        "(array_agg(DISTINCT c.name ORDER BY c.name))[1:4] AS names,"
                        f"max({WEIGHT}) AS weight FROM alfa_customers c WHERE "
                        + scope
                        + " AND c.contact_id=ANY(CAST(:ids AS uuid[])) GROUP BY c.contact_id"
                    ),
                    params,
                ).mappings()
            }
            groups = {
                r["contact_id"]: r
                for r in db.execute(
                    text(
                        "SELECT c.contact_id,count(DISTINCT (g.branch_id,g.external_id)) AS group_count,"
                        "(array_agg(DISTINCT g.name ORDER BY g.name))[1:4] AS names "
                        "FROM alfa_customers c JOIN alfa_memberships m "
                        "ON m.workspace_id=c.workspace_id AND m.run_id=c.run_id AND "
                        "m.branch_id=c.branch_id "
                        "AND m.customer_id=c.external_id JOIN alfa_groups g "
                        "ON g.workspace_id=m.workspace_id AND g.run_id=m.run_id AND "
                        "g.branch_id=m.branch_id "
                        "AND g.external_id=m.group_id WHERE "
                        + scope
                        + " AND c.contact_id=ANY(CAST(:ids AS uuid[])) "
                        "AND (m.b_date IS NULL OR m.b_date<=:today) AND (m.e_date IS NULL OR "
                        "m.e_date>=:today) "
                        "GROUP BY c.contact_id"
                    ),
                    params,
                ).mappings()
            }
            for row in records:
                summary, group = summaries[row["contact_id"]], groups.get(row["contact_id"], {})
                result["contacts"].append(
                    {
                        "id": str(row["contact_id"]),
                        "name": row["display_name"],
                        "status": STATUS[summary["weight"]],
                        "needs_review": row["needs_review"],
                        "max_connected": bool(row["max_connected"]),
                        "max_bot_connected": bool(row["max_bot_connected"]),
                        "students": summary["names"],
                        "student_count": summary["student_count"],
                        "groups": group.get("names", []),
                        "group_count": group.get("group_count", 0),
                    }
                )
            result["has_more"] = (page + 1) * 30 < result["total"]
            return result

    def detail(self, actor, workspace, contact_id, *, branch=None, conversation=None):
        source_contact = contact_id
        transaction = self.connections.chat_transaction if conversation else self.connections._transaction
        with transaction(actor, workspace, snapshot=True) as db:
            if conversation and not db.execute(text(
                "SELECT 1 FROM conversations c JOIN connections cn ON cn.workspace_id=c.workspace_id AND cn.id=c.connection_id "
                "WHERE c.workspace_id=:w AND c.id=:conversation AND c.contact_id=:contact AND c.kind='direct' "
                "AND cn.kind IN ('max_bot','alfa_max_browser')"
            ), {"w": workspace, "conversation": conversation, "contact": contact_id}).scalar_one_or_none():
                raise AuthError(404, "Контакт недоступен в этом диалоге.")
            snapshot = self._published(db, workspace)
            if snapshot is None:
                raise AuthError(404, "Контакт недоступен.")
            selection = card_preference.selected(db, workspace, snapshot, conversation, source_contact)
            contact_id = UUID(selection["contact"]) if selection else contact_id
            family_contacts = [UUID(value) for value in selection["contacts"]] if selection else [contact_id]
            params = {
                "w": workspace,
                "r": snapshot["run"],
                "id": contact_id,
                "conn": snapshot["connection_id"],
                "branch": branch,
                "today": snapshot["today"],
                "family_contacts": family_contacts,
            }
            contact = (
                db.execute(
                    text(
                        "SELECT d.*,("
                        + MAX_CONNECTED
                        + ") AS max_connected,("
                        + MAX_BOT_CONNECTED
                        + ") AS max_bot_connected FROM alfa_directory d WHERE d.workspace_id=:w "
                        "AND d.run_id=:r AND d.contact_id=:id"
                    ),
                    params,
                )
                .mappings()
                .one_or_none()
            )
            if contact is None:
                raise AuthError(404, "Контакт недоступен.")
            scope = (
                "c.workspace_id=:w AND c.run_id=:r AND c.contact_id=ANY(CAST(:family_contacts AS uuid[])) AND b.available "
                "AND b.connection_id=:conn" + self._filter(branch, None)
            )
            if selection and selection.get("hidden_customers"):
                scope += " AND NOT(c.external_id=ANY(CAST(:hidden_customers AS bigint[])))"
                params["hidden_customers"] = list(map(int, selection["hidden_customers"]))
            cards = list(
                db.execute(
                    text(
                        "SELECT c.*,b.name AS branch_name,s.name AS study_status FROM "
                        "alfa_customers c "
                        "JOIN alfa_branches b ON b.workspace_id=c.workspace_id AND "
                        "b.external_id=c.branch_id "
                        "AND b.connection_id=:conn LEFT JOIN alfa_statuses s ON "
                        "s.workspace_id=c.workspace_id "
                        "AND s.run_id=c.run_id AND s.branch_id=c.branch_id AND "
                        "s.kind='study-status' "
                        "AND s.external_id=c.study_status_id WHERE "
                        + scope
                        + " ORDER BY c.name,c.student_id,c.branch_id,c.external_id LIMIT 1001"
                    ),
                    params,
                ).mappings()
            )
            if not cards:
                raise AuthError(404, "Контакт недоступен.")
            members = list(
                db.execute(
                    text(
                        "SELECT c.student_id,c.external_id AS customer_id,"
                        "g.external_id,g.name,g.branch_id,m.b_date,m.e_date,"
                        "b.name AS branch_name FROM alfa_customers c "
                        "JOIN alfa_branches b ON b.workspace_id=c.workspace_id AND "
                        "b.external_id=c.branch_id "
                        "AND b.connection_id=:conn JOIN alfa_memberships m ON "
                        "m.workspace_id=c.workspace_id "
                        "AND m.run_id=c.run_id AND m.branch_id=c.branch_id AND "
                        "m.customer_id=c.external_id "
                        "JOIN alfa_groups g ON g.workspace_id=m.workspace_id AND g.run_id=m.run_id "
                        "AND g.branch_id=m.branch_id AND g.external_id=m.group_id WHERE "
                        + scope
                        + " AND (m.b_date IS NULL OR m.b_date<=:today) AND (m.e_date IS NULL OR "
                        "m.e_date>=:today) "
                        "ORDER BY g.name,g.external_id LIMIT 5000"
                    ),
                    params,
                ).mappings()
            )
            people = {}
            for row in cards[:1000]:
                key = str(row["student_id"])
                person = people.setdefault(
                    key,
                    {
                        "id": key,
                        "name": row["name"],
                        "dob": row["dob"].isoformat() if row["dob"] else None,
                        "cards": [],
                        "groups": [],
                    },
                )
                person["cards"].append(
                    {
                        "id": str(row["external_id"]),
                        "branch_id": str(row["branch_id"]),
                        "branch_name": row["branch_name"],
                        "status": row["status"],
                        "study_status": row["study_status"],
                        "legal_name": row["legal_name"],
                        "balance": str(row["balance"]) if row["balance"] is not None else None,
                        "balance_updated_at": snapshot["finished_at"].isoformat() if snapshot["finished_at"] else None,
                        "paid_lesson_count": row["paid_lesson_count"],
                        "url": (
                            f"{snapshot['origin']}/company/{row['branch_id']}/customer/view"
                            f"?id={row['external_id']}"
                        ),
                    }
                )
            cached = {
                (row["branch_id"], row["customer_id"]): row
                for row in db.execute(
                    text(
                        "SELECT f.* FROM alfa_family_details f JOIN alfa_customers c ON "
                        "c.workspace_id=f.workspace_id AND c.branch_id=f.branch_id "
                        "AND c.external_id=f.customer_id WHERE f.workspace_id=:w "
                        "AND f.connection_id=:conn AND f.settings_version=:version "
                        "AND c.run_id=:r AND c.contact_id=ANY(CAST(:family_contacts AS uuid[]))"
                    ),
                    {**params, "version": snapshot["version"]},
                ).mappings()
            }
            group_states = {(row["branch_id"], row["external_id"]): row["payload"] for row in db.execute(text(
                "SELECT c.branch_id,c.external_id,e.payload FROM alfa_customers c JOIN outbox_events e "
                "ON e.workspace_id=c.workspace_id AND e.topic=:topic AND e.event_key=concat(CAST(:conn AS text),':',c.branch_id,':',c.external_id) "
                "WHERE c.workspace_id=:w AND c.run_id=:r AND c.contact_id=ANY(CAST(:family_contacts AS uuid[])) AND e.payload->>'version'=CAST(:version AS text) "
                "AND e.payload->>'student'=c.student_id::text AND e.payload->>'contact'=c.contact_id::text "
                "AND e.payload->>'checked_run'=CAST(:r AS text)"),
                {**params, "topic": GROUPS_TOPIC, "version": snapshot["version"]}).mappings()}
            for person in people.values():
                for card in person["cards"]:
                    card["groups_refreshed_at"] = group_states.get((int(card["branch_id"]), int(card["id"])), {}).get("refreshed_at")
                    source = cached.get((int(card["branch_id"]), int(card["id"])))
                    if source:
                        if snapshot["finished_at"] is None or source["refreshed_at"] >= snapshot["finished_at"]:
                            card.update(
                                balance=str(source["balance"]) if source["balance"] is not None else None,
                                paid_lesson_count=source["paid_lesson_count"],
                                balance_updated_at=source["refreshed_at"].isoformat(),
                            )
                        card.update(
                            subscriptions=source["subscriptions"],
                            lessons=source["lessons"],
                            refreshed_at=source["refreshed_at"].isoformat(),
                            lessons_refreshed_at=source["lessons_refreshed_at"].isoformat() if source["lessons_refreshed_at"] else None,
                            lessons_full_refreshed_at=source["lessons_full_refreshed_at"].isoformat() if source["lessons_full_refreshed_at"] else None,
                        )
                        zone = ZoneInfo(snapshot.get("timezone", "UTC"))
                        card["lessons_recent_day"] = source["lessons_refreshed_at"].astimezone(zone).date().isoformat() if source["lessons_refreshed_at"] else None
                        card["lessons_full_day"] = source["lessons_full_refreshed_at"].astimezone(zone).date().isoformat() if source["lessons_full_refreshed_at"] else None
                    card["needs_mapping_refresh"] = mapping_pending(card)
                    card["needs_refresh"] = card["needs_mapping_refresh"] or source is None or source["refreshed_at"] < datetime.now(
                        UTC
                    ) - timedelta(minutes=30)
            for row in members:
                person = people.get(str(row["student_id"]))
                group = {
                    "id": str(row["external_id"]),
                    "name": row["name"],
                    "branch_id": str(row["branch_id"]),
                    "branch_name": row["branch_name"],
                    "url": (
                        f"{snapshot['origin']}/company/{row['branch_id']}/group/view"
                        f"?id={row['external_id']}"
                    ),
                }
                if person:
                    existing = next(
                        (
                            item
                            for item in person["groups"]
                            if item["id"] == group["id"] and item["branch_id"] == group["branch_id"]
                        ),
                        None,
                    )
                    if existing is None:
                        group["customer_ids"] = [str(row["customer_id"])]
                        group["memberships"] = []
                        person["groups"].append(group)
                        existing = group
                    elif str(row["customer_id"]) not in existing["customer_ids"]:
                        existing["customer_ids"].append(str(row["customer_id"]))
                    period = {"customer_id": str(row["customer_id"]),
                              "begin": row["b_date"].isoformat() if row["b_date"] else None,
                              "end": row["e_date"].isoformat() if row["e_date"] else None}
                    if period not in existing["memberships"]:
                        existing["memberships"].append(period)
            for person in people.values():
                person["balance"] = customer_balance(person["cards"])
                person["paid_lesson_count"] = customer_paid_lessons(person["cards"])
                person["subscriptions"] = customer_subscriptions(person["cards"], groups=person["groups"])
                group_facts(person["groups"], person["cards"], today=snapshot["today"], subscriptions=person["subscriptions"])
            return {
                "id": str(contact_id),
                "source_contact_id": str(source_contact),
                "selection": card_preference.public(selection),
                "name": selection["name"] if selection else contact["display_name"],
                "phones": contact["phones"],
                "emails": contact["emails"],
                "needs_review": bool(contact["needs_review"] or (selection and selection.get("ambiguous"))),
                "max_connected": bool(contact["max_connected"] or selection),
                "max_bot_connected": bool(contact["max_bot_connected"]),
                "students": list(people.values()),
                "cache": cache_status(db, workspace, snapshot["connection_id"], list(people.values()), contact_id, snapshot["version"],
                                      contacts=family_contacts, snapshot=snapshot),
                "snapshot_at": snapshot["finished_at"].isoformat()
                if snapshot["finished_at"]
                else None,
                "today": snapshot["today"].isoformat(),
                "has_more": len(cards) > 1000 or len(members) == 5000,
            }

    def prepare(self, actor, workspace, contact_id, *, conversation, auto=False, force=False, cached_only=False, compact=False, known_version=None):
        # An ordinary chat selection must not prepare a read client's full card.
        # Both cache hits and requests retain the same chat membership checks.
        from center_messenger.chat_queue import state
        with self.connections.chat_transaction(actor, workspace) as db:
            linked = db.execute(text("SELECT 1 FROM conversations WHERE workspace_id=:w AND id=:c "
                "AND contact_id=:contact AND kind='direct'"),
                {"w": workspace, "c": conversation, "contact": contact_id}).scalar_one_or_none()
            if not linked:
                raise AuthError(404, "Контакт недоступен в этом диалоге.")
            if auto and not state(db, workspace, conversation)["needs_reply"]:
                return {"deferred": True}
            if force:
                self.connections.accounts._workspace(db, actor, workspace, manage=True)
            snapshot = self._published(db, workspace)
            if snapshot is None:
                raise AuthError(404, "Контакт недоступен.")
            if not cached_only:
                card_preference.refresh(db, workspace, snapshot, [contact_id])
                selection = card_preference.selected(db, workspace, snapshot, conversation, contact_id)
                for target in selection["contacts"] if selection else [contact_id]:
                    enqueue(db, workspace, snapshot, contact=target, force=force)
        if compact:
            return self.prepared_view(actor, workspace, contact_id, conversation, known_version=known_version)
        return self.detail(actor, workspace, contact_id, conversation=conversation)

    def prepared_view(self, actor, workspace, contact_id, conversation, *, known_version=None):
        from center_messenger.chat_revision import versions
        with self.connections.chat_transaction(actor, workspace, snapshot=True) as db:
            if not db.execute(text("SELECT 1 FROM conversations c JOIN connections cn ON "
                "cn.workspace_id=c.workspace_id AND cn.id=c.connection_id WHERE c.workspace_id=:w "
                "AND c.id=:c AND c.contact_id=:contact AND c.kind='direct' "
                "AND cn.kind IN ('max_bot','alfa_max_browser')"),
                {"w": workspace, "c": conversation, "contact": contact_id}).scalar_one_or_none():
                raise AuthError(404, "Контакт недоступен в этом диалоге.")
            snapshot = self._published(db, workspace)
            if snapshot is None:
                raise AuthError(404, "Контакт недоступен.")
            key = (str(workspace), str(getattr(actor, "user_id", None)), str(conversation), str(contact_id),
                   versions(db, workspace)[1], str(snapshot["run"]), snapshot["version"], str(snapshot["today"]))
        value = self.card_views.get(key)
        if value is None:
            value = self.card_views.put(key, self.detail(actor, workspace, contact_id, conversation=conversation))
        return self.card_views.reply(value, known_version)

    def refresh_card(self, actor, workspace, contact_id, student_id, branch_id, customer_id):
        """Refresh one source card without holding a transaction over network IO."""
        current = self.detail(actor, workspace, contact_id)
        student = next((row for row in current["students"] if row["id"] == str(student_id)), None)
        source_card = next(
            (
                card
                for card in (student["cards"] if student else [])
                if card["branch_id"] == str(branch_id) and card["id"] == str(customer_id)
            ),
            None,
        )
        if source_card is None:
            raise AuthError(404, "Карточка ребёнка не найдена в этом контакте.")
        self.connections.accounts.throttle(
            f"alfa-family:{workspace}:{actor.user_id}", limit=30, minutes=1
        )
        with self.connections._transaction(actor, workspace) as db:
            row = self.connections._row(db, workspace)
            if row is None or row["secret_ciphertext"] is None:
                raise AuthError(409, "Подключение АльфаCRM недоступно.")
            settings = dict(row)
            crm_today = db.execute(
                text(
                    "SELECT CAST(now() AT TIME ZONE timezone AS date) FROM workspaces WHERE id=:w"
                ),
                {"w": workspace},
            ).scalar_one()
        try:
            with self.connections._host_gate(settings["origin"]) as gate:
                with self.connections._transaction(actor, workspace) as db:
                    row = self.connections._row(db, workspace)
                    if row is None or row["version"] != settings["version"]:
                        raise AuthError(409, "Подключение изменилось. Повторите обновление.")
                    settings = dict(row)
                credentials = self.connections.vault.open(
                    workspace, settings["connection_id"], settings["secret_ciphertext"]
                )
                client = self.connections.client_factory(
                    settings["origin"], settings["api_email"], credentials, gate
                )
                data = fetch_family_card(
                    client,
                    branch_id,
                    customer_id,
                    source_customer=source_card,
                    today=crm_today,
                )
                with self.connections._transaction(actor, workspace, lock=True) as db:
                    row = self.connections._row(db, workspace)
                    if row is None or row["version"] != settings["version"]:
                        raise AuthError(409, "Подключение изменилось. Повторите обновление.")
                    belongs = db.execute(
                        text(
                            "SELECT 1 FROM alfa_customers c JOIN alfa_sync_control s ON "
                            "s.workspace_id=c.workspace_id AND s.published_id=c.run_id "
                            "WHERE c.workspace_id=:w AND c.branch_id=:b AND c.external_id=:id "
                            "AND c.contact_id=:contact AND c.student_id=:student "
                            "AND s.connection_id=:conn"
                        ),
                        {
                            "w": workspace,
                            "b": branch_id,
                            "id": customer_id,
                            "contact": contact_id,
                            "student": student_id,
                            "conn": settings["connection_id"],
                        },
                    ).scalar_one_or_none()
                    if belongs is None:
                        raise AuthError(409, "Сведения CRM изменились. Откройте карточку заново.")
                    db.execute(
                        text(
                            "INSERT INTO alfa_family_details(workspace_id,connection_id,branch_id,"
                            "customer_id,settings_version,balance,paid_lesson_count,"
                            "subscriptions,lessons,lessons_refreshed_at,lessons_full_refreshed_at) VALUES "
                            "(:w,:conn,:b,:id,:version,:balance,:paid,CAST(:subs AS jsonb),"
                            "CAST(:lessons AS jsonb),now(),now()) ON CONFLICT "
                            "(workspace_id,connection_id,branch_id,customer_id) DO UPDATE SET "
                            "settings_version=EXCLUDED.settings_version,balance=EXCLUDED.balance,"
                            "paid_lesson_count=EXCLUDED.paid_lesson_count,"
                            "subscriptions=EXCLUDED.subscriptions,lessons=EXCLUDED.lessons,"
                            "refreshed_at=now(),lessons_refreshed_at=now(),lessons_full_refreshed_at=now()"
                        ),
                        {
                            "w": workspace,
                            "conn": settings["connection_id"],
                            "b": branch_id,
                            "id": customer_id,
                            "version": settings["version"],
                            "balance": data["balance"],
                            "paid": data["paid_lesson_count"],
                            "subs": json.dumps(data["subscriptions"]),
                            "lessons": json.dumps(data["lessons"]),
                        },
                    )
                    db.execute(
                        text(
                            "UPDATE alfa_settings SET secret_ciphertext=:secret "
                            "WHERE workspace_id=:w"
                        ),
                        {
                            "w": workspace,
                            "secret": self.connections.vault.seal(
                                workspace, settings["connection_id"], credentials
                            ),
                        },
                    )
        except AlfaError as error:
            raise AuthError(502, str(error)) from None
        return self.detail(actor, workspace, contact_id)


@router.get("/sync")
def sync_status(workspace_id: UUID, request: Request):
    service(request)
    return request.app.state.alfa_sync.status(identity(request), workspace_id)


@router.post("/sync", status_code=202)
def sync_start(workspace_id: UUID, model: SyncOptions, request: Request):
    service(request)
    job = request.app.state.alfa_sync.start(identity(request, mutation=True), workspace_id, model)
    return {"run_id": job}


@router.delete("/sync", status_code=204)
def sync_stop(workspace_id: UUID, request: Request):
    service(request)
    request.app.state.alfa_sync.stop(identity(request, mutation=True), workspace_id)


@router.get("")
def listing(
    workspace_id: UUID,
    request: Request,
    q: str = Query(default="", max_length=200),
    branch: int | None = Query(default=None, gt=0, le=9223372036854775807),
    status: str | None = Query(default=None, pattern="^(client|lead|archive)$"),
    max_status: str = Query(
        default="all", pattern="^(all|connected|disconnected|connected_first)$"
    ),
    max_bot_status: str = Query(
        default="connected_first",
        pattern="^(all|connected|disconnected|connected_first|disconnected_first)$",
    ),
    page: int = Query(default=0, ge=0, le=2000),
):
    service(request)
    return request.app.state.directory.listing(
        identity(request),
        workspace_id,
        query=q,
        branch=branch,
        status=status,
        max_status=max_status,
        max_bot_status=max_bot_status,
        page=page,
    )


@router.get("/contacts/{contact_id}")
def detail(
    workspace_id: UUID,
    contact_id: UUID,
    request: Request,
    branch: int | None = Query(default=None, gt=0, le=9223372036854775807),
):
    service(request)
    return request.app.state.directory.detail(
        identity(request), workspace_id, contact_id, branch=branch
    )


@router.post("/contacts/{contact_id}/students/{student_id}/cards/{branch_id}/{customer_id}/refresh")
def refresh_card(
    workspace_id: UUID,
    contact_id: UUID,
    student_id: UUID,
    branch_id: int,
    customer_id: int,
    request: Request,
):
    service(request)
    if branch_id <= 0 or customer_id <= 0:
        raise AuthError(422, "Некорректный идентификатор карточки CRM.")
    return request.app.state.directory.refresh_card(
        identity(request, mutation=True),
        workspace_id,
        contact_id,
        student_id,
        branch_id,
        customer_id,
    )
