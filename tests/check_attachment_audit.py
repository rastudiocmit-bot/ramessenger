"""Real Chromium audit check against intercepted synthetic CRM pages only."""
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app/backend/src'))
from playwright.sync_api import sync_playwright
from center_messenger.alfa_max_browser import AlfaMaxBrowser
from center_messenger.attachment_audit import BrowserProbe, audit_browser


def main():
    executable = os.environ.get('CM_TEST_CHROMIUM_EXECUTABLE')
    if not executable:
        raise SystemExit('CM_TEST_CHROMIUM_EXECUTABLE required')
    now = datetime.now(UTC)
    requested = []
    blocked = []
    origin = 'https://test.s20.online'
    def link(name):
        return f'<a href="/uploads/storage/{name}"><i class="ion-android-attach">Synthetic</i></a>'
    rows = []
    for number, name in enumerate(['image.jpg', 'document.pdf', 'old.jpg', 'login.jpg', 'redirect.jpg', 'large.jpg'], 1):
        stamp = now - timedelta(hours=73 if name == 'old.jpg' else 1)
        rows.append(f'<tr data-key="{number}"><td></td><td></td><td>Sender</td><td>{link(name)}</td>'
                    f'<td><time datetime="{stamp.isoformat()}"></time></td></tr>')
    markup = '<table class="crm-table"><tbody>' + ''.join(rows) + '</tbody></table>'
    markup += ('<img src="/uploads/storage/old.jpg"><script src="/tracker.js"></script>'
               '<script>fetch("/mutate", {method:"POST",body:"blocked"});</script>')
    compose = ('<form action="/company/1/max/bulk?related_class=Customer&amp;related_ids%5B%5D=42&amp;maxUserId=77" '
               'method="post" enctype="multipart/form-data">'
               '<input name="_csrf" value="synthetic"><input name="MaxComposeBulkForm[related_ids][]" value="42">'
               '<input type="file" name="MaxComposeBulkForm[attachment][]" multiple>'
               '<textarea name="MaxComposeBulkForm[text]"></textarea></form>'
               '<script>let maxFileSize=0;fileCountError=value.files.length>10;'
               'if(totalAttachmentSize>8388608){}</script>')
    def intercept(route):
        request = route.request
        path = urlsplit(request.url).path
        assert request.method == 'GET', 'Mutation escaped the GET-only gate'
        assert urlsplit(request.url).hostname == 'test.s20.online'
        requested.append(path)
        if path == '/company/1/max/receive':
            route.fulfill(status=200, content_type='text/html', body=markup)
        elif path == '/company/1/customer/view':
            route.fulfill(status=200, content_type='text/html', body=(
                '<a href="/company/1/max/bulk?related_class=Customer&amp;related_ids%5B%5D=42&amp;maxUserId=77">MAX</a>'
                '<div class="feed-element"><small class="text-muted">'
                + (now - timedelta(hours=1)).astimezone().strftime('%Y-%m-%dT%H:%M:%S%z')
                + '</small><div class="m-t-sm">' + link('image.jpg') + '</div></div>'))
        elif path == '/company/1/max/bulk':
            route.fulfill(status=200, content_type='application/json', body=json.dumps({'form': compose}))
        elif path == '/uploads/storage/image.jpg':
            route.fulfill(status=200, content_type='image/jpeg', body=b'\xff\xd8\xff' + b'x' * 100 + b'\xff\xd9')
        elif path == '/uploads/storage/document.pdf':
            route.fulfill(status=200, content_type='application/pdf', body=b'%PDF-1.7\nSynthetic\n%%EOF')
        elif path == '/uploads/storage/login.jpg':
            route.fulfill(status=200, content_type='image/jpeg', body='<!doctype html><html>Login</html>')
        elif path == '/uploads/storage/redirect.jpg':
            route.fulfill(status=302, headers={'Location': origin + '/login'}, body='')
        elif path == '/uploads/storage/large.jpg':
            route.fulfill(status=200, content_type='image/jpeg', body=b'\xff\xd8\xff' + b'x' * (17 * 1024 * 1024))
        else:
            raise AssertionError('Unapproved request escaped probe route: ' + path)

    class Manager:
        def __init__(self):
            self.native = sync_playwright().start()
            self.chromium = SimpleNamespace(launch=self.launch)
        def launch(self, **options):
            options['executable_path'] = executable
            options['args'] += json.loads(os.environ.get('CM_TEST_CHROMIUM_ARGS', '[]'))
            return self.native.chromium.launch(**options)
        def stop(self):
            self.native.stop()

    class RecordingProbe(BrowserProbe):
        def route(self, route):
            request = route.request
            if request.method != 'GET' or request.url not in self.allowed or request.resource_type not in {'document', 'fetch'}:
                blocked.append({'method': request.method, 'kind': request.resource_type})
            return super().route(route)

    with patch.object(AlfaMaxBrowser, '_playwright', return_value=lambda: SimpleNamespace(start=Manager)), \
         patch.object(AlfaMaxBrowser, '_route_factory', return_value=intercept), \
         patch('center_messenger.alfa_max_browser._public_dns'):
        with RecordingProbe(origin, {}) as probe:
            report = audit_browser(probe, [1], direction='receive', pages=1, downloads=10, customer=42)
    assert report['image_download_verified'] and report['document_download_verified'], report
    assert report['download_verified_count'] == 2, report
    assert not report['delivery_verified'], report
    assert report['skipped_not_recent'] == 1, report
    codes = {item.get('error_code') for item in report['downloads']}
    assert {'html_instead_of_file', 'network_or_redirect_blocked', 'probe_size_limit'} <= codes, report
    assert report['customer_inspection']['compose']['max_files_in_script'] == 10, report
    assert report['customer_inspection']['compose']['max_total_bytes_in_script'] == 8_388_608, report
    assert report['customer_inspection']['feed_candidates'] == 1, report
    assert any(item['kind'] == 'image' for item in blocked), blocked
    assert any(item['method'] == 'POST' for item in blocked), blocked
    assert '/uploads/storage/old.jpg' not in requested
    assert '/login' not in requested and '/mutate' not in requested
    assert requested.count('/uploads/storage/image.jpg') == 1
    print(json.dumps({'synthetic_browser_check': 'passed', 'requests': len(requested),
                      'blocked_requests': blocked, 'report': report}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
