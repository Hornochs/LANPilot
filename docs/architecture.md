# Architecture

This document describes how LANPilot is built. It is the reference to read before touching
the code, and it is updated in every change that alters structure, interfaces or the data
model.

> **Implementation state:** phases 0 and 1 are complete (see [ROADMAP.md](ROADMAP.md)): the
> whole core exists, with a simulated demo server as the only game. Sections about real
> games, online image sources and the LAN scan are still the **target design**; each section
> carries a state marker that is changed to "implemented" together with the code.

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
| HTTP client | `httpx` | WebAdmin transport, image fetching |
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
    maps.py ✔               stored map lists
    map_images.py ✔         image processing, cache directory, MapImageIndex
    events.py ✔             EventBus for live updates
    secrets.py ✔            key loading, SecretBox (AES-256-GCM)
    redaction.py ✔          keeps known secrets out of the log
  games/demo.py ✔           simulated server
  testing/contract.py ✔     check_adapter() for adapter authors
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
  core/ (planned)
    transports/            source_rcon, goldsrc_rcon, q3_rcon, gbxremote, uweb (no game logic)
  games/ (planned)  counterstrike/, trackmania/, unreal/, quake3/, monitor_only
  maps/ (planned)   catalog/ (stock maps as YAML), images/ (provider chain, cache)
  scan/ (planned)   on-demand LAN scan
  testing/ (planned) protocol simulators
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
| `family` | group of related games sharing maps and image sources, e.g. `counterstrike` |
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
| `LOAD_STATS` | no method: promises that `query()` fills `ServerStatus.load` |
| `DISCOVER` | classmethod `discover(networks) -> Sequence[DiscoveredServer]` |

Methods of undeclared capabilities raise `UnsupportedError` (the base class does this).
`validate_adapter_class()` runs when an adapter is registered and rejects classes whose
declared capabilities and implemented methods disagree in either direction, so
`capabilities` can be trusted.

**Data types** (frozen dataclasses): `MapRef(key, name, game_type, extra)` where `key` is
what is sent back to the server and `extra` carries image lookup ids such as `uid` or
`workshop_id`; `Player(id, name, score, ping, team)`; `ServerStatus(name, players,
max_players, map, game_type, password_protected, load)`.

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
- `manual` – entered by an administrator.

The list is refreshed on demand and cached, so the searchable dropdown is instant. A map
change is only accepted for a map that is in this list.

## Protocols

_State: planned (phases 2–4, 7)._

| Protocol | Games | Implementation |
|---|---|---|
| Status query | all | `opengsq` |
| Source RCON (TCP) | CS:Source, CS:GO, CS2 | `opengsq.rcon_protocols.source_rcon` |
| GoldSrc RCON (UDP) | Counter-Strike 1.6 | own transport |
| GBXRemote / XML-RPC (TCP 5000) | Trackmania | own transport, encoding via `xmlrpc.client` |
| UWeb WebAdmin (HTTP) | UT99, UT2003/2004, UT3 | own transport with `httpx` and HTML parsing |
| Quake 3 RCON (UDP) | Call of Duty 1/4/5, Quake 3, Jedi Knight | own transport |

Research results, including what is still unverified, are in
[research/remote-control-protocols.md](research/remote-control-protocols.md). Facts confirmed
against real servers move to `games/<game>.md`.

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

_State: all tables below are implemented (migrations 0001–0005)._

| Table | Content |
|---|---|
| `users` | username (lower-case, unique), Argon2id hash, role, active flag, last login |
| `sessions` | SHA-256 of the session token, user, CSRF token, created/last seen/expires, client IP and user agent |
| `servers` | name (unique), adapter id, host, port, `settings` (JSON of the other non-secret connection values), `credentials` (encrypted secrets or NULL), `enabled` |
| `server_maps` | server (cascade delete), `key`, `name`, `game_type` ("" if none), `source`, `extra` (JSON: track UId, workshop id); unique per server, key and game type |
| `map_images` | `family`, `map_key` (lower-case; unique together), `filename` (random, unique), `provider`, `source_url`, `width`, `height` |
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

_State: implemented as listed; an `images` command is planned._

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

**Routes** (`web/routes_servers.py`): `GET /servers/{id}` (any signed-in user),
`POST /servers/{id}/map`, `/restart`, `/maps/refresh` (operator, CSRF-protected). On
success they redirect back with a notice; on failure they re-render the page with the
message and the fitting status code. `GET /admin/audit` shows the newest 200 audit entries.

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
`web/routes_map_images.py`). Online providers follow with the game phases._

An image belongs to a **game family and a map key** (table `map_images`, key stored
lower-case), not to one server: every server of that family with a map of that name shows
it.

- **Processing** (`process()`): only JPEG, PNG and WebP are accepted, at most 10 MB and 40
  megapixels (checked before decoding, so a small file cannot expand into a huge image). The
  image is decoded, rotated according to EXIF, converted to RGB, scaled into 1280×720 and
  encoded again as WebP. Nothing of the original file is kept: metadata and anything appended
  to the file are gone. This runs in a worker thread.
- **Storage**: `<data_dir>/map-images/<random>.webp`, directory mode 700, files mode 600.
  File names are random and generated by LANPilot; no path is ever built from a map name or
  an upload's file name. Replacing an image writes a new file and deletes the old one.
- **Lookup**: `MapImageIndex` (`app.state.map_images`) keeps (family, key) → file name in
  memory, loaded at start and updated on every change, so rendering cards needs no query.
  Templates call `map_image_url(family, key)`.
- **Delivery**: `GET /map-images/<file>` for signed-in users. Only names that match the
  generated pattern and are in the index are served. Because the name changes with the
  image, responses are cached for a year (`private, immutable`).
- **Management**: `/admin/servers/{id}/maps` lists a server's maps with upload, replace and
  remove (administrators, CSRF-protected, audited). Maps are addressed by `server_maps` row
  id, checked to belong to the server. After a change `manager.announce()` makes open pages
  refetch.
- **Display**: `_card.html` and `_server_live.html` show the image of the current map, or the
  coloured placeholder with the map name when there is none.

Planned provider chain per game family (first hit is stored through the same `store()`):

1. image uploaded by an administrator,
2. game-specific sources – Trackmania: TMX by track UId; Unreal Tournament: an index built
   from the public-domain metadata of Unreal Archive; Counter-Strike: Steam Workshop by id,
   then GameBanana, then GameTracker,
3. the placeholder.

Images are **not** shipped in the repository or the packages. A `lanpilot images prefetch`
command will load all images for the installed maps in advance. Details and verified
endpoints: [research/map-image-sources.md](research/map-image-sources.md).

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
- **Outbound requests** (with the first online image provider) – image fetching only to a
  fixed list of hosts, no redirects into private networks, size limit; the fetched data goes
  through the same re-encoding as uploads.
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
