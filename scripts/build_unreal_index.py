#!/usr/bin/env python3
"""Build the Unreal Archive image index that LANPilot ships.

Unreal Archive publishes its metadata (public domain, the Unlicense) as one YAML file per
upload in https://github.com/unreal-archive/unreal-archive-data. LANPilot needs only
"game + map file name -> first screenshot URL". Fetch the map folders and run this script:

    git clone --depth 1 --filter=blob:none --sparse \\
        https://github.com/unreal-archive/unreal-archive-data.git ua
    git -C ua sparse-checkout set "content/Unreal Tournament/Maps" \\
        "content/Unreal Tournament 2004/Maps" "content/Unreal Tournament 3/Maps"
    python3 scripts/build_unreal_index.py ua/content \\
        src/lanpilot/catalog/unreal-archive.json.gz

The files are read as text and never executed. Only the standard library is used: the files
follow one fixed layout, so a small line parser is enough and no YAML library is needed.
When several uploads contain a map of the same name, an original wins over a variation, then
the one indexed first. The images themselves are not copied; LANPilot fetches one when it
needs it (lanpilot.core.image_sources.UnrealArchive).
"""

from __future__ import annotations

import gzip
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

GAMES = {
    "Unreal Tournament": ".unr",
    "Unreal Tournament 2004": ".ut2",
    "Unreal Tournament 3": ".ut3",
}
HOSTS = ("unreal-archive-img.s3.sgp.io.cloud.ovh.net", "ua-img.s3.us-west-002.backblazeb2.com")
MAP_NAME = re.compile(r"[A-Za-z0-9_\-.\[\]]{1,64}")
SCALAR = re.compile(r'^(\w+): "(.*)"$')
ITEM = re.compile(r'^(?:- |  )(\w+): "(.*)"$')
MAX_FILE_BYTES = 1024 * 1024


def read(path: Path) -> dict[str, object] | None:
    """The fields LANPilot needs from one upload's YAML file."""
    if path.stat().st_size > MAX_FILE_BYTES:
        return None
    entry: dict[str, object] = {"files": [], "images": []}
    section = ""
    item: dict[str, str] = {}

    def finish_item() -> None:
        if section == "files" and "name" in item:
            entry["files"].append(item["name"])  # type: ignore[attr-defined]
        elif section == "attachments" and item.get("type") == "IMAGE" and "url" in item:
            entry["images"].append(item["url"])  # type: ignore[attr-defined]

    for line in path.read_text("utf-8", errors="replace").splitlines():
        if line and not line.startswith((" ", "-")):
            finish_item()
            item = {}
            section = line.rstrip(":") if line.endswith(":") else ""
            match = SCALAR.match(line)
            if match:
                entry[match.group(1)] = match.group(2)
            continue
        match = ITEM.match(line)
        if match is None:
            continue
        if line.startswith("- "):
            finish_item()
            item = {}
        item[match.group(1)] = match.group(2)
    finish_item()
    return entry


def build(content: Path) -> dict[str, object]:
    games: dict[str, dict[str, list[str]]] = {}
    for game, extension in GAMES.items():
        best: dict[str, tuple[tuple[int, str], str, str]] = {}
        for path in sorted((content / game / "Maps").rglob("*.yml")):
            entry = read(path)
            if entry is None or entry.get("deleted") == "true" or not entry["images"]:
                continue
            image = str(entry["images"][0])  # type: ignore[index]
            parts = urlsplit(image)
            if parts.scheme != "https" or parts.hostname not in HOSTS:
                continue
            rank = (1 if entry.get("variationOf") else 0, str(entry.get("firstIndex", "")))
            title = str(entry.get("title", ""))[:100]
            for name in entry["files"]:  # type: ignore[attr-defined]
                stem, dot, suffix = str(name).rpartition(".")
                if not dot or f".{suffix.lower()}" != extension or not MAP_NAME.fullmatch(stem):
                    continue
                key = stem.lower()
                if key not in best or rank < best[key][0]:
                    best[key] = (rank, image, title)
        games[game] = {key: [image, title] for key, (_, image, title) in sorted(best.items())}
    return games


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)  # noqa: T201 - command line tool
        return 2
    content, output = Path(argv[1]), Path(argv[2])
    try:
        revision = subprocess.run(  # noqa: S603 - fixed command, path given by the user
            ["git", "-C", str(content), "rev-parse", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        revision = "unknown"
    games = build(content)
    document = {
        "source": "https://github.com/unreal-archive/unreal-archive-data",
        "license": "Unlicense (public domain)",
        "revision": revision,
        "generated": datetime.now(UTC).date().isoformat(),
        "games": games,
    }
    data = json.dumps(document, separators=(",", ":"), sort_keys=True).encode()
    # mtime=0 keeps the file identical when nothing changed.
    output.write_bytes(gzip.compress(data, compresslevel=9, mtime=0))
    for game, maps in games.items():
        print(f"{game}: {len(maps)} maps")  # noqa: T201 - command line tool
    print(f"{output}: {output.stat().st_size} bytes")  # noqa: T201 - command line tool
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
