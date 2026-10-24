"""Optional real Chromium check; all CRM pages are synthetic and intercepted."""
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app/backend/src'))
from playwright.sync_api import sync_playwright
from center_messenger.alfa_max_browser import AlfaMaxBrowser
from center_messenger.history_audit import compare_source_history


def main():
    executable = os.environ.get('CM_TEST_CHROMIUM_EXECUTABLE')
    if not executable:
        raise SystemExit('Set CM_TEST_CHROMIUM_EXECUTABLE to a local test Chromium binary')
    outcomes = []
    for persistent in (False, True):
        counts = {}
        def intercept(route):
            url = urlsplit(route.request.url)
            assert url.hostname == 'test.s20.online', 'No external requests are permitted'
            assert url.path == '/company/4/max/sent'
            number = int(parse_qs(url.query).get('page', ['1'])[0])
            counts[number] = counts.get(number, 0) + 1
            if number == 20 and (persistent or counts[number] == 1):
                route.abort('connectionreset')
                return
            first, last = (number-1)*20+1, min(number*20, 684)
            rows = ''.join(
                f'<tr data-key="{n}"><td></td><td><a href="/company/4/customer/view?id=7">Synthetic child</a></td>'
                f'<td>Operator</td><td>Synthetic message {n}</td>'
                '<td><time datetime="2026-10-24T00:00:00Z">00:00 24.10.2026</time></td></tr>'
                for n in range(first,last+1))
            html = f'<div class="summary">{first}–{last} из 684</div><table class="crm-table"><tbody>{rows}</tbody></table>'
            route.fulfill(status=200, content_type='text/html', body=html)

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

        with patch.object(AlfaMaxBrowser, '_playwright', return_value=lambda: SimpleNamespace(start=Manager)), \
             patch.object(AlfaMaxBrowser, '_route_factory', return_value=intercept), \
             patch('center_messenger.alfa_max_browser._public_dns'):
            report = compare_source_history(AlfaMaxBrowser(), 'https://test.s20.online', {}, 4, 'sent',
                                            {str(n) for n in range(1, 676)})
        if persistent:
            assert report['status'] == 'failed', report
            assert report['failed_page'] == 20, report
            assert report['pages_checked'] == 19 and report['observed_rows'] == 380, report
            assert report['attempts'] == counts[20] == 3, report
            assert report['network_error'] == 'ERR_CONNECTION_RESET', report
            assert 'missing_in_database' not in report, report
        else:
            assert report['status'] == 'complete' and report['pages_checked'] == 35, report
            assert report['observed_rows'] == report['distinct_source_rows'] == 684, report
            assert report['missing_in_database'] == 9, report
            assert set(report['missing_ids']) == {str(n) for n in range(676, 685)}, report
            assert report['duplicate_source_count'] == 0, report
            assert counts[20] == 2, counts
        outcomes.append({'scenario':'persistent' if persistent else 'transient', 'page20_requests':counts[20], 'report':report})
    print(json.dumps(outcomes,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
