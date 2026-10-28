"""Bounded GET-only diagnosis of recent failed native MAX recipients.

Run inside the API container. No sends, client/history changes, raw forms,
message texts, phone numbers, CSRF, credentials or raw MAX IDs in the report.
Only the shared browser lease and refreshed session cookies may be saved.
"""
import argparse
import hashlib
from contextlib import contextmanager
from datetime import UTC, datetime
import json
import re
import sys
import time
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup
from sqlalchemy import text

from center_messenger.accounts import Accounts
from center_messenger.alfa_max import AlfaMaxConnections
from center_messenger.alfa_max_browser import (
    AlfaBrowserError, MAX_HTML_BYTES, ONCLICK_BULK_URL, _compose_url_key,
    _compose_url_max_user_id, _inherit_max_user_id, _json_envelope, _login_form,
    _otp_form, _validated_compose_url, parse_compose_form,
    parse_history_html, _history_url,
)
from center_messenger.crm_directory import phone_number
from center_messenger.crm_api import DirectoryReader
from center_messenger import card_preference
from center_messenger.alfa_service import AlfaConnections
from center_messenger.alfa_session import control, stamp
from center_messenger.attachment_audit import BrowserProbe, load_source
from center_messenger.config import Settings
from center_messenger.connector_vault import ConnectorVault
from center_messenger.database import make_engine
from center_messenger.message_order import message_order


def peer_tag(peer):
    return hashlib.sha256(peer.encode()).hexdigest()[:12] if isinstance(peer, str) else None


def phone_tags(value):
    # Metadata only. Never copy names, numbers or message bodies into a report.
    values = re.findall(r'(?<!\d)(?:\+7|7|8)(?:[\s().-]*\d){10}(?!\d)', value or '')
    return sorted({peer_tag('phone:' + number) for value in values
                   if (number := phone_number(value))})


def contact_route_contexts(markup, origin, branch, customer):
    """Describe the phone/link relationship; do not infer a recipient from it."""
    soup, result, seen = BeautifulSoup(markup, 'html.parser'), [], set()
    for node in soup.select('[href],[data-url],[data-href],[onclick]'):
        values = [node.get(name) for name in ('href', 'data-url', 'data-href')]
        values += [match.group(1) for match in ONCLICK_BULK_URL.finditer(node.get('onclick', ''))]
        for value in values:
            try:
                address = _validated_compose_url(value, origin=origin, branch=branch, customer=customer)
            except (AlfaBrowserError, TypeError):
                continue
            peer = _compose_url_max_user_id(address)
            if not peer or peer in seen:
                continue
            seen.add(peer)
            item = {'peer_tag': peer_tag(peer), 'inside_feed': node.find_parent(class_='feed-element') is not None,
                    'phone_tags': []}
            result.append(item)
            if item['inside_feed']:
                continue
            for parent in [node, *list(node.parents)[:5]]:
                if getattr(parent, 'attrs', None) is None or parent.name in {'body', 'html'}:
                    break
                # A broad parent containing communication text proves nothing.
                if parent.select_one('.feed-element'):
                    break
                text_value = parent.get_text(' ', strip=True)
                if len(text_value) > 2000:
                    break
                tags = phone_tags(text_value)
                for anchor in parent.select('a[href^="tel:"]'):
                    tags += phone_tags(anchor.get('href', '')[4:])
                if tags:
                    item.update(phone_tags=sorted(set(tags)), context_tag=parent.name,
                                context_classes=[c for c in parent.get('class', [])
                                                 if re.fullmatch(r'[A-Za-z0-9_-]{1,60}', c)][:8])
                    break
            if len(result) >= 8:
                return result
    return result


def journal_row_schema(markup, record, origin, branch, page=0):
    soup = BeautifulSoup(markup, 'html.parser')
    row = next((r for r in soup.select('table.crm-table tbody tr[data-key]')
                if r.get('data-key') == record), None)
    if row is None:
        return None
    attrs, links = [], []
    for node in [row, *row.find_all(True)]:
        for name, value in node.attrs.items():
            if (name.startswith('data-') and re.search(r'max|user|peer|recipient|phone|contact|customer', name, re.I)
                    and isinstance(value, str)):
                attrs.append({'tag': node.name, 'name': name[:80], 'value_tag': peer_tag(value)})
        for name in ('href', 'data-url', 'data-href'):
            address = node.get(name)
            if not isinstance(address, str):
                continue
            parts = urlsplit(urljoin(origin, address))
            if parts.scheme != 'https' or parts.netloc != urlsplit(origin).netloc:
                continue
            if not re.fullmatch(r'/company/\d+/(?:max|customer)/[A-Za-z0-9_-]+', parts.path):
                continue
            query = parse_qs(parts.query, keep_blank_values=True)
            links.append({'path': parts.path, 'query_keys': sorted(
                re.sub(r'[^A-Za-z0-9_\[\]-]', '?', key)[:80] for key in query
                if not re.search(r'token|csrf|secret|auth', key, re.I)),
                'peer_tag': peer_tag(_compose_url_max_user_id(address))})
    parsed = parse_history_html(markup, origin=origin, branch=branch, direction='receive', page=page)
    message = next(m for m in parsed['messages'] if m['record_id'] == record)
    return {'record_id': record, 'customers': [{key: c[key] for key in ('branch_id', 'customer_id')}
                                             for c in message['customers']],
            'parsed_peer_tag': peer_tag(message.get('max_user_id')),
            'sender_phone_tags': phone_tags(message.get('actor_name')),
            'attributes': attrs[:25], 'links': links[:20]}


def inspect_incoming(probe, stored, renew):
    branch, record = stored['source_branch'], stored['record_id']
    if not branch or not record:
        return {'status': 'source_identity_missing'}
    # Saved rank is only a hint. Report evidence only after finding the exact ID.
    result, visited = {'status': 'record_not_found', 'pages_checked': []}, set()
    head = None
    for number in range(4):
        if number == 0:
            page = 0
        else:
            size = (head or {}).get('row_count') or 20
            estimate = stored['newer_source_rows'] // size
            options = [estimate, max(0, estimate - 1), estimate + 1]
            page = next((p for p in options if p not in visited), None)
            if page is None:
                break
        visited.add(page)
        renew()
        print(f'MAX source: branch={branch} receive page={page + 1}', file=sys.stderr, flush=True)
        markup = probe.navigate(_history_url(probe.origin, branch, 'receive', page))
        parsed = parse_history_html(markup, origin=probe.origin, branch=branch, direction='receive', page=page)
        if page == 0:
            head = parsed
        result['pages_checked'].append(page + 1)
        if any(m['record_id'] == record for m in parsed['messages']):
            # Pass the parsed page number as well: default page zero would
            # otherwise reject a valid later page's active pagination marker.
            result.update(status='record_found', row=journal_row_schema(markup, record, probe.origin, branch, page))
            return result
    return result


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
def wait_for_browser(service, workspace, connection, report, *, seconds=120):
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


def inspect_card(probe, branch, customer, expected_peer, renew):
    renew()
    markup = probe.navigate(f'{probe.origin}/company/{branch}/customer/view?' + urlencode({'id': customer}))
    choices = routes(markup, probe.origin, branch, customer)
    report = {'compose_routes': len(choices), 'routes_truncated': len(choices) > 4,
              'card_peer_tags': sorted({peer_tag(peer) for address in choices
                                       if (peer := _compose_url_max_user_id(address))}), 'forms': [],
              'contact_routes': contact_route_contexts(markup, probe.origin, branch, customer)}
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


def reference_card(value):
    if not re.fullmatch(r'[1-9][0-9]{0,8}:[1-9][0-9]{0,17}', value):
        raise argparse.ArgumentTypeError('Use BRANCH:CUSTOMER')
    return tuple(map(int, value.split(':')))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference-card', action='append', type=reference_card, default=[])
    args = parser.parse_args(argv)
    if len(args.reference_card) > 2:
        parser.error('At most two reference cards')
    config = Settings()
    engine = make_engine(config)
    report = {'message_writes': 0, 'message_posts': 0, 'stage': 'load_source', 'cases': [], 'reference_cards': []}
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
                incoming = db.execute(text("SELECT m.payload,m.occurred_at FROM messages m WHERE m.workspace_id=:w AND m.conversation_id=:c "
                    "AND m.direction='in' AND m.payload->>'customer_id' IS NOT NULL "
                    "AND m.payload->>'customer_branch_id' IS NOT NULL ORDER BY " + message_order(descending=True) + " LIMIT 1"),
                    {'w': workspace, 'c': failure['conversation_id']}).mappings().one_or_none()
                if not incoming:
                    case['error_code'] = 'no_incoming_target'
                    continue
                payload = incoming['payload']
                case['stored_incoming'] = {'record_id': str(payload.get('crm_record_id') or ''),
                    'source_branch': int(payload['branch_id']) if payload.get('branch_id') else None,
                    'occurred_at': incoming['occurred_at'].isoformat(),
                    'sender_phone_tags': phone_tags(payload.get('actor_name')),
                    'peer_tag': peer_tag(payload.get('max_user_id')),
                    'hidden': bool(payload.get('superseded_by') or payload.get('crm_mirror') in (True, 'true')
                                   or payload.get('deleted') in (True, 'true'))}
                stored = case['stored_incoming']
                stored['newer_source_rows'] = db.execute(text(
                    "SELECT count(DISTINCT m.payload->>'crm_record_id') FROM messages m JOIN conversations c "
                    "ON c.workspace_id=m.workspace_id AND c.id=m.conversation_id WHERE m.workspace_id=:w "
                    "AND c.connection_id=:c AND m.direction='in' AND m.payload->>'branch_id'=:branch "
                    "AND m.occurred_at>:at"), {'w': workspace, 'c': source['connection_id'],
                    'branch': str(stored['source_branch']), 'at': incoming['occurred_at']}).scalar_one()
                contact = db.execute(text('SELECT contact_id FROM conversations WHERE workspace_id=:w AND id=:c'),
                    {'w': workspace, 'c': failure['conversation_id']}).scalar_one_or_none()
                snapshot = DirectoryReader._published(db, workspace)
                selection = card_preference.selected(db, workspace, snapshot, failure['conversation_id'], contact)
                case['display_card'] = ({key: selection.get(key) for key in ('customer', 'branch', 'reason', 'ambiguous')}
                                        if selection else None)
                case.update(branch=int(payload['customer_branch_id']), customer=int(payload['customer_id']))
                peer = None
                try:
                    target = service._send_target_in_transaction(db, workspace, source['connection_id'], failure['conversation_id'])
                    peer = target.get('max_user_id')
                    case.update(branch=target['branch'], customer=target['customer'])
                except AlfaBrowserError as error:
                    case['target_error_code'] = error.code
                case['expected_peer_tag'] = peer_tag(peer)
                targets.append((case, peer))
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
                    for case, peer in targets:
                        print(f"Проверяем MAX: branch={case['branch']} customer={case['customer']}",
                              file=sys.stderr, flush=True)
                        try:
                            case['source'] = inspect_card(probe, case['branch'], case['customer'], peer, renew)
                        except AlfaBrowserError as error:
                            case['error_code'] = error.code
                    for branch, customer in args.reference_card:
                        item = {'branch': branch, 'customer': customer}
                        report['reference_cards'].append(item)
                        try:
                            item['source'] = inspect_card(probe, branch, customer, None, renew)
                        except AlfaBrowserError as error:
                            item['error_code'] = error.code
                    report['stage'] = 'incoming_source_rows'
                    for case, _ in targets:
                        try:
                            case['incoming_source'] = inspect_incoming(probe, case['stored_incoming'], renew)
                        except AlfaBrowserError as error:
                            case['incoming_source'] = {'error_code': error.code}
                    errors = [case.get('error_code') for case in report['cases']]
                    errors += [item.get('error_code') for case in report['cases']
                               for item in case.get('source', {}).get('forms', [])]
                    errors += [case.get('incoming_source', {}).get('error_code') for case in report['cases']]
                    errors += [item.get('error_code') for item in report['reference_cards']]
                    errors += [form.get('error_code') for item in report['reference_cards']
                               for form in item.get('source', {}).get('forms', [])]
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
