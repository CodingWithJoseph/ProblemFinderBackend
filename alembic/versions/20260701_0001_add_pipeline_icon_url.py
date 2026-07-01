"""Add the optional Pipeline product icon.

Revision ID: 20260701_0001
Revises: None
Create Date: 2026-07-01
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260701_0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "pipeline",
        sa.Column("icon_url", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("pipeline", "icon_url")
