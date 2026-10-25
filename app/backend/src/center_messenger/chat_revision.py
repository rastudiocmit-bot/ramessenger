"""Cheap transactional invalidation shared by chat reads and the event stream."""
from datetime import UTC, datetime

from sqlalchemy import text


def versions(db, workspace):
    row = db.execute(text("SELECT revision,card_revision FROM chat_read_versions WHERE workspace_id=:w"),
                     {"w": workspace}).mappings().one_or_none()
    return (str(row["revision"]), str(row["card_revision"])) if row else ("0", "0")


def fingerprint(db, workspace):
    return versions(db, workspace)[0] + ":" + datetime.now(UTC).strftime("%Y-%m-%d")
