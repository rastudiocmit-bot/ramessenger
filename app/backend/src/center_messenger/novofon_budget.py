"""Opaque provider counters in the existing global throttle table.

Only hashes, counts and timestamps; no credentials/phone/account identifiers.
Reservations commit before HTTP IO, observations commit independently of job
success. API checks and telephony workers share the same PostgreSQL counters.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import math
import time
from sqlalchemy import text

from center_messenger.novofon_quota import Budget, SLOTS, MAX_COUNTER, digest


class PostgresStore:
    def __init__(self, engine):
        self.engine = engine

    @contextmanager
    def transaction(self, scopes):
        keys = {digest(scope + ':' + slot): (scope, slot) for scope in scopes for slot in SLOTS}
        with self.engine.begin() as db:
            for scope in sorted(scopes):
                # The lock namespaces include the provider and token/account.
                lock = int(digest('lock:' + scope)[:16], 16)
                if lock >= 2**63:lock -= 2**64
                db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': lock})
            rows = db.execute(text('SELECT bucket_hash,attempts,window_at FROM auth_throttles '
                                   'WHERE bucket_hash=ANY(CAST(:keys AS text[]))'), {'keys': list(keys)}).mappings()
            values = {scope: {} for scope in scopes}
            for row in rows:
                scope, slot = keys[row['bucket_hash']]
                values[scope][slot] = (row['attempts'] - 1, row['window_at'].timestamp())
            original = {scope: dict(slots) for scope, slots in values.items()}
            yield values
            for scope, slots in values.items():
                for slot, (value, at) in slots.items():
                    if original[scope].get(slot) == (value, at):continue
                    db.execute(text('INSERT INTO auth_throttles(bucket_hash,attempts,window_at) '
                        'VALUES(:key,:value,:at) ON CONFLICT(bucket_hash) DO UPDATE SET '
                        'attempts=EXCLUDED.attempts,window_at=EXCLUDED.window_at'),
                        {'key': digest(scope + ':' + slot), 'value': min(MAX_COUNTER, max(0, int(value))) + 1,
                         'at': datetime.fromtimestamp(at, timezone.utc)})


def client_factory(engine):
    from center_messenger.novofon_client import Client
    store = PostgresStore(engine)
    return lambda key: Client(key, budget=Budget(key, store))


def account_snapshot(engine, account, clock=time.time):
    if not account:return {}
    scope, now = digest('account:' + str(account)), clock()
    with PostgresStore(engine).transaction([scope]) as states:
        state, result = states[scope], {}
        for kind in ('minute', 'day'):
            cap, observed = state.get(kind + '_cap', (0, 0))
            left, until = state.get(kind + '_left', (0, 0))
            if observed > 0 and until > now:
                result.update({kind + '_limit': cap, kind + '_remaining': left,
                               kind + '_reset': math.ceil(until - now)})
        at = state.get('reported_day', (0, 0))[1]
        if at:result['observed_at'] = datetime.fromtimestamp(at, timezone.utc).isoformat()
        return result
