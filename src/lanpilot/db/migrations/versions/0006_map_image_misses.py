"""Create map_image_misses.

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "map_image_misses",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("family", sa.String(length=32), nullable=False),
        sa.Column("map_key", sa.String(length=200), nullable=False),
        sa.Column("reason", sa.String(length=16), nullable=False),
        sa.Column("checked_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_map_image_misses")),
        sa.UniqueConstraint("family", "map_key", name=op.f("uq_map_image_misses_family")),
    )


def downgrade() -> None:
    op.drop_table("map_image_misses")
