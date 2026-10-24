"""Bounded list view cursors. Expiry/restart safely falls back to a full prefix.

Rows are still selected in one database snapshot. A cursor reduces transfer, not
consistency: inserts, priority moves and removals replace the exact loaded prefix.
It carries no authority and is always bound to actor, workspace and filters.
"""
import hashlib
import json
import threading
import time
from collections import OrderedDict
from uuid import uuid4


class ListWindows:
    def __init__(self, *, limit=64, ttl=300):
        self.limit, self.ttl = limit, ttl
        self.lock, self.views = threading.Lock(), OrderedDict()

    def previous(self, token, scope):
        with self.lock:
            saved = self.views.get(token)
            if saved and saved['scope'] == scope and saved['expires'] > time.monotonic():
                return saved
        return None

    def unchanged(self, previous, fingerprint, size):
        if previous and previous['fingerprint'] == fingerprint and previous['size'] == size:
            return previous['meta'] | {'conversations': [], 'not_modified': True,
                                       'window_token': previous['token']}
        return None

    def publish(self, previous, scope, fingerprint, size, result):
        versions = {row['id']: hashlib.blake2s(json.dumps(row, sort_keys=True, default=str).encode()).hexdigest()
                    for row in result['conversations']}
        meta = {key: value for key, value in result.items() if key != 'conversations'}
        token = str(uuid4())
        saved = dict(token=token, scope=scope, fingerprint=fingerprint, size=size, versions=versions,
                     expires=time.monotonic()+self.ttl, meta=meta)
        with self.lock:
            if previous:
                self.views.pop(previous["token"], None)
            self.views[token] = saved
            while len(self.views) > self.limit:
                self.views.popitem(last=False)
        old = previous['versions'] if previous else {}
        return meta | {'window_token': token, 'reset': previous is None,
                       'order': list(versions),
                       'conversations': [row for row in result['conversations'] if old.get(row['id']) != versions[row['id']]]}
