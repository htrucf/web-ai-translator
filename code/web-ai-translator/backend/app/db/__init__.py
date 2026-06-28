"""SQLAlchemy + Alembic database layer (replaces sqlite3 in app.database).

Models map to the same tables the legacy SQLite code already used; this lets us
swap PostgreSQL in without rewriting every call site. The compatibility shim in
`app.database` re-exports the helpers that the rest of the code imports.
"""

from app.db.session import (
    engine,
    SessionLocal,
    get_session,
    session_scope,
    DATABASE_URL,
)
from app.db.models import (
    Base,
    Job,
    Chunk,
    User,
    Session as SessionRow,
    GlobalTerm,
)

__all__ = [
    "engine",
    "SessionLocal",
    "get_session",
    "session_scope",
    "DATABASE_URL",
    "Base",
    "Job",
    "Chunk",
    "User",
    "SessionRow",
    "GlobalTerm",
]
