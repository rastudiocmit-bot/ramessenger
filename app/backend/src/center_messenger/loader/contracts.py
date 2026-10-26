"""Versioned, provider-neutral contracts; credentials never belong in these objects."""

import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from threading import Event
from typing import Callable
from uuid import UUID

CONTRACT_VERSION = 1


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,79}", value):
        raise ValueError("Invalid loader identifier")
    return value


class Capability(StrEnum):
    RECEIVE = "receive_messages"
    SEND = "send_text"
    DIRECTORY = "directory"
    CARDS = "cards"
    PROJECTION = "projection"
    ATTACHMENTS = "fetch_attachments"
    SEND_ATTACHMENTS = "send_attachments"
    CALLS = "list_calls"
    RECORDINGS = "fetch_recordings"
    TRANSCRIBE = "transcribe"


@dataclass(frozen=True)
class Manifest:
    provider: str
    capabilities: frozenset[Capability]
    version: int = CONTRACT_VERSION

    def __post_init__(self):
        identifier(self.provider)
        if not self.capabilities or any(not isinstance(c, Capability) for c in self.capabilities):
            raise ValueError("Explicit supported capabilities required")


class RetryLater(Exception):
    def __init__(self, code="temporarily_unavailable", delay=3):
        self.code = identifier(code)
        self.delay = float(delay)
        if not 0.1 <= self.delay <= 86400:
            raise ValueError("Invalid retry delay")
        super().__init__(self.code)


class OperationCancelled(Exception):
    pass


@dataclass(frozen=True)
class Operation:
    deadline: float
    stopping: Event = field(repr=False)
    clock: Callable[[], float] = field(default=time.monotonic, repr=False)

    def remaining(self, maximum=None):
        if self.stopping.is_set():
            raise OperationCancelled()
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise RetryLater("operation_timeout")
        return min(remaining, maximum) if maximum is not None else remaining


@dataclass(frozen=True)
class StepResult:
    worked: bool = False
    delay: float | None = None


@dataclass(frozen=True)
class Step:
    name: str
    capability: Capability
    run: Callable[[Operation], bool | StepResult] = field(repr=False)
    lane: str = "messages"
    active_delay: float = 0.1
    idle_delay: float = 1.5
    error_delay: float = 3
    budget: float = 60

    def __post_init__(self):
        identifier(self.name)
        if self.lane not in {"messages", "maintenance", "media"}:
            raise ValueError("Unknown loader lane")
        for value in (self.active_delay, self.idle_delay, self.error_delay, self.budget):
            if not 0 < value <= 3600:
                raise ValueError("Invalid loader interval")
        if not callable(self.run):
            raise ValueError("A bounded step callable is required")


@dataclass(frozen=True)
class Scope:
    workspace: UUID
    connection: UUID
    provider: str
    stream: str

    def __post_init__(self):
        if not isinstance(self.workspace, UUID) or not isinstance(self.connection, UUID):
            raise ValueError("Trusted workspace and connection UUIDs required")
        identifier(self.provider)
        identifier(self.stream)


@dataclass(frozen=True)
class SourceEvent:
    namespace: str
    source_id: str
    kind: str
    occurred_at: datetime
    payload: dict = field(repr=False)
    crm_id: str | None = None

    def __post_init__(self):
        identifier(self.namespace)
        if self.kind not in {"message", "call", "attachment", "recording", "transcript", "call_summary"}:
            raise ValueError("Unsupported event kind")
        if not isinstance(self.source_id, str) or not 1 <= len(self.source_id) <= 256:
            raise ValueError("Stable source ID required")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("Source timestamp must include timezone")
        if not isinstance(self.payload, dict):
            raise ValueError("Event payload must be an object")


@dataclass(frozen=True)
class Page:
    events: tuple[SourceEvent, ...]
    cursor: dict
    has_more: bool
    # Relative to the parent event; idempotency also includes provider/connection.
    followups: tuple[dict, ...] = ()

    def validate(self, limit=100):
        if len(self.events) > limit or not isinstance(self.cursor, dict) or not isinstance(self.has_more, bool):
            raise ValueError("Invalid or oversized source page")
        if any(not isinstance(event, SourceEvent) for event in self.events):
            raise ValueError("Invalid source event")
        if len(self.followups) > limit * 10:
            raise ValueError("Too many follow-up jobs")
        for job in self.followups:
            if not isinstance(job, dict) or not isinstance(job.get("key"), str) or not 1 <= len(job["key"]) <= 256:
                raise ValueError("Follow-up idempotency key required")
            identifier(job.get("kind", ""))
