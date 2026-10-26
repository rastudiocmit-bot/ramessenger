"""Bounded lanes and fair scheduling of resumable steps, never of infinite loops.

Database leases remain authoritative across processes. This scheduler controls
local resource use, not external delivery semantics or provider authentication.
"""

import logging
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from .contracts import Operation, OperationCancelled, RetryLater, StepResult

logger = logging.getLogger(__name__)
DEFAULT_LIMITS = {"messages": 4, "maintenance": 3, "media": 1}


@dataclass
class State:
    due: float = 0
    running: bool = False
    attempts: int = 0
    last_started: float = 0
    last_finished: str | None = None
    error: str | None = None


class Scheduler:
    def __init__(self, registry, *, limits=None, clock=time.monotonic):
        self.steps = registry.resolve()
        self.registration_errors = dict(registry.errors)
        for provider, code in self.registration_errors.items():
            logger.warning("Loader adapter disabled provider=%s code=%s", provider, code)
        self.limits = dict(DEFAULT_LIMITS if limits is None else limits)
        if set(self.limits) != set(DEFAULT_LIMITS) or any(type(v) is not int or not 1 <= v <= 4 for v in self.limits.values()) or sum(self.limits.values()) > 8:
            raise ValueError("Loader requires bounded lanes, at most eight workers")
        self.clock = clock
        self.stopping = threading.Event()
        self._lock = threading.Lock()
        self._states = {key: State() for key in self.steps}
        self._threads = []
        self._started = False

    def tick(self, lane):
        with self._lock:
            now = self.clock()
            available = [(key, state) for key, state in self._states.items()
                         if not state.running and state.due <= now and self.steps[key].lane == lane]
            if not available or self.stopping.is_set():
                return False
            # Oldest due first prevents a permanently busy source starving peers.
            key, state = min(available, key=lambda item: (item[1].due, item[1].last_started, item[0]))
            step = self.steps[key]
            state.running, state.last_started = True, now
        error, delay = None, step.idle_delay
        try:
            result = step.run(Operation(now + step.budget, self.stopping, self.clock))
            if isinstance(result, bool):
                result = StepResult(result)
            if not isinstance(result, StepResult) or type(result.worked) is not bool:
                raise TypeError("Invalid step result")
            delay = result.delay if result.delay is not None else step.active_delay if result.worked else step.idle_delay
            if not 0.01 <= delay <= 86400:
                raise ValueError("Invalid step retry")
        except OperationCancelled:
            error = "cancelled"
        except RetryLater as exc:
            error, delay = exc.code, exc.delay
        except Exception as exc:
            error = "step_failed"
            delay = min(120, step.error_delay * (2 ** min(state.attempts, 6)))
            logger.warning("Loader step %s interrupted (%s)", key, type(exc).__name__)
        finally:
            with self._lock:
                state.running = False
                state.due = self.clock() + delay
                state.attempts = state.attempts + 1 if error else 0
                state.error = error
                state.last_finished = datetime.now(UTC).isoformat()
        return True

    def _run(self, lane):
        while not self.stopping.is_set():
            if not self.tick(lane):
                self.stopping.wait(0.1)

    def start(self):
        if self._started:
            raise RuntimeError("Loader already started")
        self._started = True
        for lane, limit in self.limits.items():
            count = min(limit, sum(s.lane == lane for s in self.steps.values()))
            for index in range(count):
                thread = threading.Thread(target=self._run, args=(lane,), name=f"loader-{lane}-{index}", daemon=True)
                self._threads.append(thread)
                thread.start()

    def stop(self, timeout=30):
        self.stopping.set()
        deadline = time.monotonic() + timeout
        for thread in self._threads:
            thread.join(max(0, deadline - time.monotonic()))
        return not any(t.is_alive() for t in self._threads)

    def status(self):
        # Process-level diagnostics contain no tenant IDs, texts or credentials.
        with self._lock:
            return {"registration_errors": dict(self.registration_errors), "limits": dict(self.limits),
                    "steps": [{"id": key, "lane": self.steps[key].lane, "running": state.running,
                               "error_code": state.error, "attempts": state.attempts,
                               "next_in_seconds": max(0, round(state.due - self.clock(), 2)),
                               "last_finished_at": state.last_finished} for key, state in self._states.items()]}
