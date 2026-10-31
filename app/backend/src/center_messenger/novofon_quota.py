"""Data API budgets. Time-based pacing, shared reservations, no network sleeps."""
from contextlib import contextmanager
import copy
import hashlib
import math
from threading import RLock
import time

PERIODS = {'minute': (60, 6), 'day': (86400, 500)}
METHODS = ('get.account', 'get.calls_report', 'get.sip_lines')
SLOTS = ('minute_left', 'minute_cap', 'day_left', 'day_cap', 'pace', 'blocked', 'reported_day',
         *('cost_' + name for name in METHODS))
MAX_COUNTER = 2_000_000_000


def digest(value):
    return hashlib.sha256(('center-messenger:novofon:data:v1:' + value).encode()).hexdigest()


class QuotaWait(Exception):
    def __init__(self, seconds):
        self.retry_after = max(1, math.ceil(seconds))
        super().__init__('quota_exhausted')


class MemoryStore:
    """Single CLI client; production uses the shared PostgreSQL counter store."""
    def __init__(self):
        self.rows, self.lock = {}, RLock()

    @contextmanager
    def transaction(self, scopes):
        with self.lock:
            values = {scope: copy.deepcopy(self.rows.get(scope, {})) for scope in scopes}
            yield values
            self.rows.update(values)


class Budget:
    def __init__(self, token, store=None, clock=time.time):
        self.scopes = [digest('token:' + token)]
        self.store, self.clock = store or MemoryStore(), clock

    def bind_account(self, account):
        scope = digest('account:' + str(account))
        if scope not in self.scopes:
            self.scopes.append(scope)

    @staticmethod
    def _cost(state, method):
        return max(1, state.get('cost_' + method, (1, 0))[0])

    @staticmethod
    def _periods(state, now):
        for kind, (seconds, fallback) in PERIODS.items():
            # The fallbacks are our conservative bootstrap budget, not claims
            # about the provider's tariff. The first metadata replaces them.
            cap = state.setdefault(kind + '_cap', (fallback, 0))[0]
            left, until = state.get(kind + '_left', (cap, 0))
            if until <= now:
                state[kind + '_left'] = (cap, now + seconds)

    @staticmethod
    def _interval(state, cost, now):
        return max(1., *(max(1., until - now) * cost / max(1, left)
                         for kind in PERIODS for left, until in [state[kind + '_left']]))

    def reserve(self, method):
        if method not in METHODS:
            raise ValueError('Unsupported quota method')
        now, waits = self.clock(), []
        with self.store.transaction(self.scopes) as states:
            for state in states.values():
                self._periods(state, now)
                cost = self._cost(state, method)
                interval = self._interval(state, cost, now)
                known=any(state.get(kind+'_cap',(0,0))[1]>0 for kind in PERIODS)
                credit, at = state.get('pace', (3000 if known else 1000, now))
                credit = min(3000, credit + math.floor(max(0, now - at) * 1000 / interval))
                state['pace'] = (credit, now)
                if credit < 1000:
                    waits.append((1000 - credit) / 1000 * interval)
                waits.append(max(0, state.get('blocked', (0, 0))[1] - now))
                for kind in PERIODS:
                    left, until = state[kind + '_left']
                    if left < cost:
                        waits.append(until - now)
            delay = max(waits, default=0)
            if delay <= 0:
                for state in states.values():
                    cost = self._cost(state, method)
                    state['pace'] = (state['pace'][0] - 1000, now)
                    for kind in PERIODS:
                        left, until = state[kind + '_left']
                        state[kind + '_left'] = (left - cost, until)
        # Commit even a denied request's refill/reset state before signalling.
        if delay > 0:
            raise QuotaWait(delay)

    def observe(self, method, metadata, *, retry_after=0, limit_type=None, exceeded=False):
        now = self.clock()
        limits = metadata.get('limits', {}) if isinstance(metadata, dict) else {}
        with self.store.transaction(self.scopes) as states:
            for state in states.values():
                self._periods(state, now)
                was_known=any(state.get(kind+'_cap',(0,0))[1]>0 for kind in PERIODS)
                # All costs are estimates in provider quota units. External
                # consumers can make the estimate larger, never over-credit us.
                day = limits.get('day_remaining')
                previous, sampled = state.get('reported_day', (0, 0))
                if type(day) is int and 0 <= day <= MAX_COUNTER:
                    if sampled and sampled > now - 300 and previous > day:
                        name = 'cost_' + method
                        state[name] = (max(self._cost(state, method), previous - day), now)
                    state['reported_day'] = (day, now)
                for kind, (seconds, fallback) in PERIODS.items():
                    left, cap, reset = (limits.get(kind + suffix) for suffix in ('_remaining', '_limit', '_reset'))
                    if type(left) is not int or not 0 <= left <= MAX_COUNTER:
                        continue
                    if type(cap) is not int or not 0 <= cap <= MAX_COUNTER:
                        cap = max(left, state[kind + '_cap'][0])
                    reset = reset if type(reset) is int and 0 <= reset <= seconds else seconds
                    old_cap = state[kind + '_cap']
                    old_left, old_until = state[kind + '_left']
                    until = now + reset + 1
                    # Concurrent/out-of-order replies must not refund requests
                    # already reserved by another API/worker process.
                    if old_cap[1] > 0 and old_until > now:
                        left, until = min(left, old_left), max(until, old_until)
                    state[kind + '_cap'] = (cap, until)
                    state[kind + '_left'] = (min(cap, left), until)
                    if left == 0:
                        retry_after = max(retry_after, until - now)
                if not was_known and any(state.get(kind+'_cap',(0,0))[1]>0 for kind in PERIODS) and 'pace' in state:
                    credit, at=state['pace']
                    state['pace']=(min(3000,credit+2000),at)
                if exceeded:
                    kinds = [limit_type] if limit_type in PERIODS else ['minute' if limits else 'day']
                    for kind in kinds:
                        left, until = state[kind + '_left']
                        state[kind + '_left'] = (0, until)
                        retry_after = max(retry_after, until - now)
                if retry_after > 0:
                    state['blocked'] = (1, max(now + retry_after, state.get('blocked', (0, 0))[1]))
        return max(0, math.ceil(retry_after))


def public_limits(metadata):
    limits = metadata.get('limits', {}) if isinstance(metadata, dict) else {}
    return {key: value for key, value in limits.items()
            if key in {kind + suffix for kind in PERIODS for suffix in ('_limit', '_remaining', '_reset')}
            and type(value) is int and 0 <= value <= MAX_COUNTER}
