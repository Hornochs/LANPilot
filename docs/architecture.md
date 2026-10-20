# Architecture

This document describes how LANPilot is built. It is the reference to read before touching
the code, and it is updated in every change that alters structure, interfaces or the data
model.

> **Implementation state:** phases 0–2 and 4–6 are complete, phase 3 (Trackmania) except
> TM2/TM2020 (see [ROADMAP.md](ROADMAP.md)). Sections about later phases are still the **target design**;
> each section carries a state marker.

## Overview

```
 Browser ──HTTPS──▶ Reverse proxy ──HTTP──▶ LANPilot (FastAPI, one process)
                                              │
              ┌───────────────┬───────────────┼────────────────┬──────────────┐
              ▼               ▼               ▼                ▼              ▼
           web/ + auth/    core/actions    core/poller      maps/images     db/ (SQLite)
           (pages, SSE)    (authorise,     (status loop,    (provider
                            validate,       event bus)       chain, cache)
                            audit)
                               │               │
                               ▼               ▼
                         games/ adapters (one per game)
                               │
                               ▼
                         core/transports (RCON, GBXRemote, UWeb …)
                               │
                               ▼
                         Game servers on the LAN
```

LANPilot is **protocol-only**: it reaches game servers solely through their remote protocols.
There is no SSH access and no agent on the game hosts.

## Technology

_State: decided; web, database and configuration parts in use since step 1.1._

| Area | Choice | Reason |
|---|---|---|
| Language | Python ≥ 3.11 | existing protocol knowledge (opengsq), easy adapter authoring |
| Web | FastAPI + Uvicorn | async I/O fits UDP/TCP game protocols |
| Database | SQLite (WAL) via SQLAlchemy 2 async + Alembic | one process, few users, few writes |
| Frontend | Jinja2 + HTMX (with its SSE extension) + Tom Select, vendored | no Node build |
| Live updates | Server-sent events | one-way status stream is enough |
| Status queries | `opengsq` | MIT, covers nearly all games |
| Crypto | `argon2-cffi`, `cryptography` | password hashing, credential encryption |
| HTTP client | `aiohttp` (already required by `opengsq`) | image fetching; WebAdmin transport later |
| Images | `Pillow` | re-encoding of fetched and uploaded images |

GPL-licensed libraries are not used (see [CONTRIBUTING.md](../CONTRIBUTING.md)).

## Package layout

_State: files marked ✔ exist; the rest is planned._

```
src/lanpilot/
  cli.py ✔      argparse commands: serve, db, user, config
  config.py ✔   Settings model and loader (defaults < TOML < environment)
  app.py ✔      create_app(settings): middleware, error handlers, routers, static mount
  audit.py ✔    record(): add an audit entry to the caller's transaction
  web/
    health.py ✔              /healthz
    middleware.py ✔          HostCheck, OriginCheck, BodyLimit, SecurityHeaders (plain ASGI)
    deps.py ✔                DbSession, OptionalAuth, RequireAuth, RequireAdmin,
                             require_role(), csrf_protect
    templating.py ✔          Jinja2 environment, render(), fixed notice texts
    routes_auth.py ✔         /login, /logout
    routes_account.py ✔      / (start page), /account, /account/password
    routes_admin_users.py ✔  /admin/users…
    routes_admin_servers.py ✔  /admin/servers…: list, add, edit, delete, test connection
    routes_admin_audit.py ✔    /admin/audit
    routes_admin_scan.py ✔     /admin/scan: the on-demand LAN scan
    routes_servers.py ✔        /servers/{id} and its actions
    routes_events.py ✔         /events (server-sent events)
    routes_map_images.py ✔     /map-images/<file>, /admin/servers/{id}/maps
    templates/ ✔  pages plus the fragments _card, _grid, _server_live, _status
    static/ ✔     css/app.css, js/app.js, vendor/ (htmx, htmx-ext-sse, tom-select)
  core/
    adapters/base.py ✔      GameAdapter, Capability, data types, errors, validation
    adapters/schema.py ✔    connection schema -> form fields, public/secret split
    adapters/registry.py ✔  entry point discovery
    servers.py ✔            create/update/delete servers, credential storage, probe()
    runtime.py ✔            ServerManager: adapter instances, polling, serialised calls
    actions.py ✔            Actions: the only path that changes a game server; audit
    maps.py ✔               stored map lists (names filled in from the catalogue)
    catalog.py ✔            stock map catalogue: display names per game
    map_images.py ✔         image processing, cache directory, MapImageIndex
    image_sources.py ✔      Fetcher (the only outbound HTTP), Steam Workshop, GameBanana,
                            GameTracker
    image_lookup.py ✔       ImageLookup: source chain, remembered misses, background queue
    discovery.py ✔          helpers for discover(): broadcast, follow-up queries, bounds
    scan.py ✔               Scanner: one LAN scan on request, over every DISCOVER adapter
    events.py ✔             EventBus for live updates
    secrets.py ✔            key loading, SecretBox (AES-256-GCM)
    redaction.py ✔          keeps known secrets out of the log
  games/demo.py ✔           simulated server
  testing/contract.py ✔     check_adapter() for adapter authors
  testing/goldsrc.py ✔      simulator of a GoldSrc server (status query and RCON)
  testing/source.py ✔       simulator of CS:Source, CS:GO and CS2 servers (query, Source RCON)
  testing/gbxremote.py ✔    simulator of TrackMania Forever and Sunrise servers (GBXRemote 2)
  testing/uweb.py ✔         simulators of the UT99/UT2004 WebAdmin + GameSpy query, and UT3
  testing/lan.py ✔          simulators of the LAN discovery answers of Trackmania and Unreal
  auth/
    roles.py ✔      Role enum with ordering (viewer < operator < admin)
    passwords.py ✔  Argon2id hashing, policy, dummy verification
    users.py ✔      create, authenticate, set_password/role/active, delete; last-admin rule
    sessions.py ✔   create, load, end, purge; cookie naming
    throttle.py ✔   in-memory login throttle
  db/
    base.py ✔      declarative Base, naming convention, UTCDateTime column type
    models.py ✔    ORM models
    engine.py ✔    async engine with SQLite pragmas, session factory
    migrate.py ✔   upgrade(), current_revision(), head_revision(), pre-migration backup
    migrations/ ✔  Alembic environment and versions/ (shipped inside the package)
    transports/goldsrc_rcon.py ✔  RCON over UDP for GoldSrc servers
    transports/source_rcon.py ✔   RCON over TCP for Source and Source 2 servers
    transports/gbxremote.py ✔     GBXRemote 2 (XML-RPC over TCP) for Trackmania
    transports/uweb.py ✔          WebAdmin of Unreal servers: HTTP Basic auth, form parsing
    transports/ (planned)        q3_rcon
  games/counterstrike/common.py ✔   status query, map name pattern, stats parser
  games/counterstrike/goldsrc.py ✔  Counter-Strike 1.6
  games/counterstrike/source.py ✔   CS:Source, CS:GO, CS2 (one base class, three adapters)
  games/trackmania/common.py ✔      ChallengeAdapter: what the "challenge" generation shares
  games/trackmania/forever.py ✔     TrackMania Nations/United Forever
  games/trackmania/sunrise.py ✔     TrackMania Original, Sunrise (eXtreme), Nations ESWC
  games/trackmania/discovery.py ✔   finding Forever and Sunrise (UDP session query)
  games/unreal/common.py ✔          GameSpy status, map name, game class and URL option rules
  games/unreal/webadmin.py ✔        WebAdminAdapter: what UT99 and UT2004 share
  games/unreal/ut2004.py ✔, ut99.py ✔   Unreal Tournament 2004 and UT99
  games/unreal/ut3.py ✔             Unreal Tournament 3 (status and control over its WebAdmin)
  games/unreal/discovery.py ✔       LAN beacons of UT99, UT2004 and UT3
  games/ (planned)  quake3/, monitor_only
  catalog/ ✔        <family>.toml: stock map names per adapter id; unreal-archive.json.gz:
                    Unreal Archive image index (package data)
  testing/ (planned) simulators for the other protocols
```

Dependency direction: `web` → `core` → `games` → `core/transports`. Transports know nothing
about games; adapters know nothing about HTTP or the database.

## Adapter interface

_State: implemented (step 1.3) in `core/adapters/`. Adapter authors import everything from
`lanpilot.core.adapters`._

Every game is a class deriving from `GameAdapter` (`base.py`). LANPilot creates one instance
per configured server and keeps it, so an adapter may hold a connection open; `close()`
releases it.

| Class attribute | Meaning |
|---|---|
| `id` | stable identifier stored with every server (`[a-z][a-z0-9_]{1,31}`) |
| `name` | display name, e.g. "Counter-Strike 1.6" |
| `family` | group of related games, e.g. `counterstrike`; names the catalogue file (images are per game, not per family) |
| `capabilities` | `frozenset[Capability]`; the user interface shows controls only for these |
| `connection_schema` | subclass of `ConnectionSettings` describing how to reach the server |

| Capability | Method |
|---|---|
| `STATUS` (mandatory) | `query() -> ServerStatus` |
| `MAP_LIST` | `list_maps() -> Sequence[MapRef]` |
| `MAP_CHANGE` | `change_map(target: MapRef)` |
| `RESTART_SOFT`, `RESTART_HARD` | `restart(mode: RestartMode)` |
| `PLAYERS` | `list_players() -> Sequence[Player]` |
| `KICK`, `BAN` | `kick(player, reason)`, `ban(player, reason)` |
| `CONSOLE` | `console(command) -> str` |
| `MANUAL_MAPS` | `look_up_map(key) -> MapRef`: confirm a map the server has but does not list |
| `LOAD_STATS` | no method: promises that `query()` fills `ServerStatus.load` |
| `DISCOVER` | classmethod `discover(networks, wait) -> Sequence[DiscoveredServer]` |

Methods of undeclared capabilities raise `UnsupportedError` (the base class does this).
`validate_adapter_class()` runs when an adapter is registered and rejects classes whose
declared capabilities and implemented methods disagree in either direction, so
`capabilities` can be trusted.

**Data types** (frozen dataclasses): `MapRef(key, name, game_type, extra)` where `key` is
what is sent back to the server and `extra` carries image lookup ids such as `uid` or
`workshop_id`; `Player(id, name, score, ping, team, bot, can_kick, can_ban)` where `id` is
what `kick()`/`ban()` need and `can_kick`/`can_ban` are false for entries the protocol cannot
remove (bots on UT99 and UT3, bots and players without a reported address for an IP ban);
`ServerStatus(name, players, max_players, map, game_type, password_protected, load)`;
`DiscoveredServer(adapter_id, host, port, name, game_type, values)` where `port` is `None`
when the scan cannot learn the remote-control port and `values` pre-fills further public
connection fields (a query port).

**Errors** (all derive from `AdapterError`; messages are shown to operators and must not
contain secrets): `ServerUnreachableError`, `AuthenticationError`, `CommandRejectedError`,
`ProtocolError`, `UnsupportedError`.

**Connection schema** (`schema.py`): a Pydantic model. `ConnectionSettings` provides `host`
(validated: host name or IP, no scheme, path or whitespace) and `port`. Supported field
types and the form control they become: `str` → text, `int` → number (with `ge`/`le` as
min/max), `bool` → checkbox, `Literal[...]` of strings → select, `SecretStr` → password.
`title` and `description` become label and help text. `describe_fields()` produces the form
fields, so a new adapter needs **no UI code**; `split_values()` separates public values from
secrets; `describe_errors()` turns validation errors into per-field messages without echoing
input. Defaults of secret fields never reach the form.

**Registry** (`registry.py`): adapters are discovered through the entry point group
`lanpilot.adapters`, bundled ones included (see `pyproject.toml`), so third parties ship
adapters as separate pip packages. A plugin that fails to load is logged and skipped;
duplicate ids are rejected. `app.state.registry` holds the registry.

**Contract** (`lanpilot.testing.contract.check_adapter`): exercises exactly the capabilities
an adapter declares against a simulator and checks that everything else is refused. Every
adapter's tests call it.

**Demo adapter** (`games/demo.py`, id `demo`): a simulated server with every capability
except ban and discover, and switchable behaviour (`normal`, `slow`, `offline`; wrong
password → `AuthenticationError`). It is offered in the user interface only when
`enable_demo_adapter` is set.

Games without remote control use a generic adapter that only has `STATUS` (planned, 7.3).

### Restart semantics

| Mode | Meaning | Examples |
|---|---|---|
| Soft | restart the round or match, players stay connected | `sv_restart 1`, `mp_restartgame 1`, `RestartChallenge` |
| Hard | reload the level completely | `restart`, `changelevel <current>`, WebAdmin restart |

Terminating the server process ("quit") is deliberately not offered: LANPilot cannot start
processes, and a stopped server helps nobody.

### Map lists

Maps are stored per server in `server_maps` with a source marker:

- `protocol` – reported by the server (`maps *`, `GetChallengeList`, WebAdmin dropdown),
- `catalog` – stock maps bundled with LANPilot for games that cannot list maps,
- `manual` – added by an administrator (games with `MANUAL_MAPS`).

The list is refreshed on demand and cached, so the searchable dropdown is instant. A map
change is only accepted for a map that is in this list.

**Maps added by hand** (implemented with step 3.2). On the "Maps of …" page
(`/admin/servers/{id}/maps`) an administrator enters a map key; `Actions.add_map()` (role
admin, audited as `server.add_map`) has the adapter confirm it with `look_up_map()` and
stores what the server answered (`maps.add_manual()`, source `manual`). A refresh of the
reported maps replaces only `protocol` rows and skips reported maps that exist as `manual`
rows, so an added map stays one entry, also once the server lists it. Removing an entry
(`server.remove_map`) only touches LANPilot's list, never the server's. `change_map()` must be
able to switch to every map `look_up_map()` returned.

## Protocols

_State: GoldSrc RCON and the Counter-Strike 1.6 adapter are implemented and verified against
a real server (steps 2.1, 2.2). Source RCON and the CS:Source, CS:GO and CS2 adapters are
implemented and verified against real servers (step 2.3). GBXRemote and the TrackMania
Forever adapter are implemented and verified against a TMNF server (steps 3.1, 3.2). The rest
is planned (phases 3–4, 7). Player lists, kick, ban and the console (phase 5) are implemented
for all of them except player management on CS2; see the game documents for what was
measured._

| Protocol | Games | Implementation | State |
|---|---|---|---|
| Status query | all | `opengsq` | used by `cs16`, `css`, `csgo`, `cs2` |
| GoldSrc RCON (UDP) | Counter-Strike 1.6 | `core/transports/goldsrc_rcon.py` | ✔ |
| Source RCON (TCP) | CS:Source, CS:GO, CS2 | `core/transports/source_rcon.py` | ✔ |
| GBXRemote / XML-RPC (TCP 5000) | Trackmania | `core/transports/gbxremote.py`, encoding via `xmlrpc.client` | ✔ (TMNF measured) |
| UWeb WebAdmin (HTTP) | UT99, UT2004, UT3 | `core/transports/uweb.py` (aiohttp, `html.parser`) | ✔ |
| GameSpy query (UDP) | UT99, UT2004 | `opengsq` `GameSpy1` | used by `ut99`, `ut2004` |
| Quake 3 RCON (UDP) | Call of Duty 1/4/5, Quake 3, Jedi Knight | own transport | planned |

**Layers.** A *transport* (`core/transports/`) speaks one protocol and knows nothing about
games; it turns protocol failures into the adapter errors. An *adapter* (`games/`) knows the
game's commands and parses their output. A *simulator* (`testing/`) reproduces the protocol
as a real server was observed to speak it, and the adapter's tests run against it. What was
observed is written down in `games/<game>.md` first.

**Patterns set by the first real adapter** (`games/counterstrike/goldsrc.py`), to be followed
by the next ones:

- Status comes from the password-less server query wherever the game has one, so a wrong
  remote-control password shows up as a control problem, not as "offline".
- A transport that was refused its password does not send it again: game servers ban
  addresses that keep trying.
- Everything that goes into a command is checked against a strict pattern in the adapter,
  directly before sending.
- Expensive or log-spamming extras (here: `stats`) are fetched at a slower pace than the
  status, inside `query()`, and never make the status fail.

**Source RCON** (`core/transports/source_rcon.py`). `opengsq` has a Source RCON client, but
it waits for a packet with the end marker's id, and CS2 puts that id on the command's output
too (all answers to packets that arrived together carry the last id), so it returns empty
text on CS2. The own transport sends the command and an empty end marker together and
collects every answer carrying either id until the empty answer with the marker's id that
follows at least one other answer; packets with other ids are skipped. Lines of the server
log, which CS:GO adds to every answer (the marker's included), are ignored for that check
and removed from the text. It keeps one TCP connection per adapter, logs in on first use,
opens a new connection after 60 s without a command and after any error; when a kept
connection turns out to be closed before any answer arrived (CS:GO closes them on every map
change), it sends the command once more on a new connection. It bounds packet size (64 KiB), answer size (1 MiB) and command
length. Adapters hold the transport and release it in `close()`.

**GBXRemote** (`core/transports/gbxremote.py`). One TCP connection per `GbxRemote`; on
connect it checks the greeting `GBXRemote 2` and calls `Authenticate`, unless it was created
without a login (Sunrise answers its status methods without one; TrackMania Forever answers
nothing without a login, so its status needs the password too). Calls carry handles from
`0x80000001`; frames with other handles (callbacks, abandoned calls) are skipped, at most 100
in a row. Answers are limited to 8 MB before `xmlrpc.client` parses them. Faults become
`GbxFaultError` (a `CommandRejectedError` with the server's text, cut to 200 printable
characters) and leave the connection usable; "Password incorrect." becomes an
`AuthenticationError` and stops further logins. `multicall()` batches calls through
`system.multicall`. A kept connection found closed before any answer is opened again and the
call repeated once, as for Source RCON.

**Trackmania, challenge generation** (`games/trackmania/common.py`). `ChallengeAdapter`
holds everything; `tmforever` (`forever.py`) and `tmsunrise` (`sunrise.py`) set
`status_needs_login` (Sunrise reads the status on a second connection without login, so a
wrong password does not look like "offline"), `soft_restart_method` (`RestartChallenge`,
`ChallengeRestart`), `check_method` (Forever only) and `image_lookup`. The map key is the
track's `FileName`; the display name has its formatting codes removed (`plain()`); `UId` and
environment go into `MapRef.extra`. A change is `ChooseNextChallenge` + `NextChallenge`
(which reorders the playlist; there is no other way), for a track added by hand preceded
by `InsertChallenge`; the soft restart `RestartChallenge`,
the hard restart a change to the current track. "Track unknown.", "Change in progress." and
"Permission denied." become operator messages. Players come from `GetPlayerList` (Forever
asks for struct version 1) with scores from `GetCurrentRanking`; kick and ban are `Kick` and
`Ban` by login (Forever passes the reason as the player's message, Sunrise takes none), and
"Login unknown." means the player has left. A ban lasts until the server restarts.

**UWeb** (`core/transports/uweb.py`). `UWebAdmin` sends every request with HTTP Basic
authentication (ISO-8859-1), never follows redirects, limits pages to 2 MB and keeps the
session cookie (UT3 opens a day-long session for every request without it). 401 becomes an
`AuthenticationError` and stops further requests; adapters call `refuse()` when a WebAdmin
answers with its login form instead (UT3). `parse()` turns a page into a `Page`: selects
(by name, else id) with their options, inputs, checked checkboxes and radio buttons,
definition lists, and tables (rows of cells with their text and inputs, a `colspan` cell
followed by empty cells so rows line up with the header). `table(page, *headers)` finds a
table by its header texts; `text_lines(html, element_id)` gives the text of a page or of one
element, with `<br>` as line breaks. Nothing of the page is ever executed or rendered.

**Unreal Tournament** (`games/unreal/`). Maps belong to game types: the map list holds every
installed map per game type the WebAdmin offers, with `game_type` = the game type's name and
`extra["game_class"]` = its class; layouts (`?LinkSetup=`) are the only URL options accepted
from a list. A change loads map and game type together; only the hard restart exists.
`WebAdminAdapter` (UT99, UT2004) reads the status from the GameSpy query and differs per game
in how it travels (`server_restart` form vs. `SwitchGameTypeAndMap`) and reloads; `ut3` reads
status, lists and changes through Epic's UT3 WebAdmin. After a map list was loaded, the
runtime polls once more at once, because adapters learn game type names from it.
Players: UT99 and UT2004 read the table on `current_players`, where each row has a kick and a
ban checkbox named after the player id, and remove a player by posting that page with the box
ticked; only boxes *before* the name cell are used, because UT99 does not escape names. UT3
lists everyone from the status page's scoreboard (with a bot flag) and the human players with
their key from `current/players`, where a kick or IP ban is a form post with that key. A kick
is checked against the page the server answers with. Console: UT99 and UT2004 post the
command to `current_console`; the answer lands in the WebAdmin's message log
(`current_console_log`), and the adapter returns the lines the log gained (`new_lines()`,
which copes with the log being a window over the last messages). UT3's `/console` answers on
the same page.

**Counter-Strike on Source** (`games/counterstrike/source.py`). `SourceCounterStrikeAdapter`
holds everything; `css`, `csgo` and `cs2` are small subclasses that set `id`, `name`,
capabilities (`LOAD_STATS` only for Source 1, CS2 prints nothing for `stats`) and the
`maps *` format (`PENDING:   (fs) <name>` on Source 1, bare names on CS2). CS2 adds workshop maps from `ds_workshop_listmaps` as `MapRef`s with
`extra = {"workshop": "1"}` (they win over the same name from `maps *`); `change_map()` sends
`ds_workshop_changelevel` for them. A change that CS2 refuses because it is still loading
a map raises `CommandRejectedError`. A hard
restart is `changelevel` to the current map, because `restart` is a cheat command on CS2.
`common.py` holds what all Counter-Strike adapters share, including the Counter-Strike 1.6
one, among it `RconPlayerControl`: players from `status` (`parse_status()` per engine), kick
by user id (`kick #<id>` on GoldSrc, `kickid <id>` on Source), ban by IP address (`addip 0
<ip>`, the kick, `writeip`) because LAN servers give every player the same Steam ID. A kick
message is reduced to letters, digits and a few punctuation marks (`safe_message()`) before it
becomes part of the command; user ids must be ASCII digits. `css` and `csgo` mix it in
(`Source1PlayerControl`); `cs2` does not, its `status` format is unmeasured. All four have
the console: the command goes to RCON as it is.

Research results for the games not yet implemented, including what is unverified, are in
[research/remote-control-protocols.md](research/remote-control-protocols.md).

## LAN scan

_State: implemented (phase 6) in `core/discovery.py`, `core/scan.py`,
`web/routes_admin_scan.py` and the adapters' `discover()`._

**Discovery** is a classmethod of an adapter with `DISCOVER`: given the configured networks
and how long to wait for broadcast answers, it finds servers of its game without a password
and returns `DiscoveredServer`s. `core/discovery.py` holds what does not depend on a game:
`broadcast()` sends datagrams to the directed broadcast address of every network (a /32
network is its own address, which the tests use with 127.0.0.1) from one socket, optionally
bound to a fixed and shared source port, and keeps at most 1024 answers, only from addresses
inside the networks; `gather_found()` runs follow-up queries and drops failures; `clean_text()` reduces
server-supplied names to 64 printable characters. Packets and parsers come from opengsq.

| Adapters | How they are found |
|---|---|
| `cs16`, `css`, `csgo`, `cs2` | A2S_INFO broadcast, then `Source.get_info()` per answering address; `classify()` picks the game; one shared broadcast per scan (`find_servers()` joins concurrent calls) |
| `tmforever` | UDP session query (`TmForever`, Forever's checksum key) to 2350–2353, then the TCP info on the announced port |
| `tmsunrise` | the same per game id of Original, Sunrise and Nations ESWC, with Sunrise's key |
| `ut99` | `REPORTQUERY` to 8777–8786, then GameSpy on the announced query port |
| `ut2004` | LAN query to 10777, then the Unreal 2 details; GameSpy port = game port + 10 |
| `ut3` | UDK LAN beacon from UDP 14001 (`SO_REUSEADDR`) |

**Scanner** (`core/scan.py`, `app.state.scanner`). `Scanner.run()` calls every registered
adapter with `DISCOVER` (not the demo) concurrently, each with a limit of `timeout_seconds` +
15 s; one that fails or runs out of time is listed in `failed` and never fails the scan. Finds
are kept only if they name the adapter that reported them and lie inside the networks, are
deduplicated by adapter, host and port, capped at 512, sorted by game and address, and marked
`configured` when a server of that game with that host exists. One scan runs at a time;
another request gets `ScanBusyError`. Nothing is stored and nothing runs in the background.

**Page** (`/admin/scan`, administrators): the networks and the games a scan can find, a
button (`POST`, CSRF-protected, audited as `scan.run` with networks and counts) that runs the
scan while the request waits, and the result table. "Add" links to
`/admin/servers/new?adapter=…&name=…&c_host=…&c_port=…&c_<field>=…`; `new_server()` takes
only text, number and select fields of the schema from the query string (printable, at most
253 characters), never password fields, and the form is validated as usual on submit.
`lanpilot scan [--network CIDR]` prints the same result.

## Configuration and reverse proxy

_State: loading and validation implemented (step 1.1, `config.py`); trusted proxies are
passed to Uvicorn by `lanpilot serve`. The host, origin and cookie behaviour below lands in
step 1.2._

Precedence, lowest to highest: built-in defaults, the TOML file, environment variables
`LANPILOT_<SECTION>__<KEY>` (`LANPILOT_<KEY>` for top-level settings). The file is taken from
`--config`, else `$LANPILOT_CONFIG`, else `/etc/lanpilot/lanpilot.toml` if it exists; without
a file the environment alone must be sufficient. Empty environment values are ignored. Unknown
keys are rejected, and error messages never echo the offending value because it may be a
secret. The loader is a small function on top of Pydantic models; `pydantic-settings` is
deliberately not used, so list values can be plain comma-separated strings.

Top-level settings:

| Key | Meaning | Default |
|---|---|---|
| `data_dir` | directory for the SQLite database and the image cache | `/var/lib/lanpilot` |
| `secret_key` | key for encrypting server credentials: 32 random bytes, URL-safe base64 | unset |
| `secret_key_file` | file containing that key; must not be readable by group or others | unset |
| `log_level` | `debug`, `info`, `warning` or `error` | `info` |
| `auto_migrate` | upgrade the database schema when `lanpilot serve` starts | `true` |
| `enable_demo_adapter` | offer the simulated demo server | `false` |

Exactly one of `secret_key` and `secret_key_file` must be set; `lanpilot serve` refuses to
start otherwise, before it touches the database.

Section `[server]`:

| Key | Meaning | Default |
|---|---|---|
| `bind`, `port` | address and port of the Uvicorn process | `127.0.0.1`, `9030` |
| `public_url` | address under which users reach LANPilot | required |
| `allowed_hosts` | additional accepted host names or IPs; wildcards are rejected | empty |
| `trusted_proxies` | IPs/networks whose `X-Forwarded-*` headers are honoured | `127.0.0.1` |

`public_url` is normalised (lower-case host, no trailing slash) and must not contain
credentials, a query or a fragment. `ServerSettings` derives from it: `public_origin`
(compared with the `Origin` header), `public_host` (compared with the `Host` header),
`is_https`, and `root_path` — a path in `public_url` such as `https://host/pilot` is the
sub-path LANPilot is served from, so there is no separate `root_path` setting.

Section `[polling]`:

| Key | Meaning | Default |
|---|---|---|
| `interval_seconds` | how often each server is asked for its status | `10` |
| `timeout_seconds` | how long LANPilot waits for an answer | `8` |

Section `[images]` (step 2.4): `online` (`true`), `sources` (comma list of
`steam_workshop`, `gamebanana`, `gametracker`, in the order they are asked; unknown names
are rejected), `retry_days` (`7`). See "Map images".

Section `[scan]` (phase 6): `networks` (comma list of IPv4 networks,
normalised to the network address; empty, the default, offers no scan) and
`timeout_seconds` (`2`, between 0.5 and 10). See "LAN scan".

Section `[auth]`:

| Key | Meaning | Default |
|---|---|---|
| `session_idle_minutes` | a session ends after this much inactivity | `720` |
| `session_lifetime_hours` | a session ends this long after login, whatever happens | `168` |

Derived behaviour (implemented in `web/middleware.py` and `auth/sessions.py`):

- **Host check** – requests whose `Host` header names neither `public_host` nor an
  `allowed_hosts` entry (ports are ignored) are rejected with 400 and logged (protection
  against host header attacks and DNS rebinding). `/healthz` is exempt so local monitoring
  can call it by IP address.
- **Origin check** – every request with a method other than GET/HEAD/OPTIONS must carry an
  `Origin` (or, if absent, `Referer`) whose authority equals that of `public_url` or the
  request's own `Host` header; the latter covers access through `allowed_hosts`. Otherwise
  403. This protects the login form too, before any session exists.
- **Cookies** – `__Host-lanpilot_session` with `Secure` when `public_url` is `https://`
  (`__Secure-…` with `Path=<root_path>` under a sub-path). With `http://` (LAN without TLS)
  the cookie is `lanpilot_session` without `Secure`, with a warning at start-up and a banner
  on every page. Always `HttpOnly` and `SameSite=Lax`.
- **Redirects and links** are relative paths prefixed with `root_path`; nothing is built from
  the incoming `Host` header.
- **Client IP** for login throttling and the audit log is taken from `X-Forwarded-For` only
  when the request comes from a `trusted_proxies` entry.
- **Live updates** – SSE responses carry `X-Accel-Buffering: no`, so Nginx streams them
  without a dedicated `location` block. Other proxies need response buffering disabled for the
  events path; the reverse proxy documentation (step 1.9) lists the settings.

## Data model

_State: all tables below are implemented (migrations 0001–0007)._

| Table | Content |
|---|---|
| `users` | username (lower-case, unique), Argon2id hash, role, active flag, last login |
| `sessions` | SHA-256 of the session token, user, CSRF token, created/last seen/expires, client IP and user agent |
| `servers` | name (unique), adapter id, host, port, `settings` (JSON of the other non-secret connection values), `credentials` (encrypted secrets or NULL), `enabled` |
| `server_maps` | server (cascade delete), `key`, `name`, `game_type` ("" if none), `source`, `extra` (JSON: track UId, workshop id); unique per server, key and game type |
| `map_images` | `game` (adapter id), `map_key` (lower-case; unique together), `filename` (random, unique), `provider` (`upload` or a source id), `source_url`, `width`, `height` |
| `map_image_misses` | `game`, `map_key` (unique together), `reason` (`not_found`, `removed`), `checked_at` |
| `audit_log` | time, actor id and copied actor name, action, target type and id, detail (JSON), client IP, success flag |
| `app_settings` | key/value settings changed at runtime |

Live server status is held in memory only.

Conventions: datetimes are timezone-aware in Python and stored as naive UTC (`UTCDateTime`);
naive values are rejected. Constraints are named through the metadata naming convention.
Connections set `journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout=5000`.

### Migrations

Alembic migrations live in `src/lanpilot/db/migrations/versions/` and ship inside the wheel,
so an installed LANPilot upgrades its own database. `lanpilot serve` upgrades on start unless
`auto_migrate` is off, in which case it refuses to start with an outdated schema.
`migrate.upgrade()` first copies an existing database to
`lanpilot.db.backup-<timestamp>-<revision>` (mode 600) using SQLite's backup API. The database
file is mode 600, the data directory mode 700. A test compares the models with the migrated
schema, so a model change without a migration fails CI. For development, `alembic.ini` in the
repository root lets `alembic revision --autogenerate` find the migrations.

## Command line

_State: implemented as listed._

| Command | Purpose |
|---|---|
| `lanpilot serve` | migrate if needed, then run Uvicorn on `server.bind`:`server.port` |
| `lanpilot user create NAME [--role R]` | create a user (default role `admin`); the way the first administrator is made |
| `lanpilot user set-password NAME` | set a password and end that user's sessions; the recovery path for a lost admin password |
| `lanpilot user list` | list users with role and state |
| `lanpilot db upgrade` | create or upgrade the database, with backup |
| `lanpilot db current` | show the schema revision; exit code 1 if it is not the newest |
| `lanpilot secret generate-key [--output FILE]` | print a new encryption key, or write it to FILE with mode 600; never overwrites |
| `lanpilot config show` | print the effective configuration with secrets masked |
| `lanpilot images prefetch [--server ID]` | look online for the images of all stored maps that have none; prints what was found |
| `lanpilot scan [--network CIDR]` | look for game servers in `scan.networks` (or the given networks) once and print them |

`--config FILE` selects the configuration file. Configuration errors exit with code 2 and a
one-line message, other command errors with code 1. Password prompts read from the terminal;
`--password-stdin` reads one line from standard input for scripted use. User commands write
audit entries with the actor `(command line)`.

`GET /healthz` answers `{"status": "ok"}` or 503 `{"status": "error"}` after a `SELECT 1`. It
is the only unauthenticated endpoint besides the login page and reveals nothing else. The
interactive API documentation (`/docs`, `/openapi.json`) is disabled.

## Authentication and authorisation

_State: implemented (step 1.2)._

**Request pipeline**, outermost first: `SecurityHeadersMiddleware` → `HostCheckMiddleware` →
`OriginCheckMiddleware` → `BodyLimitMiddleware` → routing → dependencies (`optional_auth` → `require_auth` →
`require_role` → `csrf_protect`) → handler.

- **Login** (`routes_auth.py`): throttle check, then `users.authenticate()`. Unknown and
  disabled users cost the same time as a wrong password (dummy Argon2 verification) and get
  the same message. On success expired sessions are purged, any session the browser already
  had is ended, and a new one is created.
- **Throttle** (`auth/throttle.py`): 5 failures per (user name, client address) pair and 30
  per client address within 15 minutes; further attempts get 429 with `Retry-After`, even
  with the right password. Counters are in memory and bounded.
- **Sessions** (`auth/sessions.py`): the cookie holds a random token, the database only its
  SHA-256 hash. `load()` enforces the absolute and the idle limit and that the user is still
  active; `last_seen_at` is written at most once a minute.
- **Roles**: `viewer < operator < admin`. Routes declare the role they need with
  `require_role()`; `/admin/users` sets it on the router so no route below it can forget.
  The role is read from the user row on every request, so changes apply immediately.
- **CSRF**: `csrf_protect` compares the session's token with the `csrf_token` form field or
  the `X-CSRF-Token` header (for HTMX), in constant time. It is attached to every
  state-changing route; the origin check is the second, independent layer.
- **Not signed in**: GET requests are redirected to `/login?next=…`; other methods and HTMX
  requests get 401. `next` is only honoured if it is a path inside LANPilot (`safe_next`).
- **User management rules** (`auth/users.py`): the last active administrator cannot be
  demoted, disabled or deleted; nobody can delete their own account; changing one's own
  password needs the current one; an administrator resetting someone's password, disabling
  or deleting a user ends that user's sessions; a password change ends all sessions and
  gives the current browser a new one. Service functions raise `UserError` before changing
  anything and never commit; the route adds the audit entry and commits once.
- **Passwords**: 10 to 256 characters, not equal to the user name. Argon2id with the
  library's default parameters; hashes are upgraded on login when the parameters change.
- **Audit**: logins (including failed and throttled ones), logouts and every user change are
  recorded. Entries never contain passwords.

**Templates** are rendered through `templating.render()`, which supplies `auth`, `root`,
`insecure_transport`, `notice` and `version`. Autoescaping is on, undefined variables raise
errors. Messages after a redirect are selected by a `?notice=<key>` parameter from a fixed
table, so no request text is ever reflected. Pages work without JavaScript; the
Content-Security-Policy allows no inline scripts or styles.

## Server management

_State: implemented (step 1.4) in `core/servers.py` and `web/routes_admin_servers.py`._

- Administrators add a server by picking a game; the form is rendered from
  `describe_fields(adapter.connection_schema)` by `templates/admin/server_form.html`.
  Connection fields are named `c_<field>` in the form so they cannot collide with LANPilot's
  own fields (`name`, `enabled`). Fields outside the schema are ignored.
- `ServerInput` carries the raw form values; `create_server()` / `update_server()` validate
  them through the adapter's Pydantic schema and raise `ServerError` with per-field messages
  (never echoing input). A rejected form is re-rendered with the entries, minus passwords.
- Storage: `host` and `port` in their own columns, the remaining public values as JSON in
  `settings`, secrets encrypted in `credentials` (see Security).
- Editing: an empty password field keeps the stored value. If host or port change, stored
  passwords are not carried over and required ones must be entered again. The game of a
  server cannot be changed; delete and re-add instead.
- `load_connection()` rebuilds the validated connection object with decrypted secrets and
  registers them with the log redactor. `probe()` ("Test connection") creates a temporary
  adapter instance, queries the status and, if the game has `MAP_LIST`, asks for the map list
  to prove that the password works; it reports plain sentences and has an 8 s timeout.
- The demo adapter is only offered for new servers when `enable_demo_adapter` is set.
  A server whose adapter is no longer installed is listed as "not installed" and can only
  be deleted.
- Every create, update and delete is audited with name, game and address, never secrets.
- The start page `/` lists the servers for every signed-in user (dashboard follows in 1.6).

## Live state, actions and events

_State: implemented (step 1.5)._

**Runtime** (`core/runtime.py`). `ServerManager` (in `app.state.manager`) owns one
`ServerRuntime` per configured server: the adapter instance, an `asyncio.Lock`, and a poll
task. `ServerState` is the immutable snapshot pages render: identity, capabilities, `online`
(`None` before the first poll), `status`, `error` (why it is offline or unusable),
`control_error` (status works but remote control does not, typically a wrong password) and
timestamps. It never contains credentials. State lives in memory only.

- Each enabled server is polled every `polling.interval_seconds` (default 10) with
  `polling.timeout_seconds` (default 8). While a server is down the interval backs off up to
  six-fold; a little jitter spreads the polls of many servers.
- An adapter bug (unexpected exception) is logged and shown as an internal error; it never
  ends the loop.
- After a server first answers, its map list is fetched once and stored. That is also how a
  wrong password is noticed.
- `manager.reload(server_id)` is called after a server is created, edited or deleted;
  `manager.call(server_id, operation)` runs an operation under the server's lock, so polls
  and actions never interleave on one connection; `poll_now()` refreshes after an action.
- Servers that are disabled, whose adapter is missing or whose credentials cannot be
  decrypted get a runtime without adapter and an explanatory `error`.

**Map lists** (`core/maps.py`, table `server_maps`). The list a server reports is cached, so
the picker is instant and a map change can be validated. Reported entries are untrusted:
keys and names with non-printable characters, overlong values and duplicates are dropped,
and the list is capped at 5000. Rows carry a `source` (`protocol`, later `catalog` and
`manual`); a refresh replaces only the `protocol` rows.

**Actions** (`core/actions.py`, `app.state.actions`). `Actions._perform()` is the only code
path that changes a game server:

1. check the actor's role (again, independent of the route's dependency),
2. check that the server exists, is enabled, is set up correctly and has the capability,
3. run the operation through `manager.call()` with a 15 s limit,
4. write the audit entry, for success and for every refusal or failure,
5. trigger a fresh poll.

Failures become `ActionError(message, status)`; adapter error texts are passed on because
adapters guarantee they contain no secrets. `change_map()` takes the id of a `server_maps`
row and verifies it belongs to that server, so no client-supplied text ever reaches a
console command. Actions use their own database session; a failure can be rolled back
without invalidating the objects the web request holds. `Actor` carries plain values for the
same reason.

Players: `list_players()` reads the list under the server's lock (not audited, as it changes
nothing) and cleans it (`clean_players()`: at most 256 entries, printable names and teams of
at most 64 characters, entries without a usable or unique id dropped). `kick()` (operator,
`KICK`) and `ban()` (administrator, `BAN`) take the player's id and an optional reason; inside
the lock they list the players again and act only on someone who is there now and whose entry
allows it, because ids can be reused. The audit entry records id, name and reason. `console()`
(administrator, `CONSOLE`) refuses with 403 unless `enable_console` is set, accepts one
printable line of at most 500 characters, audits the command (first 200 characters) and
returns at most 64 KB of the answer.

**Routes** (`web/routes_servers.py`): `GET /servers/{id}` (any signed-in user),
`POST /servers/{id}/map`, `/restart`, `/maps/refresh` (operator, CSRF-protected). On
success they redirect back with a notice; on failure they re-render the page with the
message and the fitting status code. `GET /servers/{id}/players` is the player list fragment
(`_players.html`, refetched on `sse:server-<id>`); `POST /servers/{id}/players/kick`
(operator) and `/players/ban` (administrator) act on it; the list shows only the buttons the
user's role, the adapter and the player's entry allow. `GET`/`POST /servers/{id}/console`
(administrator) is the console page (`console.html`); the answer is shown escaped in a
`<pre>`. `GET /admin/audit` shows the newest 200 audit entries.

**Events** (`core/events.py`, `web/routes_events.py`). `EventBus` fans out `Event(kind,
server_id)` to subscribers through bounded queues; a subscriber that falls behind gets one
`resync` instead of a backlog. `GET /events` is a server-sent event stream with the event
names `server-<id>`, `servers` and `resync` and **no payload**: pages react by fetching what
they show through the normal routes, so nothing bypasses the access checks. The stream
authenticates with short-lived database sessions (the request dependency would pin a
connection for the stream's lifetime), re-checks the session every 15 s with the keepalive
and ends when it is gone. It sends `X-Accel-Buffering: no`.

## Frontend and live updates in the browser

_State: implemented (step 1.6)._

Pages are rendered on the server and work without JavaScript: every action is a normal form
post followed by a redirect. JavaScript adds two things for signed-in users only.

- **Live updates.** `home.html` and `server.html` wrap their content in
  `hx-ext="sse" sse-connect="/events"`. Elements refetch themselves when their event arrives:
  a dashboard card (`_card.html`, `GET /servers/{id}/card`) on `sse:server-<id>`, the whole
  grid (`_grid.html`, `GET /cards`) on `sse:servers` and `sse:resync`, and the
  state-dependent part of a server's page (`_server_live.html`, `GET /servers/{id}/live`).
  Forms are deliberately outside the refreshed parts, so an update never disturbs a
  half-made selection. The fragments are rendered by the same templates as the full pages.
- **Searchable map picker.** `static/js/app.js` turns every `<select data-searchable>` into
  a Tom Select control. The picker must keep its height when focused (see the comment in
  `app.css`): if it grows, the submit button below moves and the first click on it is lost.

When a fragment request returns 401 (the session ended), `app.js` reloads the page, which
leads to the sign-in page.

**Content-Security-Policy.** No inline scripts, styles or event handlers anywhere; a test
checks the pages for them. htmx is configured through a `<meta name="htmx-config">` tag with
`allowEval`, `allowScriptTags` and `includeIndicatorStyles` off and `selfRequestsOnly` on.
Values that would need an inline style use elements or classes instead: the player count is
a `<progress>`, the map placeholder colour is one of six `tone-N` classes chosen by the
`tone` template filter.

**Vendored libraries** live in `static/vendor/` with their license files and are listed in
`NOTICE.md`. `scripts/vendor_frontend.py` reproduces them from the npm registry, pinned by
version and integrity hash; updating a library means editing that table and re-running the
script. Alpine.js, mentioned in early plans, is not used: nothing needs it so far.

Until map images exist (step 1.7) cards show a coloured placeholder with the map name.

## Map images

_State: storage, delivery, upload and display implemented (step 1.7, `core/map_images.py`,
`web/routes_map_images.py`); online sources for Counter-Strike and the stock map catalogue
implemented (step 2.4, `core/image_sources.py`, `core/image_lookup.py`, `core/catalog.py`);
TrackMania Exchange for Trackmania (step 3.4). Unreal Archive follows with phase 4._

An image belongs to a **game and a map key** (table `map_images`: adapter id and
lower-case key), not to one server: every server of that game with a map of that name
shows it. Games of one family do not share images, because their maps look different
(`de_dust2` in CS 1.6 and in CS2); this changed in migration 0007, which kept the images it
could attribute to a game and dropped the others.

- **Processing** (`process()`): only JPEG, PNG and WebP are accepted, at most 10 MB and 40
  megapixels (checked before decoding, so a small file cannot expand into a huge image). The
  image is decoded, rotated according to EXIF, converted to RGB, scaled into 1280×720 and
  encoded again as WebP. Nothing of the original file is kept: metadata and anything appended
  to the file are gone. This runs in a worker thread.
- **Storage**: `<data_dir>/map-images/<random>.webp`, directory mode 700, files mode 600.
  File names are random and generated by LANPilot; no path is ever built from a map name or
  an upload's file name. Replacing an image writes a new file and deletes the old one.
- **Lookup**: `MapImageIndex` (`app.state.map_images`) keeps (game, key) → file name in
  memory, loaded at start and updated on every change, so rendering cards needs no query.
  Templates call `map_image_url(adapter_id, key)`.
- **Delivery**: `GET /map-images/<file>` for signed-in users. Only names that match the
  generated pattern and are in the index are served. Because the name changes with the
  image, responses are cached for a year (`private, immutable`).
- **Management**: `/admin/servers/{id}/maps` lists a server's maps with upload, replace and
  remove (administrators, CSRF-protected, audited). Maps are addressed by `server_maps` row
  id, checked to belong to the server. After a change `manager.announce()` makes open pages
  refetch.
- **Display**: `_card.html` and `_server_live.html` show the image of the current map, or the
  coloured placeholder with the map name when there is none.

**Online sources** (`core/image_sources.py`). `Fetcher` is the only code that talks to the
internet: HTTPS only, to the hosts of the configured sources only, redirects are never
followed, JSON answers are limited to 2 MB and images to 10 MB, 15 s per request, no proxy
from the environment, a `User-Agent` naming LANPilot. HTTP 404 and anything odd count as
"not here"; network errors, 429 and 5xx raise `FetchError` ("ask again later"). A source
(`ImageSource.find()`) turns a map into the URL of a candidate image and knows nothing about
games: adapters declare the identifiers a source needs in `GameAdapter.image_lookup`.

| Source | Lookup | Identifier from the adapter |
|---|---|---|
| `steam_workshop` | `GetPublishedFileDetails` for `MapRef.extra["workshop_id"]`, app id must match | `steam_app` |
| `gamebanana` | API search, only an upload in "Maps" of that game named exactly like the map; skipped for stock maps | `gamebanana` (game id) |
| `gametracker` | fixed thumbnail URL `…/160x120/<slug>/<map>.jpg` | `gametracker` (slug) |
| `tmx` | `/api/tracks?uid=…` on each listed site; screenshot if `HasImages`, else thumbnail if `HasThumbnail` | `tmx` (comma-separated TMX sites) |
| `unreal_archive` | the bundled index `catalog/unreal-archive.json.gz` (no request); then the image | `unreal_archive` (the game's name in the archive) |

Values from answers are checked before use (GameBanana's image must lie under its own image
host with a plain file name; a TMX image URL is only built from the id TMX returned for
exactly that UId, because TMX answers unknown ids with a placeholder picture); map keys and slugs go into URLs only if they match
`[A-Za-z0-9_.-]{1,64}`.

**Lookup** (`core/image_lookup.py`, `app.state.image_lookup`). `ImageLookup.lookup()` asks
the sources in the configured order and stores the first image that can be fetched and
processed, through `MapImageIndex.store()` with the source's id as `provider` and the image
URL as `source_url`. A lookup without result is remembered in `map_image_misses`
(`not_found`); removing an image remembers `removed`. "Not found" is forgotten whenever
LANPilot starts, so an update that brings a new source gets its chance at once. How hard to try depends on who asks:

| Who asks (`Reach`) | `not_found` | `removed` | after a network error |
|---|---|---|---|
| the poll, for each server's current map (`AUTOMATIC`) | skip for `retry_days` (and not queued again for an hour) | skip | skip for 10 min |
| "look for all missing images", `lanpilot images prefetch` (`MISSING`) | ask again | skip | ask |
| "look online" for one map (`ONE`) | ask again | ask again | ask |

`ServerManager.on_map` reports the current map after every poll; `want()` queues a lookup
unless there is an image or one is queued already. One background task works through the
queue with a pause of 0.5 s between maps, and calls `manager.announce()` after a find, so
open pages show the image. While idle it reads the index again every minute, which picks
up images stored by `lanpilot images prefetch` in another process. With `images.online`
off nothing is queued and no task runs.

**Catalogue** (`core/catalog.py`, `catalog/*.toml`). Display names of stock maps per
adapter id (`de_dust2 = "Dust II"`), loaded once per process and validated. `maps.replace_reported()`
fills in a name for every map the server reports without one, and the poll does the same for
the current map, so cards say "Dust II" and the picker "Dust II (de_dust2)". The catalogue
never adds maps a server did not report; games whose protocol cannot list maps will use it
for that when they are added (planned with Call of Duty).

**Unreal Archive index.** `scripts/build_unreal_index.py` reads the archive's public-domain
metadata (one YAML file per upload, from a sparse clone of `unreal-archive-data`) and writes
game → lower-case map file name → first screenshot URL and title, gzip-compressed JSON (about
0.7 MB for 16 000 UT99, 5 000 UT2004 and 1 200 UT3 maps). An original upload wins over a
variation, then the earliest; map packs are left out because their screenshots are not tied
to a map. The index is regenerated by hand and committed; its revision is stored inside.

Images are **not** shipped in the repository or the packages. `lanpilot images prefetch`
loads all images for the stored maps in advance. Details and verified endpoints:
[research/map-image-sources.md](research/map-image-sources.md).

**Orphaned files.** At start-up LANPilot deletes image files that no database row refers to
and that are older than an hour (the prefetch command may have written a file it has not
committed yet).

## Security

_State: planned; each mechanism lands with the step named in brackets._

- **Authentication** (1.2 ✔) – Argon2id; no default password, the first admin is created with
  `lanpilot user create`. Login throttling per IP and per username, uniform error message.
- **Sessions** (1.2 ✔) – server-side, token stored hashed, cookie `HttpOnly`, `Secure`,
  `SameSite=Lax`; idle and absolute expiry; a password change ends all sessions.
- **CSRF** (1.2 ✔) – token on every state-changing request, no changes via GET, origin check.
- **Authorisation** (1.5 ✔) – central in `core/actions.py`. Raw console and server credentials
  are `admin` only; the console can be disabled globally.
- **Server credentials** (1.3 ✔, `core/secrets.py`) – the secret fields of a connection are
  stored as one JSON object encrypted with AES-256-GCM (`SecretBox`, format `v1:<base64 of
  nonce + ciphertext>`), key outside the database. The ciphertext is bound to
  `server|<adapter id>|<host>|<port>` as associated data: copied to another server entry, or
  after the entry's address is changed, it no longer decrypts. Whoever can write to the
  database therefore cannot make LANPilot send a stored password to another host, and
  **changing a server's host, port or game requires entering its passwords again**. Secret
  fields are write-only and never sent back to the browser.
- **Log redaction** (1.3 ✔, `core/redaction.py`) – secrets are registered with a process-wide
  `redactor` when they are loaded or decrypted; the log formatter replaces them everywhere,
  tracebacks and Uvicorn's own lines included.
- **Command injection** (2.x onwards) – map names must be in the stored list and match a
  strict character set; transports reject `;`, quotes and line breaks; kick uses numeric ids.
- **Untrusted server data** (1.6 onwards) – autoescaping, strict Content-Security-Policy
  without inline scripts, size and time limits in all protocol clients, parser fuzz tests.
- **Request size** (1.7 ✔) – `BodyLimitMiddleware` rejects bodies over 11 MB with 413 before
  anything reads them, and requests with a body but without `Content-Length` with 411.
- **Uploads** (1.7 ✔) – images are never stored as received; see "Map images".
- **LAN scan** (6 ✔) – on request by an administrator only, only inside the configured
  networks (answers from elsewhere are dropped), bounded in hosts, answers and time; names
  from answers are cleaned and escaped like all server data; pre-filled form values never
  reach password fields.
- **Outbound requests** (2.4 ✔, `core/image_sources.py`) – HTTPS only, to the hosts of the
  configured image sources only, redirects never followed, no proxy from the environment,
  size and time limits; values from answers are checked before use, and every image goes
  through the same re-encoding as uploads. `images.online = false` switches it off.
- **Operation** (1.8) – binds to `127.0.0.1` by default, TLS through a reverse proxy,
  dedicated system user, hardened systemd unit.
- **Supply chain** (0.4, 1.8) – pinned dependencies with hashes, Dependabot, `pip-audit`,
  CodeQL, secret scanning; Actions pinned by commit SHA with minimal permissions; releases
  with checksums.
- **Documented residual risk** – RCON, XML-RPC and WebAdmin transmit passwords unencrypted.

## Packaging

_State: implemented (step 1.8) in `packaging/` and `.github/workflows/release.yml`. The
workflow has built every package and passed the install, upgrade and removal test on all
seven distributions._

**One description, three formats.** `packaging/build.sh <deb|rpm|archlinux> <wheel>` builds
a package with [nfpm](https://nfpm.goreleaser.com) from `packaging/nfpm.yaml.in`.
`fetch-nfpm.sh` downloads nfpm pinned by version and checksum.

**The Python environment is inside the package.** `build.sh` creates a virtual environment,
installs the wheel with all dependencies, removes pip and the activation scripts, rewrites
the scripts' interpreter path to `/opt/lanpilot` and recompiles the bytecode for that path.
The package then needs no internet access to install. Because the environment contains
compiled extensions for one Python version, each package depends on exactly that version
(`python3 (>= 3.13), python3 (<< 3.14)`, `python(abi) = 3.13`, …) and is built once per
distribution release in a container of that release.

| Path | Content |
|---|---|
| `/usr/bin/lanpilot` | wrapper; when run by root it switches to the `lanpilot` user, so files created through the command line stay writable for the service |
| `/opt/lanpilot/` | the Python environment, owned by root, never written at runtime |
| `/etc/lanpilot/lanpilot.toml` | configuration (`root:lanpilot`, 0640), never overwritten by an upgrade; `public_url` must be set before the first start |
| `/etc/lanpilot/secret.key` | encryption key (`lanpilot`, 0600) |
| `/var/lib/lanpilot/` | SQLite database and image cache (`lanpilot`, 0700) |
| `/usr/lib/systemd/system/lanpilot.service` | unit running as `lanpilot`, sandboxed (`systemd-analyze security`: 1.3) |
| `/usr/lib/sysusers.d/lanpilot.conf` | the system user |

**Install scripts** (`packaging/scripts/`), written to be safe to run again:

- `postinstall.sh` creates the user, fixes ownership of the configuration, **generates the
  encryption key only if none exists**, creates the state directory, and restarts the
  service if it is running. On a first install it prints the three steps to get started; it
  does not start the service, which could not run without `public_url`.
- `preremove.sh` stops and disables the service on removal, not on upgrade.
- `postremove.sh` leaves configuration, key and data in place; only `apt purge` deletes them.

**Upgrades.** The application code is package content, so the package manager replaces it;
nothing is rebuilt on the target system and no stale environment can remain. What still has
to happen on every upgrade is the service restart, so the post-install script is registered
for upgrades in every format (Arch needs a separate `postupgrade` entry). The database schema
is migrated when the service starts, after a backup.

**Pipeline** (`release.yml`, on version tags, on pull requests touching `packaging/`, and
on demand): build the wheel → build packages in containers for Debian 12 and 13, Ubuntu 24.04
and 26.04, Fedora 44, Rocky Linux 9 (with `python3.11`) and Arch → in a fresh container per
distribution run `packaging/test-install.sh`: install an older build, configure, create a
user, start, **upgrade**, check that the new version is in place and that key, configuration
and data are untouched, reinstall, remove, and purge on Debian → on a tag publish everything
with `SHA256SUMS` and a build provenance attestation as a GitHub release. The tag must match
`__version__`.
