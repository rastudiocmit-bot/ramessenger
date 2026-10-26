"""Bounded read/commit pipeline; sends must use a durable send state machine."""

from .contracts import Operation, OperationCancelled, RetryLater, StepResult, identifier
from .persistence import LostClaim


class SourceFailure(Exception):
    def __init__(self, code, *, retryable=True, delay=30):
        self.code = identifier(code)
        if not 0.1 <= delay <= 86400:
            raise ValueError("Invalid source retry")
        self.retryable, self.delay = retryable, delay
        super().__init__(self.code)


class PageReader:
    def __init__(self, store, next_scope, fetch, *, project=None, poll_seconds=15):
        self.store, self.next_scope, self.fetch = store, next_scope, fetch
        self.project, self.poll_seconds = project, poll_seconds

    def __call__(self, operation):
        scope = self.next_scope()
        if scope is None:
            return False
        budget = operation.remaining(300)
        operation = Operation(operation.clock() + budget, operation.stopping, operation.clock)
        claim = self.store.claim(scope, seconds=budget + 30)
        if claim is None:
            return False
        try:
            page = self.fetch(scope, claim.cursor, operation)
            operation.remaining()  # a cancelled/late result must not advance a cursor
            self.store.commit(claim, page, project=self.project, poll_seconds=self.poll_seconds)
            return True
        except LostClaim:
            return False
        except OperationCancelled:
            self._fail(claim, "cancelled", delay=3)
            raise
        except RetryLater as error:
            self._fail(claim, error.code, delay=error.delay)
            return StepResult(True, .1)
        except SourceFailure as error:
            self._fail(claim, error.code, retryable=error.retryable, delay=error.delay)
            # Only this persisted stream pauses; the next account still gets a turn.
            return StepResult(True, .1)
        except Exception:
            self._fail(claim, "source_failed", delay=min(300, 3 * 2 ** min(claim.attempts, 7)))
            return StepResult(True, .1)

    def _fail(self, claim, code, **kwargs):
        try:
            self.store.fail(claim, code, **kwargs)
        except LostClaim:
            pass
