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
| `viewer` | see servers and their status |
| `operator` | also change maps, restart matches and reload levels |
| `admin` | also manage users, servers and map images, and read the audit log |

The first administrator is created on the command line (`lanpilot user create NAME`),
further accounts in the web interface under "Users". Passwords have at least 10
characters.

## Game servers and their passwords

Remote-control passwords are stored encrypted and are never shown again. They are tied to
the server's game, host and port: after changing host or port, enter the password again.

The remote-control protocols of the games (RCON, XML-RPC, WebAdmin) send their passwords
**unencrypted** over the network. Use a dedicated password per server and keep game servers
and LANPilot in a network you trust.
