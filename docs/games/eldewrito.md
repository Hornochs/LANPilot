# ElDewrito (Halo Online)

| Game | Adapter | State |
|---|---|---|
| ElDewrito 0.7.1 (Halo Online build `1.106708_cert_ms23___release`) | `eldewrito` | implemented, verified against a real server without players |

Measured on 2026-10-21 against an ElDewrito 0.7.1 dedicated server on Windows
(`eldorado.exe -launcher -dedicated`), lobby type multiplayer, network mode "Online".
No players were connected, so a running game, the player list with players, kick and ban
could not be tried. What is said about them comes from the server's `help` text and from
the source of ElDewrito 0.6 ([ElDewrito/ElDorito](https://github.com/ElDewrito/ElDorito),
`Patches/Network.cpp` and `Modules/ModuleServer.cpp`), and is marked as such.

The RCON protocol was first read from [RconTool](https://github.com/jaron780/RconTool). Neither
repository has a license file, so nothing is copied from them; only facts were taken and
checked against the server.

## Setting up the server

All settings live in `dewrito_prefs.cfg` in the game folder. On the test server a password
set in `server.json` was not used for RCON (`deny`); it worked once `Server.RconPassword` was
set in `dewrito_prefs.cfg`.

| Setting | Default | Meaning |
|---|---|---|
| `Server.RconPassword "…"` | empty | RCON password |
| `Server.GamePort` | 11774 (UDP) | game traffic |
| `Server.Port` | 11775 (TCP) | HTTP status server |
| `Game.RconPort` | 11776 (TCP) | RCON WebSocket |
| `Server.SignalServerPort` | 11777 (TCP) | voice signalling WebSocket (also answers `dew-rcon`, different password) |
| `Server.FileServerPort` | 11778 (TCP) | mod file server, only with `Server.FileServerEnabled 1` |
| `Server.SendChatToRconClients`, `Server.RconConsoleOutputEnabled` | 0 | must stay 0 for LANPilot (see below) |

- Two instances on one host must use different ports for all of these. With two instances
  on the defaults, the second one's HTTP server (11776) took the first one's RCON port, so the
  first instance had no RCON at all. Check `GET /` on each port when RCON misbehaves.
- The signalling server on 11777 also accepts the `dew-rcon` subprotocol and answers every
  message with `bad password`. It is not the RCON port. Its password is shown by
  `Server.WebsocketInfo`.

In LANPilot the form asks for host, RCON port (11776), status port (11775) and the RCON
password.

## Status: HTTP `GET /` on `Server.Port`

Plain HTTP, no password, `Content-Type: application/json`, `Server: ElDewrito/0.7.1`,
`Connection: close`.

```json
{"name":"HaloOnline Server","port":11774,"fileServerPort":11778,"hostPlayer":"…",
 "sprintState":"2","sprintUnlimitedEnabled":"0","dualWielding":"1","assassinationEnabled":"0",
 "voteSystemType":0,"teams":false,"map":"Guardian","mapFile":"guardian",
 "variant":"none","variantType":"none","status":"InLobby","numPlayers":0,"maxPlayers":16,
 "modCount":0,"modPackageName":"","modPackageAuthor":"","modPackageHash":"",
 "modPackageVersion":"","xnkid":"…","xnaddr":"…","players":[],"isDedicated":true,
 "gameVersion":"1.106708_cert_ms23___release","eldewritoVersion":"0.7.1"}
```

- `port` is the game port, not the HTTP port.
- `map` is the display name ("Last Resort"), `mapFile` the internal name (`zanzibar`).
- `variant`/`variantType` stayed `none` in the lobby even after `Game.GameType` loaded a
  variant. By the 0.6 source they carry the variant's name and base type while a game runs.
- `status` was `InLobby` throughout; the 0.6 source also knows `Loading` and `InGame`.
- **Join password** (`Server.Password`): measured by setting one over RCON. Without
  credentials the answer has `"passworded": true` and no `xnkid`, `xnaddr`, mod fields or
  `players`; with HTTP Basic credentials `dorito:<join password>` it is complete. The join
  password can be read over RCON (`Server.Password` prints it).
- Without a join password the answer is cached for `Server.Http.CacheTime` seconds
  (default 5); the 0.6 source does not cache it when a join password is set.
- Players, by the 0.6 source: `name`, `serviceTag`, `team` (index), `uid` (16 hex digits),
  `primaryColor`, `isAlive`, `score`, `kills`, `assists`, `deaths`, `betrayals`,
  `timeSpentAlive`, `suicides`, `bestStreak`. No address and no ping.

## RCON: WebSocket with subprotocol `dew-rcon`

- Connect to `ws://<host>:<Game.RconPort>/` with `Sec-WebSocket-Protocol: dew-rcon`
  (server: `WebSocket++/0.8.2`). Without the subprotocol the handshake fails with HTTP 400.
- The first text frame is the password. The server answers `accept`, or `deny` and closes the
  connection with code 1008.
- Every further text frame is one console command; the server answers with one text frame.
  Commands that print nothing (for example `Server.Say`) are answered with an empty frame.
  Replies arrived within 0.2 s.
- Two connections at the same time both worked.
- Unknown commands: `Command/Variable not found`.
- With `Server.RconConsoleOutputEnabled 1` or `Server.SendChatToRconClients 1` the server
  pushes console output or chat over the connection unasked (both off by default, not tried).
  Frames carry no request id, so LANPilot would take such a frame for the answer to its
  command.

### Argument parsing

- One frame is one command; `;` does **not** separate commands (`Server.MaxPlayers;
  Server.Name` → `Command/Variable not found`).
- Arguments are split on spaces; double quotes group them. Unquoted
  `Server.Name HaloOnline Server` sets the name to `HaloOnline`.
- Text right after a closing quote is glued to the argument: `Server.Name "x"; Server.Name`
  set the name to `x;`. An unclosed quote is accepted (`Game.Map "guardian` loads the map).
- Setting a variable answers `<old> -> <new>`.

LANPilot therefore quotes map and game type names and only sends names from fixed patterns
(maps `[A-Za-z0-9_]`, game types from its own list, player slots `0`–`15`).

### Secrets in command output

`help` lists every variable with its value, **including `Server.RconPassword` in clear text**,
and `Server.WebsocketInfo` prints the signalling password. LANPilot's console replaces the RCON
password in every answer with `[redacted]`.

## Commands

| Purpose | Command | Reply (measured unless marked) |
|---|---|---|
| Version | `Game.Version` | `0.7.1` |
| Server name | `Server.Name` | `HaloOnline Server` |
| Lobby or game | `Game.Info` | many lines, among them `Current Map: mainmenu` in the lobby |
| Maps | `Game.ListMaps` | `bunkerworld,chill,cyberdyne,deadlock,guardian,riverworld,s3d_avalanche,s3d_edge,s3d_reactor,s3d_turf,shrine,zanzibar` |
| Load map | `Game.Map "<name>"` | `Loading built-in map maps\<name>.map...\nMap variant loaded successfully!` / `Invalid map!` |
| Load game type | `Game.GameType "<name>"` | `Loading built-in game variant <name>...\nGame variant loaded successfully!` / `…\nInvalid game variant name!` |
| Start | `Game.Start` | `Starting game...`; see below |
| Start countdown | `Game.StartCountdown` | `failed to start game` without players |
| Stop | `Game.Stop` (back to lobby) | `Stopping game...` (also in the lobby) |
| End | `Game.End`, `Game.EndRound` | `Ending game...`, `Unable to end the round!` in the lobby |
| Chat | `Server.Say "<text>"`, `Server.PM "<player>" "<text>"` | empty frame |
| Players | `Server.ListPlayers` | empty without players; with players, by the 0.6 source, one line each: `[<slot>] "<name>" (uid: <16 hex>, ip: <address>:<port>)` |
| Kick | `Server.KickIndex <slot>` | `Failed to kick player ` for an empty slot; by the 0.6 source `Issued kick request for player <name> (<slot>)` |
| Ban | `Server.KickBanIndex <slot>` (IP ban and kick) | `Failed to kick player ` for an empty slot; by the 0.6 source `Added IP <ip> to the ban list` and the kick line |
| Kick by name or UID | `Server.KickPlayer "<name>"`, `Server.KickUid <uid>` | `Player "<name>" not found.`, `Player with UID <uid> not found.` |
| Join password | `Server.Password` | the password, empty without one |
| Other | `Server.KickTempBan…`, `Server.AddBan`, `Server.Unban`, `Server.ListTempBans`, `Server.ShuffleTeams`, `Server.MutePlayer`, `Voting.SystemType`, `Server.PlayersInfo`, `Server.PlayersPingInfo` | not used |

- **`Game.Start` without players resets the lobby.** It answers `Starting game...`, cannot
  start, and within about three seconds the lobby is back on the default map (`guardian`),
  so a map set before is lost. A map loaded with `Game.Map` and no `Game.Start` stays in the
  lobby (seen for minutes).
- `Server.KickUid` removes every player with that UID. Players on a LAN often share one: the
  UID comes from a key in the player's preferences, which a copied game folder copies too.

## How the adapter uses it

| Capability | How |
|---|---|
| Status | `GET /` on the status port: name, players, max players, map (`mapFile` with `map` as its name), game type (`variant`, absent in the lobby), join password flag |
| Map list | `Game.ListMaps` once per built-in game variant (12 × 32 entries), the game type in `MapRef.extra["gametype"]` |
| Map change | `Game.Info`; in a game `Game.Stop` and `Game.Info` every 0.5 s until the lobby shows (at most 20 s); `Game.GameType "<variant>"`, `Game.Map "<map>"`; `Game.Start` only if `Server.ListPlayers` lists somebody |
| Hard restart | the same without loading anything; no soft restart |
| Players | `Server.ListPlayers` for slot, name and address; score and team (Red, Blue, Green, Orange, Purple, Gold, Brown, Pink by index, only when `teams` is true) from the status server, matched by UID, or by name when a UID is shared; with a join password the status server is asked with it (read by `Server.Password`) |
| Kick, ban | `Server.ListPlayers` again, then `Server.KickIndex <slot>` / `Server.KickBanIndex <slot>` only if the slot still holds a player of that name; no reason (ElDewrito takes none) |
| Console | the command as it is; the RCON password is replaced in the answer |
| LAN scan | the server browser's query to UDP 11774 (see below), then the status server on the answering port + 1 |

Measured with the adapter against the server: status, map list, map change (Narrows with Team
Oddball, Last Resort with Team Slayer, also through the web interface), hard restart in the
empty lobby, the console with `help` (password replaced), and a wrong password (refused once,
then not sent again), and the LAN scan from the development instance over a /23 network
(found the server, "Add" pre-filled host, name and status port).

### LAN discovery

The game's server browser finds LAN servers with a UDP broadcast to port 11774 (the default
game port). The query is 23 bytes:

```
01 62 6c 61 6d 00 00 00 09 81 00 02 00 01 2d c3 04 93 dc 05 d9 95 40   (01 "blam" ...)
```

It is the one the Discord-Gameserver-Notifier uses (from the `opengsq` package, which LANPilot
already depends on). Measured on 0.7.1:

- The server answers from its game port (11774) with 160 bytes that also start with
  `01 blam`; the answer contains the variant and map names in Blam's bit-packed encoding,
  which LANPilot does not decode (it reads the status server instead).
- It answers to any source port; the DGN sends from 11774, which is not needed. Unicast to
  the server and broadcast to the network's broadcast address both got an answer.
- A broadcast also reaches the sending host: LANPilot's own 23-byte query came back, so
  answers shorter than 121 bytes are dropped.
- The answer does not name the status or RCON port. LANPilot assumes the status server on the
  game port + 1 (the default layout) and accepts it only if the JSON has `eldewritoVersion`;
  the RCON port stays at the form's default.

### Map names

`Game.ListMaps` returns the internal names; the display names come from the HTTP status.
Every map was loaded with `Game.Map` and its name read from the status
(`src/lanpilot/catalog/eldewrito.toml`):

| Internal | Display |
|---|---|
| `bunkerworld` | Standoff |
| `chill` | Narrows |
| `cyberdyne` | The Pit |
| `deadlock` | High Ground |
| `guardian` | Guardian |
| `riverworld` | Valhalla |
| `s3d_avalanche` | Diamondback |
| `s3d_edge` | Edge |
| `s3d_reactor` | Reactor |
| `s3d_turf` | Icebox |
| `shrine` | Sandtrap |
| `zanzibar` | Last Resort |

### Game types

Game type names are **case-sensitive** (`Slayer` loads, `slayer` does not) and there is no
command that lists them. All 32 built-in variants loaded:

Slayer, Team Slayer, Rockets, Elimination, Duel, Oddball, Team Oddball, Lowball, Ninjaball,
Rocketball, Multi Flag, One Flag, Tank Flag, Attrition CTF, Assault, Neutral Bomb, One Bomb,
Attrition Bomb, Infection, Save One Bullet, Alpha Zombie, Hide and Seek, Crazy King,
Team King, Mosh Pit, Juggernaut, Mad Dash, Ninjanaut, VIP, One Sided VIP, Escort,
Influential VIP.

Custom variants and Forge maps are loaded by name with the same commands; LANPilot does not
offer them (not tried).

## Open

To be checked with a player on the server:

- Whether `Game.Start` with players starts the game with the variant and map set in the lobby,
  and how long `Game.Stop` takes to reach the lobby.
- `status` and `variant` in the status server while a game runs.
- Servers on another game port than 11774: whether they answer the scan.
- The format of `Server.ListPlayers` with players, kick and ban (taken from the 0.6 source),
  and the team colours beyond red and blue.
