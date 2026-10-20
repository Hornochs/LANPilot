"""Create server_maps.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "server_maps",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("server_id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=200), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=True),
        sa.Column("game_type", sa.String(length=100), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("extra", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["server_id"],
            ["servers.id"],
            name=op.f("fk_server_maps_server_id_servers"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_server_maps")),
        sa.UniqueConstraint("server_id", "key", "game_type", name=op.f("uq_server_maps_server_id")),
    )
    with op.batch_alter_table("server_maps", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_server_maps_server_id"), ["server_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("server_maps", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_server_maps_server_id"))
    op.drop_table("server_maps")
