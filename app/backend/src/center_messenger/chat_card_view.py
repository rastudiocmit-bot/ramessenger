"""Bounded, private, revision-scoped card views; the CRM facts remain in PostgreSQL."""
from collections import OrderedDict
import hashlib
import json
import threading
import time


def compact_card(family):
    # Aggregates and the previous/next lesson were calculated from the full facts
    # by DirectoryReader. The chat does not render raw yearly lesson arrays.
    result = dict(family)
    result["students"] = []
    for person in family["students"]:
        student = dict(person)
        student["cards"] = []
        for source in person["cards"]:
            card = {key: value for key, value in source.items() if key not in {"lessons", "subscriptions"}}
            card["lessons_ready"] = isinstance(source.get("lessons"), list) or source.get("lessons_ready", False)
            card["subscriptions_ready"] = isinstance(source.get("subscriptions"), list) or source.get("subscriptions_ready", False)
            student["cards"].append(card)
        result["students"].append(student)
    return result


class CardViews:
    def __init__(self, *, limit=96, byte_limit=8 * 1024 * 1024, ttl=60):
        self.limit, self.byte_limit, self.ttl = limit, byte_limit, ttl
        self.entries, self.bytes, self.lock = OrderedDict(), 0, threading.Lock()

    def get(self, key):
        with self.lock:
            entry = self.entries.get(key)
            if not entry or entry[0] <= time.monotonic():
                if entry:
                    self.bytes -= self.entries.pop(key)[1]
                return None
            self.entries.move_to_end(key)
            return entry[2]

    def put(self, key, family):
        encoded = json.dumps(compact_card(family), sort_keys=True, default=str, ensure_ascii=False).encode()
        value = json.loads(encoded)
        value["version"] = hashlib.blake2s(encoded).hexdigest()
        with self.lock:
            if key in self.entries:
                self.bytes -= self.entries.pop(key)[1]
            if len(encoded) <= self.byte_limit:
                self.entries[key] = (time.monotonic() + self.ttl, len(encoded), value)
                self.bytes += len(encoded)
                while len(self.entries) > self.limit or self.bytes > self.byte_limit:
                    self.bytes -= self.entries.popitem(last=False)[1][1]
        return value

    @staticmethod
    def reply(value, known_version):
        return {"not_modified": True, "version": value["version"]} if value["version"] == known_version else value
