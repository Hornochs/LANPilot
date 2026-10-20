# Changelog

All notable changes to this project are documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Project foundation: packaging metadata, lint/type/test tooling, contributor documentation,
  architecture and roadmap documents, CI and security workflows.
- Configuration from a TOML file and `LANPILOT_*` environment variables, including
  `server.public_url`, `server.allowed_hosts` and `server.trusted_proxies`.
- SQLite database with migrations that ship in the package; an existing database is backed up
  before its schema is changed.
- Commands `lanpilot serve`, `lanpilot db upgrade`, `lanpilot db current` and
  `lanpilot config show`; health endpoint `/healthz`.
- Accounts with the roles viewer, operator and admin; sign-in with throttling, server-side
  sessions, password change, and user management for administrators.
- Commands `lanpilot user create`, `lanpilot user set-password` and `lanpilot user list`.
- Request checks: `Host` header validation, origin check and CSRF tokens for state-changing
  requests, security headers including a strict Content-Security-Policy.
- Audit log of sign-ins and account changes.
- Adapter framework for game support: capabilities, connection schemas, discovery of adapters
  through the `lanpilot.adapters` entry point group, contract checks for adapter authors, and
  a simulated demo server (`enable_demo_adapter`).
- Server setup for administrators: add, edit and delete game servers through a form that is
  generated from the game's connection settings, and test the connection.
- Live status of every server (map, players, load), a page per server with map change,
  match restart and level reload for operators, and an audit log view for administrators.
- Dashboard with one card per server that updates itself without reloading, and a
  searchable map picker.
- Map preview images: administrators upload an image per map; it is scaled down, stripped
  of metadata and shown on the dashboard and the server page.
- Requests larger than 11 MB are refused.
- Packages for Debian, Ubuntu, Fedora, Rocky Linux and Arch that contain the complete Python
  environment and install without internet access; they create the service user and the
  encryption key and keep key, configuration and data across upgrades and removal.
- **Counter-Strike 1.6** support: status, map list, map change, round restart, level reload
  and server load (CPU, FPS) over RCON.
- **Counter-Strike: Source, CS:GO and Counter-Strike 2** support: status, map list (on CS2
  including downloaded workshop maps), map change, match restart and level reload over
  Source RCON; server load on CS:Source and CS:GO.
- Map images from the internet: LANPilot looks for a picture of each server's current map in
  the Steam Workshop, on GameBanana and on GameTracker, remembers maps without one, and can
  fetch all missing images in advance (button on the map image page, or
  `lanpilot images prefetch`). New configuration section `[images]`.
- Display names for the stock maps of all Counter-Strike games ("Dust II" for `de_dust2`).
- **TrackMania Nations/United Forever** support over GBXRemote: status (name, track, players,
  game mode, join password yes/no), track list, track change, round restart and track
  reload. Track images from TrackMania Exchange. Administrators can add track files that
  are on the server but not in its playlist; LANPilot checks them with the server and puts
  them into the playlist when an operator changes to one.
- **TrackMania Original, Sunrise (eXtreme) and Nations ESWC** support with the same
  features (one adapter, as these games share one server); their status needs no password.
- **Unreal Tournament 99, 2004 and 3** support over the WebAdmin: status (UT99/UT2004 from the
  GameSpy query, without password), map list per game type, change of map and game type
  together, and level reload. The map picker groups maps by game type.
- Map images for Unreal Tournament from Unreal Archive, through an index of the archive's
  public-domain metadata that ships with LANPilot (`scripts/build_unreal_index.py`).
- **Player management**: the server page lists the players (name, score, ping, team, bots
  marked) and refreshes the list live. Operators can kick a player, administrators can ban
  one (with an optional reason); both are recorded in the audit log. Supported for
  Counter-Strike 1.6, CS:Source and CS:GO (ban by IP address), TrackMania Forever and
  Sunrise, and Unreal Tournament 99, 2004 and 3; not yet for CS2.
- **Console** for administrators: send a raw command to a game server and see its answer
  (all Counter-Strike games and Unreal Tournament 99, 2004 and 3). Switched off unless
  `enable_console` is set; every command is recorded in the audit log.
- **LAN scan** for administrators ("Scan the network", `lanpilot scan`): looks once for
  Counter-Strike, TrackMania and Unreal Tournament servers in the networks of the new
  `[scan]` setting, without passwords, lists name, game, game type and address, and opens the
  server form pre-filled for a found server. Every scan is recorded in the audit log.
- **German user interface** in addition to English. Each user picks the language on the
  account page; otherwise the browser's preference decides, else the new setting
  `default_language`.

### Changed

- Map images belong to one game instead of a family of games: CS 1.6, CS:Source, CS:GO and
  CS2 each have their own picture of `de_dust2`. Existing images whose game cannot be told
  (uploaded Counter-Strike images in particular) are dropped by the database upgrade and have
  to be uploaded again; online images are found again by themselves.
- Documentation for installation, configuration, reverse proxies and writing a game adapter,
  a checklist for organisers of LAN parties (`docs/lan-party-guide.md`) and the threat model
  (`docs/security.md`).
- Server-sent events that tell open pages when a server's state changed.
- Encryption of game server credentials with a key outside the database
  (`secret_key` / `secret_key_file`, `lanpilot secret generate-key`); known secrets are
  redacted from the log.

### Security

- Security review with a threat model (`docs/security.md`). Fixed with it: the Counter-Strike
  status query uses an own A2S client that refuses compressed and split answers (the library
  used before inflated them without a limit, so one forged datagram could exhaust memory);
  the login throttle can no longer be bypassed with parallel requests, and password hashing
  runs at most four at a time; failed logins in the audit log are bounded; UT99 player pages
  with forged rows are refused; WebAdmin pages, Source RCON answers and GameSpy queries are
  bounded in size and time; LAN scan answers and follow-up queries are bounded per host; map
  images are looked up only for maps a server lists; console commands that set passwords
  are audited without their arguments; the current-password check of a password change is
  throttled; at most eight live-update streams per session.
- The packages install their Python dependencies from a hash-pinned lock file
  (`packaging/requirements.txt`); the installation guide explains how to verify a release's
  build attestation.
- The key file is written atomically; at most five database backups are kept; the
  `lanpilot` command run by root switches to the service user with a clean environment; a
  server form pre-filled from a link asks to check the host before a password is entered.
