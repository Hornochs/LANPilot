#!/usr/bin/env python3
"""Fetch the JavaScript and CSS libraries that are vendored under web/static/vendor.

LANPilot has no Node build. The few frontend libraries it uses are committed to the
repository; this script reproduces them from the npm registry. Every package is pinned to
a version and to the registry's integrity hash, so an update is an explicit, reviewable
change: edit the table below, run the script, test, and update NOTICE.md.

Usage: python3 scripts/vendor_frontend.py
"""

from __future__ import annotations

import base64
import hashlib
import io
import tarfile
import urllib.request
from pathlib import Path

TARGET = Path(__file__).resolve().parent.parent / "src/lanpilot/web/static/vendor"

# name, version, integrity, target folder, {path in package: file name}
PACKAGES = [
    (
        "htmx.org",
        "2.0.11",
        "sha512-Thx/WtpeOQqSrqBCw/A1cwGJGg4UrVa3+sW0GmrM3p4gJgO89ecH4qtbnyzDDWFvBTqjnIMCgELTNt636dtamA==",
        "htmx",
        {"dist/htmx.min.js": "htmx.min.js", "LICENSE": "LICENSE"},
    ),
    (
        "htmx-ext-sse",
        "2.2.4",
        "sha512-LJmxVhykyflBWgh5PvbRidcyuqMHlgfajmmzumvKctv9puvsufeH6OaejSMZTNTnEI8O2wXXn4ZZtBdpEMqmEQ==",
        "htmx-ext-sse",
        {"dist/sse.min.js": "sse.min.js", "LICENSE": "LICENSE"},
    ),
    (
        "tom-select",
        "2.6.2",
        "sha512-eiXIkk82XzzUTOVQDFpy+ZGejuqdTvMukyUyeZBQaKnqW42UoTiTZnrod/eX0rkqJEc4rZY9DZ3zdjV07i29wQ==",
        "tom-select",
        {
            "dist/js/tom-select.base.min.js": "tom-select.base.min.js",
            "dist/css/tom-select.min.css": "tom-select.min.css",
            "LICENSE": "LICENSE",
        },
    ),
]


def fetch(name: str, version: str, integrity: str) -> bytes:
    url = f"https://registry.npmjs.org/{name}/-/{name}-{version}.tgz"
    with urllib.request.urlopen(url, timeout=60) as response:
        data: bytes = response.read()
    algorithm, _, expected = integrity.partition("-")
    actual = base64.b64encode(hashlib.new(algorithm, data).digest()).decode()
    if actual != expected:
        raise SystemExit(f"{name} {version}: integrity mismatch, refusing to use the download")
    return data


def main() -> None:
    for name, version, integrity, folder, files in PACKAGES:
        destination = TARGET / folder
        destination.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(fetch(name, version, integrity))) as archive:
            for member, filename in files.items():
                extracted = archive.extractfile(f"package/{member}")
                if extracted is None:
                    raise SystemExit(f"{name} {version}: {member} is missing from the package")
                (destination / filename).write_bytes(extracted.read())
                print(f"{name} {version}: {folder}/{filename}")  # noqa: T201 - command line tool


if __name__ == "__main__":
    main()
