# Security policy

LANPilot stores credentials for game servers and executes commands on them. Security reports
are taken seriously and handled with priority.

## Supported versions

LANPilot is in pre-release development. Only the latest commit on `main` receives fixes.
This table will list supported releases once version 1.0 is published.

## Reporting a vulnerability

Please **do not open a public issue** for security problems.

Report privately through GitHub: open the repository's **Security** tab and choose
**Report a vulnerability** (private vulnerability reporting). Include:

- what you found and where (file, endpoint, version or commit),
- how to reproduce it,
- what an attacker could achieve.

You can expect an acknowledgement within 7 days. Once a fix is available it is released
together with an advisory that credits you, unless you prefer to stay anonymous.

## Security model in brief

The full description lives in [docs/architecture.md](docs/architecture.md#security).

- Every page requires a login; there is no anonymous access.
- Passwords of LANPilot users are hashed with Argon2id.
- Game server credentials are encrypted at rest with a key stored outside the database.
- Everything a game server sends (server names, player names, HTML) is treated as untrusted.
- Console commands are built from validated values only; map names must be in the stored map
  list of the server.

## Known limitation of the game protocols

RCON, GBXRemote/XML-RPC and the Unreal WebAdmin transmit their passwords **unencrypted**.
LANPilot cannot change that. Run game servers and LANPilot in a network segment you trust,
and use a separate remote-control password for every server.

## Out of scope

- Vulnerabilities in the game servers themselves.
- Attacks that require an already compromised administrator account.
- Denial of service through traffic volume on the local network.
