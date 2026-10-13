# LANPilot

A web interface for monitoring and remote-controlling game servers at LAN parties: see every
server on one dashboard, change the map, restart a match and manage players without logging
into each machine.

> **Status: early development.** The core is in place: accounts, server management, live
> dashboard, map changes and restarts, map images and distribution packages. What is still
> missing is the reason to use it: **no real game is supported yet**, only a simulated demo
> server. Counter-Strike, Trackmania and Unreal Tournament come next. Progress is tracked in
> [docs/ROADMAP.md](docs/ROADMAP.md).

## What LANPilot will do

- **Dashboard** with one card per server: game, current map with a preview image, player
  count and status, updated live.
- **Control** per server, as far as the game supports it: change map from a searchable list of
  the maps installed on that server, soft restart (restart the round or match), hard restart
  (reload the level), player list with kick and ban, and a raw console for administrators.
- **Map images** fetched from community sources, cached locally and pre-loadable for LAN
  parties without internet access.
- **Accounts** with username and password, three roles (admin, operator, viewer) and an audit
  log of every action.
- **Monitoring-only mode** for games that offer no remote control at all.
- **Packages** for Debian/Ubuntu (`.deb`), Fedora/RHEL (`.rpm`) and Arch Linux.

## Design principles

- **Protocol-only.** LANPilot talks to game servers exclusively through their own remote
  protocols (RCON, XML-RPC, WebAdmin). It needs no SSH access and no agent on the game hosts.
  The consequence: it cannot start a stopped server process or read host CPU and memory.
- **Modular.** Every game is an adapter that declares what it can do. New games can be added
  without touching the user interface, including as separately installed Python packages.
- **Secure by default.** Stored server credentials are encrypted, everything requires a login,
  and data coming from game servers is treated as untrusted. See [SECURITY.md](SECURITY.md).

## Planned game support

| Game | Remote protocol | Priority |
|---|---|---|
| Counter-Strike 1.6 | GoldSrc RCON (UDP) | high |
| Counter-Strike: Source, CS:GO, CS2 | Source RCON (TCP) | high |
| TrackMania Nations/United Forever, TrackMania 2, Trackmania (2020) | GBXRemote / XML-RPC | high |
| Unreal Tournament 99, 2003, 2004, 3 | UWeb WebAdmin (HTTP) | high |
| Call of Duty 1/4/5, Quake 3, Jedi Knight | Quake 3 RCON (UDP) | later |
| Battlefield 2, Renegade X, ElDewrito | game-specific | later |
| Age of Empires 1/2, FlatOut 2, Stronghold Crusader, Warcraft III and others | none (monitoring only) | later |

## Documentation

- [docs/installation.md](docs/installation.md) – packages, first start, backup, upgrade
- [docs/configuration.md](docs/configuration.md) – all settings, accounts and roles
- [docs/reverse-proxy.md](docs/reverse-proxy.md) – Nginx, Caddy, Traefik, or none
- [docs/adapters.md](docs/adapters.md) – adding support for a game
- [docs/ROADMAP.md](docs/ROADMAP.md) – phases, numbered steps and current status
- [docs/architecture.md](docs/architecture.md) – how LANPilot is built
- [docs/research/](docs/research/) – protocol and map image research notes
- [CONTRIBUTING.md](CONTRIBUTING.md) – development setup and contribution rules
- [SECURITY.md](SECURITY.md) – reporting vulnerabilities, security model

## Development quick start

Requires Python 3.11 or newer.

```bash
git clone https://github.com/lan-dot-party/LANPilot.git
cd LANPilot
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

## Related projects

- [Discord-Gameserver-Notifier](https://github.com/lan-dot-party/Discord-Gameserver-Notifier) –
  discovers game servers on the LAN and announces them on Discord (read-only).

## License

[MIT](LICENSE). Third-party material and code provenance are listed in [NOTICE.md](NOTICE.md).
