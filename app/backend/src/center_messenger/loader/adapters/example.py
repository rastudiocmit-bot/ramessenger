"""Offline transport example, also imported by the contract tests.

For durable execution wrap fetch in PageReader(PageStore(...), next_scope,
fetch, project=...). This CLI deliberately needs neither DB nor credentials.
"""

import json
from datetime import UTC, datetime
from threading import Event

from ..contracts import Capability as C, Manifest, Operation, Page, SourceEvent, Step
from ..registry import Registry
from ..scheduler import Scheduler

MANIFEST = Manifest("example", frozenset({C.RECEIVE, C.CALLS}))


def fetch(scope, cursor, operation):
    operation.remaining(5)  # pass this remaining budget to a real HTTP/browser call
    if cursor.get("after") == "2":
        return Page((), cursor, False)
    event = SourceEvent("calls" if scope.stream == "calls" else "messages", "2",
                        "call" if scope.stream == "calls" else "message",
                        datetime(2026, 10, 26, 12, tzinfo=UTC), {"text": "Вымышленный пример"}, crm_id="7")
    return Page((event,), {"after": "2"}, False)


def main():
    from types import SimpleNamespace
    result = []
    registry = Registry()
    def steps():
        def read(stream):
            page = fetch(SimpleNamespace(stream=stream), {}, Operation(60, Event(), lambda: 0))
            page.validate()
            result.extend({"kind": e.kind, "source_id": e.source_id, "crm_id": e.crm_id} for e in page.events)
            return True
        return [Step("messages", C.RECEIVE, lambda op: read("messages")),
                Step("calls", C.CALLS, lambda op: read("calls"))]
    registry.register(MANIFEST, steps)
    scheduler = Scheduler(registry, clock=lambda: 0)
    scheduler.tick("messages"); scheduler.tick("messages")
    print(json.dumps({"offline": True, "persistent": False, "events": result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
