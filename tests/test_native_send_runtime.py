"""Real child-process failures; no CRM, browser, credentials or customer sends."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app/backend/src'))
from center_messenger.alfa_max_browser import AlfaBrowserError, AlfaMaxBrowser
from center_messenger.native_send_runtime import run_send, _processes, runtime_supported, main as runtime_main

PREFIX = '''import sys,json,time,subprocess,os
data=json.loads(sys.stdin.readline())
def emit(value):
 print(json.dumps(value),flush=True)
def submit():
 emit({'event':'before_post'})
 return json.loads(sys.stdin.readline())
'''


@unittest.skipUnless(runtime_supported(), 'Linux with a matching /proc PID namespace required')
class NativeSendRuntimeTests(unittest.TestCase):
    def run_child(self, code, **options):
        return run_send('https://test.s20.online', {'cookies': [], '_cm_login': {'password': 'not-for-child'}},
                        1, 42, 'Synthetic reply', command=[sys.executable, '-c', PREFIX + code],
                        budget=options.pop('budget', .8), **options)

    def test_child_diagnostics_reach_the_server_log(self):
        # The child's stderr is discarded; its sanitised notes ride on the error event.
        note = 'AlfaCRM MAX compose form rejected phase=parse_compose_form reason=form_unavailable status=200'
        with self.assertLogs('center_messenger.native_send_runtime', 'WARNING') as logs, self.assertRaises(AlfaBrowserError) as raised:
            self.run_child("emit({'event':'error','code':'form_unavailable','diagnostics':[%r]})" % note)
        self.assertEqual(raised.exception.code, 'form_unavailable')
        joined = '\n'.join(logs.output)
        self.assertIn('Native send diagnostic: ' + note, joined)
        self.assertIn('Native send failed code=form_unavailable post_started=False', joined)

    def test_hung_preparation_is_bounded_and_never_requests_post(self):
        before = Mock()
        started = time.monotonic()
        with self.assertRaises(AlfaBrowserError) as raised:
            self.run_child('time.sleep(60)', before_post=before)
        self.assertEqual(raised.exception.code, 'send_prepare_timeout')
        before.assert_not_called()
        self.assertLess(time.monotonic() - started, 3)

    def test_hung_post_is_unknown_and_next_operation_can_run(self):
        before = Mock()
        with self.assertRaises(AlfaBrowserError) as raised:
            self.run_child('submit(); time.sleep(60)', before_post=before)
        self.assertEqual(raised.exception.code, 'send_unknown')
        before.assert_called_once()
        result = self.run_child("submit(); emit({'event':'accepted','result':{'accepted':True}})")
        self.assertTrue(result['accepted'])

    def test_accepted_survives_hung_cookie_export_and_persists_receipt_first(self):
        accepted = Mock()
        result = self.run_child("submit(); emit({'event':'accepted','result':{'accepted':True}}); time.sleep(60)", on_accepted=accepted)
        self.assertTrue(result['accepted'])
        accepted.assert_called_once_with({'accepted': True})

    def test_receipt_callback_failure_keeps_result_for_second_persistence_attempt(self):
        result = self.run_child("submit(); emit({'event':'accepted','result':{'accepted':True}})",
                                on_accepted=Mock(side_effect=RuntimeError('synthetic database interruption')))
        self.assertTrue(result['accepted'])

    def test_revoked_permission_never_releases_child_to_submit(self):
        with tempfile.TemporaryDirectory() as root:
            marker = str(Path(root) / 'submitted')
            with self.assertRaises(AlfaBrowserError) as raised:
                self.run_child(f"submit(); open({marker!r},'w').write('sent')", before_post=Mock(side_effect=AlfaBrowserError('access_changed')))
            self.assertEqual(raised.exception.code, 'access_changed')
            self.assertFalse(Path(marker).exists())

    def test_wrong_failure_code_after_post_is_conservatively_unknown(self):
        with self.assertRaises(AlfaBrowserError) as raised:
            self.run_child("submit(); emit({'event':'error','code':'send_prepare_failed'})")
        self.assertEqual(raised.exception.code, 'send_unknown')

    def test_explicit_rejection_is_failed_not_replayed(self):
        with self.assertRaises(AlfaBrowserError) as raised:
            self.run_child("submit(); emit({'event':'error','code':'crm_rejected'}); time.sleep(60)")
        self.assertEqual(raised.exception.code, 'crm_rejected')

    def test_unrelated_and_detached_child_processes_are_distinguished(self):
        import subprocess
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'])
        try:
            with tempfile.TemporaryDirectory() as root:
                marker = Path(root) / 'child'
                code = ("child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],start_new_session=True);"
                        f"open({str(marker)!r},'w').write(str(child.pid)+','+str(os.getpid()));time.sleep(60)")
                with self.assertRaises(AlfaBrowserError):
                    self.run_child(code)
                child, parent = map(int, marker.read_text().split(','))
                self.assertNotIn(parent, _processes(), 'The sender itself must be killed and reaped')
                state = _processes().get(child)
                self.assertTrue(state is None or state[2] == 'Z')
                self.assertIsNone(unrelated.poll())
        finally:
            unrelated.kill(); unrelated.wait()

    def test_stop_during_preparation_does_not_submit(self):
        stopping = threading.Event()
        timer = threading.Timer(.2, stopping.set)
        timer.start()
        try:
            before = Mock()
            with self.assertRaises(AlfaBrowserError) as raised:
                self.run_child('time.sleep(60)', stopping=stopping, before_post=before, budget=2)
            self.assertEqual(raised.exception.code, 'send_prepare_timeout')
            before.assert_not_called()
        finally:
            timer.cancel()

    def test_child_receives_no_saved_login_secret_and_post_has_a_timeout(self):
        result = self.run_child("assert '_cm_login' not in data['state']; answer=submit(); assert 0<answer['timeout_ms']<=20000; emit({'event':'accepted','result':{'accepted':True}})")
        self.assertTrue(result['accepted'])


class InlineSendTests(unittest.TestCase):
    def setUp(self):
        self.page, self.context = Mock(), Mock()
        self.page.url = 'https://test.s20.online/company/1/customer/view?id=42'
        self.bulk = 'https://test.s20.online/company/1/max/bulk?related_class=Customer&related_ids%5B%5D=42&maxUserId=77'
        self.form = ('<form action="' + self.bulk.replace('&', '&amp;') + '"><input name="_csrf" value="synthetic">'
                     '<input name="MaxComposeBulkForm[related_ids][]" value="42"><textarea name="MaxComposeBulkForm[text]"></textarea></form>')
        self.page.goto.return_value = SimpleNamespace(status=200)
        self.page.content.return_value = '<a href="' + self.bulk.replace('&', '&amp;') + '">MAX</a>'
        self.page.evaluate.side_effect = [self.reply(self.form), self.reply('{"success":true}')]
        self.context.storage_state.return_value = {'cookies': [], 'origins': []}
        opened = patch.object(AlfaMaxBrowser, '_open', return_value=(Mock(), Mock(), self.context, self.page))
        opened.start()
        self.addCleanup(opened.stop)

    def reply(self, body):
        return {'status': 200, 'url': self.bulk, 'text': body}

    def send(self, **options):
        return AlfaMaxBrowser()._send_inline('https://test.s20.online', {'cookies': []}, 1, 42, 'Synthetic', **options)

    def test_revocation_after_form_load_performs_no_post(self):
        with self.assertRaises(AlfaBrowserError) as raised:
            self.send(before_post=Mock(side_effect=AlfaBrowserError('access_changed')))
        self.assertEqual(raised.exception.code, 'access_changed')
        self.assertEqual(self.page.evaluate.call_count, 1)

    def test_acceptance_is_reported_before_cookie_export(self):
        seen = []
        self.context.storage_state.side_effect = lambda: seen.append('cookies') or {'cookies': []}
        self.send(before_post=lambda: seen.append('intent') or 1000,
                  on_accepted=lambda receipt: seen.append('accepted'))
        self.assertEqual(seen[:3], ['intent', 'accepted', 'cookies'])
        self.assertLessEqual(self.page.evaluate.call_args.args[1]['timeoutMs'], 1000)

    def test_preparation_abort_has_a_safe_retry_code(self):
        self.page.evaluate.side_effect = [{'timed_out': True}]
        with self.assertRaises(AlfaBrowserError) as raised:
            self.send()
        self.assertEqual(raised.exception.code, 'send_prepare_timeout')

    def test_explicit_rejection_is_reported_before_cookie_export(self):
        seen = []
        self.page.evaluate.side_effect = [self.reply(self.form), self.reply('{"errors":["Synthetic refusal"]}')]
        self.context.storage_state.side_effect = lambda: seen.append('cookies') or {'cookies': []}
        with self.assertRaises(AlfaBrowserError) as raised:
            self.send(on_rejected=lambda code: seen.append(code))
        self.assertEqual(raised.exception.code, 'crm_rejected')
        self.assertEqual(seen[:2], ['crm_rejected', 'cookies'])

    def test_post_script_actually_aborts_a_stalled_fetch(self):
        from playwright.sync_api import sync_playwright
        self.send()
        script = self.page.evaluate.call_args.args[0]
        # Execute the exact production JS in the Chromium shipped with the
        # application. A synthetic fetch never touches the network. The test
        # used to depend on a system `node`, absent from the production image.
        program = ("async () => {const originalFetch=globalThis.fetch;let timer;"
                   "globalThis.fetch=(url,options)=>new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>reject(new Error('aborted'))));"
                   "try {const send=" + script + ";return await Promise.race(["
                   "send({url:'https://example.test',fields:[],timeoutMs:5}).then(()=> 'unexpected_success',e=>e.message),"
                   "new Promise(resolve=>{timer=setTimeout(()=>resolve('test_deadline'),1000)})]);"
                   "} finally {clearTimeout(timer);globalThis.fetch=originalFetch;}}")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                self.assertEqual(page.evaluate(program), 'aborted')
            finally:
                browser.close()

    def test_supervised_child_forwards_expected_dialog_peer(self):
        from center_messenger import native_send_runtime
        data = {'origin': 'https://test.s20.online', 'state': {'cookies': []},
                'branch': 1, 'customer': 42, 'text': 'Synthetic',
                'deadline': time.monotonic() + 30, 'expected_peer': '77', 'expected_phone': '+79161234567'}
        stdin = SimpleNamespace(buffer=io.BytesIO((json.dumps(data) + '\n').encode()))
        with patch.object(sys, 'stdin', stdin), patch.object(sys, 'stdout', io.StringIO()), \
                patch.object(AlfaMaxBrowser, '_send_inline', return_value={'accepted': True}) as send:
            native_send_runtime.main()
        self.assertEqual(send.call_args.kwargs['expected_peer'], '77')
        self.assertEqual(send.call_args.kwargs['expected_phone'], '+79161234567')

    def test_mismatched_proc_namespace_fails_before_starting_browser(self):
        with patch('center_messenger.native_send_runtime.runtime_supported', return_value=False), \
                patch('center_messenger.native_send_runtime.subprocess.Popen') as start:
            with self.assertRaises(AlfaBrowserError) as raised:
                run_send('https://test.s20.online', {}, 1, 42, 'Synthetic')
            self.assertEqual(raised.exception.code, 'browser_unavailable')
            start.assert_not_called()


if __name__ == '__main__':
    unittest.main()
