"""Map images per game instead of per game family.

Images were shared by all games of a family, so all Counter-Strike games showed the same
de_dust2. They now belong to one game (adapter id). Existing images are kept where their
game is known: images of the single-game families ("demo") and GameTracker images, whose
URL names the game. Other Counter-Strike images (uploads, GameBanana, Steam Workshop) cannot
be attributed and are dropped; the online lookup finds them again, and LANPilot deletes the
orphaned files when it starts. Remembered misses are a cache and are cleared.

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | None = None
depends_on: str | None = None

GAMETRACKER = "https://image.gametracker.com/images/maps/160x120/"
# GameTracker slug -> adapter id, for the images found before this migration.
SLUGS = {"cs": "cs16", "css": "css", "csgo": "csgo"}


def upgrade() -> None:
    for slug, game in SLUGS.items():
        op.get_bind().execute(
            sa.text(
                "UPDATE map_images SET family = :game "
                "WHERE family = 'counterstrike' AND source_url LIKE :pattern"
            ),
            {"game": game, "pattern": f"{GAMETRACKER}{slug}/%"},
        )
    op.execute("DELETE FROM map_images WHERE family = 'counterstrike'")
    op.execute("DELETE FROM map_image_misses")
    _rename("family", "game")


def downgrade() -> None:
    _rename("game", "family")


def _rename(old: str, new: str) -> None:
    for table in ("map_images", "map_image_misses"):
        # Two batches: a constraint created in the batch that renames its column gets lost.
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(f"uq_{table}_{old}", type_="unique")
            batch.alter_column(old, new_column_name=new)
        with op.batch_alter_table(table) as batch:
            batch.create_unique_constraint(f"uq_{table}_{new}", [new, "map_key"])
