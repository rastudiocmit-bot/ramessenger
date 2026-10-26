"""Shared integration runtime. Importing this package starts no workers or IO."""

from .contracts import Capability, Manifest, Operation, Step, StepResult
from .registry import Registry
from .scheduler import Scheduler

__all__ = ["Capability", "Manifest", "Operation", "Step", "StepResult", "Registry", "Scheduler"]
