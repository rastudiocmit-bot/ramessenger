"""A total chronological order shared by history, cursors and chat previews."""


def source_order(alias="m"):
    return (
        f"CASE WHEN {alias}.payload->>'crm_order' ~ '^[0-9]{{1,19}}$' "
        f"THEN ({alias}.payload->>'crm_order')::numeric ELSE 0 END"
    )


def message_order(alias="m", *, descending=False):
    direction = " DESC" if descending else ""
    return ",".join(
        part + direction for part in (f"{alias}.occurred_at", source_order(alias), f"{alias}.id")
    )


def visible_message(alias="m"):
    return (
        f"coalesce({alias}.payload->>'deleted','false')<>'true' "
        f"AND {alias}.payload->>'superseded_by' IS NULL"
    )
