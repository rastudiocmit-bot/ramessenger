"""Exercise the real WAL snapshot bug and fail-closed legacy resealing."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
SPEC = importlib.util.spec_from_file_location('seal_sqlite_backup', SCRIPTS / 'seal_sqlite_backup.py')
seal = importlib.util.module_from_spec(SPEC)
with patch.object(sys, 'path', [str(SCRIPTS), *sys.path]):
    SPEC.loader.exec_module(seal)
backup = seal.backup


class SealTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.patcher = patch.object(backup, 'ROOT', self.project)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.directory = self.root / 'original'
        self.directory.mkdir(mode=0o700)
        self.output = self.root / 'sealed'
        # Reproduce the old backup implementation, including leaked WAL-mode
        # destination and an integrity read that creates its SHM/empty WAL.
        source = sqlite3.connect(self.root / 'live.sqlite')
        target = sqlite3.connect(self.directory / seal.QUEUE)
        try:
            source.execute('PRAGMA journal_mode=WAL')
            source.execute('CREATE TABLE requests(id INTEGER PRIMARY KEY, state TEXT)')
            source.executemany('INSERT INTO requests VALUES (?,?)', [(1, 'queued'), (2, 'done')])
            source.commit()
            source.backup(target)
            self.assertEqual(target.execute('PRAGMA integrity_check').fetchall(), [('ok',)])
            required = ['deployment-files.tar.gz', 'container-images.tar.gz', 'postgres-globals.sql',
                        'database-000.dump', 'recovery-metadata.json', 'runtime-audit.json',
                        'full_backup_split.py', 'RESTORE.txt']
            for name in required:
                (self.directory / name).write_bytes(b'previously verified payload ' + name.encode())
            parts = []
            for path in sorted(self.directory.iterdir()):
                path.chmod(0o600)
                parts.append({'name': path.name, 'bytes': path.stat().st_size, 'sha256': backup.checksum(path)})
            self.manifest = {'format': 2, 'state': 'complete', 'mode': 'plain',
                             'finished_at': '2026-10-31T09:28:12+00:00', 'parts': parts,
                             'databases': [{'database': 'center_messenger', 'part': 'database-000.dump',
                                            'restore_verified': True, 'full_dump_read_verified': True}]}
            self.write_manifest()
            backup.verify_integrity(self.directory)
        finally:
            target.close()
            source.close()
        self.assertFalse((self.directory / (seal.QUEUE + '-wal')).exists())
        self.assertFalse((self.directory / (seal.QUEUE + '-shm')).exists())

    def write_manifest(self):
        backup.save_json(self.directory / 'manifest.json', self.manifest)
        path = self.directory / 'SHA256SUMS'
        path.write_text(''.join(p['sha256'] + '  ' + p['name'] + '\n' for p in self.manifest['parts']))
        path.chmod(0o600)

    def test_reproduces_failure_then_seals_only_verified_payload_without_changing_original(self):
        originals = {p.name: p.read_bytes() for p in self.directory.iterdir()}
        with self.assertRaisesRegex(backup.BackupError, 'checksum mismatch'):
            backup.verify_integrity(self.directory)
        result = seal.seal(self.directory, self.output)
        self.assertEqual(backup.verify_integrity(self.output), result)
        self.assertEqual(result['finished_at'], self.manifest['finished_at'])
        self.assertEqual(result['databases'], self.manifest['databases'])
        self.assertEqual(originals, {p.name: p.read_bytes() for p in self.directory.iterdir()})
        self.assertEqual((self.output / 'legacy-manifest.json').read_bytes(), originals['manifest.json'])
        for part in self.manifest['parts']:
            if part['name'] not in seal.SIDECARS:
                self.assertTrue(os.path.samefile(self.directory / part['name'], self.output / part['name']))
        report = json.loads((self.output / 'sqlite-seal-report.json').read_text())
        self.assertEqual(report['sqlite_main']['table_counts'], {'requests': 2})
        self.assertEqual(report['source_manifest_sha256'], hashlib.sha256(originals['manifest.json']).hexdigest())
        self.assertEqual({p['name'] for p in report['excluded_parts']}, seal.SIDECARS)
        self.assertFalse(any((self.output / n).exists() for n in seal.SIDECARS))
        with self.assertRaises(backup.BackupError):
            backup.verify_integrity(self.directory)

    def test_any_real_payload_corruption_refuses_recovery(self):
        for name in ('container-images.tar.gz', 'database-000.dump', seal.QUEUE):
            with self.subTest(name=name):
                path = self.directory / name
                original = path.read_bytes()
                try:
                    path.write_bytes(original + b'damage')
                    with self.assertRaisesRegex(backup.BackupError, 'checksum mismatch'):
                        seal.seal(self.directory, self.output)
                    self.assertFalse(self.output.exists())
                finally:
                    path.write_bytes(original)

    def test_nonempty_original_wal_is_never_discarded(self):
        part = next(p for p in self.manifest['parts'] if p['name'] == seal.QUEUE + '-wal')
        part.update(bytes=4, sha256=hashlib.sha256(b'data').hexdigest())
        self.write_manifest()
        with self.assertRaisesRegex(backup.BackupError, 'Recorded SQLite WAL is not empty'):
            seal.seal(self.directory, self.output)
        self.assertFalse(self.output.exists())

    def test_rebuildable_shm_drift_with_empty_wal_does_not_change_main_payload(self):
        (self.directory / (seal.QUEUE + '-wal')).write_bytes(b'')
        (self.directory / (seal.QUEUE + '-shm')).write_bytes(b'changed rebuildable index')
        result = seal.seal(self.directory, self.output)
        self.assertEqual(backup.verify_integrity(self.output), result)
        self.assertEqual((self.directory / (seal.QUEUE + '-shm')).read_bytes(), b'changed rebuildable index')

    def test_matching_checksum_is_insufficient_for_an_invalid_sqlite_main_file(self):
        path = self.directory / seal.QUEUE
        path.write_bytes(b'not a SQLite database')
        part = next(p for p in self.manifest['parts'] if p['name'] == seal.QUEUE)
        part.update(bytes=path.stat().st_size, sha256=backup.checksum(path))
        self.write_manifest()
        with self.assertRaises(sqlite3.DatabaseError):
            seal.seal(self.directory, self.output)
        self.assertFalse(self.output.exists())

    def test_current_nonempty_wal_or_hot_journal_is_never_discarded(self):
        for suffix in ('-wal', '-journal'):
            with self.subTest(suffix=suffix):
                path = self.directory / (seal.QUEUE + suffix)
                path.write_bytes(b'potential committed data')
                try:
                    with self.assertRaises(backup.BackupError):
                        seal.seal(self.directory, self.output)
                    self.assertFalse(self.output.exists())
                finally:
                    path.unlink()

    def test_source_checksum_inventory_drift_is_rejected(self):
        (self.directory / 'SHA256SUMS').write_text('changed')
        with self.assertRaisesRegex(backup.BackupError, 'SHA256SUMS disagrees'):
            seal.seal(self.directory, self.output)

    def test_existing_output_and_source_sidecar_symlinks_are_refused(self):
        self.output.mkdir()
        with self.assertRaisesRegex(backup.BackupError, 'already exists'):
            seal.seal(self.directory, self.output)
        self.output.rmdir()
        (self.directory / (seal.QUEUE + '-shm')).symlink_to(self.directory / seal.QUEUE)
        with self.assertRaisesRegex(backup.BackupError, 'symlink'):
            seal.seal(self.directory, self.output)

    def test_final_verification_failure_cannot_publish_completed_manifest(self):
        with patch.object(backup, 'verify_integrity', side_effect=backup.BackupError('late checksum mismatch')):
            with self.assertRaisesRegex(backup.BackupError, 'late checksum mismatch'):
                seal.seal(self.directory, self.output)
        self.assertFalse((self.output / 'manifest.json').exists())
        self.assertEqual(json.loads((self.output / 'status.json').read_text())['state'], 'failed')
