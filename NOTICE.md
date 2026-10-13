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
