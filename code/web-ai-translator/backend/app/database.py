"""Persistent storage layer — now backed by SQLAlchemy (Postgres in prod,
SQLite as a fallback for tests / dev).

The public API is intentionally identical to the legacy sqlite3 module so the
rest of the codebase doesn't have to change. Function signatures are stable;
only the implementation underneath was swapped.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from sqlalchemy import func, select, update, delete, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import (
    Base,
    Chunk,
    GlobalTerm,
    Job,
    SessionLocal,
    SessionRow,
    User,
    engine,
    session_scope,
)


# ── Legacy export — some call sites import DB_PATH for diagnostics ───────────
DB_PATH = os.getenv("DATABASE_URL", "")


# ── Connection helpers ───────────────────────────────────────────────────────

@contextmanager
def get_db() -> Iterator[Session]:
    """Drop-in replacement for the old sqlite3 connection context manager.

    Returns a SQLAlchemy Session. The few call sites that still use raw
    ``conn.execute(...)`` keep working because Session.execute() accepts
    ``text()`` SQL strings.
    """
    with session_scope() as s:
        yield s


def init_db() -> None:
    """Create tables if they don't exist.

    In production this is a no-op because Alembic migrations run at container
    startup. In tests / dev where Alembic isn't configured we fall back to
    ``Base.metadata.create_all`` so a blank SQLite file boots clean.
    """
    Base.metadata.create_all(engine)
    _ensure_columns()


def _ensure_columns() -> None:
    """Dev-safe migration: thêm cột mới vào bảng SQLite ĐÃ tồn tại.

    ``create_all`` chỉ tạo bảng mới, KHÔNG ALTER bảng cũ → thêm cột vào model
    sẽ gây 'no such column' trên DB cũ. Postgres (prod) dùng Alembic nên bỏ qua.
    """
    if engine.dialect.name != "sqlite":
        return
    new_cols = {
        "jobs": {
            "num_tabs": "INTEGER",
            "duration_seconds": "FLOAT",
            "agentic": "BOOLEAN",
        },
        "global_terms": {
            "field": "VARCHAR(64)",
        },
    }
    try:
        with engine.begin() as conn:
            for table, cols in new_cols.items():
                existing = {
                    row[1]
                    for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")
                }
                for col, ctype in cols.items():
                    if col not in existing:
                        conn.exec_driver_sql(
                            f"ALTER TABLE {table} ADD COLUMN {col} {ctype}"
                        )
                        print(f"[DB] Added column {table}.{col}")
    except Exception as e:
        print(f"[DB] _ensure_columns skipped: {e}")


# ── Job rows ─────────────────────────────────────────────────────────────────

def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def upsert_job(job_id: str, **fields) -> None:
    """Insert-or-update a job. Unknown columns are silently dropped to keep
    the call-site contract loose (the legacy raw-SQL version was forgiving)."""
    valid = {c.name for c in Job.__table__.columns}
    clean = {k: v for k, v in fields.items() if k in valid}
    with session_scope() as s:
        row = s.get(Job, job_id)
        if row is None:
            row = Job(job_id=job_id, **clean)
            s.add(row)
        else:
            for k, v in clean.items():
                setattr(row, k, v)


def upsert_chunk(job_id: str, chunk_key: str, **fields) -> None:
    valid = {c.name for c in Chunk.__table__.columns}
    clean = {k: v for k, v in fields.items() if k in valid and k not in ("job_id", "chunk_key")}
    with session_scope() as s:
        row = s.execute(
            select(Chunk).where(Chunk.job_id == job_id, Chunk.chunk_key == chunk_key)
        ).scalar_one_or_none()
        if row is None:
            row = Chunk(job_id=job_id, chunk_key=chunk_key, **clean)
            s.add(row)
        else:
            for k, v in clean.items():
                setattr(row, k, v)


def sync_job_to_db(job_id: str, progress: dict, workspace: str) -> None:
    """Reflect the latest progress.json into the jobs/chunks tables."""
    now = _utcnow_iso()
    source_type = progress.get("source_type", "latex")
    status = progress.get("status", "unknown")

    job_dir = os.path.join("jobs", job_id)
    orig = os.path.join(job_dir, "original.pdf")
    trans = os.path.join(job_dir, "output", "translated.pdf")
    orig_abs = os.path.join(workspace, orig)
    trans_abs = os.path.join(workspace, trans)

    hq = progress.get("quality") or {}
    hq_score = hq.get("score") if hq else None

    tc = progress.get("translated_chunks", {})
    done_chunks = len(tc)
    for k, v in progress.items():
        if k.startswith("input_chunks:"):
            done_chunks += len(v)

    job_fields = dict(
        source_type=source_type,
        arxiv_id=progress.get("arxiv_id", job_id.replace("_", "/")),
        status=status,
        updated_at=now,
        original_pdf=orig if os.path.exists(orig_abs) else None,
        translated_pdf=trans if os.path.exists(trans_abs) else None,
        done_chunks=done_chunks,
        heuristic_score=hq_score,
    )
    # Metadata / benchmark — chỉ ghi khi progress.json có (tránh ghi đè None)
    if progress.get("total_chunks") is not None:
        job_fields["total_chunks"] = progress["total_chunks"]
    if progress.get("num_tabs") is not None:
        job_fields["num_tabs"] = progress["num_tabs"]
    if progress.get("duration_seconds") is not None:
        job_fields["duration_seconds"] = progress["duration_seconds"]
    if "agentic" in progress:
        job_fields["agentic"] = bool(progress.get("agentic"))
    upsert_job(job_id, **job_fields)

    for key, mt in tc.items():
        upsert_chunk(job_id, str(key), src_latex=None, mt_latex=mt)

    for k, chunks_dict in progress.items():
        if not k.startswith("input_chunks:"):
            continue
        input_rel = k[len("input_chunks:"):]
        for chunk_key, mt in chunks_dict.items():
            db_key = f"input:{input_rel}:{chunk_key}"
            upsert_chunk(job_id, db_key, src_latex=None, mt_latex=mt)


def get_job(job_id: str) -> dict | None:
    with session_scope() as s:
        row = s.get(Job, job_id)
        return _row_to_dict(row) if row else None


def get_jobs(limit: int = 100, offset: int = 0) -> list[dict]:
    with session_scope() as s:
        rows = s.execute(
            select(Job).order_by(Job.updated_at.desc()).limit(limit).offset(offset)
        ).scalars().all()
        return [_row_to_dict(r) for r in rows]


def get_jobs_for_user(
    username: str,
    include_unowned: bool = False,
    limit: int = 500,
    offset: int = 0,
) -> list[dict]:
    stmt = select(Job).where(
        (Job.username == username)
        | (Job.username.is_(None) if include_unowned else False)
    ).order_by(Job.updated_at.desc()).limit(limit).offset(offset)
    with session_scope() as s:
        rows = s.execute(stmt).scalars().all()
        return [_row_to_dict(r) for r in rows]


def get_job_owner(job_id: str) -> str | None:
    with session_scope() as s:
        row = s.get(Job, job_id)
        return row.username if row else None


def set_job_owner(job_id: str, username: str) -> None:
    with session_scope() as s:
        s.execute(update(Job).where(Job.job_id == job_id).values(username=username))


def get_chunks(job_id: str) -> list[dict]:
    with session_scope() as s:
        rows = s.execute(
            select(Chunk).where(Chunk.job_id == job_id).order_by(Chunk.id)
        ).scalars().all()
        return [_row_to_dict(r) for r in rows]


def update_chunk_translation(job_id: str, chunk_key: str, mt_latex: str, edit_note: str = "") -> bool:
    with session_scope() as s:
        result = s.execute(
            update(Chunk)
            .where(Chunk.job_id == job_id, Chunk.chunk_key == chunk_key)
            .values(mt_latex=mt_latex, edited=1, edit_note=edit_note)
        )
        return (result.rowcount or 0) > 0


def update_job_notes(job_id: str, notes: str) -> None:
    with session_scope() as s:
        s.execute(update(Job).where(Job.job_id == job_id).values(notes=notes))


# ── Global terminology store ─────────────────────────────────────────────────

def _clean_field(field: str | None) -> str | None:
    """Normalize a free-text 'lĩnh vực' label: trim, cap at 64 chars, '' → None."""
    f = (field or "").strip()
    return f[:64] or None


def merge_job_glossary_to_global(
    job_id: str,
    glossary: dict[str, str],
    fields: dict[str, str] | None = None,
) -> None:
    """Merge a job's glossary into the cross-document store.

    `fields` (optional): en-term (any case) → lĩnh vực. Used to tag NEW terms
    and backfill the field of existing terms that don't have one yet. Never
    overwrites a field a user already curated.
    """
    now = _utcnow_iso()
    field_map = {(k or "").lower().strip(): _clean_field(v) for k, v in (fields or {}).items()}

    with session_scope() as s:
        for en_term, vi_term in glossary.items():
            en_lower = (en_term or "").lower().strip()
            vi_clean = (vi_term or "").strip()
            if not en_lower or not vi_clean:
                continue
            term_field = field_map.get(en_lower)

            existing = s.get(GlobalTerm, en_lower)
            if existing is None:
                s.add(GlobalTerm(
                    en_term=en_lower, vi_term=vi_clean, field=term_field, frequency=1,
                    confidence=0.5, first_job=job_id, updated_at=now,
                ))
            elif existing.vi_term.strip().lower() == vi_clean.lower():
                existing.frequency += 1
                existing.confidence = min(0.95, existing.confidence + 0.1)
                if not existing.field and term_field:
                    existing.field = term_field
                existing.updated_at = now
            elif existing.frequency < 3:
                existing.frequency += 1
                if not existing.field and term_field:
                    existing.field = term_field
                existing.updated_at = now


def get_global_glossary(
    min_confidence: float = 0.5,
    min_frequency: int = 1,
    limit: int = 2000,
) -> dict[str, str]:
    with session_scope() as s:
        rows = s.execute(
            select(GlobalTerm)
            .where(GlobalTerm.confidence >= min_confidence)
            .where(GlobalTerm.frequency >= min_frequency)
            .order_by(GlobalTerm.frequency.desc(), GlobalTerm.confidence.desc())
            .limit(limit)
        ).scalars().all()
        # Build the dict INSIDE the session — rows are expired+detached after the
        # `with` exits (session_scope commits, expire_on_commit=True), so reading
        # r.en_term outside here raises DetachedInstanceError.
        return {r.en_term: r.vi_term for r in rows}


def get_global_terms(
    min_confidence: float = 0.0,
    min_frequency: int = 1,
    limit: int = 2000,
    field: str | None = None,
) -> list[dict]:
    """Detailed global terms (incl. `field`/frequency/confidence) for the UI.

    Unlike `get_global_glossary` (flat en→vi for pipeline pre-seeding), this
    returns full rows. Optional `field` filters to one lĩnh vực.
    """
    with session_scope() as s:
        stmt = (
            select(GlobalTerm)
            .where(GlobalTerm.confidence >= min_confidence)
            .where(GlobalTerm.frequency >= min_frequency)
        )
        if field:
            stmt = stmt.where(GlobalTerm.field == field)
        stmt = stmt.order_by(
            GlobalTerm.frequency.desc(), GlobalTerm.confidence.desc()
        ).limit(limit)
        rows = s.execute(stmt).scalars().all()
        return [
            {
                "en_term": r.en_term,
                "vi_term": r.vi_term,
                "field": r.field,
                "frequency": r.frequency,
                "confidence": round(float(r.confidence), 3),
                "first_job": r.first_job,
                "updated_at": r.updated_at,
            }
            for r in rows
        ]


def get_global_terms_stats() -> dict:
    with session_scope() as s:
        total = s.scalar(select(func.count(GlobalTerm.en_term))) or 0
        stable = s.scalar(
            select(func.count(GlobalTerm.en_term)).where(GlobalTerm.frequency >= 3)
        ) or 0
        avg_conf = s.scalar(select(func.avg(GlobalTerm.confidence))) or 0.0
        field_rows = s.execute(
            select(GlobalTerm.field, func.count(GlobalTerm.en_term))
            .group_by(GlobalTerm.field)
        ).all()
    by_field: dict[str, int] = {}
    uncategorized = 0
    for fld, cnt in field_rows:
        if fld and fld.strip():
            by_field[fld] = int(cnt)
        else:
            uncategorized += int(cnt)
    return {
        "total_terms": int(total),
        "stable_terms": int(stable),
        "avg_confidence": round(float(avg_conf), 3),
        # Per-lĩnh-vực breakdown + distinct list (frontend autocomplete source)
        "by_field": dict(sorted(by_field.items(), key=lambda kv: kv[1], reverse=True)),
        "fields": sorted(by_field.keys()),
        "uncategorized": uncategorized,
    }


def delete_global_term(en_term: str) -> bool:
    with session_scope() as s:
        result = s.execute(
            delete(GlobalTerm).where(GlobalTerm.en_term == en_term.lower())
        )
        return (result.rowcount or 0) > 0


def upsert_global_term(en_term: str, vi_term: str, field: str | None = None) -> None:
    """User-curated term — high confidence, sourced from manual entry.

    `field` (lĩnh vực): pass a string to set it (``""`` clears it). Pass None to
    leave an existing term's field untouched.
    """
    now = _utcnow_iso()
    en_lower = en_term.lower().strip()
    field_clean = _clean_field(field)
    with session_scope() as s:
        existing = s.get(GlobalTerm, en_lower)
        if existing is None:
            s.add(GlobalTerm(
                en_term=en_lower, vi_term=vi_term.strip(), field=field_clean, frequency=1,
                confidence=0.9, first_job="manual", updated_at=now,
            ))
        else:
            existing.vi_term = vi_term.strip()
            if field is not None:   # explicit (incl. "" to clear); None = leave as-is
                existing.field = field_clean
            existing.confidence = 0.9
            existing.updated_at = now


# ── User accounts ────────────────────────────────────────────────────────────

def create_user(
    username: str,
    password_hash: str,
    security_question: str,
    security_answer_hash: str,
    is_admin: bool = False,
) -> bool:
    now = _utcnow_iso()
    try:
        with session_scope() as s:
            s.add(User(
                username=username,
                password_hash=password_hash,
                security_question=security_question,
                security_answer_hash=security_answer_hash,
                created_at=now,
                is_admin=1 if is_admin else 0,
            ))
        return True
    except IntegrityError:
        return False


def is_db_admin(username: str) -> bool:
    with session_scope() as s:
        row = s.get(User, username)
        return bool(row and row.is_admin)


def get_user(username: str) -> dict | None:
    with session_scope() as s:
        row = s.get(User, username)
        return _row_to_dict(row) if row else None


def update_user_password(username: str, new_password_hash: str) -> bool:
    with session_scope() as s:
        result = s.execute(
            update(User).where(User.username == username)
            .values(password_hash=new_password_hash)
        )
        return (result.rowcount or 0) > 0


def count_users() -> int:
    with session_scope() as s:
        return int(s.scalar(select(func.count(User.username))) or 0)


# ── Sessions (auth tokens) ───────────────────────────────────────────────────

def create_session(token: str, username: str, last_active: float) -> None:
    with session_scope() as s:
        s.add(SessionRow(token=token, username=username, last_active=last_active))


def get_session(token: str) -> dict | None:
    with session_scope() as s:
        row = s.get(SessionRow, token)
        if not row:
            return None
        return {"username": row.username, "last_active": row.last_active}


def touch_session(token: str, last_active: float) -> None:
    with session_scope() as s:
        s.execute(
            update(SessionRow).where(SessionRow.token == token)
            .values(last_active=last_active)
        )


def delete_session(token: str) -> None:
    with session_scope() as s:
        s.execute(delete(SessionRow).where(SessionRow.token == token))


def purge_expired_sessions(min_last_active: float) -> int:
    with session_scope() as s:
        result = s.execute(
            delete(SessionRow).where(SessionRow.last_active < min_last_active)
        )
        return int(result.rowcount or 0)


# ── Migration helper (legacy progress.json → DB) ─────────────────────────────

def migrate_existing_jobs(workspace: str, admin_username: str | None = None) -> None:
    """Import existing progress.json files into DB on startup.

    Walks BOTH the legacy `workspace/jobs/` directory and the new
    `workspace/users/*/jobs/` directories. Legacy jobs (no owner) get
    assigned to `admin_username` so the original developer keeps access.
    """
    now = _utcnow_iso()

    def _import(job_id: str, job_dir: str, owner: str | None) -> None:
        with session_scope() as s:
            exists = s.get(Job, job_id)
            if exists:
                # Read attribute while still attached — accessing exists.username
                # after `with` exits raises DetachedInstanceError.
                if owner and not exists.username:
                    s.execute(
                        update(Job).where(Job.job_id == job_id, Job.username.is_(None))
                        .values(username=owner)
                    )
                return
        pf = os.path.join(job_dir, "progress.json")
        if not os.path.exists(pf):
            upsert_job(job_id, status="unknown", created_at=now, updated_at=now,
                       username=owner)
            return
        try:
            with open(pf, "r", encoding="utf-8") as f:
                progress = json.load(f)
            upsert_job(job_id, created_at=now, username=owner)
            sync_job_to_db(job_id, progress, workspace)
            print(f"[DB] Migrated job: {job_id} (owner={owner})")
        except Exception as e:
            print(f"[DB] Migration failed for {job_id}: {e}")

    legacy_jobs = os.path.join(workspace, "jobs")
    if os.path.isdir(legacy_jobs):
        for job_id in os.listdir(legacy_jobs):
            job_dir = os.path.join(legacy_jobs, job_id)
            if os.path.isdir(job_dir):
                _import(job_id, job_dir, admin_username)

    users_root = os.path.join(workspace, "users")
    if os.path.isdir(users_root):
        for safe_user in os.listdir(users_root):
            user_jobs = os.path.join(users_root, safe_user, "jobs")
            if not os.path.isdir(user_jobs):
                continue
            for job_id in os.listdir(user_jobs):
                job_dir = os.path.join(user_jobs, job_id)
                if not os.path.isdir(job_dir):
                    continue
                owner = get_job_owner(job_id) or safe_user
                _import(job_id, job_dir, owner)


# ── Internal helpers ─────────────────────────────────────────────────────────

def _row_to_dict(row) -> dict:
    """SQLAlchemy ORM row → plain dict (mirrors what the legacy sqlite3.Row gave)."""
    if row is None:
        return {}
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}
