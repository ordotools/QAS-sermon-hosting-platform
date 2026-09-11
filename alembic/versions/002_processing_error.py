"""add media processing_error

Revision ID: 002
Revises: 001
Create Date: 2026-09-11

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "mediaitem",
        sa.Column("processing_error", sa.String(length=500), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("mediaitem", "processing_error")
