"""Night-only historical lane. All dates are aware; no in-memory rate clock."""
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

ZONE = ZoneInfo('Europe/Moscow')
START = datetime(2020, 10, 31, tzinfo=ZONE)
MIN_INTERVAL = timedelta(seconds=120)
AGE = timedelta(days=7)


def now_utc(): return datetime.now(timezone.utc)


def old_cutoff(now):
    if now.tzinfo is None: raise ValueError('Aware time required')
    return now.astimezone(timezone.utc) - AGE


def eligible(started_at, now):
    if started_at.tzinfo is None: raise ValueError('Aware call time required')
    return START <= started_at < old_cutoff(now)


def night(now):
    if now.tzinfo is None: raise ValueError('Aware time required')
    return now.astimezone(ZONE).hour < 8


def next_night(now):
    if now.tzinfo is None: raise ValueError('Aware time required')
    local = now.astimezone(ZONE)
    if local.hour < 8: return now.astimezone(timezone.utc)
    return datetime.combine(local.date() + timedelta(days=1), time(), ZONE).astimezone(timezone.utc)


def due(now, last_started=None):
    """Persist last_started before starting a new archived call, never per part."""
    if last_started is not None and last_started.tzinfo is None: raise ValueError('Aware start required')
    candidate = max(now, last_started + MIN_INTERVAL) if last_started else now
    return next_night(candidate)


def status(control, now):
    enabled = bool(control and control.get('enabled'))
    clocks=[control.get(key) for key in ('last_started_at','last_imported_at')] if control else []
    last=max((value for value in clocks if value),default=None)
    next_at = due(now, last) if enabled else None
    if next_at and control.get('retry_at') and control['retry_at']>next_at:
        next_at=next_night(control['retry_at'])
    return {'enabled': enabled, 'timezone': str(ZONE), 'start_hour': 0, 'end_hour': 8,
            'interval_seconds': 120, 'from_at': START.isoformat(),
            'before_at': old_cutoff(now).isoformat(),
            'next_at': next_at.isoformat() if next_at else None,
            'waiting_for_night': enabled and not night(now)}
