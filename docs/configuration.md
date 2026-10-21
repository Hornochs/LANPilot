# Configuration

LANPilot reads its settings from a TOML file and from environment variables.

- **File:** the one given with `--config`, else `$LANPILOT_CONFIG`, else
  `/etc/lanpilot/lanpilot.toml` if it exists.
- **Environment:** `LANPILOT_<KEY>` for top-level settings and `LANPILOT_<SECTION>__<KEY>`
  (two underscores) for settings in a section, for example `LANPILOT_SERVER__PORT=9100`.
  Lists are comma separated. Environment variables override the file; empty ones are
  ignored. The packaged service also reads `/etc/lanpilot/lanpilot.env` if present.

`lanpilot config show` prints what is in effect, with secrets masked. Unknown settings
are rejected, so a typing error does not go unnoticed. Restart the service after a change.

## Settings

### Top level

| Key | Meaning | Default |
|---|---|---|
| `data_dir` | directory for the database and the map image cache | `/var/lib/lanpilot` |
| `secret_key_file` | file with the key that encrypts game server passwords; must be readable by the service user only | – |
| `secret_key` | the key itself, as an alternative to the file (meant for the environment) | – |
| `log_level` | `debug`, `info`, `warning` or `error` | `info` |
| `auto_migrate` | bring the database to the current schema when the service starts | `true` |
| `enable_demo_adapter` | offer a simulated game server for trying LANPilot out | `false` |
| `enable_console` | let administrators send raw console commands to game servers (each one is recorded in the audit log) | `false` |
| `default_language` | language of the user interface (`en` or `de`) when neither the user (under "Account") nor the browser chooses one of them | `en` |

Exactly one of `secret_key_file` and `secret_key` must be set. Create a key with
`lanpilot secret generate-key --output <file>`; the packages do this at installation.
**Losing the key means entering every game server password again.**

### `[server]`

| Key | Meaning | Default |
|---|---|---|
| `public_url` | the address under which users reach LANPilot, as typed into the browser | required |
| `bind` | address LANPilot listens on | `127.0.0.1` |
| `port` | port LANPilot listens on | `9030` |
| `allowed_hosts` | further host names or IPs under which LANPilot may be reached | none |
| `trusted_proxies` | IPs or networks of reverse proxies whose `X-Forwarded-*` headers are trusted | `127.0.0.1` |

`public_url` decides several things:

- LANPilot answers only requests whose `Host` is the one in `public_url` or one of
  `allowed_hosts`. Everything else gets "400 Invalid Host header".
- Requests that change something must come from pages of that address.
- With `https://` the session cookie is marked `Secure`. With `http://` it is not, and
  every page shows a warning, because passwords then travel unencrypted.
- A path, as in `https://tools.example.lan/lanpilot`, means LANPilot is served from that
  sub-path.

### `[auth]`

| Key | Meaning | Default |
|---|---|---|
| `session_idle_minutes` | a session ends after this much inactivity | `720` (12 hours) |
| `session_lifetime_hours` | a session ends this long after sign-in in any case | `168` (7 days) |

### `[polling]`

| Key | Meaning | Default |
|---|---|---|
| `interval_seconds` | how often each game server is asked for its status | `10` |
| `timeout_seconds` | how long LANPilot waits for an answer | `8` |

Servers that do not answer are asked less often, up to six times the interval.

### `[images]`

| Key | Meaning | Default |
|---|---|---|
| `online` | look for map images on the internet | `true` |
| `sources` | which sources to ask, in this order | `["steam_workshop", "gamebanana", "gametracker", "tmx", "unreal_archive", "liandri"]` |
| `retry_days` | after no source had an image, wait this long before asking again | `7` |

With `online` on, LANPilot looks for an image of the current map of every server by
itself. Administrators can ask for all missing images of a server on its map image page,
and `lanpilot images prefetch` does the same for every server from the command line, for
example before a LAN party without internet. Found images are stored in the data
directory and shown from there; the internet is only needed while looking.

| Source | What it has | Used for |
|---|---|---|
| `steam_workshop` | the preview image of a Steam Workshop item | maps whose server reports the workshop id |
| `gamebanana` | screenshots of community maps | custom maps whose name matches an upload exactly |
| `gametracker` | 160×120 thumbnails of stock and popular maps | other Counter-Strike maps |
| `tmx` | TrackMania Exchange screenshots (320×240) or thumbnails | Trackmania tracks, found by their unique id |
| `unreal_archive` | screenshots of maps uploaded to Unreal Archive | Unreal Tournament maps, looked up in an index shipped with LANPilot; mostly community maps, few stock maps |
| `liandri` | screenshots from the Liandri Archives (the Unreal wiki) | Unreal Tournament stock maps, looked up in an index shipped with LANPilot; the wiki has hardly any community maps |

GameBanana is not asked for stock maps, because it holds remakes, not the originals. The
terms of use of GameBanana and GameTracker have not been confirmed; leave them out of
`sources` if that matters to you. Requests go only to the hosts of the configured sources,
over HTTPS, without following redirects; every image is decoded and encoded again before it
is stored. An image an administrator removes is not fetched again automatically.

### `[scan]`

| Key | Meaning | Default |
|---|---|---|
| `networks` | IPv4 networks the LAN scan looks in, e.g. `["192.168.1.0/24"]`; empty: no scan | empty |
| `timeout_seconds` | how long a scan waits for answers to its broadcasts | `2` |

Administrators start a scan under "Server setup" → "Scan the network" (or with
`lanpilot scan`); it runs once, never in the background, and takes a few seconds. It
finds Counter-Strike 1.6, CS:Source, CS:GO, CS2, TrackMania Forever, TrackMania
Original/Sunrise/Nations ESWC, UT99, UT2004, UT3, Quake III Arena and Call of Duty (1, United
Offensive, 2, 4, World at War) servers without a password and lists
name, game, game type and address; "Add" opens the server form with these values, and the
passwords are entered there.

The scan sends broadcasts, so LANPilot must be in the same network segment as the game
servers (a router does not forward them). Unreal Tournament 3 answers only to UDP port
14001; the scan uses that port for a moment, which also works while a UT3 server runs on the
LANPilot host.

## Examples

Behind a reverse proxy on the same machine (the usual setup):

```toml
data_dir = "/var/lib/lanpilot"
secret_key_file = "/etc/lanpilot/secret.key"

[server]
public_url = "https://lanpilot.example.lan"
```

Directly reachable in the LAN, without TLS:

```toml
data_dir = "/var/lib/lanpilot"
secret_key_file = "/etc/lanpilot/secret.key"

[server]
public_url = "http://192.168.1.10:9030"
bind = "0.0.0.0"
trusted_proxies = []
```

## Accounts and roles

| Role | May |
|---|---|
| `viewer` | see servers, maps, players and their status |
| `operator` | also change maps, read a server's map list again, restart matches, reload levels and kick players |
| `admin` | also ban players; manage users, servers and map images; scan the network; read the audit log; use the console (only with `enable_console`) |

The first administrator is created on the command line (`lanpilot user create NAME`,
optionally `--role viewer|operator|admin`; the default is `admin`), further accounts in the
web interface under "Users". Passwords have at least 10 characters. Each user chooses the
language of the interface (English, German, or as the browser prefers) on the "Account"
page.

**Console.** With `enable_console = true` (and a restart), administrators find a "Console"
link on the page of every server whose game has one (all Counter-Strike games, Unreal
Tournament 99, 2004 and 3). Commands go to the server as they are and are recorded in the
audit log; a command that sets a password is recorded without its arguments.

## Game servers and their passwords

Remote-control passwords are stored encrypted and are never shown again. They are tied to
the server's game, host and port: after changing host or port, enter the password again.

The remote-control protocols of the games (RCON, XML-RPC, WebAdmin) send their passwords
**unencrypted** over the network. Use a dedicated password per server and keep game servers
and LANPilot in a network you trust.
