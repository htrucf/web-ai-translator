"""Initial schema — mirrors the legacy SQLite tables.

Revision ID: 001_initial
Revises:
Create Date: 2026-05-21
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("job_id", sa.String(128), primary_key=True),
        sa.Column("source_type", sa.String(16), nullable=False, server_default="latex"),
        sa.Column("arxiv_id", sa.String(64)),
        sa.Column("title", sa.Text),
        sa.Column("status", sa.String(64)),
        sa.Column("created_at", sa.String(40)),
        sa.Column("updated_at", sa.String(40)),
        sa.Column("original_pdf", sa.Text),
        sa.Column("translated_pdf", sa.Text),
        sa.Column("total_chunks", sa.Integer, server_default="0"),
        sa.Column("done_chunks", sa.Integer, server_default="0"),
        sa.Column("heuristic_score", sa.Float),
        sa.Column("username", sa.String(64)),
        sa.Column("notes", sa.Text),
    )
    op.create_index("idx_jobs_status", "jobs", ["status"])
    op.create_index("idx_jobs_user", "jobs", ["username"])

    op.create_table(
        "chunks",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("job_id", sa.String(128), sa.ForeignKey("jobs.job_id", ondelete="CASCADE"), nullable=False),
        sa.Column("chunk_key", sa.String(256), nullable=False),
        sa.Column("src_latex", sa.Text),
        sa.Column("mt_latex", sa.Text),
        sa.Column("edited", sa.Integer, server_default="0"),
        sa.Column("edit_note", sa.Text),
        sa.UniqueConstraint("job_id", "chunk_key", name="uq_chunks_job_key"),
    )
    op.create_index("idx_chunks_job", "chunks", ["job_id"])

    op.create_table(
        "global_terms",
        sa.Column("en_term", sa.String(256), primary_key=True),
        sa.Column("vi_term", sa.Text, nullable=False),
        sa.Column("frequency", sa.Integer, server_default="1"),
        sa.Column("confidence", sa.Float, server_default="0.5"),
        sa.Column("first_job", sa.String(128)),
        sa.Column("updated_at", sa.String(40)),
    )
    op.create_index("idx_terms_freq", "global_terms", ["frequency"])

    op.create_table(
        "users",
        sa.Column("username", sa.String(64), primary_key=True),
        sa.Column("password_hash", sa.Text, nullable=False),
        sa.Column("security_question", sa.Text, nullable=False),
        sa.Column("security_answer_hash", sa.Text, nullable=False),
        sa.Column("created_at", sa.String(40)),
        sa.Column("is_admin", sa.Integer, server_default="0"),
    )

    op.create_table(
        "sessions",
        sa.Column("token", sa.String(128), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("last_active", sa.Float, nullable=False),
    )
    op.create_index("idx_sessions_username", "sessions", ["username"])


def downgrade() -> None:
    op.drop_table("sessions")
    op.drop_table("users")
    op.drop_table("global_terms")
    op.drop_table("chunks")
    op.drop_table("jobs")
