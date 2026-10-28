"""Pinned candidate imports remain isolated and cannot expand server commands."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('recipient_preview', ROOT / 'scripts/preview_native_recipient.py')
preview = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preview)


class PreviewTests(unittest.TestCase):
    def test_ref_is_validated_before_any_server_command(self):
        with patch.object(preview.subprocess, 'run') as run:
            for value in ['main', 'a'*12, '$(id)', 'a'*40 + '; id']:
                with self.assertRaises(ValueError):
                    preview.download(value)
        run.assert_not_called()

    def test_downloads_only_three_pinned_files_and_checks_python_before_execution(self):
        with patch.object(preview.subprocess, 'run', return_value=Mock(stdout='pass')) as run:
            packet = preview.download('a'*40)
        self.assertEqual(set(packet), set(preview.FILES))
        for call in run.call_args_list:
            self.assertIn('?ref=' + 'a'*40, call.args[0][2])
            self.assertNotIn('shell', call.kwargs)

    def test_only_the_fixed_container_and_bootstrap_are_executed(self):
        with patch.object(preview, 'download', return_value={'code': 'data'}) as download, \
             patch.object(preview.subprocess, 'run') as run:
            preview.main(['--ref', 'a'*40])
        download.assert_called_once_with('a'*40)
        self.assertEqual(run.call_args.args[0][:6],
            ['docker', 'exec', '-i', 'center-messenger-api-1', '/opt/venv/bin/python', '-c'])
        self.assertEqual(json.loads(run.call_args.kwargs['input']), {'code': 'data'})

    def test_bootstrap_loads_candidate_modules_only_in_child_in_dependency_order(self):
        packet = {
            preview.FILES[0]: 'candidate_value=42',
            preview.FILES[1]: 'from center_messenger.alfa_max_browser import candidate_value\nassert candidate_value==42',
            preview.FILES[2]: "import center_messenger.alfa_max_browser as candidate\nassert __name__=='__main__'\nprint(candidate.candidate_value)",
        }
        result = subprocess.run([sys.executable, '-c', preview.BOOTSTRAP], input=json.dumps(packet),
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '42')
        from center_messenger import alfa_max_browser
        self.assertFalse(hasattr(alfa_max_browser, 'candidate_value'))


if __name__ == '__main__':
    unittest.main()
