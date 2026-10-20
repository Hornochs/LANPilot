# Security and threat model

This document describes what LANPilot protects, against whom, how, and what it leaves to
the operator. How to report a vulnerability is in [SECURITY.md](../SECURITY.md); the
mechanisms themselves are described in [architecture.md](architecture.md) ("Security" and
the sections it points to).

_Last reviewed: 2026-10-20 (step 8.1), with the findings listed at the end._

## Setting

LANPilot runs on one host in the network of a LAN party, usually behind a reverse proxy on
the same host. It talks to game servers in that network through their remote protocols and,
if enabled, fetches map images from a few internet sites. A LAN party network is shared with
many people the organisers do not control; that is the main assumption behind this model.

**Trusted:** the LANPilot host and its root account, the people with the `admin` role, the
configuration file, and the reverse proxy in front of LANPilot.

**Not trusted:** everyone else in the network, every game server and everything it sends
(names, map lists, player lists, HTML pages), the answers to LAN scans, the image sources on
the internet, and the network itself (it can be sniffed and spoofed).

## Assets

| Asset | Where | Why it matters |
|---|---|---|
| Remote-control passwords of the game servers | encrypted in the database | full control of every game server |
| Credential encryption key | `/etc/lanpilot/secret.key` | decrypts those passwords |
| User accounts, sessions | database (Argon2id hashes, SHA-256 of session tokens) | control through LANPilot |
| Availability of LANPilot | the service | the organisers' tool during the event |
| Integrity of actions | audit log | the right map, the right player kicked, a record of who did it |

## Threats and what counters them

### Anonymous clients in the network

They can reach the sign-in page and `/healthz`, nothing else.

- Password guessing: login throttle per (name, address) and per address, counted before the
  password is checked so parallel requests cannot slip through; uniform answers and timing
  for unknown names; Argon2id with at most four verifications at a time.
- Flooding: failed logins are kept in the audit log up to 5000 entries, throttled attempts
  once per address and window; request bodies are limited before they are read.
- Cross-site requests and host header tricks: origin check on every state-changing request
  (the sign-in included), CSRF tokens, host allow-list, `SameSite=Lax` cookies.
- Log injection: only names that could be user names reach the audit log.

### Signed-in users

Roles are checked on every route and again in the action layer: viewers only read,
operators change maps, restart and kick, administrators do everything else. Server data in
pages is escaped and the Content-Security-Policy allows no inline scripts; the live-update
stream carries no data and is limited to eight per session.

### Administrators

Administrators are trusted, with two safeguards against mistakes and lures:

- Passwords are write-only: never shown again, never sent back to the browser, never in the
  log or the audit log (console commands that look like they set a password are recorded
  without their arguments).
- A stored password is bound to its server's game, host and port; whoever can write to the
  database cannot redirect it to another host. A server form pre-filled from a link (such as
  the scan's "add") says so and asks to check the host before a password is entered.

The raw console is off unless `enable_console` is set.

### Game servers, LAN scan answers and other hosts in the network

Anyone can run a game server or answer a scan. LANPilot treats what they send as hostile:

- **Command injection:** values that end up in console commands (map names, player ids, kick
  reasons, IP addresses) are checked against strict patterns right before sending; map
  changes accept only entries of the stored map list.
- **Parser abuse:** every protocol client bounds what it reads: the A2S status query takes a
  single datagram and refuses split and compressed answers (a forged compressed answer once
  inflated to gigabytes in the library used before), RCON limits packets and bytes,
  GBXRemote limits answers to 8 MB before XML parsing, WebAdmin pages are limited in size,
  elements, table cells and nesting. Every query has an overall deadline.
- **Forged data:** a WebAdmin player page whose rows do not add up (a player id twice, kick
  boxes outside the player table) is refused, because UT99 does not escape player names and a
  name could otherwise pose as another player's row. Kick and ban act only on a player who is
  in a freshly read list.
- **Scan abuse:** answers count only from inside the configured networks, at most eight per
  host; follow-up queries go only to the address that answered, 32 at a time, five seconds
  each. Nothing is scanned in the background.
- **Outbound requests:** only to the configured image sources, over HTTPS, without
  redirects, with size and time limits; a map name from a server's status leads to a lookup
  only if the server also lists that map. Every image is decoded and encoded again.
- **Password bans:** a transport that was refused its password does not send it again until
  the server entry is changed or LANPilot restarts.

### The network itself

RCON, XML-RPC and the WebAdmin send passwords in clear text; that is how these protocols
work, and LANPilot cannot change it. Someone who can read or redirect the traffic between
LANPilot and a game server gets the password. Between browsers and LANPilot, use HTTPS
(`public_url` with `https://`): over plain HTTP, session cookies and passwords can be read
as well, which LANPilot warns about at start-up and on every page.

### Supply chain

The packages ship a Python environment whose dependencies are pinned to exact versions and
checked against their hashes when the package is built (`packaging/requirements.txt`,
updated through Dependabot pull requests). Frontend libraries are vendored and pinned by
integrity hash. GitHub Actions are pinned by commit, workflows have minimal permissions,
releases carry checksums and a build provenance attestation. No GPL-licensed code is used.
FastAPI's OpenTelemetry hooks stay inactive: no OpenTelemetry SDK or exporter is installed,
so nothing is reported anywhere.

### The LANPilot host

The service runs as the unprivileged `lanpilot` user under a hardened systemd unit; its
files are readable by that user only, the key file is created atomically and never
replaced, database backups before migrations are limited to the newest five. The
`lanpilot` command switches to the service user with a clean environment when run by root.

## Residual risks

Accepted, and what the operator can do about them:

| Risk | What helps |
|---|---|
| Game-server passwords travel in clear text | a separate network for LANPilot and the game servers' admin ports; a password per server |
| Plain HTTP between browsers and LANPilot | HTTPS through the reverse proxy |
| A hostile player on UT99 can make the player list unusable (it is refused, not guessed) | kick through the game's own WebAdmin or console |
| UT3 discovery shares UDP 14001 for the scan's wait (2 s); unicast datagrams to a UT3 server on the same host land on LANPilot during that time | run UT3 servers on another host |
| After a refused password, editing or re-testing the server, or a restart, sends it once more | correct the password before testing again |
| An administrator can point a server entry at any host and port | administrators are trusted |
| Release packages are not signed with a distribution key | verify the attestation (`gh attestation verify`, see [installation.md](installation.md)) |
| No HSTS header from LANPilot itself | set it in the reverse proxy |

## Review of 2026-10-20

Three parallel reviews (web and authentication; protocols and adapters; packaging and
operation) found, and the same step fixed:

| Severity | Finding | Fix |
|---|---|---|
| high | A2S answers were parsed by opengsq, which inflates bz2-compressed split answers without a limit: one datagram could exhaust memory | own A2S client (`core/transports/a2s.py`): single datagram, no compression, one deadline |
| medium | login throttle could be bypassed with parallel requests | the attempt is counted before the password is checked; hashing limited to four at a time |
| medium | failed logins filled the audit log without bound | failures pruned to 5000, throttled attempts recorded once per window |
| medium | crafted UT99 player names could forge rows with another player's id | such pages are refused |
| medium | WebAdmin HTML parser could be made to use gigabytes and minutes | limits on elements, cells, nesting |
| medium | timeouts per packet only (GameSpy, Source RCON) | overall deadlines, packet limits |
| medium | package dependencies not pinned | hash-pinned lock file, `--require-hashes` |
| low | one host could crowd out scan answers; unbounded follow-ups | per-host limit, bounded follow-ups |
| low | invented current-map names caused image requests and database rows | only listed maps are looked up |
| low | console commands with passwords in the audit log; passwords typed as user names | arguments not recorded; only valid names recorded |
| low | password change without throttle; unlimited event streams | throttled; eight streams per session |
| low | partial key file after an interrupted install; backups never pruned; root's environment passed to the service user | atomic key file; five backups; clean environment |
| low | form pre-filled from a link could lure a password to a foreign host | warning on pre-filled forms |
