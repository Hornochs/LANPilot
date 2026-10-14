# Trackmania

| Game | Adapter | State |
|---|---|---|
| TrackMania Nations Forever, TrackMania United Forever | `tmforever` | implemented; verified against a TMNF server |
| TrackMania 2, Trackmania (2020) | – | planned (roadmap step 3.3) |
| TrackMania Sunrise, Sunrise eXtreme | `tmsunrise` | implemented; verified against a Sunrise eXtreme server |
| TrackMania Nations ESWC | `tmsunrise` | verified with the same server binary (`/game=nations`) |
| TrackMania Original | `tmsunrise` | same server binary; not tested separately |

## TrackMania Forever

Measured on 2026-10-14 against a Linux TMNF dedicated server: `GetVersion` reports
`TmForever 2.11.26`, build `2011-02-21`, started with `/lan` and the match settings
`MatchSettings/Nations/NationsAll.txt` (60 tracks). TrackMania United Forever runs the same
server software; it has not been tested separately.

### Setting up the server

- Remote control is XML-RPC on `xmlrpc_port` (default 5000, TCP). With
  `xmlrpc_allowremote` set to `False` only connections from the same machine are accepted;
  set it to `True` or to LANPilot's address otherwise.
- The logins are the `<authorization_levels>` in `dedicated_cfg.txt`: `SuperAdmin`,
  `Admin` and `User`, each with its own password. LANPilot uses **`Admin`**: it may change
  and restart tracks. `SuperAdmin` works too but could also stop the server; `User` cannot
  change anything and is therefore not offered.

### Protocol

- On connect the server sends a 4-byte length (11) and `GBXRemote 2`.
- Calls and answers are framed as 4-byte length, 4-byte handle, XML-RPC document. Answers
  carry the handle of the call (`0x80000001`, `0x80000002`, …). No other frames arrived
  while callbacks were not enabled.
- **Nothing works without a login**, not even `GetVersion`: the fault is
  `-1000 Permission denied.` There is no password-less status query as with Counter-Strike,
  so a wrong password makes the server look offline in LANPilot.
- `Authenticate` with a wrong password: fault `-1000 Password incorrect.`; the connection
  stays open and later calls get "Permission denied.". No lock-out was observed after one
  attempt.
- Every call took about 45 ms, also on the same machine. `system.multicall` runs several
  methods in one round trip: entries come back as `[result]` or as a struct with
  `faultCode`/`faultString`. LANPilot reads the status with one multicall (about 50 ms).
- Unknown method: fault `-506 Method '<name>' not defined`. `system.listMethods` lists 232
  methods; every method LANPilot uses is among them. There is no method that jumps to a
  track without changing the playlist (no `Jump…`).

### What each login may do

| Method | User | Admin |
|---|---|---|
| `GetVersion`, `GetServerName`, `GetCurrentChallengeInfo`, `GetChallengeList`, `GetPlayerList`, `GetMaxPlayers`, `GetGameMode`, `GetStatus`, `GetSystemInfo`, `GetCurrentGameInfo` | yes | yes |
| `GetServerPassword` | `No password` (11 characters) – a sentence, see Sunrise | the join password (empty here) |
| `GetNetworkStats` | no | no (SuperAdmin only) |
| `ChooseNextChallenge`, `NextChallenge`, `RestartChallenge` | no | yes |

Because the load figures need SuperAdmin, the adapter offers no load values.

### Tracks

- `GetChallengeList(count, start)` returns the playlist: `UId`, `Name`, `FileName` (relative
  to the tracks directory, e.g. `Campaigns/Nations/Green/B11-Race.Challenge.Gbx`),
  `Environnement`, `Author`, `GoldTime`, `CopperPrice`. `GetCurrentChallengeInfo` adds
  `Mood`, the medal times, `LapRace`, `NbLaps`, `NbCheckpoints`.
- Names may contain formatting codes (`$o`, `$fff`, `$l[…]`, `$$`); LANPilot shows them
  without.

### Changing and restarting

| Purpose | Calls | Observed |
|---|---|---|
| Change track | `ChooseNextChallenge(FileName)`, `NextChallenge()` | both `true` within 45 ms; status `Running - Finish` → `Launching` (0.4 s) → `Running - Synchronization` (1.6 s) → `Running - Play` (6 s) |
| Restart the round | `RestartChallenge()` | `Running - Finish` → `Running - Synchronization` → `Running - Play` after 4.5 s; the track is not loaded again |
| Reload the track | `ChooseNextChallenge(current FileName)`, `NextChallenge()` | like a track change, including `Launching` |
| Unknown track | `ChooseNextChallenge("…")` | fault `-1000 Track unknown.`; also for `../../../etc/passwd` |

- **`ChooseNextChallenge` reorders the playlist:** the chosen track moves directly behind
  the current one (B15-Race moved from index 5 to 1; the set of tracks stays the same). After
  a change the playlist continues from there.
- `GetCurrentChallengeInfo` names the new track about 0.1 s after `NextChallenge`, long
  before it can be played.
- **While a track is loading** (about 6 s after `NextChallenge`, 4.5 s after
  `RestartChallenge`) `NextChallenge` and `RestartChallenge` are refused with
  `-1000 Change in progress.`; `ChooseNextChallenge` is accepted, so the chosen track becomes
  the next one but is not loaded. LANPilot reports "still loading, try again".
- A second `NextChallenge` sent right after the first returned `true` but had no visible
  effect.

### Tracks outside the playlist

The Tracks directory of the test server held 275 track files, the playlist 60. Measured with
the five Nations "Black" tracks (`Campaigns/Nations/Black/E01-Obstacle.Challenge.Gbx` …),
which are not in `NationsAll.txt`, and with United tracks:

| Call | Track in the playlist | File on disk, not in the playlist | File the server cannot run (United Coast track on TMNF) | No such file, a directory, `../…` |
|---|---|---|---|---|
| `GetChallengeInfo(file)` | info | info (`UId`, `Name`, …) | info, `Environnement: Coast` | `Challenge unknown.` |
| `CheckChallengeForCurrentServerParams(file)` | `true` | `true` | `The track doesn't match the server packmask.` | `Challenge unknown.` |
| `ChooseNextChallenge(file)` | `true` | `Challenge not in the selection.` | `Challenge not in the selection.` | `Track unknown.` |
| `InsertChallenge(file)` | `Challenge already added.` | `true`, inserted behind the current track | `The track doesn't match the server packmask.` | `Challenge unknown.` |

- `GetServerPackMask` reported `Stadium`. `CheckChallengeForCurrentServerParams` works for
  the `User` login too.
- After `InsertChallenge` the track is the next one; `NextChallenge` loads it (6 s, like any
  change). It stays in the playlist until the server restarts with its match settings.

### What the adapter does with it

- One connection per server, logged in once; a refused password is not sent again.
- Status: `GetServerName`, `GetCurrentChallengeInfo`, `GetPlayerList(255, 0)`,
  `GetMaxPlayers`, `GetGameMode`, `GetServerPassword` in one multicall. The game mode
  becomes the game type (`Time Attack`, `Rounds`, `Team`, `Laps`, `Stunts`, `Cup`); the join
  password only becomes "password protected: yes/no" and is not kept.
- Map key is the `FileName`, display name the plain `Name`; `UId` and environment go into
  `MapRef.extra`. The image lookup asks TrackMania Exchange by `UId`, first
  `tmnf.exchange`, then `tmuf.exchange`; TMX has the Nadeo tracks too (59 of the 60 Nations
  tracks were found; `D13-Race` was not). File names are checked before they
  are sent: printable, at most 255 characters, no `..` component, ending in `.gbx`.
- The track list is read in pages of 500, at most 5000 tracks.
- Administrators can add a track file by hand (`MANUAL_MAPS`): `look_up_map()` checks the
  file name, then `CheckChallengeForCurrentServerParams` (refusing tracks the server cannot
  run, with the server's reason) and takes name and `UId` from `GetChallengeInfo`. A change
  to such a track tries `ChooseNextChallenge`; on "Challenge not in the selection." it calls
  `InsertChallenge`, `ChooseNextChallenge` and `NextChallenge`.

The `tmforever` adapter was run against this server on 2026-10-14: status (about 90 ms),
60 tracks, an unknown track, a track change, a restart while loading (reported as such),
round restart, track reload and a wrong password (sent once). Through the web interface a
Black track was added by hand and loaded, a United Coast track was refused with the pack
mask reason, and the added track stayed one entry after the list was read again.

### Players

Nobody was connected to the test server, so only the empty answers and the faults were seen
(2026-10-14); the struct fields are those `ListMethods.html` documents.

- `GetPlayerList(max, start, 1)` (the third parameter asks for the Forever struct; 2 would
  include relayed servers) answers `[]`; a player is `Login`, `NickName`, `PlayerId`,
  `TeamId`, `SpectatorStatus`, `LadderRanking`, `Flags`.
- `GetCurrentRanking(max, start)`: `Login`, `NickName`, `PlayerId`, `Rank`, `BestTime`,
  `Score`, `NbrLapsFinished` (in team mode the two teams instead).
- `Kick(login, message)` and `Ban(login, message)` (message optional; Admin) answer the fault
  "Login unknown." for a login that is not on the server, with or without the message.
  `BanAndBlackList(login, message, save)` would also put the login on the blacklist. A plain
  ban is held in memory (`GetBanList`, `UnBan`, `CleanBanList`) and ends with a restart.
- `KickId(id)` with an unknown id: "UId unknown.".

The adapter lists players with `GetPlayerList(255, 0, 1)` and `GetCurrentRanking(255, 0)` in
one multicall: id = login (printable, no spaces, at most 64 characters; LAN logins can look
like `name/10.0.0.5:2350`), name = the nickname without formatting codes, score from the
ranking, team `Blue` (0) or `Red` (1). Kick and ban use `Kick`/`Ban` by login with the reason
as message; "Login unknown." becomes "This player is no longer on the server.". There is no
console: GBXRemote has no raw command.

### Not verified yet

- Players, kick and ban with players connected; the player count is the length of
  `GetPlayerList`, spectators possibly included.
- TrackMania United Forever, Windows servers, servers with `xmlrpc_allowremote` open.

## TrackMania Sunrise

Measured on 2026-10-14 against the Linux dedicated server of TrackMania Sunrise eXtreme:
`GetVersion` reports `TmSunrise 0.1.5.0`, build `2006-05-30`. The 32-bit, statically linked
binary ran on Debian 13 as is, started like the shipped `RunSunriseLAN.bat`:
`./TrackManiaServer /game=sunrise /game_settings=MatchSettings/Lan/Sunrise_TimeAttack.txt
/dedicated_cfg=<file> /nodaemon` (54 tracks). The same binary serves TrackMania Original,
Sunrise and Nations ESWC.

**Nations ESWC**, measured the same day with that binary and
`/game=nations /game_settings=MatchSettings/Internet/AdvancedTraining.txt` (22 tracks):
`GetVersion` reports `TmNationsESWC 0.1.7.4`, build `2006-05-30`. Status without login,
track change with the same short "No challenge currently loaded.", both restarts, a track
outside the playlist (`Challenges/Nadeo/Nations/Pro/Pro A-0.Challenge.Gbx`), an unknown file
and a wrong password behaved exactly as on Sunrise. Its Stadium tracks are on
`tmnf.exchange` with the same UId (for example `F-9`, `D-2`), mostly without screenshot.

**Remote control exists**: the same GBXRemote 2 on `xmlrpc_port` (5000), configured like
TrackMania Forever (`<authorization_levels>`, `xmlrpc_allowremote`). `ListMethods.html` lists
143 methods. What differs from TrackMania Forever:

| | TrackMania Forever | TrackMania Sunrise |
|---|---|---|
| Without a login | everything "Permission denied." | **status methods answer**: `GetVersion`, `GetServerName`, `GetCurrentChallengeInfo`, `GetChallengeList`, `GetStatus`, `GetPlayerList`, `GetMaxPlayers`, `GetGameMode`, `GetServerOptions`, `GetServerPassword`, `system.multicall`, `system.listMethods` |
| Round restart | `RestartChallenge` | `ChallengeRestart` (Admin); `RestartChallenge` does not exist |
| Track the server cannot run | `CheckChallengeForCurrentServerParams` | no such method |
| Unknown file in `ChooseNextChallenge` | `Track unknown.` | `Challenge unknown.` |
| While a track loads | `NextChallenge`, `RestartChallenge`: `Change in progress.` | they answer `true`; `GetCurrentChallengeInfo` fails with `No challenge currently loaded.` for about a second |
| Loading | about 6 s until `Running - Play` | about 5 s; `ChallengeRestart` 0.4 s |

The same in both: `ChooseNextChallenge` reorders the playlist; tracks outside the playlist
(`Challenge not in the selection.`) are added with `InsertChallenge` (duplicate:
`Challenge already added.`); `GetChallengeInfo` works for any file on disk, also with
backslashes in the path; the server reports paths with `/`; a wrong password gives
`Password incorrect.` and leaves the connection open.

**Join password.** A client that may not read it (no login, or `User`) gets a sentence from
`GetServerPassword` and in `GetServerOptions`: `No password` or, after
`SetServerPassword("…")`, `Password required`. `Admin` gets the password itself. (Tried by
setting a password and removing it again.)

**What the adapter does** (`tmsunrise`): the status is read on a connection without login
(about 50 ms per multicall), so a wrong password does not make the server look offline; the
join password becomes yes/no from the sentence. "No challenge currently loaded." means no
current map for that moment. Control uses a second, logged-in connection; everything else is
shared with the Forever adapter. Track images come from TMX by `UId`, first
`tmuf.exchange` (Sunrise and Original environments), then `tmnf.exchange` (Stadium); the
exchange sites of these games no longer resolve. 45 of 56 Sunrise eXtreme tracks were found.

The `tmsunrise` adapter was run against this server on 2026-10-14: status without password,
55 tracks, a change with the status during loading, both restarts, a track outside the
playlist (bonus track `Crazy8`) looked up and loaded, an unknown file, and a wrong password
(status unaffected, control refused, password sent once). Through the web interface: track
change and restart.

**Players** (measured with nobody connected): `GetPlayerList(max, start)` takes **no third
parameter** (`Too many items in array`, fault -502); a player is `Login`, `NickName`,
`PlayerId`, `TeamId`, `IsSpectator`, `IsInOfficialMode`, `LadderRanking`. `Kick(login)` and
`Ban(login)` take **no message** (a second parameter is the same fault) and answer
"Login unknown." for a login that is not there; there is no `BanAndBlackList`. `KickId`
with an unknown id: "PlayerUId unknown.". The adapter therefore asks for the list without a
struct version and kicks and bans without the reason.

### Simulator

`lanpilot.testing.gbxremote.GbxRemoteSimulator` speaks GBXRemote 2 the way the measured
servers did, `game="forever"` (default) or `"sunrise"`: login levels, multicall, the
playlist reordering of `ChooseNextChallenge`, the differences in the table above, track
files outside the playlist (`disk_tracks`) and the pack mask (`environments`), and players
(`roster`) with ranking, `Kick` and `Ban` and the faults measured above. The adapters' tests
run against it.
