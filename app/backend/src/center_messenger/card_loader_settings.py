"""Validated workspace policy for AlfaCRM family-card caching."""

from pydantic import Field, model_validator
from sqlalchemy import text

from center_messenger.auth_security import InputModel


class CardLoaderSettings(InputModel):
    preload_unanswered: bool = True
    preload_active_groups: bool = True
    finance_interval_minutes: int = Field(default=30, ge=15, le=1440)
    lessons_interval_minutes: int = Field(default=30, ge=15, le=1440)
    groups_interval_minutes: int = Field(default=360, ge=60, le=10080)
    # Zero means: load the old lesson range once, then keep only the recent
    # lesson window moving. Old immutable lessons are not reread.
    history_interval_minutes: int = Field(default=0, ge=0, le=10080)
    requests_per_turn: int = Field(default=3, ge=1, le=10)
    busy_retry_seconds: int = Field(default=5, ge=1, le=60)
    retry_initial_seconds: int = Field(default=30, ge=5, le=300)
    retry_max_seconds: int = Field(default=1800, ge=60, le=86400)
    manual_priority_seconds: int = Field(default=30, ge=10, le=300)
    manual_interest_minutes: int = Field(default=10, ge=1, le=60)
    manual_burst: int = Field(default=5, ge=1, le=20)

    @model_validator(mode="after")
    def valid_ranges(self):
        if self.history_interval_minutes and self.history_interval_minutes < 360:
            raise ValueError("History refresh must be disabled or at least six hours")
        if self.retry_max_seconds < self.retry_initial_seconds:
            raise ValueError("Maximum retry must not be shorter than the first retry")
        return self


DEFAULTS = CardLoaderSettings().model_dump()
FIELDS = tuple(DEFAULTS)


def get(db, workspace):
    row = db.execute(text("SELECT " + ",".join(FIELDS) +
                          " FROM crm_card_loader_settings WHERE workspace_id=:w"),
                     {"w": workspace}).mappings().one_or_none()
    return dict(DEFAULTS if row is None else row)


def save(db, workspace, value: CardLoaderSettings):
    payload = value.model_dump()
    columns = ",".join(FIELDS)
    values = ",".join(":" + field for field in FIELDS)
    updates = ",".join(field + "=EXCLUDED." + field for field in FIELDS)
    db.execute(text("INSERT INTO crm_card_loader_settings(workspace_id," + columns + ") VALUES(:w," + values + ") "
                    "ON CONFLICT(workspace_id) DO UPDATE SET " + updates + ",updated_at=now()"),
               {"w": workspace, **payload})
    return payload


def public(value):
    return dict(value, parallel_card_jobs=1, host_request_gap_ms=350)
