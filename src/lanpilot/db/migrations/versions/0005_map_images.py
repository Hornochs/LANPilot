"""Create map_images.

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "map_images",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("family", sa.String(length=32), nullable=False),
        sa.Column("map_key", sa.String(length=200), nullable=False),
        sa.Column("filename", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("source_url", sa.String(length=500), nullable=True),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_map_images")),
        sa.UniqueConstraint("family", "map_key", name=op.f("uq_map_images_family")),
        sa.UniqueConstraint("filename", name=op.f("uq_map_images_filename")),
    )


def downgrade() -> None:
    op.drop_table("map_images")
