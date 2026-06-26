"""Database models — the shapes of the records Kerno stores.

This package also defines ``Base``, the single SQLAlchemy declarative base that
every model inherits from. Alembic uses ``Base.metadata`` to know the full set
of tables when it generates and applies migrations, so every model module must
import ``Base`` from here rather than declaring its own.
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """The shared parent of every Kerno database model.

    Holds the metadata catalogue that links all tables together. A model becomes
    part of the schema simply by subclassing this.
    """
