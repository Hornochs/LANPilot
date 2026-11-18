# Quake 3 engine family: Quake III Arena and Call of Duty

| Game | Adapter | State |
|---|---|---|
| Quake III Arena (1.32c; ioquake3 and similar should behave alike) | `q3a` | implemented, verified against a real server |
| Call of Duty (1.5) | `cod` | implemented, verified against a real server |
| Call of Duty: United Offensive (1.51) | `coduo` | implemented, verified against a real server |
| Call of Duty 2 (1.3) | `cod2` | implemented, verified against a real server |
| Call of Duty 4: Modern Warfare (CoD4X 1.8) | `cod4` | implemented, verified against a real server |
| Call of Duty: World at War (1.7) | `codwaw` | implemented, verified against a real server |
| Jedi Knight | – | see [Not implemented](#not-implemented) |

All of them speak the out-of-band UDP protocol of id Tech 3 on the game port: a status query
without password (`getstatus`) and RCON. The commands, the output of `status` and the way a
server lists its maps differ per game; the adapters share everything else
(`games/quake3/common.py`).

Measured on 2026-10-21 against servers installed through LinuxGSM on Linux:

- Quake III Arena 1.32c (`Q3 1.32c linux-i386 May  8 2006`, protocol 68),
- Call of Duty 1.5 (`cod_lnxded`, protocol 6),
- Call of Duty 4 with CoD4X 1.8 (`CoD4 X - linux-i386 build 1154 May  1 2022`, protocol
  21/6). LinuxGSM installs CoD4X instead of the stock 1.7 server, and so do most LAN
  parties.
- Call of Duty: World at War 1.7 (`codwaw_lnxded`, protocol 101; LinuxGSM's package).

Everything below was observed on these servers unless marked otherwise.

## Setting up the server

| Game | Port (UDP) | RCON password | Config (LinuxGSM) |
|---|---|---|---|
| Quake III Arena | 27960 | `set rconpassword "…"` | `serverfiles/baseq3/q3server.cfg` |
| Call of Duty | 28960 | `set rconpassword "…"` | `serverfiles/main/codserver.cfg` |
| Call of Duty 4 | 28960 | `set rcon_password "…"` | `serverfiles/main/cod4server.cfg` |
| World at War | 28960 | `set rcon_password "…"` | `serverfiles/main/codwawserver.cfg` |

- LinuxGSM generates a password (`admin` + 8 characters). LANPilot needs it without spaces
  or quotes: the engines take the password as one word.
- LinuxGSM starts the servers with `+set dedicated 2` (internet: heartbeats to the master
  servers). On a LAN `dedicated 1` is enough; both answer LANPilot alike.
- **Call of Duty 1 needs `libstdc++.so.5` (32-bit)**, which Debian 13 no longer packages.
  On the test machine it came from Ubuntu's `libstdc++5_3.3.6-30ubuntu2_i386.deb`
  (checksum compared with Ubuntu's package index), unpacked into a folder of the server's
  own and passed with `LD_LIBRARY_PATH` to `./codserver start`.
- **World at War wants `rcon_password`; LinuxGSM's config template sets `rconpassword`**,
  which WaW ignores: every RCON command is then answered with `The server must set
  'rcon_password' for clients to use 'rcon'.` Rename the line.
- The WaW package is 6.5 GB compressed, about 2 GB of it single-player videos
  (`main/video/*.bik`) that a dedicated server does not need; on the test machine it was
  unpacked while downloading, without them (6.7 GB on disk).
- Call of Duty 1, CoD4 and WaW use the same default port; for the test they ran side by
  side with CoD 1 on 28962 and WaW on 28964.

## Status: `getstatus`

`FF FF FF FF getstatus\n` is answered with one datagram, without password:

```
FF FF FF FF statusResponse\n
\sv_hostname\LinuxGSM\mapname\q3dm17\g_gametype\0\sv_maxclients\16\g_needpass\0\…\n
0 0 "Sarge"\n
<score> <ping> "<name>"\n …
```

| Key | Quake 3 | CoD 1 | CoD4X | WaW |
|---|---|---|---|---|
| server name | `sv_hostname` (with colour codes `^1`…) | same | same | same |
| map | `mapname` | same | same | same |
| game type | `g_gametype` = `0` FFA, `1` Tournament, `3` TDM, `4` CTF (`2` is single player) | `dm`, `tdm`, `sd`, `re`, `bel`, `hq` | `dm`, `war`, `sd`, `sab`, `dom`, `koth` | `dm`, `tdm`, `sd`, `sab`, `dom`, `koth`, `ctf`, `twar` (War) |
| slots | `sv_maxclients` | same | same (32, of which `sv_privateClients` 2) | same |
| password | `g_needpass` `0`/`1` | `pswrd` `0`/`1` | `pswrd\1` **only when a password is set**; no key otherwise | `pswrd` `0`/`1` |
| game | `version` `Q3 1.32c …`, `gamename` `baseq3` | `gamename` `Call of Duty`, `shortversion` `1.5` | `gamename` `Call of Duty 4`, `version` `CoD4 X …` | `gamename` `Call of Duty: World at War`, `shortversion` `1` |

- Bots are listed among the players (`0 0 "Sarge"`), so the player count includes them.
- `getinfo <challenge>` answers `infoResponse` with fewer keys (`clients`, `hostname`,
  `gametype`; CoD4X also `pswrd`). LANPilot uses `getstatus`, which has everything.
- Colour codes: `^` followed by any character other than `^`; LANPilot removes them from
  names, as the engine's own `Q_CleanStr` does.

## RCON

- Request: `FF FF FF FF rcon "<password>" <command>\n`. The quoted password works on all
  three. **One command per packet**: `echo a; echo b` printed `a; echo b`, and a line break
  ends the command (`echo a\necho b` printed `a`).
- Answer: `FF FF FF FF print\n` + text, in packets of up to about 1000 bytes (`cvarlist`
  on Quake 3: six packets within 4 ms). There is no end marker; a map change printed three
  packets over 360 ms with gaps of about 100 ms. LANPilot waits until 300 ms pass without
  another packet.
- The console log of the command is part of the answer (`addbot` prints the bot's skill
  files; a map change prints the whole level load, including local file paths).
- **Wrong password**: `Bad rconpassword.` (Quake 3, CoD 1), `Bad rcon` (CoD4X),
  `Invalid password.` (WaW). No lockout
  was observed from localhost after eight wrong attempts in a row on CoD4X. LANPilot does not
  send a refused password again.
- **Rate limit: Quake 3, Call of Duty 1 and WaW drop every RCON packet that arrives less
  than 500 ms after the previous one, without an answer** – whoever sent it, and also
  packets with a wrong password. Measured with two commands sent 0 to 1 s apart: the second
  was lost up to 450 ms (Quake 3) and 500 ms (CoD 1; WaW in one of two tries), answered
  from 500 ms (Quake 3) and 550 ms (CoD 1, WaW). CoD4X answered every command, even sent
  at the same moment. LANPilot keeps 700 ms between its commands to these three; another
  RCON user at the same time can still make a command go unanswered ("The server did not
  answer").

## Maps and game types

| | Quake 3 | CoD 1 | CoD4X | WaW |
|---|---|---|---|---|
| list maps | `dir maps bsp`: all `.bsp`, also those inside the pk3 archives (36 on the test server) | `dir maps/mp bsp`: the 16 multiplayer maps (`dir maps bsp` lists the single-player levels) | **impossible**: no `dir` command (73 commands, see `cmdlist`), maps are fast files (`zone/english/mp_*.ff`); `which` finds no fast file | **impossible**: `dir` and `fdir` exist but find no fast file (`0 files listed`) |
| change map | `map <name>` | same | same | same |
| unknown map | `Can't find map maps/<name>.bsp` | `Can't find map maps/mp/<name>.bsp` | `^1Error: Can't find map <name>` + `A mod is required to run custom maps` | `^1Error: Can't find map "<name>".` + `A mod is required for custom maps` |
| change game type | `g_gametype <code>` (answer `g_gametype will be changed upon restarting.`), then `map` | same | same | same; an unknown type becomes `dm` at the next load (`g_gametype bogus is not a valid gametype, defaulting to dm`) |
| rotation | `vstr` chains (`set m1 "map q3dm1; set nextmap vstr m2"`) | `sv_mapRotation` (empty by default) | `sv_mapRotation` `gametype war map mp_backlot …`; `map_rotate` loads the next entry | `sv_mapRotation` `map mp_airfield map mp_asylum …` (all 23 maps in LinuxGSM's config) |
| soft restart | `map_restart` (match restarts, players stay) | `map_restart` (no `fast_restart` in CoD 1) | `fast_restart` (round restarts, level stays) | `fast_restart` |
| hard restart | `map <current>` | `map <current>` | `map_restart` (reloads the level, players stay) | `map_restart` (game type kept) |

- The map lists of `cod4` and `codwaw` are therefore the stock maps from LANPilot's
  catalogue (21 and 23) plus every map named in `sv_mapRotation`, which is where LAN
  parties configure custom maps.
- Every map is listed once per game type, as for Unreal Tournament; a change sets
  `g_gametype` and then loads the map. Map names that go into a command must match
  `[A-Za-z0-9_-]{1,64}`; game types must be one of the adapter's codes.
- **A pending game type**: `g_gametype` is latched until the next level load or
  `map_restart`. Quake 3 and CoD 1 drop a pending value when the running one is set again;
  so does WaW; **CoD4X does not** (`g_gametype sd` while `sd` runs and `dm` is pending left `dm`
  pending, and the next `map_restart` loaded `dm`; `gametype war` too). Setting another
  value replaces the pending one on all three. LANPilot sets the game type with every change,
  and puts the old one back when the map then turns out to be missing; on CoD4X the new one
  stays pending in that case and takes effect with the next restart.
- The cvar answer: `"g_gametype" is:"sd^7" default:"dm^7"` (Quake 3, CoD 1) and
  `"g_gametype" is: "sd^7" default: "dm^7" info: "Current game type^7"` + `latched: "dm"`
  (CoD4X).
- Map names in the catalogue: Quake 3's long names come from the arena scripts in
  `baseq3/pak*.pk3` (`longname`); those scripts also say which game types a map is made for
  (`type "ffa tourney team ctf"`), which LANPilot does not use – the server accepts any
  combination. CoD 1's arena file names its maps only by key; CoD4's names are the ones the
  game shows. WaW's come from the localized strings in its fast files
  (`zone/english/localized_common_mp.ff`, zlib-compressed after a 12-byte header): the
  `PATCH_*` name where the release renamed a map (`mp_kwai` "Banzai", `mp_stalingrad`
  "Corrosion", `mp_docks` "Sub Pens"), otherwise `MPUI_*`.

## Players: `status`

```
map: q3dm1
num score ping name            lastmsg address               qport rate
--- ----- ---- --------------- ------- --------------------- ----- -----
  0     0    0 Sarge^7               100 bot                       0 16384
```

- Columns: Quake 3 `num score ping name lastmsg address qport rate`; CoD 1 and WaW add
  `guid` before the name; CoD4X prints a header block (`hostname:`, `version :`, …, `map     :`) and
  the columns `num score ping playerid steamid name lastmsg address qport rate`. LANPilot
  reads the header to know how many fields come before and after the name (names may
  contain spaces, the other fields never do).
- `ping` is `CNCT` or `ZMBI` while a client connects or leaves (from the engine source,
  not observed). Bots have the address `bot`, ping 0; CoD4X bots have playerid and steamid
  `0`.
- **The slot (`num`) is reused** when a player leaves. LANPilot reads `status` again before a
  kick or ban and acts only if the slot still holds a player of that name.
- Bots to test with: Quake 3 `addbot <name> <skill>`; CoD4X `set scr_testclients <n>`, which
  needs the server started with `+set developer 1 +set developer_script 1`
  (`developer_script is write protected` otherwise). No bots on CoD 1; on WaW none were
  tried, so its player rows are unmeasured (the header was: as CoD 1).
- Human players were not available on any test server; their rows (address `ip:port`) are
  taken from the engine source and the CoD4X column layout.

### Kick and ban

| | Quake 3 | CoD 1 | CoD4X | WaW |
|---|---|---|---|---|
| kick | `clientkick <slot>` → `broadcast: print "Sarge^7 was kicked\n"` | `clientkick <slot>`; unknown slot `Client 3 is not active` | `clientkick <slot> <reason>` → `Player kicked: bot2 ^7id: 0` + `Reason: …`; unknown slot `Player 1 is not on the server` | `clientkick <slot>`; with a reason it prints `Usage: clientkick <client number>` and kicks nobody |
| ban | `addip <ip>` adds to `g_banIPs` (with `g_filterBan 1`, the default), then `clientkick`; `removeip <ip>` → `Removed.` | `banClient`, `banUser`, `tempBanClient` | `permban`, `tempban`, `banUser`, `banClient`, `unban` | `banClient`, `banUser`, `tempBanClient` |

- **LANPilot bans only on Quake 3**, by IP address. The ban lasts until the server restarts
  (the cvar is not saved). CoD 1, CoD4X and WaW ban by player id: on CoD4X, `permban 4 test ban`
  banned the bot with `id: 0` and `IP: bot` (`adminactions.log`), and bots all have id 0.
  LAN players without a key or Steam may share an id the same way, so one ban could lock
  out everyone; until that is measured with real players there is no ban for Call of Duty.
- CoD4X's reasons must not contain `" ; % / \` (its own usage text). LANPilot reduces a
  reason to letters, digits, spaces and `.,!?()_-`, at most 64 characters. Quake 3 and
  CoD 1 take no reason.

## Call of Duty: United Offensive and Call of Duty 2

Measured on 2026-10-21 against LinuxGSM's United Offensive 1.51 (`coduo_lnxded`, protocol
22) and Call of Duty 2 1.3 (`cod2_lnxded`, protocol 118). Both need `libstdc++.so.5` like
CoD 1. They use the protocol, `status` columns (with `guid`) and cvar answers described
above; what differs:

| | United Offensive (`coduo`) | Call of Duty 2 (`cod2`) |
|---|---|---|
| `gamename` | `CoD:United Offensive` (`shortversion` 1.51) | `Call of Duty 2` (`shortversion` 1.3) |
| RCON password | `rconpassword` | **`rcon_password`**: LinuxGSM's template sets `rconpassword`, and every command is then answered `The server must set 'rcon_password' …` |
| wrong password | `Bad rconpassword.` | `Invalid password.` |
| rate limit | 500 ms, as CoD 1 (lost up to 450 ms, answered from 500 ms) | the same |
| list maps | `dir maps/mp bsp`: 27 maps, its own 11 and the 16 of CoD 1 | `dir maps/mp d3dbsp`: 15 maps (`.d3dbsp` files) |
| unknown map | `Can't find map maps/mp/<name>.bsp` | `Can't find map maps/mp/<name>.d3dbsp` |
| game types | CoD 1's plus `bas` (Base Assault), `ctf` (Capture The Flag), `dom` (Domination) | `dm`, `tdm`, `sd`, `hq`, `ctf` |
| restarts | `map_restart` only, as CoD 1 | `fast_restart` and `map_restart`, as CoD4 |
| kick | `clientkick <slot>` | `clientkick <slot>`; with a reason it prints `Usage: clientkick <client number>` |
| pending game type | cleared by setting the running one | the same |

- **A level load answers only when it is done**: `map` was answered after 2.1–2.8 s
  (United Offensive, mp_kursk, mp_carentan, mp_berlin) and 2.2–2.8 s (CoD 2); one change to
  UO's mp_foy took longer than 3 s. LANPilot waits up to 15 s for the first packet of a map
  change or restart (3 s for other commands). Because the command returns after the load,
  the status read right after it is current.
- **A map that is not made for the game type falls back to Deathmatch**: UO loaded
  mp_sicily with Base Assault as `dm` (its arena file lists `dom ctf dm tdm sd re bel hq`
  for mp_sicily; Base Assault only for mp_foy, mp_italy, mp_kursk, mp_ponyri and
  mp_rhinevalley). WaW does the same with an unknown game type. LANPilot reads the status
  after a change and reports such a fallback as an error instead of success.
- Names in the catalogue: UO's maps by the places they show (its strings name them only in
  single-player texts); CoD 2's from `mp/CoD2maps.arena` (`longname` "Toujane, Tunisia").
- No bots on either; player rows are unmeasured.

## Console

The raw console sends the command as it is, one per packet. Long answers arrive complete.

## LAN discovery

- The in-game browsers ask the first four ports from the default one. LANPilot broadcasts
  `getstatus` to UDP 27960–27963 and 28960–28963 once per scan and tells the games apart by
  the answer: `gamename` `Call of Duty`, `CoD:United Offensive`, `Call of Duty 2`,
  `Call of Duty 4` or `Call of Duty: World at War`, otherwise a `version` starting
  with `Q3 ` or `ioq3` for Quake 3 (other id Tech 3 games, such as Enemy Territory, are not
  reported).
- Measured on the development instance over a /23: all servers on the LANPilot host were
  found with name, game type and game port (CoD 1 on 28962; WaW on 28964, outside the
  scanned ports, once 28964 was added for the test).

## Not implemented

- **Jedi Knight II / Jedi Academy**: LinuxGSM installs the engine (`jk2mvded` from JK2MV) but
  not the game's assets (`base/assets0.pk3`), which come only with the game; no test server.
