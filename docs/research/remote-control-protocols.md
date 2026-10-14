# Research: remote-control protocols

_Collected 2026-10-13 from public sources. This is a starting point, not a specification._

Each fact carries one of these markers:

- **[verified]** – confirmed by a fetched source during the research.
- **[background]** – commonly known, but not confirmed by a source in this research.
- **[unverified]** – could not be confirmed; must be measured against a real server.

Facts confirmed against real servers are moved to `docs/games/<game>.md` in the step that
implements the game.

## Library situation

| Protocol | Usable library | Decision |
|---|---|---|
| Status queries | `opengsq` (MIT, async) [verified] | use it |
| Source RCON | `opengsq.rcon_protocols.source_rcon` (MIT, async) [verified] | not used: returns empty answers on CS2 (measured, see `games/counter-strike.md`); own transport |
| Source RCON | `rcon` on PyPI (GPL-3.0) [verified] | not usable (license) |
| GoldSrc RCON | none found | own transport, about 100 lines |
| GBXRemote | PyPlanet (GPL-3.0) [verified]; `trackmania-nations-forever-client` (largely untested by its own README) [verified] | own transport, about 150–200 lines |
| UWeb WebAdmin | none found | own transport with `httpx` |
| Quake 3 RCON | none needed | own transport, about 60 lines |

Reference implementations in other languages: GbxRemote.Net (Apache-2.0), `@evotm/gbxclient`
(MIT), `rconed` (Java, GoldSrc).

## Counter-Strike 1.6 (GoldSrc)

- UDP on the game port (default 27015); the password is sent in clear text in every command.
- Every datagram is `FF FF FF FF` + ASCII text + `00`. [verified]
- Challenge request: `challenge rcon\n`; the reply contains `challenge rcon <number>`. [verified]
- Command: `rcon <challenge> "<password>" <command>`. [verified]
- A reference client waits up to 2 s for the first reply and then 300 ms for more. [verified]
- Large replies arrive as split packets with header `FE FF FF FF` and must be reassembled;
  relevant for `maps *`. [background]
- Commands [background]: `maps *` (list), `changelevel <map>`, `sv_restart 1` /
  `sv_restartround 1` (soft), `restart` (hard), `quit`, `status`, `kick #<userid>`, `stats`.
- The server bans an IP after repeated wrong passwords (`sv_rcon_maxfailures` and related
  cvars). [background]

Sources: <https://rconed.sourceforge.net/xref/net/sourceforge/rconed/Rcon.html>,
<https://cdn.jsdelivr.net/npm/rcon@1.1.0/README.md>

## Counter-Strike: Source, CS:GO, CS2 (Source RCON)

- TCP on the game port (default 27015), little-endian packets: size, id, type, body, two
  null bytes. Types: AUTH = 3, AUTH_RESPONSE = 2, EXECCOMMAND = 2, RESPONSE_VALUE = 0; a
  failed login answers with id -1. [verified via secondary sources]
- Responses larger than 4096 bytes are split. The usual technique: send an empty packet right
  after the command and collect bodies until its id comes back. [verified]
- Commands [background]: `maps *`, `changelevel <map>`, `mp_restartgame 1` (soft),
  `changelevel <current map>` (hard), `status`, `kickid <userid>`, `stats`.
- **CS2**: measured on 2026-10-14; the results are in
  [games/counter-strike.md](../games/counter-strike.md). `maps *` works but lists many
  non-maps, `stats` prints nothing, answers carry wrong ids.

Sources: <https://developer.valvesoftware.com/wiki/Source_RCON_Protocol> (only excerpts were
reachable), <https://rubydoc.info/gems/rconrb/Rcon/Client>,
<https://help.dathost.net/article/137-cs2-rcon>

## Trackmania (GBXRemote / XML-RPC)

- TrackMania Forever was measured on 2026-10-14; the results are in
  [games/trackmania.md](../games/trackmania.md). The points below remain for the other
  versions.
- TCP, default port 5000. On connect the server sends a 4-byte length (11) and the protocol
  name `GBXRemote 1` or `GBXRemote 2`. [verified via secondary source]
- GBXRemote 2: every call and response is prefixed by two unsigned 32-bit integers, length
  and handle. GBXRemote 1 has only the length. The payload is an XML-RPC `methodCall`. [verified
  via secondary source]
- The client chooses the handle; calls use handles with the high bit set (`0x80000001` …),
  callbacks lower ones. [verified for calls on TMNF]
- `Authenticate(login, password)` with login `SuperAdmin`, `Admin` or `User`. [background]
- A running server lists its own methods through `system.listMethods`. The dedicated server
  download also ships a `ListMethods.html`. [background]

| Capability | TMF ("Challenge") | TM2 / TM2020 ("Map") |
|---|---|---|
| Playlist | `GetChallengeList` | `GetMapList` |
| Current map | `GetCurrentChallengeInfo` | `GetCurrentMapInfo` |
| Jump to a map | `ChooseNextChallenge` + `NextChallenge` | `JumpToMapIndex`, `JumpToMapIdent` |
| Add to playlist | `AddChallenge`, `InsertChallenge` | `AddMap`, `InsertMap` |
| Soft restart | `RestartChallenge` | `RestartMap` |
| Players | `GetPlayerList`, `Kick`, `Ban` | same |
| Stop | `StopServer` | same |
| Statistics | `GetNetworkStats`, `GetSystemInfo` | same |

Method names are [verified] from a published method list; parameters are not.

- **No method was found that lists track files on disk.** The protocol only returns the
  playlist; `InsertChallenge`/`AddChallenge` take a path relative to the tracks directory.
  LANPilot therefore combines the playlist with manually maintained entries.
- Map info contains a `UId` that can be used for TMX lookups. [background]
- TrackMania Original, Sunrise and Nations ESWC share one dedicated server binary with
  GBXRemote 2 remote control; Sunrise eXtreme and Nations ESWC were measured on 2026-10-14
  (see [games/trackmania.md](../games/trackmania.md)).

Sources: <https://github.com/ManiaDotNet/ServerController>,
<https://github.com/ManiaDotNet/ServerController/blob/master/RPC-Method-List.md>,
<https://github.com/EvoEsports/GbxRemote.Net>, <https://github.com/EvoTM/gbxclient-node>

## Unreal Tournament (UWeb WebAdmin)

The WebAdmin is an HTTP server built into the game server. Passwords travel unencrypted.

- **UT99:** enabled in the server ini under `[UWeb.WebServer]` (`bEnabled`, `ListenPort`);
  the application `UTServerAdmin.UTServerAdmin` is mounted at `/ServerAdmin`; credentials in
  `[UTServerAdmin.UTServerAdmin]`. [verified]
- **UT2003/UT2004:** same mechanism with `xWebAdmin.UTServerAdmin` at `/ServerAdmin`; the
  default port is documented inconsistently (80, 8075, 8080). [verified] Console commands of
  the game include `Map`, `RestartMap`, `Switch <url>`, `Kick`, `KickBan`. [verified]
- **UT3:** `http://host:port/ServerAdmin`; with `bHttpAuth=False` a login form is used, with
  `bHttpAuth=True` HTTP basic auth. [verified via secondary source]
- UT99, UT2004 and UT3 were measured on 2026-10-14: page URLs, form fields, map lists and
  authentication are in [games/unreal-tournament.md](../games/unreal-tournament.md). The
  map dropdown of `current_game` lists only the rotation; `defaults_maps` lists all maps.
- A known old UT2004 bug: empty map dropdown and "Invalid map index". [verified]
- Alternative for UT2004: the MIT-licensed mutator "RestAdmin" adds an HTTP/JSON API
  (<https://git.fuxle.net/fuxle/UT2004REST>). It requires installing a mutator on the server,
  so it is an optional path at most.

Sources: <https://unrealarchive.org/wikis/unreal-wiki/UE2:Console_Commands_(UT2004).html>,
<https://icculus.org/pipermail/ut2004/2004-March/000282.html>,
<https://www.gamehostbros.com/guides/games/unreal-tournament-3/web-admin>

## Quake 3 engine family

- UDP on the game port: `FF FF FF FF rcon <password> <command>`. [background]
- Call of Duty 4: `map`, `map_rotate`, `map_restart`, `fast_restart`; `map_rotate` loads the
  next map from `sv_mapRotation`. [verified]
- Flood protection exists (`sv_floodProtect`); the exact interval is [unverified].
- **Listing installed maps remotely is [unverified]** (`dir maps bsp`, `fdir`, `dir usermaps`).
  Plan with a bundled catalogue plus the rotation plus manual entries.

Sources: <https://www.4netplayers.com/de/wiki/call-of-duty-4/rcon-commands/>,
<https://github.com/arbytez/cod4-rcon-commands>

## Other games

| Game | Remote control | State |
|---|---|---|
| Battlefield 2 | RCON over TCP 4711 with a digest seed challenge; `admin.runNextLevel`, `admin.restartMap` | partly verified |
| ElDewrito | WebSocket RCON, default port 11776 | port verified |
| Renegade X | own RCON over TCP | unverified |
| Halo: Combat Evolved | RCON inside the game protocol, or SAPP | unverified |
| Toxikk, Serious Sam Classic, Alien vs Predator 2, F.E.A.R. 2, Supreme Commander, Age of Empires 1/2, Stronghold Crusader, Warcraft III, C&C Generals, FlatOut 2 | none known | "none known" means none was found, not that none exists |

All of these can be queried for status through `opengsq`.
