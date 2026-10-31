#!/usr/bin/env python3
"""Seal a verified view of the known empty-WAL SQLite backup bug.

Never edits the original manifest or payload. This is not a second physical
backup: large payload files are hard-linked on the same filesystem. All real
payloads must match the original checksums. A nonempty WAL is never discarded.
"""
import argparse
from contextlib import closing
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3

import full_backup_split as backup

QUEUE = 'support-queue.sqlite'
SIDECARS = {QUEUE + '-wal', QUEUE + '-shm'}
EVIDENCE = {'legacy-manifest.json', 'legacy-SHA256SUMS', 'sqlite-seal-report.json'}
EMPTY_HASH = hashlib.sha256(b'').hexdigest()


def read_regular(path):
    if path.is_symlink() or not path.is_file():
        raise backup.BackupError('Expected a regular metadata file: ' + path.name)
    return path.read_bytes()


def check_sidecars(directory, parts):
    by_name = {part['name']: part for part in parts}
    if not {QUEUE, *SIDECARS}.issubset(by_name):
        raise backup.BackupError('Not the known SQLite sidecar inventory; no automatic recovery')
    if by_name[QUEUE + '-wal']['bytes'] != 0 or by_name[QUEUE + '-wal']['sha256'] != EMPTY_HASH:
        raise backup.BackupError('Recorded SQLite WAL is not empty; a fresh backup/investigation is required')
    if by_name[QUEUE + '-shm']['bytes'] != 32768:
        raise backup.BackupError('Unexpected SQLite SHM size; no automatic recovery')
    for suffix in ('-wal', '-shm', '-journal'):
        path = directory / (QUEUE + suffix)
        if path.is_symlink():
            raise backup.BackupError('SQLite sidecar must not be a symlink')
        if path.exists():
            if not path.is_file() or suffix == '-journal':
                raise backup.BackupError('Unexpected SQLite sidecar; no automatic recovery')
            if suffix == '-wal' and path.stat().st_size:
                raise backup.BackupError('Current SQLite WAL is not empty; no automatic recovery')


def sqlite_evidence(path):
    # immutable avoids creating/rebuilding SHM even for a WAL-mode file. It is
    # safe only after matching the original main hash and proving WAL empty.
    uri = path.absolute().as_uri() + '?mode=ro&immutable=1'
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.execute('PRAGMA query_only=ON')
        if connection.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise backup.BackupError('SQLite main-file integrity check failed')
        if connection.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise backup.BackupError('SQLite main-file foreign key check failed')
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        counts = {name: connection.execute('SELECT count(*) FROM ' + backup.quote_identifier(name)).fetchone()[0]
                  for name in tables}
    return {'integrity_check': 'ok', 'foreign_key_check': 'ok', 'table_counts': counts}


def seal(directory, destination):
    if directory.is_symlink() or destination.is_symlink():
        raise backup.BackupError('Backup paths must not be symlinks')
    directory, destination = directory.absolute(), destination.absolute()
    if directory.parent != destination.parent or directory == destination:
        raise backup.BackupError('A distinct new sibling directory is required')
    if destination.exists():
        raise backup.BackupError('Destination already exists; it will not be overwritten')
    with backup.locks(directory.parent):
        original_bytes = read_regular(directory / 'manifest.json')
        manifest = backup.read_manifest(directory)
        parts = manifest['parts']
        if EVIDENCE.intersection(part['name'] for part in parts):
            raise backup.BackupError('Already sealed or conflicting evidence files')
        if not manifest.get('finished_at'):
            raise backup.BackupError('Original backup completion time is required')
        check_sidecars(directory, parts)
        original_sums = read_regular(directory / 'SHA256SUMS')
        expected_sums = ''.join(p['sha256'] + '  ' + p['name'] + '\n' for p in parts).encode()
        if original_sums != expected_sums:
            raise backup.BackupError('Original SHA256SUMS disagrees with original manifest')
        kept = [part for part in parts if part['name'] not in SIDECARS]
        for part in kept:
            backup.announce('VERIFY_ORIGINAL', part['name'])
            backup.verify_part(directory, part)
            if (directory / part['name']).stat().st_mode & 0o077:
                raise backup.BackupError('Backup payload permissions are not private: ' + part['name'])
        main_part = next(p for p in kept if p['name'] == QUEUE)
        evidence = sqlite_evidence(directory / QUEUE)
        backup.verify_part(directory, main_part)
        check_sidecars(directory, parts)
        if read_regular(directory / 'manifest.json') != original_bytes:
            raise backup.BackupError('Original manifest changed during verification')
        # O_EXCL-style mkdir. Failed output remains for diagnosis, never complete.
        destination.mkdir(mode=0o700)
        backup.save_json(destination / 'status.json', {'state': 'running', 'phase': 'sqlite_seal'})
        try:
            for part in kept:
                os.link(directory / part['name'], destination / part['name'], follow_symlinks=False)
            report = {
                'format': 1, 'sealed_at': backup.now(), 'source_directory': str(directory),
                'source_manifest_sha256': hashlib.sha256(original_bytes).hexdigest(),
                'original_finished_at': manifest['finished_at'],
                'storage': 'hard-linked verified view, not an independent physical backup',
                'reason': 'closed SQLite snapshot; original WAL was empty; SHM is a rebuildable index',
                'excluded_parts': [p for p in parts if p['name'] in SIDECARS],
                'sqlite_main': dict(main_part, **evidence),
                'postgres_restore': 'original verified restore evidence retained; not rerun by this operation',
            }
            for name, content in (('legacy-manifest.json', original_bytes), ('legacy-SHA256SUMS', original_sums)):
                descriptor = os.open(destination / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(descriptor, 'wb') as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
            backup.save_json(destination / 'sqlite-seal-report.json', report)
            sealed = copy.deepcopy(manifest)
            # Preserve backup age and all original restore evidence.
            sealed['parts'] = copy.deepcopy(kept)
            sealed['sqlite_seal'] = {'report': 'sqlite-seal-report.json', 'source_directory': str(directory)}
            for name in sorted(EVIDENCE):
                path = destination / name
                sealed['parts'].append({'name': name, 'bytes': path.stat().st_size, 'sha256': backup.checksum(path)})
            backup.save_json(destination / 'manifest.json', sealed)
            descriptor = os.open(destination / 'SHA256SUMS', os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, 'w') as stream:
                stream.write(''.join(p['sha256'] + '  ' + p['name'] + '\n' for p in sealed['parts']))
                stream.flush()
                os.fsync(stream.fileno())
            backup.announce('VERIFY_SEALED_SET', destination)
            backup.verify_integrity(destination)
            check_sidecars(directory, parts)
            if read_regular(directory / 'manifest.json') != original_bytes:
                raise backup.BackupError('Original manifest changed during sealing')
            backup.save_json(destination / 'status.json', sealed)
        except BaseException:
            # A partly linked view must not be mistaken for a completed backup.
            (destination / 'manifest.json').unlink(missing_ok=True)
            backup.save_json(destination / 'status.json', {'state': 'failed', 'phase': 'sqlite_seal'})
            raise
    backup.announce('SQLITE_SEAL_COMPLETE', destination)
    return sealed


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backup', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    options = parser.parse_args()
    try:
        seal(options.backup, options.output)
    except (backup.BackupError, OSError, ValueError, sqlite3.Error) as error:
        backup.announce('SQLITE_SEAL_FAILED', type(error).__name__, str(error))
        raise SystemExit(1)
