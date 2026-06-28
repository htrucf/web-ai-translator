"""SQLAlchemy 2.0 ORM models.

Mirrors the legacy SQLite schema so the compatibility shim in `app.database`
can keep its existing public API while we swap the storage engine underneath.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"

    job_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    source_type: Mapped[str] = mapped_column(String(16), default="latex", nullable=False)
    arxiv_id: Mapped[Optional[str]] = mapped_column(String(64))
    title: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[Optional[str]] = mapped_column(String(64))
    created_at: Mapped[Optional[str]] = mapped_column(String(40))
    updated_at: Mapped[Optional[str]] = mapped_column(String(40))
    original_pdf: Mapped[Optional[str]] = mapped_column(Text)
    translated_pdf: Mapped[Optional[str]] = mapped_column(Text)
    total_chunks: Mapped[int] = mapped_column(Integer, default=0)
    done_chunks: Mapped[int] = mapped_column(Integer, default=0)
    heuristic_score: Mapped[Optional[float]] = mapped_column(Float)
    username: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    # Pipeline / benchmark metadata (multi-agent path)
    num_tabs: Mapped[Optional[int]] = mapped_column(Integer)
    duration_seconds: Mapped[Optional[float]] = mapped_column(Float)
    agentic: Mapped[Optional[bool]] = mapped_column(Boolean, default=False)

    chunks: Mapped[list["Chunk"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        Index("idx_jobs_status", "status"),
        Index("idx_jobs_user", "username"),
    )


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("jobs.job_id", ondelete="CASCADE"),
        nullable=False,
    )
    chunk_key: Mapped[str] = mapped_column(String(256), nullable=False)
    src_latex: Mapped[Optional[str]] = mapped_column(Text)
    mt_latex: Mapped[Optional[str]] = mapped_column(Text)
    edited: Mapped[int] = mapped_column(Integer, default=0)
    edit_note: Mapped[Optional[str]] = mapped_column(Text)

    job: Mapped[Job] = relationship(back_populates="chunks")

    __table_args__ = (
        UniqueConstraint("job_id", "chunk_key", name="uq_chunks_job_key"),
        Index("idx_chunks_job", "job_id"),
    )


class GlobalTerm(Base):
    __tablename__ = "global_terms"

    en_term: Mapped[str] = mapped_column(String(256), primary_key=True)
    vi_term: Mapped[str] = mapped_column(Text, nullable=False)
    # Lĩnh vực chuyên ngành của thuật ngữ (free text, vd: "Học máy", "Toán học").
    # NULL = chưa phân loại. Điền tự động khi trích glossary (AI) hoặc sửa tay.
    field: Mapped[Optional[str]] = mapped_column(String(64))
    frequency: Mapped[int] = mapped_column(Integer, default=1)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    first_job: Mapped[Optional[str]] = mapped_column(String(128))
    updated_at: Mapped[Optional[str]] = mapped_column(String(40))

    __table_args__ = (
        Index("idx_terms_freq", "frequency"),
        Index("idx_terms_field", "field"),
    )


class User(Base):
    __tablename__ = "users"

    username: Mapped[str] = mapped_column(String(64), primary_key=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    security_question: Mapped[str] = mapped_column(Text, nullable=False)
    security_answer_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[Optional[str]] = mapped_column(String(40))
    is_admin: Mapped[int] = mapped_column(Integer, default=0)


class Session(Base):
    """Auth session token. Named `Session` in the table but exported as
    `SessionRow` in app.db.__init__ so it doesn't shadow SQLAlchemy's Session."""

    __tablename__ = "sessions"

    token: Mapped[str] = mapped_column(String(128), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    last_active: Mapped[float] = mapped_column(Float, nullable=False)

    __table_args__ = (
        Index("idx_sessions_username", "username"),
    )
