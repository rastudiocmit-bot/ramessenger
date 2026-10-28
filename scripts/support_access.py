#!/usr/bin/python3
"""Project-scoped SSH dispatcher and durable deploy requests; no shell/eval.

Installed as root-owned cm-support-control. All deploy writes go through the
existing ramessenger-local-build.service, which owns the project deploy lock.
No secrets, root shell, arbitrary Docker command or other project is exposed.
"""
import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
import sys
import time
import urllib.request
from uuid import uuid4

ROOT = Path('/opt/center-messenger-github')
SUPPORT = ROOT / 'support'
BUILD_UNIT = 'ramessenger-local-build.service'
QUEUE_UNIT = 'ramessenger-support-queue.service'
REPO = 'rastudiocmit-bot/ramessenger'
SHA = re.compile(r'[a-f0-9]{40}')
ACTOR = re.compile(r'[a-z][a-z0-9-]{0,47}')


def execute(argv, *, timeout=30, stdin=None, merge_stderr=False):
    return subprocess.run(argv, input=stdin, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT if merge_stderr else subprocess.DEVNULL,
                          text=True, check=True, timeout=timeout).stdout.strip()


def current_main():
    sha = execute(['gh', 'api', f'repos/{REPO}/commits/main', '--jq', '.sha'], timeout=60)
    if not SHA.fullmatch(sha):
        raise ValueError('invalid_main_sha')
    return sha


def installed_sha():
    try:
        image = execute(['docker', 'inspect', '--format', '{{.Config.Image}}', 'center-messenger-api-1'])
        state = json.loads((ROOT / 'local-build-state.json').read_text())
        sha = state.get('last_deployed_sha', '')
        if not SHA.fullmatch(sha) or image != 'center-messenger:local-' + sha[:12]:
            return None
        with urllib.request.urlopen('http://127.0.0.1:8100/health/ready', timeout=10) as response:
            if response.status != 200 or json.load(response).get('status') != 'ready':
                return None
        return sha
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def parse_command(command):
    if not isinstance(command, str) or len(command) > 160:
        raise ValueError('invalid_command')
    words = shlex.split(command)
    if words in (['status'], ['logs'], ['deploy-logs'], ['audit']):
        return words
    if len(words) == 2 and words[0] == 'deploy' and SHA.fullmatch(words[1]):
        return words
    raise ValueError('allowed: status | logs | deploy-logs | audit | deploy <40-char-main-sha>')


class Queue:
    def __init__(self, path=None):
        self.path = path or SUPPORT / 'requests.sqlite'

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, actor TEXT NOT NULL, '
                       'sha TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL, '
                       'attempts INTEGER NOT NULL DEFAULT 0, reason TEXT)')
            yield db
            db.commit()
        finally:
            db.close()

    def enqueue(self, actor, sha):
        identifier = str(uuid4())
        with self.db() as db:
            db.execute("INSERT INTO requests VALUES (?,?,?,'queued',?,?,0,NULL)",
                       (identifier, actor, sha, time.time(), time.time()))
        return identifier

    def recover(self):
        # A worker/SSH disconnect cannot lose a durable request. The installed
        # SHA check below reconciles a promotion that completed before a crash.
        with self.db() as db:
            db.execute("UPDATE requests SET state='queued',reason='worker_restarted' WHERE state='running'")

    def claim(self):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM requests WHERE state='queued' ORDER BY created,id LIMIT 1").fetchone()
            if row:
                db.execute("UPDATE requests SET state='running',attempts=attempts+1,updated=? WHERE id=?",
                           (time.time(), row['id']))
                return dict(row)

    def finish(self, request, state, reason=None):
        with self.db() as db:
            db.execute('UPDATE requests SET state=?,reason=?,updated=? WHERE id=?',
                       (state, reason, time.time(), request['id']))

    def latest(self):
        with self.db() as db:
            return [dict(row) for row in db.execute('SELECT * FROM requests ORDER BY created DESC,id DESC LIMIT 20')]


@contextmanager
def worker_lock(path=None):
    with (path or SUPPORT / 'queue.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def process_request(queue, request):
    sha = request['sha']
    try:
        authorize(request['actor'], ['deploy', sha])
        # Reconcile first: an already deployed request stays successful even if
        # main moved since then. Never initiate a rollback for an older request.
        if installed_sha() == sha:
            queue.finish(request, 'deployed', 'already_installed')
            return
        if current_main() != sha:
            queue.finish(request, 'superseded', 'main_advanced')
            return
        # systemd joins any existing start job and continues independently of
        # SSH. A second run may be needed if that job built a previous main.
        for _ in range(2):
            execute(['systemctl', 'start', BUILD_UNIT], timeout=2500)
            if installed_sha() == sha:
                queue.finish(request, 'deployed')
                return
            if current_main() != sha:
                queue.finish(request, 'superseded', 'main_advanced')
                return
        queue.finish(request, 'failed', 'not_promoted_check_deploy_logs')
    except Exception as error:
        # Preserve a safe, bounded reason, never raw subprocess output or env.
        queue.finish(request, 'failed', type(error).__name__)


def drain():
    try:
        with worker_lock():
            queue = Queue()
            queue.recover()
            for _ in range(20):
                request = queue.claim()
                if not request:
                    return
                print(json.dumps({key: request[key] for key in ('id', 'actor', 'sha')},
                                 ensure_ascii=False), flush=True)
                process_request(queue, request)
    except BlockingIOError:
        return  # The other worker and the 20-second timer own the remaining work.


def authorize(actor, words):
    if not ACTOR.fullmatch(actor):
        raise ValueError('invalid_actor')
    registry = json.loads((SUPPORT / 'actors.json').read_text())
    entry = registry.get(actor)
    if not entry or entry.get('role') not in {'diagnostic', 'maintainer'}:
        raise ValueError('access_revoked')
    if words[0] == 'deploy' and entry['role'] != 'maintainer':
        raise ValueError('deploy_not_allowed')


def audit():
    script = (SUPPORT / 'diagnose_max_recipients.py').read_text()
    # Respect the existing browser lease. No killing browsers or deleting locks.
    for attempt in range(6):
        result = execute(['docker', 'exec', '-i', 'center-messenger-api-1',
                          '/opt/venv/bin/python', '-'], stdin=script, timeout=180)
        data = json.loads(result)
        if data.get('error_code') != 'browser_busy' or attempt == 5:
            print(result)
            return
        time.sleep(3)


def dispatch(actor, command):
    words = parse_command(command)
    authorize(actor, words)
    action = words[0]
    event = {'at': time.time(), 'actor': actor, 'action': action}
    if action == 'deploy':
        event['sha'] = words[1]
    with (SUPPORT / 'events.jsonl').open('a') as stream:
        stream.write(json.dumps(event) + '\n')
    print(json.dumps({'actor': actor, 'action': action}), file=sys.stderr, flush=True)
    if action == 'deploy':
        sha = words[1]
        if current_main() != sha:
            raise ValueError('requested_sha_is_not_current_main')
        identifier = Queue().enqueue(actor, sha)
        # Enqueue is durable before wakeup; the timer recovers a missed wakeup.
        try:
            execute(['systemctl', 'start', '--no-block', QUEUE_UNIT])
        except subprocess.CalledProcessError:
            pass
        print(json.dumps({'request_id': identifier, 'sha': sha, 'state': 'queued'}))
    elif action == 'status':
        print(json.dumps({'project': REPO, 'installed_sha': installed_sha(),
                          'requests': Queue().latest()}, ensure_ascii=False, indent=2))
    elif action == 'logs':
        print(execute(['docker', 'logs', '--since', '30m', '--tail', '300',
                       'center-messenger-api-1'], merge_stderr=True))
    elif action == 'deploy-logs':
        print(execute(['journalctl', '-u', BUILD_UNIT, '-u', QUEUE_UNIT,
                       '-n', '150', '--no-pager', '-o', 'cat']))
    elif action == 'audit':
        audit()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--drain', action='store_true')
    parser.add_argument('--actor')
    parser.add_argument('--command')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('root dispatcher required; use the restricted SSH entry')
    os.umask(0o077)
    if args.drain and args.actor is None and args.command is None:
        drain()
    elif not args.drain and args.actor and args.command:
        dispatch(args.actor, args.command)
    else:
        parser.error('invalid dispatcher mode')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'error': str(error) if isinstance(error, ValueError)
                          else type(error).__name__}), file=sys.stderr)
        sys.exit(1)
