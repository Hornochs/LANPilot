# LANPilot – notes for Claude Code

LANPilot is a web interface for monitoring and remote-controlling game servers at LAN parties.
This file is the entry point for AI-assisted work on the repository. It is public: keep
environment-specific details (hostnames, credentials, local paths) out of it.

## Read first

1. `docs/ROADMAP.md` – phases with numbered steps; the "Current status" section at the top says
   which step is in progress.
2. `docs/architecture.md` – package layout, adapter interface, data model, security mechanisms.
   Read this instead of re-exploring the code.
3. `docs/games/<game>.md` – verified protocol facts per game.
   `docs/research/` – research notes, including facts that are still unverified.

## Rules

- **Keep status and architecture current.** A step is only done when
  `docs/ROADMAP.md` ("Current status") names it as done and `docs/architecture.md` describes
  any structural change it made. Protocol facts measured against a real server go into
  `docs/games/<game>.md`.
- **Protocol-only.** LANPilot talks to game servers through their remote protocols (RCON,
  XML-RPC, WebAdmin). No SSH, no host agent, no file access on game hosts.
- **License hygiene.** The project is MIT. Do not add GPL-licensed dependencies (`rcon`,
  `pygbx`, PyPlanet) and do not copy third-party GPL code. Code taken from the
  Discord-Gameserver-Notifier must be the maintainer's own; record its origin in `NOTICE.md`.
- **No Node build.** The frontend is server-rendered (Jinja2 + HTMX + Tom Select);
  JavaScript libraries are vendored under `src/lanpilot/web/static/vendor/` by
  `scripts/vendor_frontend.py`. No inline scripts, styles or event handlers (CSP).
- **Untrusted input.** Everything a game server sends (names, player lists, HTML) is untrusted.
  Never interpolate user or server data into console commands without validation.
- Project language is English (code, comments, docs, commit messages).
- Do not commit unless asked.

## Commands

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"   # once
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy
.venv/bin/pytest
```
