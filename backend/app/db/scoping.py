import uuid

from sqlalchemy import Select, select

from app.db.base import Base


def scoped_select[T: Base](model: type[T], organization_id: uuid.UUID) -> Select[tuple[T]]:
    """The only way tenant-owned rows should be queried.

    Every business table carries organization_id; start every query from here and chain
    .where()/.join() onto it, so a lookup by id can never reach another organization's row.
    The organization_id must come from a verified membership (see api/deps.py), never straight
    from client input.
    """
    column = getattr(model, "organization_id", None)
    if column is None:
        raise TypeError(f"{model.__name__} has no organization_id; it is not tenant-scoped")
    return select(model).where(column == organization_id)
