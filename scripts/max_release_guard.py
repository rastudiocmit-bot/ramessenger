#!/usr/bin/env python3
"""Pinned server-side MAX release fence. Standard library only; no Actions.

Install explicitly from a reviewed checkout. Never self-updates from main.
The systemd entrypoint, policy, runner, helper and tests live outside checkouts.
"""
import argparse
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

ROOT = Path('/opt/center-messenger-github')
TRUST = Path('/etc/ramessenger-max-guard')
REPO = 'rastudiocmit-bot/ramessenger'
POLICY = 'deploy/max-protection.json'
DROP_IN = Path('/etc/systemd/system/ramessenger-local-build.service.d/50-max-guard.conf')
BASELINE = '30ae13c5f03f1009d6b1b63a3ab1a349119bb9ce'


class GuardError(RuntimeError):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encode(value):
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + '\n').encode()


def full_sha(value):
    if not re.fullmatch(r'[a-f0-9]{40}', value):
        raise GuardError('Full lowercase commit SHA required')
    return value


def regular(root, relative):
    """Reject path escapes and all symlink components (including broken ones)."""
    path = PurePosixPath(relative)
    if path.is_absolute() or not path.parts or any(p in {'.', '..'} for p in path.parts):
        raise GuardError('Unsafe policy path')
    current = root
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            raise GuardError('Symlink in protected path: ' + relative)
    if not current.is_file():
        raise GuardError('Missing protected file: ' + relative)
    return current.read_bytes()


def command(*args, cwd=None, timeout=120):
    return subprocess.run(args, cwd=cwd, check=True, text=True, stdout=subprocess.PIPE,
                          timeout=timeout).stdout.strip()


def load_policy(root):
    policy = json.loads(regular(root, POLICY))
    if policy.get('version') != 1 or not policy.get('protected') or not policy.get('tests'):
        raise GuardError('Invalid MAX protection policy')
    full_sha(policy['application_baseline'])
    for path, checksum in policy['protected'].items():
        if not re.fullmatch('[a-f0-9]{64}', checksum):
            raise GuardError('Invalid protected checksum')
        # Validate names even when checking a deleted candidate file later.
        parts = PurePosixPath(path)
        if parts.is_absolute() or '..' in parts.parts:
            raise GuardError('Invalid protected name')
    for path in policy['tests'] + policy['bundle']:
        if path not in policy['protected']:
            raise GuardError('Unpinned guard bundle file: ' + path)
    return policy


def changes(source, trusted):
    policy = load_policy(trusted)
    result = []
    for path, expected in sorted(policy['protected'].items()):
        try:
            actual = digest(regular(source, path))
        except GuardError:
            actual = 'missing-or-nonregular'
        if actual != expected:
            result.append({'path': path, 'before': expected, 'after': actual})
    # Adding a new migration/core module also needs review, not just edits to
    # names present in the old manifest. Traversal never follows symlinks.
    for prefix in policy['protected_prefixes']:
        base = source / prefix
        if base.is_symlink():
            raise GuardError('Symlink protected directory: ' + prefix)
        if not base.exists():
            continue
        for path in sorted(base.rglob('*')):
            if path.is_dir() and not path.is_symlink():
                continue
            relative = path.relative_to(source).as_posix()
            if relative not in policy['protected'] and '__pycache__' not in path.parts:
                result.append({'path': relative, 'before': None,
                               'after': digest(regular(source, relative))})
    known = {item['path'] for item in result} | set(policy['protected'])
    for pattern in policy.get('protected_patterns', []):
        for path in sorted(source.glob(pattern)):
            relative = path.relative_to(source).as_posix()
            if relative not in known:
                result.append({'path': relative, 'before': None,
                               'after': digest(regular(source, relative))})
                known.add(relative)
    pinned = digest(regular(trusted, POLICY))
    try:
        proposed = digest(regular(source, POLICY))
    except GuardError:
        proposed = 'missing-or-nonregular'
    if pinned != proposed:
        result.append({'path': POLICY, 'before': pinned, 'after': proposed})
    return result


def report(source, trusted, sha):
    result = {'sha': full_sha(sha), 'policy_sha256': digest(regular(trusted, POLICY)),
              'changes': changes(source, trusted)}
    result['diff_sha256'] = digest(encode(result))
    return result


def approved(result, approval, now=None):
    if not result['changes']:
        return True
    if approval is None:
        return False
    now = now or datetime.now(timezone.utc)
    try:
        return (all(approval[k] == result[k] for k in ('sha', 'policy_sha256', 'diff_sha256'))
                and now < datetime.fromisoformat(approval['expires_at'])
                and bool(approval['reason'].strip()))
    except (KeyError, ValueError, TypeError):
        return False


def root_owned(path):
    info = path.stat()
    if info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
        raise GuardError('Trusted path must be root-owned and not writable by others: ' + str(path))


def trusted_bundle(trusted):
    # The only writable authority is the server administrator, outside Git.
    root_owned(TRUST)
    if TRUST.is_symlink() or TRUST.parent.is_symlink():
        raise GuardError('Unexpected trust directory symlink')
    root_owned(trusted)
    root_owned(trusted / 'integrity.json')
    integrity = json.loads((trusted / 'integrity.json').read_bytes())
    for path, expected in integrity.items():
        data = regular(trusted, path)
        for parent in (trusted / path).parents:
            root_owned(parent)
            if parent == trusted:
                break
        root_owned(trusted / path)
        if digest(data) != expected:
            raise GuardError('Installed guard integrity mismatch: ' + path)
    policy = load_policy(trusted)
    required = {POLICY, *policy['bundle'], *policy['tests']}
    if set(integrity) != required:
        raise GuardError('Incomplete trusted guard bundle')
    return policy


@contextmanager
def project_lock():
    with (ROOT / '.deploy.lock').open('a+') as handle:
        os.chmod(handle.name, 0o600)
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


@contextmanager
def checkout(sha):
    with tempfile.TemporaryDirectory(prefix='max-guard-', dir=ROOT) as directory:
        source = Path(directory) / 'source'
        command('gh', 'repo', 'clone', REPO, str(source), '--', '--no-checkout',
                '--depth', '1', '--branch', 'main', timeout=300)
        command('git', '-C', str(source), 'fetch', '--depth', '1', 'origin', sha, timeout=300)
        command('git', '-C', str(source), 'checkout', '--detach', full_sha(sha), timeout=300)
        if command('git', '-C', str(source), 'rev-parse', 'HEAD') != sha:
            raise GuardError('Source SHA mismatch')
        yield source


def main_sha():
    return full_sha(command('gh', 'api', f'repos/{REPO}/commits/main', '--jq', '.sha', timeout=60))


def compatibility(image, trusted):
    """Trusted regression code + proposed application; no CRM or production DB."""
    name = 'cm-max-offline-' + uuid4().hex
    try:
        subprocess.run(['docker', 'run', '--rm', '--name', name, '--network', 'none', '--read-only',
                    '--tmpfs', '/tmp:rw,nosuid,size=128m', '--pids-limit', '128',
                    '--memory', '512m', '--cpus', '1',
                    '--mount', f'type=bind,src={trusted}/tests,dst=/opt/max-tests,readonly',
                    '--env', 'PYTHONPATH=/app/backend/src:/opt/max-tests',
                    '--entrypoint', '/opt/venv/bin/python', image,
                    '/opt/max-tests/run_max_compatibility.py'], check=True, timeout=180)
    finally:
        remove_test_container(name)
    database_compatibility(image, trusted)


def remove_test_container(name):
    # Name is generated here, never supplied by the release or user. Also
    # catches Docker client timeouts which do not necessarily stop a container.
    result = subprocess.run(['docker', 'rm', '-f', '-v', name],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30)
    if result.returncode and b'No such container' not in (result.stderr or b''):
        raise GuardError('Could not remove isolated test container: ' + name)


def database_compatibility(image, trusted):
    """Synthetic DB tests in a unique internal network, never the live volume."""
    identifier = uuid4().hex
    network, db = 'cm-max-check-' + identifier, 'cm-max-db-' + identifier
    label = 'com.center-messenger.max-guard=' + identifier
    password = secrets.token_hex(24)  # generated test-only credential
    pg_image = json.loads(command('docker', 'inspect', 'center-messenger-db-1'))[0]['Image']
    app = 'cm-max-test-' + identifier
    network_created = db_attempted = False
    try:
        command('docker', 'network', 'create', '--internal', '--label', label, network)
        network_created = True
        db_attempted = True
        command('docker', 'run', '-d', '--name', db, '--label', label, '--network', network,
                '--memory', '256m', '--cpus', '0.5', '--pids-limit', '128',
                '--tmpfs', '/var/lib/postgresql/data:rw,size=512m',
                '--env', 'POSTGRES_USER=cm_guard', '--env', 'POSTGRES_DB=max_guard',
                '--env', 'POSTGRES_PASSWORD=' + password, pg_image)
        for attempt in range(40):
            ready = subprocess.run(['docker', 'exec', db, 'pg_isready', '-U', 'cm_guard', '-d', 'max_guard'],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
            if ready.returncode == 0:
                break
            time.sleep(0.5)
        else:
            raise GuardError('Isolated compatibility database did not become ready')
        subprocess.run(['docker', 'run', '--rm', '--name', app, '--network', network, '--read-only',
                        '--tmpfs', '/tmp:rw,nosuid,size=128m', '--memory', '512m', '--cpus', '1',
                        '--pids-limit', '128',
                        '--mount', f'type=bind,src={trusted}/tests,dst=/opt/max-tests,readonly',
                        '--env', 'PYTHONPATH=/app/backend/src:/opt/max-tests',
                        '--env', f'CM_TEST_DATABASE_URL=postgresql+psycopg://cm_guard:{password}@{db}/max_guard',
                        '--entrypoint', '/opt/venv/bin/python', image,
                        '/opt/max-tests/run_max_compatibility.py', '--database'], check=True, timeout=240)
    finally:
        # Only names created by this invocation; no prune, shared volumes,
        # production container changes or migrations against the live DB.
        try:
            if db_attempted:
                try:
                    remove_test_container(app)
                finally:
                    remove_test_container(db)
        finally:
            if network_created:
                command('docker', 'network', 'rm', network)


def run(trusted, preview=False):
    trusted_bundle(trusted)
    with project_lock():
        # Do not clone/build if the operator disabled the existing deploy owner.
        state = json.loads((ROOT / 'local-build-state.json').read_text())
        if state.get('enabled') is not True:
            print('MAX GUARD: deployment disabled', flush=True)
            return
        sha = main_sha()
        with checkout(sha) as source:
            result = report(source, trusted, sha)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            path = TRUST / 'approvals' / (sha + '.json')
            approval = None
            if path.exists():
                root_owned(path.parent)
                root_owned(path)
                approval = json.loads(regular(TRUST, 'approvals/' + sha + '.json'))
            if not approved(result, approval):
                raise GuardError('MAX protected files changed; exact-SHA server approval required')
            if preview:
                print('MAX GUARD PREVIEW OK: no build or deployment', flush=True)
                return
            # Load the installed runner, never any script from the candidate.
            sys.path.insert(0, str(trusted / 'scripts'))
            spec = importlib.util.spec_from_file_location('guarded_local_build', trusted / 'scripts/local_build_vps.py')
            runner = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(runner)
            runner.main(False, verified_source=(source, sha), helper_root=trusted / 'scripts',
                        compatibility=lambda image: compatibility(image, trusted))


def atomic_write(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        name = Path(handle.name)
        try:
            os.fchmod(handle.fileno(), mode)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            os.replace(name, path)
        finally:
            name.unlink(missing_ok=True)


def allow(trusted, sha, expected_diff, reason):
    trusted_bundle(trusted)
    with project_lock(), checkout(full_sha(sha)) as source:
        result = report(source, trusted, sha)
        if result['diff_sha256'] != expected_diff or not reason.strip():
            raise GuardError('Review digest mismatch or missing reason; run inspect first')
        approval = dict(result, reason=reason, expires_at=(datetime.now(timezone.utc) + timedelta(hours=24)).isoformat())
        atomic_write(TRUST / 'approvals' / (sha + '.json'), encode(approval))
        print('MAX APPROVAL SAVED for exact SHA; pinned compatibility tests remain mandatory', flush=True)


def install(source, expected_policy, expected_live, previous_policy=None, *, lock_held=False):
    """Explicit root bootstrap/update; no API/DB restarts, no deployment."""
    policy = load_policy(source)
    body = regular(source, POLICY)
    if digest(body) != expected_policy:
        raise GuardError('Reviewed policy checksum mismatch')
    mismatches = changes(source, source)
    if mismatches:
        raise GuardError('Checkout differs from its manifest: ' + json.dumps(mismatches))
    with nullcontext() if lock_held else project_lock():
        current = json.loads(command('docker', 'inspect', 'center-messenger-api-1'))[0]
        if current['Config']['Image'] != 'center-messenger:local-' + full_sha(expected_live)[:12]:
            raise GuardError('Unexpected live API image')
        env = current['Config'].get('Env') or []
        if 'CM_BUILD_SHA=' + expected_live not in env or current['State']['Status'] != 'running':
            raise GuardError('Unexpected live API source/state')
        TRUST.mkdir(mode=0o755, exist_ok=True)
        root_owned(TRUST)
        link = TRUST / 'active'
        if link.exists() or link.is_symlink():
            old = link.resolve(strict=True)
            trusted_bundle(old)
            installed_policy = digest(regular(old, POLICY))
            if installed_policy != previous_policy and installed_policy != digest(body):
                raise GuardError('Expected installed policy checksum required for replacement')
        elif expected_live != policy['application_baseline']:
            raise GuardError('First installation requires the documented application baseline')
        generation = TRUST / digest(body)
        paths = [POLICY, *policy['bundle'], *policy['tests']]
        if not generation.exists():
            with tempfile.TemporaryDirectory(prefix='.install-', dir=TRUST) as temporary:
                stage = Path(temporary) / 'bundle'
                stage.mkdir(mode=0o755)
                for path in paths:
                    target = stage / path
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                    target.write_bytes(regular(source, path))
                    target.chmod(0o644)
                (stage / 'integrity.json').write_bytes(encode({p: digest(regular(stage, p)) for p in paths}))
                stage.rename(generation)
        trusted_bundle(generation)
        replacement = TRUST / '.active-new'
        replacement.unlink(missing_ok=True)
        replacement.symlink_to(generation.name)
        os.replace(replacement, link)
        dropin = ('[Service]\nExecStart=\nExecStart=/usr/bin/python3 '
                  '/etc/ramessenger-max-guard/active/scripts/max_release_guard.py run\n')
        atomic_write(DROP_IN, dropin.encode(), mode=0o644)
        command('systemctl', 'daemon-reload')
        print('MAX GUARD INSTALLED; API unchanged; use run --preview before the next release', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    start = sub.add_parser('run')
    start.add_argument('--preview', action='store_true')
    look = sub.add_parser('inspect')
    look.add_argument('--sha', required=True)
    approve = sub.add_parser('approve')
    approve.add_argument('--sha', required=True)
    approve.add_argument('--expected-diff', required=True)
    approve.add_argument('--reason', required=True)
    setup = sub.add_parser('install')
    setup.add_argument('--source', type=Path, required=True)
    setup.add_argument('--expected-policy', required=True)
    setup.add_argument('--expected-live', required=True)
    setup.add_argument('--previous-policy')
    opts = parser.parse_args()
    if os.geteuid() != 0:
        raise GuardError('Root required')
    if opts.action == 'install':
        install(opts.source, opts.expected_policy, opts.expected_live, opts.previous_policy)
        return
    trusted = TRUST.joinpath('active').resolve(strict=True)
    if trusted.parent != TRUST:
        raise GuardError('Unexpected installed guard location')
    if opts.action == 'run':
        run(trusted, opts.preview)
    elif opts.action == 'approve':
        allow(trusted, opts.sha, opts.expected_diff, opts.reason)
    else:
        trusted_bundle(trusted)
        with project_lock(), checkout(full_sha(opts.sha)) as source:
            print(json.dumps(report(source, trusted, opts.sha), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('MAX RELEASE BLOCKED:', str(exc), file=sys.stderr, flush=True)
        sys.exit(1)
