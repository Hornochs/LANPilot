# Counter-Strike

| Game | Adapter | State |
|---|---|---|
| Counter-Strike 1.6 (GoldSrc, HLDS) | `cs16` | implemented, verified against a real server |
| Counter-Strike: Source, CS:GO, CS2 (Source RCON) | – | planned (roadmap step 2.3) |

## Counter-Strike 1.6

Measured on 2026-10-13 against a Linux HLDS: `version 48/1.1.2.7/Stdio 10211 secure`,
Counter-Strike 1.6 installed through LinuxGSM. Everything below was observed on that
server unless marked otherwise.

### Setting up the server

- Set `rcon_password "<password>"` in the server's configuration (`cstrike/server.cfg`; with
  LinuxGSM `cstrike/<instance>.cfg`). Without it RCON is off.
- The password must not contain a double quote or a line break; the protocol cannot carry
  them.
- LANPilot needs to reach the game port (default UDP 27015). There is no separate RCON port.

### Status

The status comes from the standard server query (A2S_INFO, through `opengsq`), which needs
no password: server name, map, players, maximum players, and whether a join password is set.
A wrong RCON password therefore never makes a server look offline.

### RCON protocol

- UDP on the game port. Every datagram starts with `FF FF FF FF`.
- `challenge rcon\n` → `challenge rcon 283211007\n\0`. The number stayed the same for the
  whole session. This server also executed commands sent with a **wrong** challenge number,
  so the challenge is no protection worth relying on.
- A command is `rcon <challenge> "<password>" <command>\n`. The password is sent in clear
  text with every command, and the server writes every RCON command into its log.
- Output arrives as print packets: `FF FF FF FF 6C` + text + `00 00`.
- **Long output comes in two forms, and both occur.** The server flushes its print buffer at
  about 1395 bytes, so a long answer is several print packets in a row (`cmdlist`: 2,
  a map change: 4). A single print packet that exceeds 1400 bytes is cut into fragments:
  `FE FF FF FF` + 4-byte id + 1 byte (`index << 4 | count`) + data (`cvarlist` started with a
  packet in two fragments, followed by five ordinary print packets).
- There is no end marker. LANPilot treats the answer as complete after 0.3 s of silence.
- An unknown command prints nothing: the answer is `FF FF FF FF 6C 00 00`.
- A wrong password prints `Bad rcon_password.`

### Commands

| Purpose | Command | Answer |
|---|---|---|
| List maps | `maps *` | a line `-------------`, then one `<name>.bsp` per line; **every map is listed twice** |
| Change map | `changelevel <map>` | on success several packets of log lines (`Loading map "<map>"` …); unknown map: `changelevel failed: '<map>' not found on server.` |
| Restart the round | `sv_restart 1` | `L <date>: Server cvar "sv_restart" = "1"`; scores are reset, players stay |
| Reload the level | `restart` | the same log lines as a map change; the current map is loaded again |
| Load | `stats` | header `CPU In Out Uptime Users FPS Players` and one line of values |
| Players | `status` | header lines, then one line per player (`#  name userid uniqueid frag time ping loss adr`) |

- `maps *` listed only the maps in `cstrike/maps`, not those of the Half-Life base game.
- `changelevel de_dust2; echo x` was answered with `changelevel failed: 'de_dust2;' not
  found`, so a semicolon did not start a second command here. LANPilot still refuses any
  map name outside `[A-Za-z0-9_-]{1,64}` before sending it.
- After `changelevel` and `restart` the server needed a few seconds before it answered
  status queries again.

### What the adapter does with it

- `query()` asks the status query, and `stats` over RCON at most once a minute, because
  every RCON command lands in the server's log. If RCON fails, the status is still returned,
  without load values.
- **After the server has refused the password once, the adapter stops sending it.** HLDS bans
  an address that keeps sending wrong passwords (`sv_rcon_maxfailures`, 10 by default; not
  provoked on the test server). The adapter reports the refusal until the server entry is
  edited, which creates a fresh adapter.
- The map list is deduplicated and filtered through the map name pattern.

### Not verified yet

- Player list, kick and ban (roadmap phase 5).
- Behaviour with players connected, and with a join password set.
- Servers older than build 10211 and Windows servers.
- What exactly happens when `sv_rcon_maxfailures` is reached.

### Simulator

`lanpilot.testing.goldsrc.GoldSrcSimulator` answers the status query and RCON the way the
measured server did, including both forms of long output. The adapter's tests run against
it.
