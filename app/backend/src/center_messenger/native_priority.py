"""Deterministic, bounded fairness for a workspace's single CRM browser.

State is non-secret and persisted under session_control.dispatch. All callers
read/update it under the workspace lock; thread arrival order is not priority.
"""
from datetime import timedelta

KINDS = {"send", "live", "catchup", "archive"}
MAX_SEND_STREAK = 3
MAX_FOREGROUND_TURNS = 12
BACKGROUND_AGE = 30
RESERVATION_SECONDS = 1
BUSY_RETRY = .25


def choose(ready, state, now):
    if "live" in ready and (state.get("send_streak", 0) >= MAX_SEND_STREAK
            or (state.get("send_streak", 0) > 0 and (now - ready["live"]).total_seconds() >= 15)):
        return "live"
    # Reserve a small bounded share for aged background work, even at overload.
    aged = [kind for kind in ("catchup", "archive") if kind in ready
            and (now - ready[kind]).total_seconds() >= BACKGROUND_AGE]
    if aged and state.get("foreground_turns", 0) >= MAX_FOREGROUND_TURNS:
        return min(aged, key=lambda kind: ready[kind])
    return next((kind for kind in ("send", "live", "catchup", "archive") if kind in ready), None)


def record_turn(state, kind):
    state = dict(state)
    state.pop("reservation", None)
    state["send_streak"] = min(MAX_SEND_STREAK, state.get("send_streak", 0) + 1) if kind == "send" else 0
    state["foreground_turns"] = min(MAX_FOREGROUND_TURNS, state.get("foreground_turns", 0) + 1) if kind != 'archive' else 0
    return state


def reserve(state, kind, now):
    state = dict(state)
    if kind:
        state["reservation"] = {"kind": kind, "until": (now + timedelta(seconds=RESERVATION_SECONDS)).isoformat()}
    else:
        state.pop("reservation", None)
    return state
