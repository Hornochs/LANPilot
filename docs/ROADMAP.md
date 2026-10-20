# Roadmap

LANPilot is built in phases. Steps are numbered so that status notes, issues and commits can
refer to them. A step is done when CI is green, the "Current status" section below names it
as done, and [architecture.md](architecture.md) describes any structural change it made.

## Current status

_Last updated: 2026-10-20_

| Phase | State |
|---|---|
| 0 – Foundation | done |
| 1 – Core and release pipeline | done |
| 2 – Counter-Strike | done |
| 3 – Trackmania | in progress |
| 4 – Unreal Tournament | done |
| 5 – Player management and console | done |
| 6 – On-demand LAN scan | done |
| 7 – More games | not started |
| 8 – Hardening and 1.0 | in progress |

**Done:** 0.1–0.5, 1.1–1.9, 2.1–2.4, 4.1–4.5, 5.1–5.2, 6.1–6.2 and 8.1, which completes
phases 0, 1, 2, 4, 5 and 6; 3.1, 3.2, 3.4 and 3.5.

**Verified:** lint, type check and tests pass locally and in CI. The package workflow built
all seven distribution packages and ran the install, upgrade, reinstall and removal test for
each of them successfully (2026-10-13, in a private fork). CodeQL and dependency review are
skipped while the repository is private; they need a public repository.

All four Counter-Strike adapters were run against real servers (HLDS build 10211, CS:Source
11003710, CS:GO 1.38.8.1, CS2 1.41.9.0): status, map list, map change, both restarts and a
wrong password behaved as [games/counter-strike.md](games/counter-strike.md) describes; CS2
also with a workshop map. CS:Source, CS:GO and CS2 were also used through the web
interface.

Map images were found online on the development instance: the current map automatically,
and 34 of the 38 stock CS:GO maps through "look for all missing images" (GameTracker); the
Steam Workshop and GameBanana lookups were checked with a workshop map and two community
maps.

The TrackMania Forever adapter was run against a TMNF server (build 2011-02-21): status,
track list, track change, both restarts, a change while loading and a wrong password behaved
as [games/trackmania.md](games/trackmania.md) describes. The TrackMania Sunrise adapter was run against a Sunrise eXtreme server
the same way.

The Unreal Tournament adapters were run against real servers (UT99 v469, UT2004 build 3369,
UT3 patch 5): status, map lists per game type, change of map and game type, level reload and
a wrong password behaved as [games/unreal-tournament.md](games/unreal-tournament.md)
describes; all three were also used through the web interface.

Phase 5 could only partly be run against real servers, because none of the test servers had
a human player and most of them start bots only when one joins. Measured: the consoles of
CS 1.6, UT99, UT2004 and UT3; UT3's scoreboard with bots (listed, not kickable); the player
lists of all three UT WebAdmins, TMNF and Sunrise when empty, and the answers of TMNF and
Sunrise to kicking or banning a login that is not there. The player rows, kick and ban
requests of the UT WebAdmins follow the games' own WebAdmin code and templates, those of
CS:Source and CS:GO the known `status` formats; the details are in the game documents. CS2
has the console but no player management: its `status` format has not been measured.

The LAN scan (phase 6) was run on the development instance over a /23 network: it found
CS2 on another host and, started one group after another on the LANPilot host, CS 1.6,
TMNF, Sunrise eXtreme, UT99, UT2004 and UT3 (the last with its server holding UDP 14001 on
the same host), each with name and game type; "add" opened the form pre-filled. A scan
takes about 2 s (the broadcast wait). What was measured is in
the game documents ("LAN discovery").

The security review (8.1) and its fixes are summed up in [security.md](security.md); the
A2S client that replaced opengsq's was checked against CS2 and CS 1.6, the package with the
hash-pinned dependencies was built and inspected locally.

**In progress:** phase 8 (8.2 next).

**Next:** 8.2, 8.3; phase 7 (more games) when test servers exist. 3.3 (TM2/TM2020) is
postponed: no test servers.
Player management should be checked with real players at the next LAN party (see
[Open points](#open-points)).

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
- **1.9** ✔ Documentation: installation, configuration, reverse proxy (Nginx, Caddy, Traefik,
  no proxy), "writing an adapter".

### Phase 2 – Counter-Strike (high priority, v0.2)

- **2.1** ✔ GoldSrc RCON transport (challenge, split response packets) with a simulator.
- **2.2** ✔ Counter-Strike 1.6 adapter: `maps *`, `changelevel`, `sv_restart`, `restart`,
  `status`, `stats`; verified against a real server; facts in `games/counter-strike.md`.
- **2.3** ✔ Source RCON transport and adapters for CS:Source, CS:GO and CS2, including CS2
  workshop maps; verified against real servers.
- **2.4** ✔ Stock map catalogue (display names of the stock maps of all four games); online
  image sources Steam Workshop, GameBanana and GameTracker with remembered misses,
  automatic lookup of the current map, "look for all missing images" and
  `lanpilot images prefetch`. Images belong to one game, not to a family of games.
  Verified against the real services on the development instance.

### Phase 3 – Trackmania (high priority, v0.3)

- **3.1** ✔ GBXRemote 2 transport (length and handle framing, callbacks skipped, multicall)
  with a simulator; the methods used were checked against `system.listMethods` of a real
  server.
- **3.2** ✔ TMNF/TMUF adapter (`GetChallengeList`, `ChooseNextChallenge` + `NextChallenge`,
  `RestartChallenge`), verified against a TMNF server. Map list: the playlist from the
  protocol plus tracks outside the playlist that administrators add by hand (confirmed by
  the server, loaded with `InsertChallenge`).
- **3.3** TM2/TM2020 adapter (`GetMapList`, `JumpToMapIdent`, `RestartMap`).
- **3.4** ✔ Image source TrackMania Exchange by track UId (TMNF and TMUF sites); 59 of the
  60 Nations tracks found on the development instance.
- **3.5** ✔ TrackMania Original, Sunrise and Nations ESWC share one dedicated server binary
  with GBXRemote 2 remote control: `tmsunrise` adapter, verified against Sunrise eXtreme and
  Nations ESWC (status without login, own restart method); Original not tested separately.

### Phase 4 – Unreal Tournament (high priority, v0.4)

- **4.1** ✔ **Measure first:** WebAdmin forms and authentication on real UT99, UT2004 and UT3
  servers; facts in `games/unreal-tournament.md`, reproduced by the simulators.
- **4.2** ✔ UWeb transport (HTTP Basic auth, tolerant HTML form parsing, session cookie kept)
  with simulators.
- **4.3** ✔ UT99 and UT2004 adapters: status from the GameSpy query, map list per game type
  from the WebAdmin, change of map and game type together, level reload; verified against
  real servers.
- **4.4** ✔ UT3 adapter (status from the WebAdmin, as UT3's LAN beacon cannot be used;
  verified against a real server). UT2003 was dropped by decision (2026-10-14).
- **4.5** ✔ Image source Unreal Archive: `scripts/build_unreal_index.py` builds the shipped
  index from the archive's public-domain metadata; images are fetched on demand.

### Phase 5 – Player management and console (v0.5)

- **5.1** ✔ Player list, kick and ban for the three priority families: CS 1.6, CS:Source and
  CS:GO over RCON (`status`, kick by user id, ban by IP address), TrackMania Forever and
  Sunrise over XML-RPC (by login), UT99, UT2004 and UT3 over the WebAdmin. Kick for
  operators, ban for administrators; both audited. Players the game cannot remove (such as
  bots on UT99 and UT3) are listed without buttons. Not CS2 (see the current status).
- **5.2** ✔ Raw console for `admin` only, audited, switched off unless `enable_console` is
  set: RCON games (all four Counter-Strike adapters) and the three UT WebAdmins.

### Phase 6 – On-demand LAN scan (v0.6)

- **6.1** ✔ Adapter method `discover()` without passwords, limited to the configured networks
  (`[scan] networks`): A2S_INFO broadcast for the four Counter-Strike games (told apart by
  app id, game and version), the UDP session query of the in-game LAN browser for
  TrackMania Forever and Sunrise (each with its own checksum key), the LAN beacons of UT99, UT2004 and UT3 (the last
  from UDP 14001); measured against real servers.
- **6.2** ✔ Scan page for administrators and `lanpilot scan`: result list with name, game,
  game type and address, "add" opens the server form pre-filled; credentials are entered
  there. One scan at a time, audited, no background scanning.

### Phase 7 – More games (v0.7+)

- **7.1** Quake 3 RCON family: Call of Duty 1/4/5, Quake 3, Jedi Knight (map list from
  catalogue and rotation).
- **7.2** Battlefield 2, Renegade X, ElDewrito, each after verifying the protocol.
- **7.3** Monitoring only: Age of Empires 1/2, Alien vs Predator 2, C&C Generals, F.E.A.R. 2,
  FlatOut 2, Halo 1, Serious Sam, Stronghold, Supreme Commander, Toxikk, Warcraft III.

### Phase 8 – Hardening and 1.0

- **8.1** ✔ Security review (web and authentication, protocols, packaging and operation),
  threat model and residual risks in [security.md](security.md); every finding fixed.
- **8.2** Package upgrade tests.
- **8.3** German user interface in addition to English, documentation complete, release 1.0.

## Open points

- CS2: the test server hung twice while the workshop map `bhop_water` was loaded; the
  cause is probably that map. The idle limit of RCON connections is unmeasured.
- CS:GO workshop maps (`workshop/<id>/<name>`) are not supported yet.
- LAN scan: not measured against CS:Source, CS:GO, TrackMania Original and Nations ESWC
  servers, nor against a UT3 server on another host.
- UT3: the status needs the WebAdmin password; its LAN beacon (UDP 14001) only answers
  queries sent from that port.
- Unreal Tournament stock maps have no online image source (Unreal Archive holds community
  maps); they need uploaded images.
- TrackMania Original: covered by `tmsunrise` (same server binary), not tested separately.
- Player management with real players: kick and ban have not been run against a server
  with human players (none of the test servers had any). To check at the next LAN party,
  per game: the player list, a kick, a ban and that the ban holds after a reconnect.
- CS2 player management: needs the `status` format of a CS2 server with players.
- FastAPI 0.153 deprecates the `root_path` argument LANPilot passes for sub-path setups; it
  still works, the warning shows in the tests.
- GameTracker and GameBanana: terms of use not confirmed; these image sources are therefore
  low priority and can be disabled individually.
