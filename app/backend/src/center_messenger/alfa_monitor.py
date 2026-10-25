"""Bounded diagnostic view of the worker; never a source of sync checkpoints.

The deployed API has one process. Its HTTP threads can observe the worker's
current client without committing a partial message transaction. Lease matching
prevents a stale attempt from being shown as the current owner. Committed
snapshots remain in history_cursor for restarts and other API processes.
"""

from copy import deepcopy
from datetime import UTC, datetime
from threading import Lock
from uuid import uuid4

RECENT_LIMIT = 30


class AlfaSyncMonitor:
    def __init__(self):
        self._lock, self._runs = Lock(), {}

    def begin(self, workspace, connection, lease, previous, value):
        with self._lock:
            self._runs[str(workspace)] = (str(connection), str(lease), {
                **deepcopy(value), "recent": deepcopy((previous or {}).get("recent", []))[-RECENT_LIMIT:],
                "last_client": deepcopy((previous or {}).get("last_client")),
            })

    def update(self, workspace, connection, lease, value):
        with self._lock:
            run = self._runs.get(str(workspace))
            if not run or run[:2] != (str(connection), str(lease)):
                return None
            value = deepcopy(value)
            event = value.pop("event", None)
            state = run[2]
            state.update(value, updated_at=datetime.now(UTC).isoformat())
            if value.get("client"):
                state["last_client"] = deepcopy(value["client"])
            if event:
                state["recent"].insert(0, {**{key: state.get(key) for key in ("branch_id", "direction", "page", "flow")},
                                           **event, "id": str(uuid4()),
                                           "at": state["updated_at"], "committed": False})
                del state["recent"][RECENT_LIMIT:]
            return True

    def snapshot(self, workspace, connection, lease, *, committed=False):
        with self._lock:
            run = self._runs.get(str(workspace))
            if not run or run[:2] != (str(connection), str(lease)):
                return None
            state = deepcopy(run[2])
        if committed:
            for event in state.get("recent", []):
                if "rolled_back" not in event:
                    event["committed"] = True
        return state

    def committed(self, workspace, connection, lease):
        with self._lock:
            run = self._runs.get(str(workspace))
            if run and run[:2] == (str(connection), str(lease)):
                for event in run[2].get("recent", []):
                    if "rolled_back" not in event:
                        event["committed"] = True

    def rollback(self, workspace, connection, lease):
        with self._lock:
            run = self._runs.get(str(workspace))
            if run and run[:2] == (str(connection), str(lease)):
                for event in run[2].get("recent", []):
                    if not event.get("committed"):
                        event["rolled_back"] = True

    def close(self, workspace, connection, lease):
        with self._lock:
            run = self._runs.get(str(workspace))
            if run and run[:2] == (str(connection), str(lease)):
                del self._runs[str(workspace)]
