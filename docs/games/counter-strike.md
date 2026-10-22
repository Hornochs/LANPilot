# Counter-Strike

| Game | Adapter | State |
|---|---|---|
| Counter-Strike 1.6 (GoldSrc, HLDS) | `cs16` | implemented, verified against a real server |
| Counter-Strike 2 (Source 2, Source RCON) | `cs2` | implemented, verified against a real server (open points below) |
| Counter-Strike: Source (Source RCON) | `css` | implemented, verified against a real server |
| Counter-Strike: Global Offensive (Source RCON) | `csgo` | implemented, verified against a real server |

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
| Kick | `kick #<userid> [message]` | nothing, also for an unknown user id |
| IP ban | `addip <minutes> <ip>` (0 = permanent), `writeip` saves to `listip.cfg` | `listip`: `IP filter list: empty` |

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
- Players come from `status`. The player line format is the engine's (format strings
  `#%2i %8s %i %s`, ` %3i`, ` %02i:%02i`, ` %4i  %3i`, ` %s` in `engine_i486.so`): slot,
  quoted name, user id, unique id (`BOT` for bots), frags, time, ping, loss, address. The
  name is matched up to the last quote that is followed by valid fields, so quotes in names
  do not shift them; HLTV proxies (`hltv:…` instead of frags) are left out.
- Kick: `kick #<userid> <message>`. Ban: `addip 0 <ip>`, the kick, `writeip` — by IP address,
  because a server with `sv_lan 1` gives every player the same unique id
  (`STEAM_ID_LAN`); the test server's `server.cfg` runs `exec listip.cfg`, so the ban
  outlasts a restart. Bots cannot be banned. The message keeps only letters, digits, spaces
  and `.,!?()_-`, at most 64 characters, so it cannot end the command.
- Console: the command goes to RCON as it is (measured: `echo hi` → `hi `).

### Not verified yet

- Player list, kick and ban with players connected: the test server had none and has no bot
  plugin. Only the empty `status`, `kick` of an unknown user id and `listip` were measured.
- Behaviour with a join password set.
- Servers older than build 10211 and Windows servers.
- What exactly happens when `sv_rcon_maxfailures` is reached.

### Simulator

`lanpilot.testing.goldsrc.GoldSrcSimulator` answers the status query and RCON the way the
measured server did, including both forms of long output, and keeps a list of players
(`player_commands`) that `status`, `kick`, `addip` and `writeip` work on. The adapter's tests
run against it.

## Counter-Strike 2

Measured on 2026-10-14 against a Linux CS2 dedicated server installed through LinuxGSM:
A2S version `1.41.9.0`, `status` reports `1.41.9.0/14190 10924 insecure public`.
Everything below was observed on that server unless marked otherwise.

### Setting up the server

- Set `rcon_password "<password>"` in the server's configuration. Without it RCON is off.
- LANPilot needs to reach the game port over **UDP** (status query) and **TCP** (RCON); both
  use the same port number, 27015 by default.

### Status

The standard server query (A2S_INFO through `opengsq`) works without a password: name,
map, players, maximum players, visibility; `game` is `Counter-Strike 2`, `folder` `csgo`,
keywords such as `empty,insecure`. A2S_PLAYER answered with an empty list on the empty
server; A2S_RULES did not answer at all.

### RCON protocol

- TCP on the game port; packets `size`, `id`, `type` (all int32 little-endian), body, two
  null bytes.
- Login: AUTH (type 3) with the password. CS2 answers with **only** the AUTH_RESPONSE
  (type 2, same id), within about 30 ms; it does not send the empty RESPONSE_VALUE that
  CS:Source sends first.
- A command is EXECCOMMAND (type 2). The output arrives as one RESPONSE_VALUE packet; even
  `maps *` (3.4 KB) and `status` came as a single packet. A command without output (for
  example `mp_restartgame 1`) is still answered, with an empty packet.
- **Wrong ids.** CS2 executes all packets that arrive together and labels every answer with
  the id of the **last** of them. Sending a command and an empty end marker back to back
  gave two answers that both carried the marker's id: first the output, then the empty
  answer to the marker. Two commands sent together were both answered with the second id.
  When the marker was sent half a second later, the ids were correct. A client that waits
  for "a packet with the marker's id" (as `opengsq`'s `SourceRcon` does) therefore returns
  an empty answer for every command; LANPilot has its own transport for this reason.
- An empty EXECCOMMAND is answered with an empty RESPONSE_VALUE. A RESPONSE_VALUE packet
  sent as marker (the trick documented for Source 1) came back as one packet with body
  `00 01 00 00`, not as two packets.
- The connection survives map changes. An idle connection still worked after 90 s; the
  check at 180 s got no answer, but at that time the server had stopped answering
  altogether (see below), so the idle limit is unknown (CS:Source kept one for 600 s).
  LANPilot opens a new connection after 60 s without a command.
- A wrong password is answered with a single AUTH_RESPONSE with id -1; the connection
  stays open, and a following login with the right password worked. Whether CS2 bans after
  repeated failures is unknown (it has no `sv_rcon_*` cvars).
- Unknown commands print `Unknown command '<name>'!`.
- The `sv_rcon_*` cvars of older engines (`sv_rcon_maxfailures`, `sv_rcon_banpenalty`, …)
  do not exist on CS2: asking for them prints nothing.

### Commands

| Purpose | Command | Answer |
|---|---|---|
| List maps | `maps *` | one tab-indented name per line, no `.bsp`, sorted; includes many non-maps (see below) |
| Change map | `changelevel <map>` | `Changelevel to <map>`, `changelevel "<map>"`, `CHostStateMgr::QueueNewRequest( Changelevel (<map>), 3 )`; unknown map: `changelevel <map>:  invalid map name` |
| Change map while a map is loading | `changelevel <map>` | `Can't changelevel, server not in a map`; the change is dropped |
| Restart the match | `mp_restartgame 1` | nothing |
| Reload the level | `changelevel <current map>` | as a map change |
| `restart` | – | `SV: Cheat command 'restart' ignored.` |
| `map <map>` | – | the same as `changelevel` |
| Load | `stats` | **nothing**; CS2 has no load values |
| Players | `status` | server block, a `spawngroups` block, then `id time ping loss state rate adr name` lines and `#end` |
| Workshop maps on disk | `ds_workshop_listmaps` | one name per line; empty without workshop maps |
| Host a workshop map | `host_workshop_map <id>` | nothing; download and change follow in the background |
| Change to a workshop map | `ds_workshop_changelevel <name>` | nothing, also for an unknown name; the change follows after about 1.5 s |

- `maps *` listed 138 entries for 25 playable maps: `prefabs/…`, `ui/…`, `editor/…`,
  `templates/…`, `*_vanity` scenes, `workshop_preview_*`, `error`, `graphics_settings`,
  `lobby_mapveto`, `warehouse_vanity`. LANPilot keeps names without `/` and drops the
  `_vanity` and `workshop_preview_` names and the three single names. `rush_001` stays in
  the list; whether it is playable has not been checked.
- After `changelevel` the status query reported the new map after 1–3.5 s; one change
  from `de_nuke` to `de_inferno` took 9 s.
- **Changes in quick succession.** Two `changelevel` sent at the same moment: the second
  replaces the first. A `changelevel` sent 0.1–0.5 s after another one, while the server
  was loading, was refused with `Can't changelevel, server not in a map`. One 1.5 s later
  was queued normally. LANPilot reports the refusal as an error and asks to try again.
- `host_workshop_map 3070288532` downloaded and loaded `bhop_water` in about 10 s. The
  status query and `status` then report the map name `bhop_water`, and
  `ds_workshop_listmaps` prints `bhop_water`. Workshop ids appear nowhere in the output.
  After a restart of the server the list was empty again until `host_workshop_map` ran;
  the second download took 1.5 s.
- While the workshop map is on disk, `maps *` lists it too (`bhop_water` and
  `bhop_water_v1`), but `changelevel bhop_water` answers `invalid map name`: workshop maps
  can only be loaded with `ds_workshop_changelevel`.

### What the adapter does with it

- Status from the query only; no `stats`, so the adapter has no `LOAD_STATS`.
- The map list is the filtered `maps *` plus the names from `ds_workshop_listmaps`, marked
  as workshop maps (`extra = {"workshop": "1"}`); a name in both lists is taken as a
  workshop map. `bhop_water_v1` stays a normal entry; whether it can be loaded at all is
  unknown. Workshop maps are changed with
  `ds_workshop_changelevel <name>`, the others with `changelevel <name>`.
- Hard restart: the current map from the status query, `changelevel` (or
  `ds_workshop_changelevel` if it is in the workshop listing) to that map.
- A map change refused because the server is loading becomes an error for the operator.
- The transport stops sending the password after one refusal, as for Counter-Strike 1.6.

The `cs2` adapter was run against the server on 2026-10-14: status, map list (24 maps),
unknown map, map change, match restart, level reload, and a workshop map in the list and
reloaded through `ds_workshop_changelevel`. Map change and match restart also worked
through the web interface.

### Server hangs with `bhop_water`

Twice the server stopped answering about 30–60 s after the workshop map `bhop_water`
(id 3070288532) had been loaded (13:06 and 13:55): the TCP port still accepted
connections, but neither the status query nor RCON were answered, and it did not recover
on its own. When the server left that map after about 40 s, nothing happened. No wrong
password had been sent before either hang. The likely cause is this map, not RCON; a
different workshop map has not been tried. LANPilot shows such a server as offline.

### Not verified yet

- Whether `bhop_water_v1` (listed by `maps *`) can be loaded.
- Whether CS2 bans addresses after repeated wrong passwords.
- How long an idle RCON connection is kept.
- Player list, kick and ban: CS2's `status` lists players in a different layout than
  Source 1, which has not been seen with players yet, so the `cs2` adapter has no player
  management. It has the console (the command goes to RCON as it is).
- Workshop collections (`host_workshop_collection`).

## Counter-Strike: Source

Measured on 2026-10-14 against a Linux CS:Source dedicated server installed through
LinuxGSM: A2S version `11003710`, `version` reports build label `11003710`, protocol 24,
server app id 232330. It ran on the same address as the CS2 server before it.

### Status

The server query works without a password (`game` `Counter-Strike: Source`, `folder`
`cstrike`, app id 240). A2S_PLAYER answered with an empty list; A2S_RULES answered, but
`opengsq` 3.7 failed to parse it (not used by LANPilot). During a map change the query
went unanswered for about one second, during `restart` for about eight.

### RCON protocol

- Login: the server answers with an empty RESPONSE_VALUE (type 0) and then the
  AUTH_RESPONSE (type 2), both with the request's id. A wrong password gives the empty
  packet with the request's id and then AUTH_RESPONSE with id **-1**; the connection stays
  open.
- **Ids are correct**: two commands sent together were answered with their own ids, and
  the empty end marker is answered with an empty RESPONSE_VALUE carrying its id.
- Long output is split into packets of about 4000 bytes, all with the command's id
  (`cvarlist`: 173 KB in 44 packets).
- `echo` output ends in a space before the line break; unknown commands print
  `Unknown command "<name>"`.
- The connection survives map changes and `restart`, and an idle connection still worked
  after 600 s. LANPilot nevertheless opens a new one after 60 s without a command, as on
  CS2, which costs one login.
- Ban settings are those of Source 1: `sv_rcon_maxfailures` 10, `sv_rcon_minfailures` 5
  within `sv_rcon_minfailuretime` 30 s, `sv_rcon_banpenalty` 0 (minutes; 0 means
  permanent). Not provoked.

### Commands

| Purpose | Command | Answer |
|---|---|---|
| List maps | `maps *` | `-------------`, then `PENDING:   (fs) <name>` per map, **without `.bsp`** |
| Change map | `changelevel <map>` | nothing; unknown map: `Failed to find map <map>` |
| Restart the match | `mp_restartgame 1` | nothing |
| Reload the level | `changelevel <current map>` | nothing (about 1 s until the server answers again) |
| Reload the level | `restart` | nothing (about 8 s); works, but LANPilot uses `changelevel` like on CS2 |
| Load | `stats` | header `CPU In_(KB/s) Out_(KB/s) Uptime Map_changes FPS Players Connects`, one line of values |
| Players | `status` | `hostname`, `version`, `udp/ip`, `steamid`, `map`, `tags`, `players`, `edicts`, then a header `# userid name uniqueid connected ping loss state adr` |

- `maps *` listed 20 maps, including the test maps `test_hardware` and `test_speakers`,
  which LANPilot keeps.

### What the adapter does with it

The `css` adapter was run against this server on 2026-10-14: status with CPU and FPS, the
map list (20 maps), an unknown map (reported as such), map change, match restart and level
reload, each followed by the query reporting the right map after 1–2 s. With a wrong
password the status kept working and the password was sent once.

**Players** (phase 5; the server was not available for it, so this follows the known format
of Source 1 `status`, not a measurement): a player line is
`#      3 "Ada"  [U:1:12345]  01:23  25  0 active 10.0.0.5:27005` (user id, quoted name,
unique id, connected, ping, loss, state, address); a bot is `#      2 "Bot01" BOT active`.
The adapter kicks with `kickid <userid> <message>` and bans by IP address like on
Counter-Strike 1.6 (`addip 0 <ip>`, `kickid`, `writeip`), with the same rules for the
message. Status has no scores. The console sends the command as it is.

### Not verified yet

- Player list, kick and ban, against a server; behaviour with players connected.
- Workshop or custom maps in subdirectories.

## Counter-Strike: Global Offensive

Measured on 2026-10-14 against a Linux CS:GO dedicated server (the legacy build after CS2's
release) installed through LinuxGSM: A2S version `1.38.8.1`, `status` reports
`1.38.8.1/13881 1575/8853 secure`. It ran on the same address as the other two before it.

### Status

The server query works without a password (`game` `Counter-Strike: Global Offensive`,
`folder` `csgo`; `opengsq` reports the app id as 9032 and the game id as 4465480).
A2S_PLAYER answered with an empty list, A2S_RULES did not answer. During a map change the
query went unanswered for 4–10 s.

### RCON protocol

- Login and ids as on CS:Source: an empty RESPONSE_VALUE, then the AUTH_RESPONSE; correct
  ids for commands sent together.
- **Log lines in the answers.** The server appends the lines it writes to its log to the
  RCON answers, at least `L 10/14/2026 - 14:18:30: rcon from "<address>": command
  "<command>"` for every command. The empty end marker is therefore **not** answered with an
  empty packet but with its own log line (`command ""`). The transport ignores such lines
  when it looks for the end and removes them from the text; without that, every command
  would have waited for the timeout.
- **The connection is closed on every map change** (`changelevel`, and so the level
  reload). The next command on that connection fails. The transport then connects again
  and sends the command once more, because a connection that closed before any answer
  arrived cannot have run it. While the map loads, the new login waits until the server
  is ready again (6.9 s in one test).
- `echo` adds a space before the line break, like CS:Source. The `sv_rcon_*` cvars are
  unknown commands on this build.

### Commands

| Purpose | Command | Answer |
|---|---|---|
| List maps | `maps *` | `-------------`, then `PENDING:   (fs) <name>.bsp` per map (**with** `.bsp`) |
| Change map | `changelevel <map>` | nothing (plus the log line); unknown map: `CModelLoader::Map_IsValid:  No such map 'maps/<map>.bsp'` and `changelevel failed: <map> not found` |
| Restart the match | `mp_restartgame 1` | nothing |
| Reload the level | `changelevel <current map>` | as a map change |
| Load | `stats` | header `CPU NetIn NetOut Uptime Maps FPS Players Svms +-ms ~tick`, one line of values |
| Players | `status` | `hostname`, `version`, `udp/ip`, `os`, `type`, `map`, `players`, then `# userid name uniqueid connected ping loss state rate adr` and `#end` |
| Workshop | `ds_workshop_listmaps` | `Unknown command` |

- `maps *` listed 39 entries, including `lobby_mapveto`, which LANPilot leaves out, and
  `training1`, which it keeps.

### What the adapter does with it

The `csgo` adapter was run against this server on 2026-10-14: status with CPU and FPS, the
map list (38 maps), an unknown map, map change, match restart directly after the change
(through a new connection), level reload, a second map change 0.3 s after the first (it
waited for the server and arrived), and a wrong password (sent once). Map change and match
restart also worked through the web interface.

**Players** (not measured, like on CS:Source): CS:GO prints the slot after the user id and
the rate before the address: `#  3 2 "Ada" STEAM_1:1:12345 01:23 25 0 active 196608
10.0.0.5:27005`, bots `# 3 2 "Bot01" BOT active 64`, and ends the list with `#end`. The
adapter accepts both forms; kick and ban work as on CS:Source.

### Not verified yet

- Player list, kick and ban, against a server; behaviour with players connected.
- Workshop maps (`workshop/<id>/<name>`): not supported yet, such names are filtered out.

## Source RCON simulator

`lanpilot.testing.source.SourceSimulator` answers the status query and Source RCON on one
port number. `game="cs2"` (default), `"csgo"` and `"css"` reproduce the measured behaviour
of each game: the wrong ids of CS2, the log lines and dropped connections of CS:GO. For
CS:Source and CS:GO it also answers `status` with players and handles `kickid`, `addip` and
`writeip` (`player_commands`). The adapters' tests run against all three.

## LAN discovery

Measured on 2026-10-20 against CS 1.6 (HLDS build 10211, same host as LANPilot) and CS2
1.41.9.0 (another host), both on port 27015. CS:Source and CS:GO were not measured.

- `FF FF FF FF 54 "Source Engine Query" 00` (A2S_INFO) reaches both servers as a directed
  broadcast (the last address of the network), as `255.255.255.255` and as unicast; answers came
  within 1 to 11 ms, only from port 27015 (27016–27020 and 26900–26905 were asked too).
- Both answer the broadcast with a challenge (`FF FF FF FF 41` + 4 bytes), from the server's
  own address. The query repeated with that challenge, by unicast to that sender, gives the
  info (`0x49`); opengsq's `Source.get_info()` does exactly that. CS 1.6 asked over
  127.0.0.1 skips the challenge. No `0x6D` (old GoldSrc) answer was seen.
- What tells the games apart:

| Field | CS 1.6 | CS2 |
|---|---|---|
| app id | 10 | 730 |
| folder | `cstrike` | `csgo` |
| game | `Counter-Strike` | `Counter-Strike 2` |
| version | `1.1.2.7/Stdio` | `1.41.9.0` |
| protocol | 48 | 17 |

CS2 kept CS:GO's app id and folder, so the adapter takes `game` (`Counter-Strike 2`) and
then the version (1.39 and later is CS2; CS:GO ended with 1.38.x) to tell them apart;
CS:Source is app id 240. `classify()` in `games/valve/common.py` does this. The Counter-Strike
adapters of one scan share one broadcast (`find_servers()`) with the other GoldSrc and Source
games ([valve.md](valve.md)).
