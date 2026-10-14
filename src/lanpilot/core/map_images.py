"""Map preview images: processing, storage and lookup.

Images come from outside (an administrator's upload now, online sources later) and are
shown to every user, so nothing is stored as received. Every image is decoded, scaled down
and encoded again, which removes metadata and anything else hidden in the file. Files get
random names chosen by LANPilot; no path is ever built from external text.
"""

from __future__ import annotations

import asyncio
import io
import re
import secrets
import time
import warnings
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lanpilot.db.models import MapImage

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
# Decoded size limit: a small file can describe an enormous image ("decompression bomb").
MAX_PIXELS = 40_000_000
MAX_SIZE = (1280, 720)
ALLOWED_FORMATS = frozenset({"JPEG", "PNG", "WEBP"})
OUTPUT_SUFFIX = ".webp"
OUTPUT_MEDIA_TYPE = "image/webp"
FILENAME_PATTERN = re.compile(r"[A-Za-z0-9_-]{20,40}\.webp")
PROVIDER_UPLOAD = "upload"
DIRECTORY_NAME = "map-images"


class ImageError(Exception):
    """The image cannot be used; the message is safe to show to the user."""


@dataclass(frozen=True)
class ProcessedImage:
    data: bytes
    width: int
    height: int


def normalise_key(map_key: str) -> str:
    return map_key.strip().lower()


def process(data: bytes) -> ProcessedImage:
    """Decode, shrink and re-encode an image. Blocking; call it in a thread."""
    if not data:
        raise ImageError("Choose an image file.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ImageError(f"The file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
    try:
        with warnings.catch_warnings():
            # Pillow only warns about suspiciously large images up to twice its limit.
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data), formats=sorted(ALLOWED_FORMATS)) as image:
                if image.width * image.height > MAX_PIXELS:
                    raise ImageError("The image has too many pixels.")
                image.load()
                upright = ImageOps.exif_transpose(image)
                converted = upright.convert("RGB")
    except ImageError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ImageError("The image has too many pixels.") from None
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        raise ImageError("This is not a JPEG, PNG or WebP image.") from None

    converted.thumbnail(MAX_SIZE, Image.Resampling.LANCZOS)
    output = io.BytesIO()
    # A fresh RGB image carries no metadata, so saving it writes pixels only.
    converted.save(output, "WEBP", quality=82, method=4)
    return ProcessedImage(output.getvalue(), converted.width, converted.height)


def images_directory(data_dir: Path) -> Path:
    directory = data_dir / DIRECTORY_NAME
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory


# Files younger than this are left alone by sweep(): another process (the prefetch command)
# may have written one and not yet committed its database row.
SWEEP_MIN_AGE_SECONDS = 3600


def sweep(directory: Path, index: MapImageIndex) -> int:
    """Delete image files no database row refers to. Returns how many were deleted."""
    removed = 0
    if not directory.is_dir():
        return removed
    cutoff = time.time() - SWEEP_MIN_AGE_SECONDS
    for path in directory.iterdir():
        if (
            FILENAME_PATTERN.fullmatch(path.name)
            and not index.knows(path.name)
            and path.stat().st_mtime < cutoff
        ):
            path.unlink(missing_ok=True)
            removed += 1
    return removed


def _write(directory: Path, data: bytes) -> str:
    filename = secrets.token_urlsafe(18) + OUTPUT_SUFFIX
    path = directory / filename
    path.touch(mode=0o600, exist_ok=False)
    path.write_bytes(data)
    return filename


class MapImageIndex:
    """In-memory lookup (game, map key) -> file name, so rendering a page costs no query.

    ``game`` is an adapter id: every game has its own pictures, even where map names repeat
    across a family (``de_dust2`` looks different in every Counter-Strike)."""

    def __init__(self) -> None:
        self._by_map: dict[tuple[str, str], str] = {}
        self._filenames: set[str] = set()

    async def load(self, db: AsyncSession) -> bool:
        """Read the index from the database; returns whether it changed."""
        rows = (await db.execute(select(MapImage))).scalars().all()
        loaded = {(row.game, row.map_key): row.filename for row in rows}
        changed = loaded != self._by_map
        self._by_map = loaded
        self._filenames = set(loaded.values())
        return changed

    def filename(self, game: str, map_key: str) -> str | None:
        return self._by_map.get((game, normalise_key(map_key)))

    def knows(self, filename: str) -> bool:
        return filename in self._filenames

    def _set(self, game: str, map_key: str, filename: str | None) -> None:
        old = self._by_map.pop((game, map_key), None)
        if old is not None:
            self._filenames.discard(old)
        if filename is not None:
            self._by_map[(game, map_key)] = filename
            self._filenames.add(filename)

    async def store(
        self,
        db: AsyncSession,
        directory: Path,
        game: str,
        map_key: str,
        data: bytes,
        *,
        provider: str = PROVIDER_UPLOAD,
        source_url: str | None = None,
    ) -> MapImage:
        """Process and save an image for a map, replacing an existing one. Commits."""
        key = normalise_key(map_key)
        processed = await asyncio.to_thread(process, data)
        filename = await asyncio.to_thread(_write, directory, processed.data)

        row = (
            await db.execute(select(MapImage).where(MapImage.game == game, MapImage.map_key == key))
        ).scalar_one_or_none()
        replaced = row.filename if row is not None else None
        if row is None:
            row = MapImage(game=game, map_key=key)
            db.add(row)
        row.filename = filename
        row.provider = provider
        row.source_url = source_url
        row.width = processed.width
        row.height = processed.height
        try:
            await db.commit()
        except Exception:
            (directory / filename).unlink(missing_ok=True)
            raise
        self._set(game, key, filename)
        if replaced is not None:
            (directory / replaced).unlink(missing_ok=True)
        return row

    async def remove(self, db: AsyncSession, directory: Path, game: str, map_key: str) -> bool:
        """Delete a map's image. Returns whether there was one. Commits."""
        key = normalise_key(map_key)
        row = (
            await db.execute(select(MapImage).where(MapImage.game == game, MapImage.map_key == key))
        ).scalar_one_or_none()
        if row is None:
            return False
        filename = row.filename
        await db.delete(row)
        await db.commit()
        self._set(game, key, None)
        (directory / filename).unlink(missing_ok=True)
        return True
