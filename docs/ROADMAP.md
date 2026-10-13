# Roadmap

LANPilot is built in phases. Steps are numbered so that status notes, issues and commits can
refer to them. A step is done when CI is green, the "Current status" section below names it
as done, and [architecture.md](architecture.md) describes any structural change it made.

## Current status

_Last updated: 2026-10-13_

| Phase | State |
|---|---|
| 0 – Foundation | done locally; workflows not yet run on GitHub |
| 1 – Core and release pipeline | in progress |
| 2 – Counter-Strike | not started |
| 3 – Trackmania | not started |
| 4 – Unreal Tournament | not started |
| 5 – Player management and console | not started |
| 6 – On-demand LAN scan | not started |
| 7 – More games | not started |
| 8 – Hardening and 1.0 | not started |

**Done:** 0.1–0.5, 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8. Lint, type check, tests, wheel build and the secret scan pass
locally. The GitHub workflows (`ci.yml`, `security.yml`) have been syntax-checked only; their
first real run happens with the first push. The same holds for `release.yml`: the packages
build locally, but their install scripts have only been syntax-checked.

**In progress:** –

**Next:** 1.9 (documentation: installation, configuration, reverse proxy, writing an
adapter)

**Open questions:** see [Open points](#open-points).

## Scope

**In scope**

- Dashboard with one card per server: name, game, current map with image, players, status.
- Per server: change map (searchable list), soft restart, hard restart, player list with
  kick/ban, raw console. Only what the game supports is shown.
- Map list per server, cached in the database and refreshable on demand.
- Map images from online sources with a local cache and a "prefetch everything" action for
  offline LAN parties.
- User management with the roles `admin`, `operator` and `viewer`; audit log of all actions.
- Monitoring-only display for games without remote control.
- `.deb`, `.rpm` and Arch packages built by GitHub Actions; complete documentation.

**Out of scope**

- Host access: starting or stopping processes, host CPU/RAM, reading map files from disk.
- MFA, anonymous access, scheduled actions, periodic network scans, installing game servers.

**Consequences of being protocol-only**

- "Load" means the player count plus whatever the protocol itself reports (for example
  `stats` on Counter-Strike, `GetNetworkStats` on Trackmania).
- "Hard restart" means reloading the level completely. Terminating the server process is
  not offered, because LANPilot could not bring it back.
- Where a protocol cannot list installed maps (Call of Duty fast files, Trackmania tracks
  outside the playlist), the list comes from a bundled catalogue of stock maps plus manually
  maintained entries.

## Phases

### Phase 0 – Foundation

- **0.1** ✔ Working notes for AI-assisted development and `CLAUDE.md`.
- **0.2** ✔ `pyproject.toml` (hatchling, src layout), `ruff`, `mypy`, `pytest`, `.gitignore`,
  `.env.example`.
- **0.3** ✔ `README.md`, `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, `CHANGELOG.md`,
  `NOTICE.md`, this roadmap, `architecture.md`, research notes, issue and PR templates.
- **0.4** ✔ `.github/workflows/ci.yml` (lint, types, tests on Python 3.11–3.13, build) and
  `security.yml` (CodeQL, dependency review, pip-audit, secret scan), Dependabot.
- **0.5** ✔ Development deployment target prepared.

### Phase 1 – Core and release pipeline (v0.1)

- **1.1** ✔ Application skeleton, configuration (`lanpilot.toml` + `LANPILOT_*`, including
  `public_url`, `allowed_hosts`, `trusted_proxies`), database with migrations, CLI.
- **1.2** ✔ Login, sessions, CSRF with origin check, host check, roles, user management,
  security headers.
- **1.3** ✔ Adapter framework, registry, credential encryption, demo adapter (simulated server).
- **1.4** ✔ Create and edit servers with a form generated from the adapter's connection schema.
- **1.5** ✔ Poller, action layer with audit log, server-sent events.
- **1.6** ✔ Dashboard and server page (cards, searchable map list, restart buttons).
- **1.7** ✔ Map image basics: cache, placeholder, upload.
- **1.8** ✔ `release.yml`: build the wheel, then `.deb`, `.rpm` and Arch packages, install tests
  in Debian, Ubuntu, Fedora and Arch containers, upload to the GitHub release with checksums.
  The virtual environment is built per distribution in CI and shipped inside the package so
  that installation works without internet access. The post-install script creates the
  `lanpilot` system user, writes `/etc/lanpilot/lanpilot.toml` if absent and generates the
  credential encryption key (`lanpilot secret generate-key --output /etc/lanpilot/secret.key`)
  if none exists; an upgrade or reinstall never replaces an existing key. Removing the package
  keeps key, configuration and data; only a purge deletes them.
- **1.9** Documentation: installation, configuration, reverse proxy (Nginx, Caddy, Traefik,
  no proxy), "writing an adapter".

### Phase 2 – Counter-Strike (high priority, v0.2)

- **2.1** GoldSrc RCON transport (challenge, split response packets) with a simulator.
- **2.2** Counter-Strike 1.6 adapter: `maps *`, `changelevel`, `sv_restart`, `restart`,
  `status`, `stats`; verified against a real server; facts in `games/counter-strike.md`.
- **2.3** Source RCON adapters: CS:Source, CS:GO, CS2, including workshop maps.
- **2.4** Stock map catalogue; image providers Steam Workshop, GameBanana, GameTracker.

### Phase 3 – Trackmania (high priority, v0.3)

- **3.1** GBXRemote 2 transport (length and handle framing, callbacks) with a simulator;
  method list checked against a real server.
- **3.2** TMNF/TMUF adapter (`GetChallengeList`, `ChooseNextChallenge` + `NextChallenge`,
  `RestartChallenge`). Map list: playlist from the protocol plus manually maintained tracks
  (`InsertChallenge`).
- **3.3** TM2/TM2020 adapter (`GetMapList`, `JumpToMapIdent`, `RestartMap`).
- **3.4** Image provider TMX by track UId.
- **3.5** TrackMania Original/Sunrise/Nations ESWC: find out whether remote control exists;
  otherwise monitoring only.

### Phase 4 – Unreal Tournament (high priority, v0.4)

- **4.1** **Measure first:** WebAdmin forms and authentication on real UT99 and UT2004
  servers; recorded pages become test fixtures; facts in `games/unreal-tournament.md`.
- **4.2** UWeb transport (basic auth or form login, HTML parsing) with a simulator.
- **4.3** UT99 and UT2004 adapters; map list from the WebAdmin dropdown per game type.
- **4.4** UT2003 and UT3 adapters.
- **4.5** Image provider Unreal Archive, including the script that builds the index.

### Phase 5 – Player management and console (v0.5)

- **5.1** Player list, kick and ban for the three priority families.
- **5.2** Raw console for `admin` only, audited, with a global switch.

### Phase 6 – On-demand LAN scan (v0.6)

- **6.1** Adapter method `discover()` using broadcast queries, limited to configured networks.
- **6.2** Result list with an "add" action; credentials are entered afterwards. No background
  scanning.

### Phase 7 – More games (v0.7+)

- **7.1** Quake 3 RCON family: Call of Duty 1/4/5, Quake 3, Jedi Knight (map list from
  catalogue and rotation).
- **7.2** Battlefield 2, Renegade X, ElDewrito, each after verifying the protocol.
- **7.3** Monitoring only: Age of Empires 1/2, Alien vs Predator 2, C&C Generals, F.E.A.R. 2,
  FlatOut 2, Halo 1, Serious Sam, Stronghold, Supreme Commander, Toxikk, Warcraft III.

### Phase 8 – Hardening and 1.0

- **8.1** Security review, threat model in `security.md`.
- **8.2** Package upgrade tests.
- **8.3** German user interface in addition to English, documentation complete, release 1.0.

## Open points

- CS2/CS:GO: needs a test server; behaviour of `maps *` and the workshop commands is
  unverified.
- Unreal WebAdmin: form fields and authentication are unverified until step 4.1.
- TrackMania Original/Sunrise/ESWC: unknown whether any remote control exists.
- GameTracker and GameBanana: terms of use not confirmed; these image sources are therefore
  low priority and can be disabled individually.
