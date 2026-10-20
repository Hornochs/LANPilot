# Notices and provenance

LANPilot is released under the MIT License (see [LICENSE](LICENSE)). This file records
material that originates elsewhere, so the licensing of every part of the repository stays
traceable. Update it in the same change that brings such material in.

## Code derived from Discord-Gameserver-Notifier

[Discord-Gameserver-Notifier](https://github.com/lan-dot-party/Discord-Gameserver-Notifier) is
published under GPL-3.0. Portions written by the LANPilot maintainer may be reused here under
MIT by the author's own grant. Contributions to that project by other people are **not**
covered by this grant and must not be copied.

| LANPilot path | Origin | Author | Notes |
|---|---|---|---|
| _none yet_ | | | |

## Vendored third-party files

All paths are below `src/lanpilot/web/static/vendor/`. Each folder contains the project's
license file. The files are reproduced with `scripts/vendor_frontend.py`, which pins every
package to a version and to the npm registry's integrity hash.

| Path | Project | Version | License |
|---|---|---|---|
| `htmx/htmx.min.js` | [htmx](https://htmx.org) | 2.0.11 | 0BSD |
| `htmx-ext-sse/sse.min.js` | [htmx SSE extension](https://github.com/bigskysoftware/htmx-extensions) | 2.2.4 | 0BSD |
| `tom-select/tom-select.base.min.js`, `tom-select.min.css` | [Tom Select](https://tom-select.js.org) | 2.6.2 | Apache-2.0 |

## Data sources

Map preview images are not distributed with LANPilot. They are fetched at runtime from the
sources an administrator enables and stay the property of their respective owners.

| Path | Origin | License |
|---|---|---|
| `src/lanpilot/catalog/unreal-archive.json.gz` | index (map file name → first screenshot URL and title) built by `scripts/build_unreal_index.py` from the metadata in [unreal-archive-data](https://github.com/unreal-archive/unreal-archive-data); the revision is stored in the file | Unlicense (public domain) |
| `src/lanpilot/catalog/liandri.json.gz` | index (map name → screenshot path) built by `scripts/build_liandri_index.py` from the [Liandri Archives](https://unrealarchive.org/wikis/the-liandri-archives/) mirror; only names and paths are stored, the images are fetched at run time and re-encoded | CC BY-SA 3.0 (wiki text); the screenshots show Epic Games content and are not redistributed |
| `src/lanpilot/catalog/counterstrike.toml` | stock map names, written for LANPilot | MIT, like LANPilot |
