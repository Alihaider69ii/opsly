"""Portable ``INSERT ... ON CONFLICT DO UPDATE`` for PostgreSQL (production) and SQLite (unit tier).

An upsert is what makes re-running a sync idempotent and race-safe: two workers writing the same
Shopify object at once cannot both insert it, and a repeated sync updates instead of duplicating.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.db.base import Base


def upsert(
    db: Session,
    model: type[Base],
    rows: Sequence[Mapping[str, Any]],
    *,
    conflict: Sequence[str],
    update: Sequence[str],
    touch_updated_at: bool = True,
) -> None:
    """Insert ``rows``; on a conflict over the ``conflict`` columns update only ``update`` columns.

    Every row must carry the same keys. ``updated_at`` is refreshed on conflict (the ORM
    ``onupdate`` hook does not run for Core statements) when the table has that column.
    """
    if not rows:
        return
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        insert = pg_insert
    elif dialect == "sqlite":
        insert = sqlite_insert  # type: ignore[assignment]
    else:  # pragma: no cover - only the two supported databases are ever configured
        raise NotImplementedError(f"upsert is not implemented for {dialect}")
    statement = insert(model).values(list(rows))
    assignments: dict[str, Any] = {name: statement.excluded[name] for name in update}
    if touch_updated_at and "updated_at" in model.__table__.columns:
        assignments["updated_at"] = func.now()
    db.execute(statement.on_conflict_do_update(index_elements=list(conflict), set_=assignments))
