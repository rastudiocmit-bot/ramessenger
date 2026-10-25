"""Authenticated change hints. Idle streams never retain a database connection."""
import asyncio
import json
import threading
import time

from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse

from center_messenger.auth_security import AuthError


class StreamSlots:
    def __init__(self, limit=4):
        self.limit, self.active, self.lock = limit, {}, threading.Lock()

    def acquire(self, key):
        with self.lock:
            count = self.active.get(key, 0)
            if count >= self.limit:
                return False
            self.active[key] = count + 1
            return True

    def release(self, key):
        with self.lock:
            count = self.active.get(key, 0) - 1
            if count > 0:
                self.active[key] = count
            else:
                self.active.pop(key, None)


slots = StreamSlots()


def frame(event, payload):
    return f"event: {event}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"


async def events(request, reader, actor, workspace, initial, *, interval=2, heartbeat=15):
    key = (str(workspace), str(actor.session_id))
    if not slots.acquire(key):
        yield frame("retrying", {})
        return
    try:
        last, sent = initial, time.monotonic()
        yield "retry: 3000\n\n" + frame("ready", initial)
        while not await request.is_disconnected():
            await asyncio.sleep(interval)
            if await request.is_disconnected():
                break
            try:
                # The transaction rechecks the session and message-section grant.
                # It closes before yielding and before the next sleep.
                current = await run_in_threadpool(reader.event_revision, actor, workspace)
            except AuthError:
                yield frame("access_lost", {})
                return
            except Exception:
                yield frame("retrying", {})
                return
            if current != last:
                yield frame("change", current)
                last, sent = current, time.monotonic()
            elif time.monotonic() - sent >= heartbeat:
                yield frame("pulse", current)
                sent = time.monotonic()
    finally:
        slots.release(key)


async def response(request, reader, actor, workspace):
    initial = await run_in_threadpool(reader.event_revision, actor, workspace)
    return StreamingResponse(events(request, reader, actor, workspace, initial),
                             media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})
