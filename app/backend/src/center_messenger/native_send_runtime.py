"""Supervised, single-use browser sender. Secrets travel only over private pipes.

The child cannot submit until the parent has committed its durable intent. A
receipt is reported before cookie export/cleanup. Linux pidfds fence cleanup to
this operation, including Chromium descendants which create their own sessions.
"""
import json
import logging
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time

from center_messenger.alfa_max_browser import AlfaBrowserError

BUDGET = 90
POST_TIMEOUT = 20
HEARTBEAT = 30
MAX_PACKET = 4_000_000
logger = logging.getLogger(__name__)


def runtime_supported():
    try:
        # A translated/emulated PID view is unsafe for descendant discovery.
        return (sys.platform.startswith('linux') and hasattr(os, 'pidfd_open')
                and hasattr(signal, 'pidfd_send_signal')
                and int(os.readlink('/proc/self')) == os.getpid())
    except (OSError, ValueError):
        return False


def _process_info(path):
    fields = path.read_text().rsplit(')', 1)[1].split()
    return int(fields[1]), fields[19], fields[0]


def _processes():
    result = {}
    for path in Path('/proc').glob('[0-9]*/stat'):
        try:
            result[int(path.parent.name)] = _process_info(path)
        except (OSError, ValueError, IndexError):
            continue
    return result


class ProcessTree:
    def __init__(self, process):
        self.process, self.owned = process, {}
        try:
            self.discover()
            if process.pid not in self.owned:
                raise RuntimeError('Native process cannot be tracked')
            self._signal(process.pid, 0)
        except Exception:
            for fd, _started in self.owned.values():
                os.close(fd)
            raise

    def discover(self):
        table = _processes()
        pending = ({self.process.pid} if not self.owned else
                   {pid for pid, (_fd, started) in self.owned.items()
                    if table.get(pid, (None, None))[1] == started})
        changed = True
        while changed:
            changed = False
            for pid, (parent, started, _state) in table.items():
                if pid not in pending and parent in pending:
                    pending.add(pid)
                    changed = True
        added = []
        for pid in pending - self.owned.keys():
            if pid not in table:
                continue
            try:
                fd = os.pidfd_open(pid)
                # Do not retain a recycled PID between enumeration and pidfd_open.
                if _process_info(Path('/proc') / str(pid) / 'stat')[:2] != table[pid][:2]:
                    os.close(fd)
                    continue
                self.owned[pid] = (fd, table[pid][1])
                added.append(pid)
            except (ProcessLookupError, FileNotFoundError):
                pass
        return added

    def _signal(self, pid, sig):
        try:
            signal.pidfd_send_signal(self.owned[pid][0], sig)
        except ProcessLookupError:
            pass

    def close(self):
        # Freeze parents before discovering descendants, so no new browser can
        # escape between our snapshot and termination. Never use pkill/killall.
        try:
            for pid in list(self.owned):
                self._signal(pid, signal.SIGSTOP)
            for _ in range(16):
                added = self.discover()
                for pid in added:
                    self._signal(pid, signal.SIGSTOP)
                if not added:
                    break
            for pid in reversed(self.owned):
                self._signal(pid, signal.SIGKILL)
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                logger.error('Native browser process did not terminate promptly')
            for pid in self.owned:
                try:
                    os.waitpid(pid, os.WNOHANG)
                except (ChildProcessError, ProcessLookupError):
                    pass
        finally:
            for fd, _started in self.owned.values():
                os.close(fd)
            for pipe in (self.process.stdin, self.process.stdout):
                pipe.close()


def run_send(origin, state, branch, customer, message, *, before_post=None,
             on_accepted=None, heartbeat=None, stopping=None, budget=BUDGET,
             command=None):
    if not runtime_supported():
        raise AlfaBrowserError('browser_unavailable')
    start = time.monotonic()
    deadline = start + budget
    # Reserve cleanup time within the total operation budget.
    work_until = deadline - min(2, budget / 10)
    payload = {'origin': origin, 'state': {k: state[k] for k in ('cookies', 'origins') if k in state},
               'branch': branch, 'customer': customer, 'text': message, 'deadline': work_until}
    pending = json.dumps(payload, ensure_ascii=False).encode() + b'\n'
    if len(pending) > MAX_PACKET:
        raise AlfaBrowserError('form_unavailable')
    process = subprocess.Popen(command or [sys.executable, '-m', __name__],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, start_new_session=True, bufsize=0)
    try:
        tree = ProcessTree(process)
    except Exception:
        # The input has not been written: this child cannot have opened a browser.
        process.kill()
        process.wait(timeout=1)
        process.stdin.close()
        process.stdout.close()
        raise AlfaBrowserError('browser_unavailable') from None
    selector = selectors.DefaultSelector()
    os.set_blocking(process.stdin.fileno(), False)
    os.set_blocking(process.stdout.fileno(), False)
    selector.register(process.stdout, selectors.EVENT_READ)
    buffer, post_started, accepted = b'', False, None
    next_heartbeat, next_discovery = start, start
    try:
        while True:
            now = time.monotonic()
            if now >= work_until or (stopping is not None and stopping.is_set()):
                if accepted:
                    return accepted
                raise AlfaBrowserError('send_unknown' if post_started else 'send_prepare_timeout')
            if not accepted and heartbeat and now >= next_heartbeat:
                heartbeat()
                next_heartbeat = time.monotonic() + HEARTBEAT
            if now >= next_discovery:
                tree.discover()
                next_discovery = now + .1
            if pending:
                try:
                    size = os.write(process.stdin.fileno(), pending[:65536])
                    pending = pending[size:]
                except BlockingIOError:
                    pass
            for key, _mask in selector.select(.05):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    if accepted:
                        return accepted
                    raise AlfaBrowserError('send_unknown' if post_started else 'send_prepare_failed')
                buffer += chunk
                if len(buffer) > MAX_PACKET:
                    raise AlfaBrowserError('send_unknown' if post_started else 'send_prepare_failed')
                while b'\n' in buffer:
                    packet, buffer = buffer.split(b'\n', 1)
                    event = json.loads(packet)
                    kind = event.get('event')
                    if kind == 'before_post':
                        if post_started or accepted:
                            raise AlfaBrowserError('send_unknown')
                        if before_post:
                            before_post()
                        # Commit precedes permission to POST. A lost permission
                        # packet is conservatively unknown, never an automatic retry.
                        post_started = True
                        remaining = work_until - time.monotonic()
                        if remaining <= 0:
                            raise AlfaBrowserError('send_unknown')
                        pending += json.dumps({'submit': True, 'timeout_ms': max(1, int(min(POST_TIMEOUT, remaining) * 1000))}).encode() + b'\n'
                    elif kind == 'accepted':
                        if not post_started or event.get('result', {}).get('accepted') is not True:
                            raise AlfaBrowserError('send_unknown' if post_started else 'send_prepare_failed')
                        if accepted is None:
                            accepted = event['result']
                            if on_accepted:
                                on_accepted(accepted)
                        work_until = min(work_until, time.monotonic() + 2)
                    elif kind == 'result':
                        if accepted and event.get('result', {}).get('accepted') is True:
                            return event['result']
                        raise AlfaBrowserError('send_unknown' if post_started else 'send_prepare_failed')
                    elif kind == 'error':
                        if accepted:
                            return accepted
                        code = event.get('code')
                        if not isinstance(code, str) or not code.replace('_', '').isalnum() or len(code) > 64:
                            code = 'send_unknown' if post_started else 'send_prepare_failed'
                        if post_started and code not in {'crm_rejected', 'max_recipient_missing'}:
                            code = 'send_unknown'
                        error = AlfaBrowserError(code)
                        if isinstance(event.get('browser_state'), dict):
                            error.browser_state = event['browser_state']
                        raise error
                    else:
                        raise AlfaBrowserError('send_unknown' if post_started else 'send_prepare_failed')
    except AlfaBrowserError as error:
        if accepted:
            return accepted
        if post_started and error.code not in {'crm_rejected', 'max_recipient_missing', 'send_unknown'}:
            raise AlfaBrowserError('send_unknown') from None
        raise
    except Exception:
        if accepted:
            return accepted
        raise AlfaBrowserError('send_unknown' if post_started else 'send_prepare_failed') from None
    finally:
        selector.close()
        tree.close()
        logger.info('Native send attempt finished duration_ms=%s post_started=%s accepted=%s',
                    round((time.monotonic() - start) * 1000), post_started, bool(accepted))


def _emit(value):
    sys.stdout.write(json.dumps(value, ensure_ascii=False) + '\n')
    sys.stdout.flush()


def main():
    # No credentials in argv, environment overrides, temporary files or logs.
    from center_messenger.alfa_max_browser import AlfaMaxBrowser
    data = json.loads(sys.stdin.buffer.readline(MAX_PACKET + 1))
    def prepare():
        _emit({'event': 'before_post'})
        answer = json.loads(sys.stdin.buffer.readline(1024))
        if answer.get('submit') is not True:
            raise AlfaBrowserError('send_prepare_failed')
        return answer['timeout_ms']
    try:
        result = AlfaMaxBrowser()._send_inline(data['origin'], data['state'], data['branch'], data['customer'],
            data['text'], deadline=data['deadline'], before_post=prepare,
            on_accepted=lambda receipt: _emit({'event': 'accepted', 'result': receipt}))
        _emit({'event': 'result', 'result': result})
    except AlfaBrowserError as error:
        packet = {'event': 'error', 'code': error.code}
        if isinstance(getattr(error, 'browser_state', None), dict):
            packet['browser_state'] = error.browser_state
        _emit(packet)
    except Exception:
        _emit({'event': 'error', 'code': 'send_prepare_failed'})


if __name__ == '__main__':
    main()
