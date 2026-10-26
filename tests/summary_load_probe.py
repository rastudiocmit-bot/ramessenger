"""Synthetic SQL timing probe in an isolated test database, never production.

Creates/removes a random test schema. Requires CM_TEST_DATABASE_URL and schema
creation privileges. Uses cm_app with FORCE RLS during reads. Measures Summary
SQL, not HTTP/authentication, API pool contention or browser latency. A short
CI probe does not replace the specification's 30-minute representative load run.
"""
import argparse
import json
import math
import time
from datetime import timedelta

from sqlalchemy import text

from test_message_sync import MessageSyncTests
from center_messenger.cabinet_summary import calculate
from center_messenger.chat_store import refresh_unread


def percentile(values, fraction):
    return round(sorted(values)[max(0, math.ceil(len(values) * fraction)-1)], 2) if values else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--messages", type=int, default=60000)
    parser.add_argument("--seconds", type=int, default=10)
    args = parser.parse_args()
    if not 1000 <= args.messages <= 1000000 or not 1 <= args.seconds <= 3600:
        parser.error("messages: 1000..1000000; seconds: 1..3600")
    MessageSyncTests.setUpClass()
    fixture = MessageSyncTests()
    try:
        fixture.setUp()
        fixture.project(fixture.row())
        conversation = fixture.conversation()["id"]
        recent = args.messages // 10
        fixture.db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        # Seed an at-rest history, not a benchmark of per-message write triggers.
        # Foreign-key constraints remain enabled; only this isolated schema is touched.
        fixture.db.execute(text("ALTER TABLE messages DISABLE TRIGGER USER"))
        fixture.db.execute(text("INSERT INTO messages(workspace_id,id,conversation_id,direction,actor_type,text,occurred_at) "
            "SELECT :w,md5('summary-probe-'||i)::uuid,:c,CASE WHEN i%2=1 THEN 'in' ELSE 'out' END,"
            "CASE WHEN i%2=1 THEN 'contact' ELSE 'human' END,'Synthetic message',"
            "CAST(:at AS timestamptz) - CASE WHEN i>:recent THEN interval '365 days' ELSE interval '1 day' END "
            "+ i * interval '1 second' FROM generate_series(1,:n) AS i"),
            {"w": fixture.workspace, "c": conversation, "at": fixture.at, "n": args.messages, "recent": recent})
        fixture.db.execute(text("ALTER TABLE messages ENABLE TRIGGER USER"))
        fixture.db.execute(text("UPDATE conversations SET stored_message_count=:n WHERE workspace_id=:w AND id=:c"),
                           {"w": fixture.workspace, "c": conversation, "n": args.messages+1})
        refresh_unread(fixture.db, fixture.workspace, conversation)
        fixture.db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        fixture.db.execute(text("ANALYZE messages"))
        fixture.db.execute(text("ANALYZE conversations"))
        fixture.db.execute(text("ANALYZE outbox_events"))
        fixture.db.execute(text("SELECT set_config('app.workspace_id',:w,true)"), {"w": str(fixture.workspace)})
        fixture.db.execute(text(f'GRANT USAGE ON SCHEMA "{fixture.schema}" TO cm_app'))
        fixture.db.execute(text("SET LOCAL ROLE cm_app"))
        fixture.db.execute(text("SET LOCAL jit=off"))
        fixture.db.execute(text("SET LOCAL statement_timeout=2500"))
        role = fixture.db.execute(text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user")).one()
        assert not any(role), "Probe must run without RLS bypass"
        durations, stages = [], {}
        until = time.monotonic() + args.seconds
        while time.monotonic() < until:
            measurements = []
            started = time.perf_counter()
            value = calculate(fixture.db, fixture.workspace, "Europe/Moscow",
                              now=fixture.at+timedelta(hours=12), diagnostics=measurements)
            assert value["incoming"] == recent // 2 + 1
            assert value["average_response_seconds"] == 1
            durations.append((time.perf_counter() - started)*1000)
            for item in measurements:
                stages.setdefault(item["stage"], []).append(item["duration_ms"])
        print(json.dumps({"type": "synthetic_sql_probe", "messages": args.messages+1,
            "conversations": 1, "readers": 1, "background_writers": 0, "rls": True,
            "samples": len(durations), "duration_seconds": args.seconds,
            "p50_ms": percentile(durations, .5), "p95_ms": percentile(durations, .95),
            "stages_p95_ms": {k:percentile(v, .95) for k,v in stages.items()}}, indent=2))
    finally:
        if getattr(fixture, "transaction", None):
            fixture.tearDown()
            fixture.doCleanups()
        MessageSyncTests.tearDownClass()


if __name__ == "__main__":
    main()
