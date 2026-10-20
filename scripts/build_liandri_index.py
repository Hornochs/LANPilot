#!/usr/bin/env python3
"""Build the Liandri Archives image index that LANPilot ships.

The Liandri Archives (the Unreal wiki, CC BY-SA 3.0) are mirrored as static pages on
https://unrealarchive.org/wikis/the-liandri-archives/. Unlike the Unreal Archive uploads they
cover the stock maps. Each map has a page; the category pages "Unreal Tournament maps",
"UT2004 maps" and "Unreal Tournament 3 maps" list them per game. LANPilot needs only
"game + map name -> screenshot URL":

    python3 scripts/build_liandri_index.py src/lanpilot/catalog/liandri.json.gz [cache-dir]

The pages are read as text and never executed. A cache directory makes a second run
free. The first run asks for about 300 pages, one after the other with a pause. The images
themselves are not copied; LANPilot fetches one when it needs it
(lanpilot.core.image_sources.LiandriArchives). A page's first screenshot is taken; images
whose name says pickups, layout or map are plans, not screenshots, and are skipped.
"""

from __future__ import annotations

import gzip
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

BASE = "https://unrealarchive.org/wikis/the-liandri-archives/"
CATEGORIES = {
    "Unreal Tournament": "Category:Unreal_Tournament_maps.html",
    "Unreal Tournament 2004": "Category:UT2004_maps.html",
    "Unreal Tournament 3": "Category:Unreal_Tournament_3_maps.html",
}
USER_AGENT = "LANPilot-index-builder (+https://github.com/lan-dot-party/LANPilot)"
PAUSE_SECONDS = 0.3
MAX_PAGE_BYTES = 2 * 1024 * 1024
MAP_NAME = re.compile(r"[A-Za-z0-9_\-.\[\]]{1,64}")
MEMBER = re.compile(r'href="\./([^":"#]+)\.html"')
IMAGE = re.compile(r'href="(w/images/[A-Za-z0-9_.\-]+\.(?:gif|jpe?g|png))"', re.IGNORECASE)
PLAN = re.compile(
    r"placeholder|pickup|layout|overview|weapon|(?<![a-z])map(?![a-z])", re.IGNORECASE
)


def fetch(url: str, cache: Path | None) -> str | None:
    """The page as text; ``None`` if it does not exist. Anything else is an error."""
    target = cache / re.sub(r"[^A-Za-z0-9_.\-]", "_", urlsplit(url).path[-120:]) if cache else None
    if target is not None and target.exists():
        return target.read_text("utf-8", errors="replace")
    time.sleep(PAUSE_SECONDS)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            body = response.read(MAX_PAGE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    if len(body) > MAX_PAGE_BYTES:
        return None
    text = body.decode("utf-8", errors="replace")
    if target is not None:
        cache.mkdir(parents=True, exist_ok=True)  # type: ignore[union-attr]
        target.write_text(text, "utf-8")
    return text


def members(category: str) -> list[str]:
    """Page names in a category page; the links in the navigation and the footer have a
    colon or point elsewhere and are skipped."""
    seen: dict[str, None] = {}
    for raw in MEMBER.findall(category):
        name = unquote(raw)
        if MAP_NAME.fullmatch(name) and name.count("-") >= 1 and name[:2].isalpha():
            seen[name] = None
    return list(seen)


def screenshot(page: str) -> str | None:
    for path in IMAGE.findall(page):
        if not PLAN.search(Path(path).stem):
            url = urljoin(BASE, path)
            parts = urlsplit(url)
            if parts.scheme == "https" and parts.hostname == "unrealarchive.org":
                return url
    return None


def build(cache: Path | None) -> dict[str, dict[str, str]]:
    games: dict[str, dict[str, str]] = {}
    for game, category in CATEGORIES.items():
        listing = fetch(BASE + category, cache)
        if listing is None:
            raise SystemExit(f"{category}: not found")
        found: dict[str, str] = {}
        names = members(listing)
        for name in names:
            page = fetch(BASE + name + ".html", cache)
            url = screenshot(page) if page else None
            if url:
                found[name.lower()] = url[len(BASE) :]
        games[game] = dict(sorted(found.items()))
        print(f"{game}: {len(found)} of {len(names)} pages have a screenshot")  # noqa: T201
    return games


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        print(__doc__, file=sys.stderr)  # noqa: T201 - command line tool
        return 2
    output = Path(argv[1])
    games = build(Path(argv[2]) if len(argv) == 3 else None)
    document = {
        "source": BASE,
        "license": "CC BY-SA 3.0 (Liandri Archives); the screenshots show game content",
        "base": BASE,
        "generated": datetime.now(UTC).date().isoformat(),
        "games": games,
    }
    data = json.dumps(document, separators=(",", ":"), sort_keys=True).encode()
    output.write_bytes(gzip.compress(data, compresslevel=9, mtime=0))
    print(f"{output}: {output.stat().st_size} bytes")  # noqa: T201 - command line tool
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
