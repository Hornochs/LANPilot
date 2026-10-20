"""Create servers.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "servers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("adapter_id", sa.String(length=32), nullable=False),
        sa.Column("host", sa.String(length=253), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("settings", sa.Text(), nullable=False),
        sa.Column("credentials", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_servers")),
        sa.UniqueConstraint("name", name=op.f("uq_servers_name")),
    )


def downgrade() -> None:
    op.drop_table("servers")
