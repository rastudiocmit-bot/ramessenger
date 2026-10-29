"""Bounded GET-only diagnosis of recent failed native MAX recipients.

Run inside the API container. No sends, client/history changes, raw forms,
message texts, phone numbers, CSRF, credentials or raw MAX IDs in the report.
Only the shared browser lease and refreshed session cookies may be saved.
"""
import hashlib
from contextlib import contextmanager
from datetime import UTC, datetime
import json
import re
import sys
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from bs4 import BeautifulSoup
from sqlalchemy import text

from center_messenger.accounts import Accounts
from center_messenger.alfa_max import AlfaMaxConnections
from center_messenger.alfa_max_browser import (
    AlfaBrowserError, MAX_HTML_BYTES, ONCLICK_BULK_URL, _compose_url_key,
    _compose_url_max_user_id, _inherit_max_user_id, _json_envelope, _login_form,
    _otp_form, _validated_compose_url, parse_compose_form,
    _customer_card_compose_url,
)
from center_messenger.alfa_service import AlfaConnections
from center_messenger.alfa_session import control, stamp
from center_messenger.attachment_audit import BrowserProbe, load_source
from center_messenger.config import Settings
from center_messenger.connector_vault import ConnectorVault
from center_messenger.database import make_engine
from center_messenger.message_order import message_order, visible_message
from center_messenger import customer_updates
from center_messenger.crm_api import DirectoryReader


def peer_tag(peer):
    return hashlib.sha256(peer.encode()).hexdigest()[:12] if isinstance(peer, str) else None


def browser_status(service, workspace, connection):
    """Observe contention without exposing tokens, cookies or client text."""
    with service.accounts.transaction(workspace_id=workspace, snapshot=True) as db:
        row = service._row(db, workspace)
        if not row or row['connection_id'] != connection:
            raise AlfaBrowserError('settings_changed')
        meta, now = control(row), datetime.now(UTC)
        sync_running = db.execute(text('SELECT EXISTS (SELECT 1 FROM alfa_max_sync_queue '
            'WHERE workspace_id=:w AND connection_id=:c AND lease_owner IS NOT NULL '
            'AND lease_until>clock_timestamp())'), {'w': workspace, 'c': connection}).scalar_one()
        pending_sends = db.execute(text("SELECT count(*) FROM send_commands s JOIN conversations c "
            "ON c.workspace_id=s.workspace_id AND c.id=s.conversation_id "
            "WHERE s.workspace_id=:w AND c.connection_id=:c AND s.state='queued'"),
            {'w': workspace, 'c': connection}).scalar_one()
        return {'state': row['state'], 'needs_check': bool(meta.get('needs_check')),
                'lease_active': bool(meta.get('lease') and stamp(meta.get('until')) > now),
                'lease_remaining_seconds': max(0, round((stamp(meta.get('until')) - now).total_seconds())),
                'sync_running': sync_running, 'queued_sends': pending_sends,
                'last_sync_at': row['last_sync_at'].isoformat() if row.get('last_sync_at') else None}


@contextmanager
def wait_for_browser(service, workspace, connection, report, *, seconds=210):
    """Retry only lease acquisition; never rerun forms or override another lease."""
    started, next_status = time.monotonic(), 0
    while True:
        elapsed = time.monotonic() - started
        if elapsed >= next_status:
            status = browser_status(service, workspace, connection)
            report['browser_wait'] = status | {'waited_seconds': round(elapsed, 1)}
            print('MAX audit: ' + json.dumps(report['browser_wait']), file=sys.stderr, flush=True)
            next_status = elapsed + 5
        report['browser_wait']['waited_seconds'] = round(elapsed, 1)
        if elapsed >= seconds:
            raise AlfaBrowserError('browser_wait_timeout')
        if status['state'] != 'active':
            raise AlfaBrowserError('connection_not_active')
        if status['needs_check']:
            raise AlfaBrowserError('session_check_pending')
        # Yield when queued sends are observed. This probe does not change the
        # production scheduler or reserve a place in its durable queue.
        if not status['queued_sends']:
            try:
                lease = service.sessions.claim(workspace, connection)
                break
            except AlfaBrowserError as error:
                if error.code != 'browser_busy':
                    raise
        time.sleep(min(.5, max(0, seconds - (time.monotonic() - started))))
    report['browser_wait']['waited_seconds'] = round(time.monotonic() - started, 1)
    report['browser_wait']['acquired'] = True
    try:
        yield lease
    finally:
        service.sessions.release(lease)


def routes(markup, origin, branch, customer):
    soup = BeautifulSoup(markup, 'html.parser')
    candidates = {}
    for node in soup.select('[href],[data-url],[data-href],[onclick]'):
        values = [node.get(name) for name in ('href', 'data-url', 'data-href')]
        values += [match.group(1) for match in ONCLICK_BULK_URL.finditer(node.get('onclick', ''))]
        for value in values:
            if not isinstance(value, str) or '/max/bulk' not in value:
                continue
            try:
                address = _validated_compose_url(value, origin=origin, branch=branch, customer=customer)
            except AlfaBrowserError:
                continue
            candidates.setdefault(_compose_url_key(address), address)
    return list(candidates.values())


def form_report(raw):
    envelope = _json_envelope(raw)
    soup = BeautifulSoup(envelope.get('html') or '', 'html.parser')
    forms = []
    for form in soup.select('form[action]')[:4]:
        query = parse_qs(urlsplit(form.get('action', '')).query, keep_blank_values=True)
        fields = []
        for item in form.select('input[name],select[name]'):
            name = item.get('name', '')
            if re.search(r'max.?user|recipient|peer', name, re.I):
                fields.append({'name': re.sub(r'[^A-Za-z0-9_\[\]-]', '?', name)[:100],
                               'has_value': bool(item.get('value')),
                               'type': item.get('type', item.name)[:20]})
        forms.append({'action_query_keys': sorted(re.sub(r'[^A-Za-z0-9_\[\]-]', '?', key)[:100] for key in query),
                      'action_peer_tag': peer_tag(_compose_url_max_user_id(form.get('action', ''))),
                      'recipient_fields': fields[:12]})
    return {'forms': forms, 'modal_urls': len(envelope.get('modal_urls') or []),
            'empty': envelope.get('empty', False)}


class RecipientProbe(BrowserProbe):
    def navigate(self, address):
        # Same login detection and card size limit as the production sender.
        self.allowed = {address}
        try:
            response = self.page.goto(address, wait_until='domcontentloaded', timeout=15000)
            self._check_response(response)
            markup = response.text()
            if len(markup.encode('utf-8')) > MAX_HTML_BYTES:
                raise AlfaBrowserError('form_unavailable')
            soup = BeautifulSoup(markup, 'html.parser')
            if _otp_form(soup):
                raise AlfaBrowserError('otp_required')
            if _login_form(soup):
                raise AlfaBrowserError('reauth_required')
            return markup
        finally:
            self.allowed.clear()

    def _check_response(self, response):
        from center_messenger.alfa_max_browser import AlfaMaxBrowser
        AlfaMaxBrowser._check_origin(self.page, self.origin)
        if response is None or response.status != 200:
            raise AlfaBrowserError('crm_unavailable')


def inspect_card(probe, branch, customer, expected_peer, renew, contact_phone=None):
    renew()
    markup = probe.navigate(f'{probe.origin}/company/{branch}/customer/view?' + urlencode({'id': customer}))
    choices = routes(markup, probe.origin, branch, customer)
    report = {'compose_routes': len(choices), 'routes_truncated': len(choices) > 4,
              'card_peer_tags': sorted({peer_tag(peer) for address in choices
                                       if (peer := _compose_url_max_user_id(address))}), 'forms': []}
    try:
        selected = _customer_card_compose_url(markup, origin=probe.origin, branch=branch,
            customer=customer, expected_peer=expected_peer, expected_phone=contact_phone)
        report['sender_resolution'] = {'selected_peer_tag': peer_tag(_compose_url_max_user_id(selected))}
    except AlfaBrowserError as error:
        report['sender_resolution'] = {'error_code': error.code}
    if not choices:
        choices = [f'{probe.origin}/company/{branch}/max/bulk?' + urlencode([
            ('related_class', 'Customer'), ('related_ids[]', customer)])]
    for address in choices[:4]:
        item = {'route_peer_tag': peer_tag(_compose_url_max_user_id(address))}
        report['forms'].append(item)
        try:
            if expected_peer:
                address = _inherit_max_user_id(address, expected_peer)
            visited = set()
            for _ in range(4):
                address = _validated_compose_url(address, origin=probe.origin, branch=branch, customer=customer)
                if address in visited:
                    raise AlfaBrowserError('form_unavailable')
                visited.add(address)
                renew()
                response = probe.fetch(address, html=True, ajax=True)
                item['http_status'] = response.get('status')
                if response.get('error'):
                    item['error_code'] = response['error']
                    break
                raw = response.get('text', '')
                if _json_envelope(raw).get('empty'):
                    renew()
                    response = probe.fetch(address, html=True)
                    if response.get('error'):
                        item['error_code'] = response['error']
                        break
                    raw = response.get('text', '')
                item['schema'] = form_report(raw)
                parsed = parse_compose_form(raw, origin=probe.origin, branch=branch,
                                            customer=customer, compose_url=address)
                if 'compose_url' not in parsed:
                    item['status'] = 'recipient_verified_no_send'
                    item['form_peer_tag'] = peer_tag(parsed.get('max_user_id'))
                    break
                address = parsed['compose_url']
            else:
                item['error_code'] = 'modal_limit'
        except AlfaBrowserError as error:
            item['error_code'] = error.code
    return report


def catalog_report(db, workspace, target, payload):
    session = target.get('session_snapshot') or {}
    if not session.get('crm_connection_id'):
        return None
    snapshot = DirectoryReader._published(db, workspace)
    if not snapshot or snapshot['connection_id'] != session['crm_connection_id']:
        return {'status': 'snapshot_unavailable'}
    ids = {int(target['customer'])}
    ids.update(int(value) for value in (payload or {}).get('crm_customer_ids') or [])
    ids.update(int(pair[1]) for pair in (payload or {}).get('crm_observed_customers') or [])
    rows = db.execute(text("SELECT external_id,branch_id,status FROM alfa_customers WHERE workspace_id=:w "
        "AND run_id=:r AND external_id=ANY(CAST(:ids AS bigint[])) ORDER BY external_id,branch_id LIMIT 40"),
        {'w': workspace, 'r': snapshot['run'], 'ids': sorted(ids)[:20]}).mappings().all()
    saved = customer_updates.observation(db, workspace, snapshot, target['branch'], target['customer'])
    return {'status': 'available', 'run': str(snapshot['run']),
        'cards': [{'customer': r['external_id'], 'branch': r['branch_id'], 'status': r['status']} for r in rows],
        'observation': {k: saved.get(k) for k in ('observed_at', 'revision', 'source', 'settings')}}


def current_target(service, db, workspace, connection, case):
    """Read the visible source and production reply target in one snapshot."""
    payload = db.execute(text("SELECT m.payload FROM messages m JOIN conversations c "
        "ON c.workspace_id=m.workspace_id AND c.id=m.conversation_id "
        "WHERE m.workspace_id=:w AND c.id=:conv AND c.connection_id=:conn "
        "AND m.direction='in' AND m.actor_type='contact' AND " + visible_message('m') +
        " AND coalesce(m.payload->>'crm_mirror','false')<>'true' "
        "AND m.payload->>'customer_id' IS NOT NULL AND m.payload->>'customer_branch_id' IS NOT NULL "
        "ORDER BY " + message_order(descending=True) + " LIMIT 1"),
        {'w': workspace, 'conv': case['conversation_id'], 'conn': connection}).scalar_one_or_none()
    for name in ('target_error_code', 'error_code', 'stored_incoming',
                 'expected_peer_tag', 'contact_phone_tag'):
        case.pop(name, None)
    if payload:
        # Whitelist metadata: never include message bodies, raw MAX IDs,
        # phones, names, credentials or the encrypted browser session.
        case['stored_incoming'] = {key: payload.get(key) for key in (
            'crm_record_id', 'branch_id', 'customer_id', 'customer_branch_id',
            'crm_customer_ids', 'crm_observed_customers', 'crm_recipient_stale')}
        case.update(branch=int(payload['customer_branch_id']), customer=int(payload['customer_id']))
    try:
        target = service._send_target_in_transaction(db, workspace, connection, case['conversation_id'])
    except AlfaBrowserError as error:
        case['target_error_code'] = error.code
        return None
    peer, phone = target.get('max_user_id'), target.get('contact_phone')
    case.update(branch=target['branch'], customer=target['customer'],
                expected_peer_tag=peer_tag(peer),
                contact_phone_tag=peer_tag('phone:' + phone) if phone else None)
    catalog = catalog_report(db, workspace, target, payload)
    if catalog is not None:
        case['catalog'] = catalog
    return peer, phone


def main():
    config = Settings()
    engine = make_engine(config)
    report = {'message_writes': 0, 'message_posts': 0, 'stage': 'load_source', 'cases': []}
    try:
        workspace, source, _ = load_source(engine)
        vault = ConnectorVault(config.connection_master_key_file)
        service = AlfaMaxConnections(AlfaConnections(Accounts(engine, config), vault))
        report['stage'] = 'stored_targets'
        with engine.begin() as db:
            db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
            db.execute(text("SELECT set_config('app.workspace_id',:w,true)"), {'w': str(workspace)})
            failed = db.execute(text(
                "SELECT * FROM (SELECT DISTINCT ON (s.conversation_id) s.conversation_id,s.last_error_code,"
                "m.occurred_at FROM send_commands s JOIN messages m ON m.workspace_id=s.workspace_id "
                "AND m.send_command_id=s.id JOIN conversations c ON c.workspace_id=s.workspace_id "
                "AND c.id=s.conversation_id WHERE s.workspace_id=:w AND s.state='failed' "
                "AND c.connection_id=:conn AND s.last_error_code IN ('recipient_missing','multiple_recipients') "
                "ORDER BY s.conversation_id,m.occurred_at DESC) recent ORDER BY occurred_at DESC LIMIT 2"
            ), {'w': workspace, 'conn': source['connection_id']}).mappings().all()
            targets = []
            for failure in failed:
                case = {'conversation_id': str(failure['conversation_id']), 'last_error': failure['last_error_code']}
                report['cases'].append(case)
                targets.append(case)
        if targets:
            report['stage'] = 'browser_lease'
            with wait_for_browser(service, workspace, source['connection_id'], report) as operation:
                current = operation['row']
                if (current['crm_connection_id'] != source['crm_connection_id']
                        or current['crm_origin'] != source['origin']):
                    raise AlfaBrowserError('settings_changed')
                if current['state'] != 'active':
                    raise AlfaBrowserError('connection_not_active')
                if control(current).get('needs_check'):
                    raise AlfaBrowserError('session_check_pending')
                state = vault.open_browser_state(workspace, source['connection_id'], current['session_ciphertext'])
                started = time.monotonic()
                def renew():
                    if time.monotonic() - started > 120:
                        raise AlfaBrowserError('audit_time_limit')
                    with service.accounts.transaction(workspace_id=workspace) as db:
                        service.sessions.renew(db, operation)
                with RecipientProbe(source['origin'], state) as probe:
                    report['stage'] = 'source_forms'
                    for case in targets:
                        # The loader can repair a binding while this probe
                        # waits for Chromium. Inspect the current target, not
                        # a stale address captured before lease acquisition.
                        with service.accounts.transaction(workspace_id=workspace, snapshot=True) as db:
                            selected = current_target(service, db, workspace, source['connection_id'], case)
                        if selected is None:
                            continue
                        peer, phone = selected
                        print(f"Проверяем MAX: branch={case['branch']} customer={case['customer']}",
                              file=sys.stderr, flush=True)
                        try:
                            case['source'] = inspect_card(probe, case['branch'], case['customer'], peer, renew, phone)
                        except AlfaBrowserError as error:
                            case['error_code'] = error.code
                    errors = [case.get('error_code') for case in report['cases']]
                    errors += [case.get('target_error_code') for case in report['cases']]
                    errors += [item.get('error_code') for case in report['cases']
                               for item in case.get('source', {}).get('forms', [])]
                    if not any(code in {'reauth_required', 'otp_required'} for code in errors):
                        service.sessions.finish(operation, probe.context.storage_state(), state='active')
        report['stage'] = 'complete'
    except Exception as error:
        # Exception messages and raw HTTP responses can contain private data.
        report['error_code'] = error.code if isinstance(error, AlfaBrowserError) else type(error).__name__
    finally:
        engine.dispose()
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
