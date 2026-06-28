"""Add global_terms.field — lĩnh vực chuyên ngành cho thuật ngữ.

Free-text domain label (vd "Học máy", "Toán học"). NULL = chưa phân loại.
Điền tự động khi trích glossary (AI) hoặc sửa tay trong editor.

Revision ID: 002_global_term_field
Revises: 001_initial
Create Date: 2026-06-22
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "002_global_term_field"
down_revision: Union[str, None] = "001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("global_terms", sa.Column("field", sa.String(64), nullable=True))
    op.create_index("idx_terms_field", "global_terms", ["field"])


def downgrade() -> None:
    op.drop_index("idx_terms_field", table_name="global_terms")
    op.drop_column("global_terms", "field")
