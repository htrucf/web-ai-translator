"""SQLAlchemy engine + session factory.

Picks DATABASE_URL from env. Defaults to a local SQLite file for dev so the
test suite can still run without docker-compose; production overrides via the
DATABASE_URL exported by docker-compose.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker, Session

from app import paths


def _default_sqlite_url() -> str:
    db_path = os.path.join(paths.workspace_dir(), "history.db")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    # Forward slashes are required on Windows too — SQLAlchemy parses the URL.
    return f"sqlite:///{db_path.replace(os.sep, '/')}"


DATABASE_URL = os.getenv("DATABASE_URL", _default_sqlite_url())


def _build_engine(url: str) -> Engine:
    """Create an engine with sensible defaults for whichever backend is in use."""
    is_sqlite = url.startswith("sqlite")
    connect_args: dict = {}
    if is_sqlite:
        # SQLite under FastAPI: threads share connections via the pool, so we
        # need to disable the same-thread guard. WAL mode is set below.
        connect_args["check_same_thread"] = False

    eng = create_engine(
        url,
        pool_pre_ping=True,
        pool_size=20 if not is_sqlite else 5,
        max_overflow=30 if not is_sqlite else 0,
        future=True,
        connect_args=connect_args,
    )

    if is_sqlite:
        # Match the pragmas the legacy sqlite3 code used.
        @_listen(eng)
        def _set_sqlite_pragmas(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    return eng


def _listen(eng: Engine):
    from sqlalchemy import event
    def decorator(fn):
        event.listen(eng, "connect", fn)
        return fn
    return decorator


engine: Engine = _build_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_session() -> Session:
    """FastAPI dependency — yields a Session that's closed when the request ends."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Imperative-style context manager for non-request code paths."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
