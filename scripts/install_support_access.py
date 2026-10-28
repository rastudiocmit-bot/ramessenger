#!/usr/bin/python3
"""Root-only, explicit installation of messenger-scoped SSH support access.

Run the downloaded file from a terminal; no interactive input or here-doc is
needed. Source is pinned to a commit already integrated into main. Existing
application/container/database/proxy settings are never modified here.
"""
import argparse
from contextlib import ExitStack
import fcntl
import json
import os
from pathlib import Path
import pwd
import re
import subprocess
import tempfile

ROOT = Path('/opt/center-messenger-github')
SUPPORT = ROOT / 'support'
USER = 'cm-support'
REPO = 'rastudiocmit-bot/ramessenger'
ACTOR = re.compile(r'[a-z][a-z0-9-]{0,47}')
ENTRY = Path('/usr/local/bin/cm-support-entry')
CONTROL = Path('/usr/local/sbin/cm-support-control')
UNITS = ('ramessenger-support-queue.service', 'ramessenger-support-queue.timer')
FILES = ('scripts/support_access.py', 'scripts/support_ssh_entry.py',
         'scripts/diagnose_max_recipients.py', 'scripts/install_support_access.py',
         *(f'infra/systemd/{unit}' for unit in UNITS))


def run(*argv):
    return subprocess.run(argv, check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, timeout=90).stdout.strip()


def write(path, body, mode):
    temporary = path.with_name(path.name + '.installing')
    temporary.write_text(body)
    temporary.chmod(mode)
    os.chown(temporary, 0, 0)
    os.replace(temporary, path)


def key_line(value):
    words = value.strip().split()
    if len(words) not in (2, 3) or words[0] != 'ssh-ed25519' or not re.fullmatch(r'[A-Za-z0-9+/=]+', words[1]):
        raise ValueError('Supply one plain ed25519 public key, without authorized_keys options')
    return ' '.join(words[:2])


def authorized_lines(old, actor, public):
    marker = f'cm-support:{actor}'
    if any(' ' + public + ' ' in line and not line.endswith(' ' + marker) for line in old):
        raise ValueError('This key belongs to another actor; use a distinct key')
    lines = [line for line in old if not line.endswith(' ' + marker)]
    lines.append(f'restrict,command="{ENTRY} {actor}" {public} {marker}')
    return lines


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ref', help='Exact integrated GitHub commit, 40 hex characters')
    parser.add_argument('--actor', required=True)
    parser.add_argument('--public-key')
    parser.add_argument('--role', choices=('diagnostic', 'maintainer'), default='diagnostic')
    parser.add_argument('--revoke', action='store_true')
    args = parser.parse_args()
    if os.geteuid() != 0 or not ACTOR.fullmatch(args.actor):
        parser.error('root and an actor matching [a-z][a-z0-9-]{0,47} are required')
    os.umask(0o077)
    if not ROOT.is_dir() or not (ROOT / 'local_build_vps.py').is_file():
        parser.error('Existing Center Messenger deployment was not found')
    SUPPORT.mkdir(mode=0o700, exist_ok=True)
    SUPPORT.chmod(0o700)
    with ExitStack() as locks:
        installer = locks.enter_context((SUPPORT / 'install.lock').open('a'))
        fcntl.flock(installer, fcntl.LOCK_EX | fcntl.LOCK_NB)
        registry_file = SUPPORT / 'actors.json'
        registry = json.loads(registry_file.read_text()) if registry_file.exists() else {}
        if args.revoke:
            registry.pop(args.actor, None)
            write(registry_file, json.dumps(registry, indent=2) + '\n', 0o600)
            print('REVOKED', args.actor, '(key retained but every dispatcher action is denied)')
            return
        # Installations are serialized in the same order as the queue worker:
        # queue -> deploy. Revocation above must not wait for a lengthy build.
        for path in (SUPPORT / 'queue.lock', ROOT / '.deploy.lock'):
            stream = locks.enter_context(path.open('a'))
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not args.ref or not re.fullmatch(r'[a-f0-9]{40}', args.ref) or not args.public_key:
            parser.error('--ref and --public-key are required for installation')
        public = key_line(args.public_key)
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / 'public.pub'
            key.write_text(public + '\n')
            fingerprint = run('ssh-keygen', '-lf', str(key))
        if any(actor != args.actor and value.get('fingerprint') == fingerprint
               for actor, value in registry.items()):
            raise ValueError('A different actor already uses this public key')
        comparison = run('gh', 'api', f'repos/{REPO}/compare/{args.ref}...main', '--jq', '.status')
        if comparison not in ('ahead', 'identical'):
            raise ValueError('Install only a commit already integrated into main')
        if 'fresh_source_before_promotion' not in (ROOT / 'local_build_vps.py').read_text():
            raise ValueError('First deploy the updated main with ramessenger-local-build.service')
        bodies = {}
        for path in FILES:
            body = run('gh', 'api', f'repos/{REPO}/contents/{path}?ref={args.ref}',
                       '-H', 'Accept: application/vnd.github.raw+json') + '\n'
            if path.endswith('.py'):
                compile(body, path, 'exec')
            bodies[path] = body
        try:
            account = pwd.getpwnam(USER)
            if not (SUPPORT / 'account.json').exists():
                raise ValueError('cm-support already exists outside this installer; refusing to take it over')
        except KeyError:
            run('useradd', '--create-home', '--shell', '/bin/sh', USER)
            account = pwd.getpwnam(USER)
        if account.pw_uid == 0 or account.pw_dir != '/home/cm-support':
            raise ValueError('Unexpected support account')
        write(SUPPORT / 'account.json', json.dumps({'user': USER, 'uid': account.pw_uid}) + '\n', 0o600)
        run('passwd', '-l', USER)
        # No docker group, broad sudo, shell or forwarding is granted.
        ssh_dir = Path(account.pw_dir) / '.ssh'
        ssh_dir.mkdir(mode=0o700, exist_ok=True)
        ssh_dir.chmod(0o700)
        os.chown(ssh_dir, account.pw_uid, account.pw_gid)
        authorized = ssh_dir / 'authorized_keys'
        old = authorized.read_text().splitlines() if authorized.exists() else []
        # The actor is a literal validated string. The caller's command is
        # passed as data by the entry script, never evaluated by a shell.
        old = authorized_lines(old, args.actor, public)
        write(ENTRY, bodies['scripts/support_ssh_entry.py'], 0o755)
        write(CONTROL, bodies['scripts/support_access.py'], 0o700)
        for name in ('diagnose_max_recipients.py', 'install_support_access.py'):
            write(SUPPORT / name, bodies['scripts/' + name], 0o700)
        policy = f'{USER} ALL=(root) NOPASSWD: {CONTROL}\n'
        with tempfile.NamedTemporaryFile(mode='w') as candidate:
            candidate.write(policy)
            candidate.flush()
            run('visudo', '-cf', candidate.name)
        write(Path('/etc/sudoers.d/cm-support'), policy, 0o440)
        registry[args.actor] = {'role': args.role, 'fingerprint': fingerprint, 'installed_ref': args.ref}
        write(registry_file, json.dumps(registry, indent=2) + '\n', 0o600)
        write(authorized, '\n'.join(old) + '\n', 0o600)
        os.chown(authorized, account.pw_uid, account.pw_gid)
        for unit in UNITS:
            write(Path('/etc/systemd/system') / unit, bodies['infra/systemd/' + unit], 0o644)
        run('systemd-analyze', 'verify', *(str(Path('/etc/systemd/system') / unit) for unit in UNITS))
        run('systemctl', 'daemon-reload')
        run('systemctl', 'enable', '--now', 'ramessenger-support-queue.timer')
        print('INSTALLED', USER, 'actor=' + args.actor, 'role=' + args.role)
        print('PUBLIC KEY:', fingerprint)
        print('SERVER HOST KEY:', run('ssh-keygen', '-lf', '/etc/ssh/ssh_host_ed25519_key.pub'))
        print('Allowed: status | logs | deploy-logs | audit' +
              (' | deploy <current-main-sha>' if args.role == 'maintainer' else ''))


if __name__ == '__main__':
    try:
        main()
    except BlockingIOError:
        raise SystemExit('Deployment/queue busy; retry after it completes. No locks were removed.')
    except ValueError as error:
        raise SystemExit(str(error))
    except (OSError, subprocess.SubprocessError) as error:
        raise SystemExit(type(error).__name__ + ': installation stopped; inspect the server locally')
