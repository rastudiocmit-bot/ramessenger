"""Bounded host diagnostics must never send, mutate queues or disclose SQL/PII."""
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location('max_runtime_diagnostics',
    Path(__file__).resolve().parents[1] / 'scripts/max_runtime_diagnostics.py')
diagnostics = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(diagnostics)


class DiagnosticTests(unittest.TestCase):
    def report(self):
        return {'checked_at': '2026-10-30T20:00:00+00:00', 'issues': [], 'limits': [],
                'containers': [{'name': name, 'id': name+'-id', 'image_id': name+'-image',
                    'image': 'image', 'started_at': '2026-10-30T19:50:00Z', 'restarts': 0}
                    for name in (diagnostics.backup.API, diagnostics.backup.MAX, diagnostics.backup.DB)],
                'database': {'connections': [{'workspace_id': 'workspace',
                    'last_sync_at': '2026-10-30T19:59:58+00:00'}]}}

    def test_postgres_cancellation_reports_reason_and_shape_without_sql_or_parameters(self):
        raw = ('2026-10-30T19:50:53.123Z 2026-10-30 19:50:53 UTC [72] ERROR:  canceling statement due to statement timeout\n'
               "2026-10-30T19:50:53.124Z 2026-10-30 19:50:53 UTC [72] STATEMENT:  UPDATE messages SET text='secret-client-text',payload='private-phone' WHERE id=9\n"
               '2026-10-30T19:50:54.123Z 2026-10-30 19:50:54 UTC [73] ERROR:  canceling statement due to user request\n')
        value = diagnostics.summarize_logs(raw, postgres=True)
        self.assertEqual(value['cancellation_count'], 2)
        self.assertEqual(value['cancellations'][0]['reason'], 'statement_timeout')
        self.assertEqual(value['cancellations'][0]['statement_kind'], 'UPDATE')
        self.assertEqual(value['cancellations'][0]['tables'], ['messages'])
        self.assertEqual(value['cancellations'][1]['reason'], 'user_request')
        self.assertNotIn('statement_fingerprint', value['cancellations'][1])
        for private in ('secret-client-text', 'private-phone', 'WHERE id', 'SET text'):
            self.assertNotIn(private, json.dumps(value))

    def test_sql_from_another_backend_is_not_attributed_to_the_cancellation(self):
        value = diagnostics.summarize_logs('[72] ERROR:  canceling statement due to lock timeout\n'
            '[73] STATEMENT: SELECT * FROM messages', postgres=True)
        self.assertEqual(value['cancellations'][0]['reason'], 'lock_timeout')
        self.assertNotIn('statement_fingerprint', value['cancellations'][0])

    def test_worker_log_reports_only_safe_history_error_fields(self):
        value = diagnostics.summarize_logs('private message body\n'
            '2026-10-30T19:50:53Z Database operation failed operation=alfa_max_sync:persist_history_page '
            'exception=OperationalError driver=QueryCanceled sqlstate=57014\n'
            'Database operation failed operation=other:query exception=Error driver=Error sqlstate=57014')
        self.assertEqual(value['history_database_error_count'], 1)
        self.assertEqual(value['history_database_errors'][0]['phase'], 'persist_history_page')
        self.assertEqual(value['history_database_errors'][0]['sqlstate'], '57014')
        self.assertNotIn('private', json.dumps(value))

    def test_log_tail_returns_bounded_output_and_marks_truncation(self):
        real_popen = subprocess.Popen
        def child(*args, **kwargs):
            return real_popen([sys.executable, '-c', 'import os; os.write(1,b"x"*2048)'], **kwargs)
        with patch.object(diagnostics.subprocess, 'Popen', side_effect=child):
            raw, truncated, code = diagnostics.log_tail('fixed-container', '1h', limit=100)
        self.assertEqual(len(raw), 100)
        self.assertTrue(truncated)

    def test_log_tail_includes_stderr_and_reports_client_error(self):
        real_popen = subprocess.Popen
        def child(*args, **kwargs):
            return real_popen([sys.executable, '-c', 'import sys; print("stderr-evidence",file=sys.stderr); sys.exit(1)'], **kwargs)
        with patch.object(diagnostics.subprocess, 'Popen', side_effect=child):
            raw, truncated, code = diagnostics.log_tail('fixed-container', '1h')
        self.assertIn('stderr-evidence', raw)
        self.assertFalse(truncated)
        self.assertEqual(code, 1)

    def test_ordinary_api_replacement_preserves_worker_and_shows_new_history(self):
        previous = diagnostics.baseline(self.report())
        report = self.report()
        report['containers'][0]['id'] = 'new-api'
        report['database']['connections'][0]['last_sync_at'] = '2026-10-30T20:03:00+00:00'
        result = diagnostics.compare(report, previous)
        self.assertTrue(result['max_unchanged'])
        self.assertTrue(result['api_replaced'])
        self.assertTrue(result['history'][0]['successful_read_advanced'])
        self.assertEqual(report['issues'], [])

    def test_worker_recreation_or_restart_cannot_pass_the_same_baseline(self):
        for key, value in (('id','different'), ('image_id','different'), ('image','different'),
                           ('started_at','different'), ('restarts',1)):
            with self.subTest(key=key):
                report = self.report()
                previous = diagnostics.baseline(copy.deepcopy(report))
                report['containers'][1][key] = value
                self.assertFalse(diagnostics.compare(report, previous)['max_unchanged'])
                self.assertEqual(len(report['issues']), 1)

    def test_collect_is_read_only_and_does_not_run_browser_or_send_commands(self):
        runner = Mock()
        report = self.report()
        with patch.object(diagnostics.backup, 'audit', return_value=report), \
             patch.object(diagnostics.backup, 'read_sql', side_effect=['[{"command_id":"failed-id"}]', '{}']) as sql, \
             patch.object(diagnostics.backup, 'inspect', return_value={}), \
             patch.object(diagnostics.backup, 'safe_container', return_value=report['containers'][1]), \
             patch.object(diagnostics, 'env_timeout', return_value=2500), \
             patch.object(diagnostics, 'log_tail', return_value=('secret-log-line',False,0)):
            result = diagnostics.collect(runner, expected_worker='wrong-id')
        self.assertEqual(result['failed_commands'], [{'command_id':'failed-id'}])
        self.assertEqual(len(result['issues']), 1)
        self.assertEqual(sql.call_count, 2)
        self.assertTrue(all(c.args[1] == diagnostics.backup.DB+'-id' for c in sql.call_args_list))
        runner.run.assert_not_called()
        self.assertNotIn('secret-log-line', json.dumps(result))

    def test_baseline_file_never_overwrites_a_previous_record(self):
        with tempfile.TemporaryDirectory() as path:
            record = Path(path)/'baseline.json'
            with patch.object(diagnostics.os,'geteuid',return_value=0), \
                 patch.object(diagnostics,'collect',side_effect=lambda *a,**k:self.report()), \
                 patch('sys.stdout', new_callable=io.StringIO):
                self.assertEqual(diagnostics.main(['--record-baseline',str(record)]), 0)
                self.assertEqual(record.stat().st_mode & 0o777, 0o600)
                original = record.read_bytes()
                with self.assertRaises(FileExistsError):
                    diagnostics.main(['--record-baseline',str(record)])
                self.assertEqual(record.read_bytes(), original)

    def test_baseline_requires_healthy_report(self):
        report = self.report()
        report['issues'] = ['not healthy']
        with patch.object(diagnostics.os,'geteuid',return_value=0), \
             patch.object(diagnostics,'collect',return_value=report):
            with self.assertRaisesRegex(diagnostics.backup.BackupError, 'Cannot record'):
                diagnostics.main(['--record-baseline','must-not-be-created'])

    def test_since_argument_is_a_duration_or_timezone_aware_timestamp(self):
        self.assertEqual(diagnostics.since_value('1h'), '1h')
        self.assertEqual(diagnostics.since_value('2026-10-30T22:50:35+03:00'), '2026-10-30T19:50:35+00:00')
        for invalid in ('yesterday','2026-10-30T19:50:35','--follow'):
            with self.assertRaises(diagnostics.argparse.ArgumentTypeError):
                diagnostics.since_value(invalid)


if __name__ == '__main__':
    unittest.main()
