# Factorio

| Game | Adapter | State |
|---|---|---|
| Factorio (headless server, Source RCON) | `factorio` | implemented, verified against a real server (open points below) |

Measured on 2026-11-17 against a Linux headless server, Factorio **2.1.24** (build 87808) with
the Space Age expansion, run by systemd; client Factorio 2.1.24 (Steam, Linux). Everything
below was observed on that server unless marked otherwise.

## Setting up the server

- RCON is off unless the server is started with both options:
  `--rcon-bind 0.0.0.0:27015 --rcon-password <password>`. The port is TCP and separate from
  the game port (default UDP 34197).
- For the LAN scan, `"visibility": {"lan": true}` in `server-settings.json`.
- Restarting from LANPilot needs a service manager that starts the server again after it
  exits, and a start that loads the newest save, e.g. a systemd unit with `Restart=always`
  and `--start-server-load-latest`. Tick "The server is started again automatically after
  /quit" in LANPilot's server form only then.

## Achievements

A save loses its achievements **for good** once a Lua command runs on it, or the map editor
or cheat mode is used. In the game client Factorio asks for the Lua command a second time
before running it; whether RCON asks as well was not tried on purpose. LANPilot never sends
such a command, and its console refuses, case-insensitively and with leading whitespace
ignored:

| Refused | Why |
|---|---|
| `/c`, `/command`, `/sc`, `/silent-command`, `/mc`, `/measured-command` | run Lua: achievements off |
| `/editor`, `/cheat` | map editor, cheat mode: achievements off |
| `/toggle-heavy-mode` | makes multiplayer unplayable until it outputs something |
| `/delete-blueprint-library` | deletes players' blueprint storage |
| any line not starting with `/` | Factorio posts it to every player as a chat message |
| `/quit` | would stop the server; Restart does that only where the server comes back |

None of the commands the adapter sends itself touches achievements.

## RCON protocol

Source RCON framing and login (see [counter-strike.md](counter-strike.md)), with two
differences that matter:

- **Every command gets exactly one RESPONSE_VALUE packet**, with an empty body if the command
  prints nothing.
- **An empty command gets no answer at all.** The end-marker trick used for Counter-Strike
  (send an empty command after the real one and wait for its answer) therefore waits for the
  full timeout. The adapter runs `SourceRcon` with `single_packet_answers=True`, which sends no
  marker and returns the first packet carrying the command's id.

`/help` over RCON lists only the commands of the server console (`/quit`, `/save <name>`);
`/help <command>` describes any command.

## Commands

| Purpose | Command | Answer (verified) |
|---|---|---|
| server name | `/config get name` | `Server name: Factorio LAN Server` |
| maximum players | `/config get max-players` | `0` (no limit) |
| players online | `/players online` | `Online players (1):` then `  Engineer (online)` per player |
| version | `/version` | `2.1.24` |
| map seed | `/seed` | `3490025918` |
| players ever joined | `/players count` | `Players (1):` |
| autosave interval | `/config get autosave-interval` | `Autosave every 10 minutes.` |
| Space Age | `/evolution` | one line per surface; `[planet=vulcanus]`, `fulgora`, `gleba`, `aquilo` only with Space Age |
| map age | `/time` | `2413 hours and 1 minute` |
| kick, ban | `/kick <name> <reason>`, `/ban <name> <reason>` | not tried (no other player online) |
| save | `/server-save` | `Saving the map` at once; the save runs in the background |
| stop | `/quit` | empty; the server waits for a running save, saves the map again (blocking) and exits |

## What the adapter does with it

- **Status:** name, players online, maximum players (0, no limit: LANPilot then shows only
  the players); `Space Age` as the game
  type when the expansion is active; Space Age comes from
  the mod list in the LAN info reply (asked on the form's game port); only if that does not
  answer, from `/evolution`, which lists only planets already reached, so a new Space Age map
  looks like the base game there. Space Age, seed and the numbers are asked once a minute.
- **Details on the server page** (text, `ServerStatus.details`): `Version` (`/version`),
  `Planets` reached, one per line (lines of `/evolution` with a `[planet=…]` tag, in that order; surfaces
  that mods create have none and are left out), `Mods` (the LAN info's mod list with versions, one
  per line, without `base`; left out if the LAN info does not answer), `Map age` (`/time`, as
  `2415 h 18 min`), `Players ever joined` (`/players count`) and `Autosave` (`/config get
  autosave-interval`; left out when autosave is off). They are counts and texts, not
  measurements, so the adapter reports no load values.
- **Map:** RCON cannot tell which save is loaded, and a Factorio world is not one of a set of
  maps to switch between. The "map" is therefore the edition, `factorio` or `space-age`,
  labelled with the world's seed (`World 3490025918`) so that two servers with different
  worlds can be told apart. It carries the Steam app id of the edition (427520, 645390), so
  the image source `steam_store` gives the card the store header picture. It is the only
  entry of the map list, which exists for that picture; there is no map change.
- **Players:** from `/players online`; suffixes like ` (online)` are removed and names that do
  not match Factorio's name rules (`[A-Za-z0-9_.-]{1,60}`) are left out. A player's id is the
  name.
- **Kick, ban:** the reason is reduced to letters, digits, space and `.,!?_-` (100
  characters, `by LANPilot` if empty); an answer containing `doesn't exist` is reported as
  "no player of that name online".
- **Restart** (hard only, and only when switched on for the server): `/server-save`, waiting
  up to 10 s so that the whole restart fits into LANPilot's 15 s limit for an action. If the
  save takes longer, or its answer contains `error`, `fail`, `cannot`, `can't` or `unable`,
  `/quit` is not sent and the operator gets the reason. Then `/quit` on a fresh connection, so
  it is sent exactly once; a connection the server closes instead of answering `/quit` counts
  as success. The service manager starts the server again, which loads the save just
  written; players are disconnected while it loads. Measured: `/quit` answered at once, the
  server waited for the running save, saved again and exited within 5 s; with systemd's
  `RestartSec=5` and about 5 s of loading it answered RCON and the LAN info again 15 s after
  the restart. Since `/server-save` answers before saving, the error check only catches
  a refusal; `/quit`'s own save is what makes sure nothing is lost.
- **Console:** everything not refused above, sent as typed.

## LAN scan

Factorio's LAN discovery is undocumented; this was read from captured packets. The message
type is the first byte's low five bits; bit `0x20` varies between packets.

- **Announcement:** a server with LAN visibility sends 3 bytes to the broadcast address, UDP
  port **34196**, every 2 seconds: `0F` (or `2F`) and its game port, u16 little-endian
  (`0f 95 85` = 34197). Clients only listen; opening "Browse LAN games" sends nothing.
- **Info request:** one byte `10` to the server's game port. It works unicast, also across
  routers, and needs no announcement.
- **Info reply:** type `11`; the layout is in `src/lanpilot/games/factorio/lan.py`. It holds
  name, version and build, description, maximum players, game time in minutes, the mods with
  their versions, tags and the names of the players online. Which byte marks a password could
  not be identified (the test server has none).

The scan listens on UDP 34196 (`SO_REUSEADDR`) for `max(wait, 2.5)` seconds, then sends the
info request to every server that announced itself. A server whose reply cannot be read is
still listed, with its address only. The RCON port is not announced; the form keeps 27015.

Because announcements are broadcasts, LANPilot must be in the same network segment as the
servers it should find.

## Not verified yet

- Kick and ban with a real second player.
- The info reply of a password-protected server.
- Names with characters outside `[A-Za-z0-9_.-]` (Factorio may not allow them at all).
- What `/server-save` answers when it refuses to save (the restart looks for error words).

## Limitations

- Several Factorio servers on one host are found by the scan as one: announcements are kept
  per host, and the scan does not learn the RCON port.
- Long lists in the details (planets, mods) show the first 30 entries and how many more
  there are.

## Simulator

`lanpilot.testing.factorio.FactorioRconSimulator` answers the commands above like the real
server, one packet per command and nothing to an empty one.
`lanpilot.testing.lan.FactorioLanSimulator` sends announcements and answers the info request;
the tests use the two replies captured from the real server
(`tests/fixtures/factorio/`).
