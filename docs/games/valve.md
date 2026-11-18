# GoldSrc and Source games beyond Counter-Strike

Half-Life Deathmatch, Team Fortress Classic, Day of Defeat, Opposing Force and
Counter-Strike: Condition Zero (GoldSrc), Day of Defeat: Source, Half-Life 2: Deathmatch
and Team Fortress 2 (Source).

These games run on the same engines as Counter-Strike and speak the same protocols: the
A2S status query, GoldSrc RCON over UDP or Source RCON over TCP, and the engine commands
`maps *`, `changelevel`, `restart`, `status`, `stats`, `kick`/`kickid`, `addip` and
`writeip`. Everything [counter-strike.md](counter-strike.md) says about the engines holds
for them; this file lists only what was measured per game and where the games differ.

Measured on 2026-10-22 on the development host: the GoldSrc games from one HLDS install
(app 90, build 10211, the five mods installed with `app_set_config 90 mod <folder>`), DoD:S
(app 232290), HL2:DM (app 232370) and TF2 (app 232250, `srcds_run_64`, build 11138802),
each started with `+sv_lan 1 +rcon_password <…>` on port 27016. None of them had human
players; the bots of Condition Zero stay out until a human joins, as on CS 1.6, TF2's bots
were used for the player list and the kick. TF2 was also added and used through the web
interface of the development instance.

## Adapters

| Adapter id | Game | Engine | Soft restart | Images (GameTracker, GameBanana id) |
|---|---|---|---|---|
| `hldm` | Half-Life Deathmatch | GoldSrc | – | `hl`, 34 |
| `tfc` | Team Fortress Classic | GoldSrc | – | `tfc`, 284 |
| `dod` | Day of Defeat | GoldSrc | `mp_clan_restartround 1` | `dod`, 11 |
| `opfor` | Half-Life: Opposing Force | GoldSrc | – | –, 262 |
| `czero` | Counter-Strike: Condition Zero | GoldSrc | `sv_restart 1` | `czero`, 7 |
| `dods` | Day of Defeat: Source | Source | `mp_restartgame 1` | `dods`, 10 |
| `hl2dm` | Half-Life 2: Deathmatch | Source | `mp_restartgame 1` | `hl2dm`, 5 |
| `tf2` | Team Fortress 2 | Source | `mp_restartgame 1` | `tf2`, 297 |

The hard restart is `restart` on GoldSrc and a `changelevel` to the current map on Source,
as for Counter-Strike. Every adapter has the map list, map change, players with kick and ban
by IP address, load values from `stats`, the console and the LAN scan.

Code: `games/valve/goldsrc.py` and `games/valve/source.py` hold the engine code (moved there
from `games/counterstrike/`), `games/valve/halflife.py` and `games/valve/sourcegames.py` the
games, `games/valve/common.py` the status query, the LAN scan and the player control.

## Status query (A2S_INFO)

| Game | folder | game | app id | version | protocol |
|---|---|---|---|---|---|
| Half-Life Deathmatch | `valve` | `Half-Life` | 70 | `1.1.2.2/Stdio` | 48 |
| Team Fortress Classic | `tfc` | `Team Fortress Classic` | 20 | `1.1.2.2/Stdio` | 48 |
| Day of Defeat | `dod` | `Day of Defeat` | 30 | `1.1.2.6/Stdio` | 48 |
| Opposing Force | `gearbox` | `Opposing Force` | 50 | `1.1.2.2/Stdio` | 48 |
| Condition Zero | `czero` | `Condition Zero` | 80 | `1.0.0.3/Stdio` | 48 |
| Day of Defeat: Source | `dod` | `Day of Defeat` | 300 | `11003710` | 17 |
| Half-Life 2: Deathmatch | `hl2mp` | `Half-Life 2 Deathmatch` | 320 | `10889068` | 17 |
| Team Fortress 2 | `tf` | `Team Fortress` | 440 | `11138802` | 17 |

The LAN scan tells the games apart by app id; a GoldSrc answer must also carry the game's
folder, and an answer without app id (the old GoldSrc layout) is classified by folder alone
(`classify()` in `games/valve/common.py`). All GoldSrc and Source adapters of one scan share
one broadcast.

**The current Source servers (DoD:S, HL2:DM, TF2) do not answer the status query from
127.0.0.1.** From the host's LAN address they answer as usual (first with a challenge,
`FF FF FF FF 41 …`, which the A2S client handles). RCON over TCP answers on both. A server
running on the LANPilot host must therefore be added with the host's LAN address, not with
`127.0.0.1`. While the Steam libraries are still starting, `status` already answers and
shows `udp/ip  : ?.?.?.?:?`; the status query only answers once the log says `Assigned
anonymous gameserver Steam ID`.

## GoldSrc games

- `maps *` lists every map twice (once per search path), names with `.bsp`, as on CS 1.6.
  Half-Life Deathmatch lists the single-player maps of the game (`c1a0` and so on) too:
  they are on the server and could be loaded.
- `changelevel <map>` answers with the `…_unload.cfg`/`…_load.cfg` lines and changes at
  once; an unknown map answers `changelevel failed: '<map>' not found on server.`
- `restart` reloads the level; `stats` and `status` print the engine formats of CS 1.6.
- `kick #<userid>` for an unknown user id prints nothing.
- Round restart: only Condition Zero has `sv_restart` (as on CS 1.6). Half-Life, TFC and
  Opposing Force have neither `sv_restart` nor any other round restart (`cvarlist
  sv_restart` lists none, `cvarlist mp_` neither), so their adapters offer only the hard
  restart. Day of Defeat has `mp_clan_restartround`; on a server without players the value
  stays at 1, also across a map change, because no round runs. The restart itself still
  has to be seen with players.
- HLDS writes every RCON command to its console and log **with the password**
  (`rcon <challenge> "<password>" <command>`). Whoever can read the server's console or log
  can read the password.

## Source games

DoD:S and HL2:DM answer RCON exactly like CS:Source (the same engine build 11003710 for
DoD:S): `maps *` prints `PENDING:   (fs) <map>` without `.bsp`, `changelevel` to a known map
prints nothing, an unknown map answers `Failed to find map <map>`, `kickid` for an unknown
id answers `userid "<id>" not found`, `stats` prints the Source 1 columns (`CPU`,
`In_(KB/s)`, … `FPS` …). The `status` header names the player columns `# userid name
uniqueid connected ping loss state  adr`, as on CS:Source.

- `mp_restartgame <seconds>` exists on both ("game will restart in the specified number of
  seconds"); `mp_restartround` exists on HL2:DM, `mp_clan_restartround` on DoD:S. A server
  without players hibernates, and the value then stays set until the server wakes up.
- DoD:S and HL2:DM have no bots (`bot_add` is an unknown command on DoD:S).

### Team Fortress 2

- RCON answers like the other Source games; `status` has an extra `account :` line (`not
  logged in  (No account specified)`) and `tags`, which the parser skips.
- `maps *` lists 238 maps, among them `background01` (the menu background) and
  `itemtest`; they are maps on the server and stay in the list.
- **A map change takes 25 seconds and more** (about 40 s with four bots): the server
  answers neither the status query nor RCON while it loads, so LANPilot shows it as
  "offline" for that time. `changelevel` itself answers at once (empty).
- `mp_restartgame 1` is carried out also on an empty server: the value is back at 0 a few
  seconds later.
- Bots (`tf_bot_add <n>`) join only with `tf_bot_join_after_player 0`, and not on maps
  without a navigation mesh (`pl_badwater` on the test server). In `status` they read `#
  <userid> "<name>" BOT active`, as on CS:Source; LANPilot lists them as bots, kicks them
  (`kickid`; with `tf_bot_quota` set, the server adds a new bot at once) and refuses to ban
  them (no IP address).

## Open

- Players: the player list, kick and ban follow the engine formats measured with CS 1.6
  and CS:Source; none of these servers had a player.
- The round restarts of Day of Defeat, DoD:S and HL2:DM with players.
- TF2 workshop maps (`workshop/<id>`) are not handled.
