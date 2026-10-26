"""Read-only native-browser probe: python -m center_messenger.attachment_audit --progress.

Samples recent journal attachments using the existing encrypted CRM session.
No API tokens, sends, history resets or persistent file writes. Uses the shared
browser lease and preserves renewed cookies.
"""

import argparse
import json
import re
import sys
import time
from datetime import UTC, datetime
from urllib.parse import urlencode
from uuid import UUID

from bs4 import BeautifulSoup
from sqlalchemy import text

from center_messenger.alfa_client import normalize_origin
from center_messenger.alfa_max_browser import (
    AlfaBrowserError, AlfaMaxBrowser, _customer_card_compose_url, _history_url,
    _json_envelope, _validated_compose_url, parse_compose_form, parse_history_html,
)
from center_messenger.attachment_probe import (
    MAX_HTML_BYTES, extract_attachment_candidates, recent_message, storage_url,
)
from center_messenger.config import Settings
from center_messenger.connector_vault import ConnectorVault
from center_messenger.database import make_engine

# Probe resource ceiling, NOT a discovered incoming transport limit.
PROBE_FILE_BYTES = 16 * 1024 * 1024

# Consume in Chromium with a fixed size/time ceiling. Only headers and a small
# signature leave the browser for binary files; no base64 original is returned.
READ_RESPONSE = r"""async ({url, maxBytes, html, ajax}) => {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 15000);
  let status = null;
  try {
    const response = await fetch(url, {
      method: 'GET', credentials: 'include', redirect: 'error', cache: 'no-store',
      headers: ajax ? {'X-Requested-With': 'XMLHttpRequest'} : {},
      signal: controller.signal
    });
    status = response.status;
    if (status !== 200) return {status, error: 'http_error'};
    const declared = response.headers.get('content-length');
    const length = /^\d+$/.test(declared || '') ? Number(declared) : null;
    if (length !== null && length > maxBytes) {
      controller.abort(); return {status, error: 'probe_size_limit'};
    }
    const reader = response.body?.getReader();
    if (!reader) return {status, error: 'empty_response'};
    let size = 0, prefix = [], chunks = [];
    while (true) {
      const {value, done} = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > maxBytes) {
        controller.abort(); return {status, error: 'probe_size_limit'};
      }
      for (let i = 0; i < value.length && prefix.length < 512; i++) prefix.push(value[i]);
      if (html) chunks.push(value);
    }
    if (!response.headers.get('content-encoding') && length !== null && length !== size)
      return {status, error: 'incomplete_response'};
    const result = {status, bytes: size, prefix,
      mime: (response.headers.get('content-type') || '').split(';')[0].trim().toLowerCase(),
      complete: true};
    if (html) {
      const bytes = new Uint8Array(size); let offset = 0;
      for (const chunk of chunks) {bytes.set(chunk, offset); offset += chunk.length;}
      result.text = new TextDecoder().decode(bytes);
    }
    return result;
  } catch (_) {return {status, error: controller.signal.aborted ? 'timeout' : 'network_or_redirect_blocked'};}
  finally {clearTimeout(timer);}
}"""


def signature_type(prefix):
    if prefix.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if prefix.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if prefix.startswith((b'GIF87a', b'GIF89a')):
        return 'image/gif'
    if prefix[:4] == b'RIFF' and prefix[8:12] == b'WEBP':
        return 'image/webp'
    if prefix.startswith(b'%PDF-'):
        return 'application/pdf'
    if prefix.startswith(b'PK\x03\x04'):
        return 'application/zip'
    return None


def file_report(result):
    """Report full transport consumption and magic bytes, not semantic validity."""
    status = result.get('status')
    report = {'http_status': status if type(status) is int and 100 <= status <= 599 else None,
              'download_verified': False}
    allowed = {'http_error', 'probe_size_limit', 'empty_response', 'incomplete_response',
               'timeout', 'network_or_redirect_blocked'}
    if result.get('error'):
        report['error_code'] = result['error'] if result['error'] in allowed else 'invalid_response'
        return report
    prefix = result.get('prefix', [])
    size = result.get('bytes')
    if (not isinstance(prefix, list) or len(prefix) > 512
            or any(type(n) is not int or not 0 <= n <= 255 for n in prefix)
            or type(size) is not int or not 0 < size <= PROBE_FILE_BYTES):
        report['error_code'] = 'invalid_response'
        return report
    prefix = bytes(prefix)
    mime = result.get('mime', '')
    mime = mime if isinstance(mime, str) and re.fullmatch(r'[a-z0-9.+-]+/[a-z0-9.+-]+', mime) else ''
    actual = signature_type(prefix)
    if mime in {'text/html', 'application/xhtml+xml'} or prefix.lstrip(b'\xef\xbb\xbf \r\n\t').lower().startswith((b'<!doctype html', b'<html', b'<form')):
        report['error_code'] = 'html_instead_of_file'
        return report
    report.update(bytes=size, declared_type=mime or None, signature_type=actual,
                  declared_type_matches_signature=mime == actual if mime and actual else None,
                  body_complete=result.get('complete') is True)
    report['download_verified'] = status == 200 and result.get('complete') is True and actual is not None
    if not report['download_verified']:
        report['error_code'] = 'unsupported_signature' if actual is None else 'incomplete_response'
    # ZIP signature proves a ZIP container, never a particular Office document.
    return report


def compose_report(markup):
    """Inspect native controls without executing scripts or submitting the form."""
    soup = BeautifulSoup(_json_envelope(markup).get('html') or '', 'html.parser')
    field = soup.select_one('input[type="file"][name="MaxComposeBulkForm[attachment][]"]')
    form = field.find_parent('form') if field else None
    scripts = '\n'.join(node.get_text() for node in soup.select('script'))
    def unique_number(pattern):
        values = set(re.findall(pattern, scripts))
        return int(next(iter(values))) if len(values) == 1 else None
    return {'file_input': field is not None,
            'multiple': bool(field and field.has_attr('multiple')),
            'multipart': bool(form and form.get('enctype', '').lower() == 'multipart/form-data'),
            'max_files_in_script': unique_number(r'files\.length\s*>\s*(\d+)'),
            'max_total_bytes_in_script': unique_number(r'totalAttachmentSize\s*>\s*(\d+)'),
            'max_file_bytes_variable': unique_number(r'maxFileSize\s*=\s*(\d+)'),
            'delivery_verified': False}


class BrowserProbe:
    """GET-only isolated browser, no automatic images, scripts or redirects."""
    def __init__(self, origin, state):
        self.origin, self.state = normalize_origin(origin), state
        self.manager = self.browser = self.context = self.page = None
        self.allowed = set()
        self.guard = AlfaMaxBrowser._route_factory(self.origin)

    def route(self, route):
        request = route.request
        if (request.method != 'GET' or request.url not in self.allowed
                or request.resource_type not in {'document', 'fetch'}):
            route.abort()
            return
        self.guard(route)

    def __enter__(self):
        self.manager, self.browser, self.context, self.page = AlfaMaxBrowser._open(self.origin, self.state)
        try:
            self.context.route('**/*', self.route)
        except Exception:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *_):
        AlfaMaxBrowser._close(self.manager, self.browser, self.context)

    def navigate(self, address):
        self.allowed = {address}
        try:
            response = self.page.goto(address, wait_until='domcontentloaded', timeout=15000)
            AlfaMaxBrowser._check_origin(self.page, self.origin)
            if response is None or response.status != 200:
                raise AlfaBrowserError('crm_unavailable', http_status=response.status if response else None)
            markup = response.text()
            if len(markup.encode('utf-8')) > MAX_HTML_BYTES:
                raise AlfaBrowserError('invalid_response')
            if BeautifulSoup(markup, 'html.parser').select_one('input[type="password"]'):
                raise AlfaBrowserError('reauth_required')
            return markup
        except AlfaBrowserError:
            raise
        except Exception:
            raise AlfaBrowserError('crm_unavailable') from None
        finally:
            self.allowed.clear()

    def fetch(self, address, *, html=False, ajax=False):
        if not html and storage_url(address, origin=self.origin) != address:
            raise AlfaBrowserError('unsafe_address')
        self.allowed = {address}
        try:
            return self.page.evaluate(READ_RESPONSE, {'url': address, 'html': html, 'ajax': ajax,
                'maxBytes': MAX_HTML_BYTES if html else PROBE_FILE_BYTES})
        finally:
            self.allowed.clear()

    def inspect_customer(self, branch, customer):
        markup = self.navigate(f'{self.origin}/company/{branch}/customer/view?' + urlencode({'id': customer}))
        feed = extract_attachment_candidates(markup, origin=self.origin)
        address = _customer_card_compose_url(markup, origin=self.origin, branch=branch, customer=customer)
        address = address or f'{self.origin}/company/{branch}/max/bulk?' + urlencode(
            [('related_class', 'Customer'), ('related_ids[]', customer)])
        visited = set()
        for _ in range(4):
            address = _validated_compose_url(address, origin=self.origin, branch=branch, customer=customer)
            if address in visited:
                raise AlfaBrowserError('form_unavailable')
            visited.add(address)
            result = self.fetch(address, html=True, ajax=True)
            if result.get('error'):
                raise AlfaBrowserError('form_unavailable')
            body = result.get('text', '')
            if _json_envelope(body).get('empty'):
                result = self.fetch(address, html=True)
                if result.get('error'):
                    raise AlfaBrowserError('form_unavailable')
                body = result.get('text', '')
            parsed = parse_compose_form(body, origin=self.origin, branch=branch, customer=customer)
            if 'compose_url' not in parsed:
                return {'compose': compose_report(body), 'feed_initial_html_only': True,
                        'feed_candidates': len(feed)}, feed
            address = parsed['compose_url']
        raise AlfaBrowserError('form_unavailable')


def safe_error(error):
    allowed = {'browser_unavailable', 'crm_unavailable', 'reauth_required', 'unsafe_address',
               'network', 'invalid_response', 'form_unavailable', 'multiple_recipients',
               'invalid_recipient', 'recipient_missing', 'csrf_missing', 'redirect_blocked',
               'workspace_required', 'workspace_unavailable', 'branch_unavailable', 'source_unavailable', 'browser_busy', 'settings_changed'}
    code = getattr(error, 'code', None)
    if isinstance(error, ValueError) and str(error) in allowed:
        code = str(error)
    return {'error_code': code if code in allowed else 'probe_failed'}


def audit_browser(probe, branches, *, direction=None, pages=5, downloads=4, customer=None,
                  now=None, on_progress=None, seconds=180):
    started, now = time.monotonic(), now or datetime.now(UTC)
    report = {'status': 'bounded_sample', 'read_only': True, 'window_hours': 72,
              'probe_file_limit_bytes': PROBE_FILE_BYTES, 'streams': [], 'downloads': [],
              'skipped_not_recent': 0, 'delivery_verified': False}
    seen, attempted = set(), 0
    def sample(candidates, branch, kind):
        nonlocal attempted
        for candidate in candidates:
            # Apply before any request, including retry at the moment of fetching.
            check_now = max(now, datetime.now(UTC))
            if not recent_message(candidate['occurred_at'], now=check_now):
                report['skipped_not_recent'] += 1
                continue
            if (candidate['url'] in seen or attempted >= downloads
                    or time.monotonic() - started >= seconds):
                continue
            seen.add(candidate['url'])
            attempted += 1
            if on_progress:
                on_progress({'stage': 'download', 'branch': branch, 'direction': kind, 'number': attempted})
            try:
                result = file_report(probe.fetch(candidate['url']))
            except Exception as error:
                result = {'download_verified': False, **safe_error(error)}
            report['downloads'].append({'branch_id': branch, 'direction': kind,
                'layout': candidate['source_layout'], **result})
    active = {(branch, kind): {'branch_id': branch, 'direction': kind, 'pages_checked': 0,
              'candidates': 0, 'stopped': None} for branch in branches
              for kind in ([direction] if direction else ['receive', 'sent'])}
    # Round-robin streams: a large outgoing journal must not consume the budget
    # before the other branches' incoming samples can be inspected.
    for number in range(pages):
        for (branch, kind), stream in active.items():
            if stream['stopped']:
                continue
            if time.monotonic() - started >= seconds:
                stream['stopped'] = 'time_limit'
                continue
            if on_progress:
                on_progress({'stage': 'journal', 'branch': branch, 'direction': kind, 'number': number + 1})
            try:
                markup = probe.navigate(_history_url(probe.origin, branch, kind, number))
                parsed = parse_history_html(markup, origin=probe.origin, branch=branch, direction=kind, page=number)
                candidates = extract_attachment_candidates(markup, origin=probe.origin)
                stream['pages_checked'] += 1
                stream['candidates'] += len(candidates)
                sample(candidates, branch, kind)
                if not parsed['has_more']:
                    stream['stopped'] = 'journal_end'
                # No early date stop: journal ordering has not been established.
            except Exception as error:
                stream.update(safe_error(error), stopped='error')
    report['streams'] = list(active.values())
    for stream in report['streams']:
        stream['stopped'] = stream['stopped'] or 'page_limit'
    if customer is not None and time.monotonic() - started < seconds:
        try:
            result, feed = probe.inspect_customer(branches[0], customer)
            report['customer_inspection'] = result
            sample(feed, branches[0], 'feed_direction_unverified')
        except Exception as error:
            report['customer_inspection'] = safe_error(error)
    report['download_verified_count'] = sum(item.get('download_verified') is True for item in report['downloads'])
    report['image_download_verified'] = any(item.get('download_verified') and
        (item.get('signature_type') or '').startswith('image/') for item in report['downloads'])
    report['document_download_verified'] = any(item.get('download_verified') and
        item.get('signature_type') == 'application/pdf' for item in report['downloads'])
    report['elapsed_seconds'] = round(time.monotonic() - started, 1)
    return report


def load_source(engine, workspace=None, branch=None):
    """Release a read-only DB transaction before any navigation."""
    with engine.begin() as db:
        db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
        workspaces = db.execute(text('SELECT workspace_id FROM alfa_max_sync_queue ORDER BY workspace_id')).scalars().all()
        if workspace is None:
            if len(workspaces) != 1:
                raise ValueError('workspace_required')
            workspace = workspaces[0]
        if workspace not in workspaces:
            raise ValueError('workspace_unavailable')
        db.execute(text("SELECT set_config('app.workspace_id',:w,true)"), {'w': str(workspace)})
        source = db.execute(text(
            'SELECT m.connection_id,m.crm_connection_id,m.session_ciphertext,a.origin '
            'FROM alfa_max_settings m JOIN alfa_settings a ON a.workspace_id=m.workspace_id '
            'AND a.connection_id=m.crm_connection_id WHERE m.workspace_id=:w'
        ), {'w': workspace}).mappings().one()
        branches = db.execute(text(
            'SELECT external_id FROM alfa_branches WHERE workspace_id=:w '
            'AND connection_id=:c AND available ORDER BY external_id'
        ), {'w': workspace, 'c': source['crm_connection_id']}).scalars().all()
        if branch is not None:
            if branch not in branches:
                raise ValueError('branch_unavailable')
            branches = [branch]
        if not branches or not source['session_ciphertext']:
            raise ValueError('source_unavailable')
        return workspace, dict(source), branches


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=UUID)
    parser.add_argument('--branch', type=int)
    parser.add_argument('--direction', choices=('receive', 'sent'))
    parser.add_argument('--pages', type=int, default=5, help='Maximum pages per stream, 1..20')
    parser.add_argument('--downloads', type=int, default=4, help='Maximum sample downloads, 1..10')
    parser.add_argument('--customer', type=int, help='Optional exact CRM customer ID; requires --branch; GET only')
    parser.add_argument('--progress', action='store_true')
    args = parser.parse_args(argv)
    if not 1 <= args.pages <= 20 or not 1 <= args.downloads <= 10:
        parser.error('pages must be 1..20; downloads must be 1..10')
    if ((args.branch is not None and not 1 <= args.branch <= 2**63 - 1)
            or (args.customer is not None and (args.branch is None or not 1 <= args.customer <= 2**63 - 1))):
        parser.error('customer requires --branch; IDs must be positive')
    engine = None
    try:
        config = Settings()
        engine = make_engine(config)
        workspace, source, branches = load_source(engine, args.workspace, args.branch)
        from center_messenger.accounts import Accounts
        from center_messenger.alfa_service import AlfaConnections
        from center_messenger.alfa_max import AlfaMaxConnections
        from center_messenger.alfa_session import control
        vault = ConnectorVault(config.connection_master_key_file)
        service = AlfaMaxConnections(AlfaConnections(Accounts(engine, config), vault))
        with service.sessions.operation(workspace, source['connection_id']) as operation:
            current = operation['row']
            if current['state'] != 'active' or control(current).get('needs_check'):
                raise AlfaBrowserError('browser_busy')
            state = vault.open_browser_state(workspace, source['connection_id'], current['session_ciphertext'])
            def progress(value):
                with service.accounts.transaction(workspace_id=workspace) as db:
                    service.sessions.renew(db, operation)
                if args.progress:
                    print(f"Attachment probe: {value['stage']} branch={value['branch']} "
                          f"direction={value['direction']} number={value['number']}", file=sys.stderr, flush=True)
            with BrowserProbe(source['origin'], state) as probe:
                report = audit_browser(probe, branches, direction=args.direction, pages=args.pages,
                    downloads=args.downloads, customer=args.customer, on_progress=progress)
                if report['streams'] and all(not item.get('error_code') for item in report['streams']) and not report.get('customer_inspection', {}).get('error_code'):
                    service.sessions.finish(operation, probe.context.storage_state(), state='active')
        print(json.dumps(report, ensure_ascii=False, indent=2))
        # 2 means inconclusive/partial; never interpret a no-sample run as success.
        errors = any(stream.get('error_code') for stream in report['streams']) or any(
            not item.get('download_verified') for item in report['downloads'])
        return 0 if report['image_download_verified'] and report['document_download_verified'] and not errors else 2
    except Exception as error:
        print(json.dumps({'status': 'failed', **safe_error(error)}, ensure_ascii=False))
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == '__main__':
    raise SystemExit(main())
