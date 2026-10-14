# Unreal Tournament

| Game | Adapter | State |
|---|---|---|
| Unreal Tournament 2004 | `ut2004` | implemented, verified against a real server |
| Unreal Tournament (UT99, incl. OldUnreal 469) | `ut99` | implemented, verified against a real server |
| Unreal Tournament 3 | `ut3` | implemented, verified against a real server |

Unreal Tournament 99 and 2004 are controlled through the **WebAdmin** (UWeb, HTTP) and
watched through the **GameSpy query**. UT3 has Epic's newer WebAdmin on the same web server
and no query LANPilot can use, so its status comes from the WebAdmin too (see below).

## Unreal Tournament 2004

Measured on 2026-10-14 against a Linux UT2004 dedicated server, build 3369, installed through
LinuxGSM. Everything below was observed on that server unless marked otherwise.

### Setting up the server

- The WebAdmin is a server actor: `ServerActors=UWeb.WebServer`, and in `[UWeb.WebServer]`
  `bEnabled=True` and `ListenPort` (8075 on the test server). Pages live under
  `/ServerAdmin`.
- With the standard access control (`AccessControlClass=Engine.AccessControl`) the WebAdmin
  user is **`admin`** and the password is `AdminPassword` in `[Engine.AccessControl]`.
  (`AdminName` in `[Engine.GameReplicationInfo]` is only the name shown to players.)
- LANPilot reads the status from the GameSpy query port, the game port plus 10 (7787 for
  game port 7777).

### Status

- **GameSpy query** (UDP 7787, `opengsq` `GameSpy1`): `hostname`, `mapname` (`DM-Deck17`),
  `maptitle` (`Deck 17`), `gametype` (`xDeathMatch`, the class without package),
  `numplayers`, `maxplayers`, `password` (`0`/`1`). No password needed.
- The UT2004 query on the game port plus 1 (UDP 7778, `opengsq` `Unreal2`) answers too:
  details and rules (mutators, time limit, …); its player query timed out on the empty
  server. LANPilot uses the GameSpy query, which has the map title and the password flag.

### WebAdmin

- HTTP/1.1 with `Connection: Close` on every answer; `Server: UnrealEngine UWeb Web Server
  Build 3369`. `/ServerAdmin` redirects (302) to `/ServerAdmin/`.
- **HTTP Basic authentication**, realm "UT Remote Admin Server". Wrong credentials: 401 on
  every page; four wrong attempts in a row did not lock anything.
- Pages are ISO-8859-1 HTML with frames (`currentframe?Page=current_game` …); LANPilot
  requests the inner pages directly. Each request took about 100 ms.
- **The WebAdmin restarts with every map change** (`xWebAdmin.UTServerAdmin Initialized on
  port 8075` in the log); for a few seconds nothing answers, the status query neither.

| Page | Content |
|---|---|
| `current_game` | select `GameTypeSelect` (class → name, 15 entries incl. mods such as `3SPNv3141.Freon`), select `MapSelect` (only the maps **in the rotation** of the current game type), buttons `SwitchGameType`, `SwitchMap` |
| `defaults_maps?GameType=<class>` | `IncludeMapsSelect` (in the rotation) and `ExcludeMapsSelect` (not in it); together **all installed maps** of that game type (93 + 11 = 104 DM maps on disk) |
| `current_restart` | hidden `GameType` and `MapName` of the running game; on this server a "Restart Conflicts" form (see below) |
| `server_restart` (POST) | target of the restart form |
| `current_console` | frames `current_console_log` and `current_console_send` (field `SendText`, "say " prefilled); also takes console commands |
| `current_players`, `current_bots`, `current_mutators`, `defaults_*` | not used yet |

Map lists per game type, read for all 15 types in 1.6 s: 1040 entries, 249 distinct names.
The spelling differs between lists (`DM-RANKIN` in a saved rotation, `DM-Rankin` on disk);
UT does not distinguish. Onslaught entries carry URL options:
`ONS-TORLAN?LinkSetup=Default`.

### Changing maps and game types

| What | Request | Observed |
|---|---|---|
| Change the map, keep the game type | POST `current_game` `MapSelect=<map>&SwitchMap=Switch` | works also for maps outside the rotation; the server travels with the **current** game type even if `GameTypeSelect` names another one (CTF-FaceClassic was loaded as DeathMatch) |
| Change map and game type | POST `server_restart` `GameType=<class>&MapName=<map>&Save=Accept` | loads that map with that game type (`ProcessServerTravel: CTF-Face3?Game=XGame.xCTFGame`) |
| Reload the current game | the same with the values from `current_restart` | the map is loaded again |
| Unknown map | either request | answered "The server is now switching to map '…'" — and nothing happens |

- Every answer to these posts is a "Please Wait" page; a map change took 7–8 s.
- **A change that arrives while the server is still busy with the previous one (within about
  a second) is dropped silently**, although the WebAdmin answers "Please wait" (observed
  twice, confirmed in the server log).
- The **"Restart Conflicts"** page appears when a value on the command line differs from the
  ini file. LinuxGSM starts the server with `?GoalScore=0?TimeLimit=15` while the ini says
  25 and 20. A checked box makes the restart take the ini value; without the box the command
  line value stays. LANPilot sends no boxes, so the running rules are kept.
- `SwitchGameType` only changes what the page shows, not the server.

### What the adapter does with it

- Status from the GameSpy query; the game type's short class is shown by its WebAdmin name
  ("DeathMatch") once the map list has been read.
- Map list: for every game type of `GameTypeSelect`, the maps of `defaults_maps`, rotation
  first, without case duplicates. Map key = map name; `game_type` = the game type's name;
  `extra` = `{"game_class": "XGame.xCTFGame"}` and, for Onslaught, `{"options":
  "?LinkSetup=Default"}`. Options are only accepted in the form `?LinkSetup=<word>`, so a
  crafted list entry cannot add `?Game=` or `?AdminPassword=`.
- Change: POST `server_restart` with the map's game type class, map name and options.
- Hard restart: the same with the values from `current_restart`. There is **no soft
  restart** (the WebAdmin cannot restart just the match), so it is not offered.
- Map names must match `[A-Za-z0-9_.\-\[\]]{1,64}`, game type classes `Package.Class`.

The adapter was run against this server on 2026-10-14: status, 1007 map entries in 15 game
types, change to CTF-Face3 as Capture the Flag and to ONS-Torlan as Onslaught (with its
option), reload, refused soft restart, wrong password (status unaffected, WebAdmin refused
once). Through the web interface: added, map changed by game type.

### Players and console

Bots join this server only once a human player is connected (`MinPlayers` and
`bWaitForNetPlayers` did not change that), and the test server had no human players. The
player page was therefore taken from the WebAdmin's own code (`QueryCurrentPlayers` in
`System/XWebAdmin.u`, templates in `Web/ServerAdmin/`) and only its empty form was seen:

- `current_players`: a table whose header cells read "Kick", "Ban", "Name:", "Team:" (team
  games only), "Ping:", "Score:", "IP" and, with ban by ID, "Global ID". Each player row
  starts with a checkbox `Kick<PlayerID>` (value `True`) and, for human players, `Ban<PlayerID>`;
  bots have no ban box ("Bots cannot be banned") and their name ends in " (Bot)". Names are
  HTML-encoded, spaces as `&nbsp;`. The ping column shows the ping times 4. An empty server
  shows one row "** No Players Connected **".
- Any request of `current_players` carrying `Ban<ID>` bans that player
  (`AccessControl.KickBanPlayer`, permanent, by IP or by ID with `bBanByID`); `Kick<ID>`
  kicks (`KickPlayer`), a bot is removed. The page is sent back afterwards.
- Console (measured): POST `current_console` with `SendText`; the answer appears in the
  message log `current_console_log` as `&gt; <line><br>`, answers prefixed with "WebAdmin: "
  (`get ini:Engine.Engine.GameEngine CacheSizeMegs` → "WebAdmin: 32"), `say` text without a
  prefix; `debug` and unknown commands add nothing. The log collects chat as well.

The adapter lists players from that table, kicks and bans by posting the box and checks that
the player is gone from the answer; there is no field for a reason. The console returns the
lines the log gained; it was run against this server on 2026-10-14 (answers, `say`, an
unknown command, a repeated command).

### Not verified yet

- Player list, kick and ban with players connected (format taken from the WebAdmin code).
- Servers with `xAdmin.AccessControlIni` (several admin accounts).

## Unreal Tournament (UT99)

Measured on 2026-10-14 against a Linux UT99 server, version 469 (OldUnreal patch), installed
through LinuxGSM. Its WebAdmin is the same UWeb server with the same realm and the same pages
as UT2004's, with these differences:

| | UT2004 | UT99 |
|---|---|---|
| Credentials | user `admin`, `AdminPassword` in `[Engine.AccessControl]` | `AdminUsername`/`AdminPassword` in `[UTServerAdmin.UTServerAdmin]` |
| WebAdmin port on the test server | 8075 | 8076 (`ListenPort` in `[UWeb.WebServer]`; the game's default is 80) |
| GameSpy query port | game port + 10 (7787) | game port + 1 (7778) |
| `password` in the query | `0`/`1` | `False`/`True` |
| Map values in the selects | `DM-Deck17` | **`DM-Deck16][.unr`** (label without extension) |
| Change map and game type | POST `server_restart` (`GameType`, `MapName`) | POST `current_game` with `GameTypeSelect`, `MapSelect` and **`SwitchGameTypeAndMap`** |
| Reload the current map | POST `server_restart` with the running values | **requesting `current_restart` (GET) restarts the map**; it is a link, not a page to look at |
| Game types offered | 15 (incl. mods) | 7 (`Botpack.*`); `Botpack.TDarkMatch` has no maps (`*** None ***`) |

- `SwitchMap` travels with the **running** game type whatever `GameTypeSelect` says
  (`ProcessServerTravel: CTF-Face.unr?game=Botpack.DeathMatchPlus?mutator=`).
  `SwitchGameType` only changes the page: afterwards the form carries the chosen type as a
  hidden `GameTypeSelect` and offers the button `SwitchGameTypeAndMap`, which travels with
  it (`CTF-Face.unr?game=Botpack.CTFGame`).
- Answers took 20–60 ms; a change 6–8 s; the WebAdmin and the query are unreachable for a
  few seconds meanwhile. An unknown map is answered with "switching to map" and has no
  effect, as on UT2004.
- Map lists: `defaults_maps?GameType=<class>` with `IncludeMapsSelect` and
  `ExcludeMapsSelect`, plus `MapListSelect` (stock map lists); 155 entries in 6 game types,
  read in 0.3 s.

**What the adapter does** (`ut99`, sharing everything else with `ut2004` in
`games/unreal/webadmin.py`): it removes `.unr` from map names (so they match the query's
`mapname` and the image keys) and adds it back when changing; it changes with
`SwitchGameTypeAndMap`; the hard restart requests `current_restart`.

The adapter was run against this server on 2026-10-14: status, 155 map entries, change to
CTF-Face as Capture the Flag and back, reload, wrong password (status unaffected, WebAdmin
refused once). Through the web interface: added, map changed by game type.

**Players and console.** As on UT2004, bots do not join without a human player, so the
player page comes from the WebAdmin code (`QueryCurrentPlayers` in
`System/UTServerAdmin.u`, `Web/current_players.uhtm`); only its empty form was seen:

- Header "Kick", "BanIP", "Name", "Team", "Ping", "Score", "IP". A human player's row starts
  with checkboxes `KickPlayer<PlayerID>` (value `kick`) and `BanPlayer<PlayerID>` (value
  `ban`); a bot's row starts with one empty cell spanning both columns, and its name ends in
  "&nbsp;(Bot)". **Names are not HTML-encoded.** The empty page has a row
  "** No Players Connected **".
- A request carrying `BanPlayer<ID>` adds `DENY,<IP>` to the IP policies, saves the
  configuration and removes the player; `KickPlayer<ID>` removes the player. Bots cannot be
  removed over the WebAdmin.
- Console (measured): as on UT2004, but answers come without a prefix
  (`get ini:Engine.Engine.GameEngine CacheSizeMegs` → "16") and `say` appears as
  "(Event) Admin: <text>"; an unknown command adds nothing.

The adapter takes the player id only from checkboxes in the cells **before** the name, so a
name with HTML in it cannot bring another player's box; a name that adds cells takes its row
out of the list. Bots are listed without buttons.

## Unreal Tournament 3

Measured on 2026-10-14 against a Linux UT3 server (patch 5, `Server: UnrealEngine UWeb Web
Server Build 3808`), installed through LinuxGSM and started as a LAN match
(`bIsLanMatch=true`).

### Setting up the server

- The WebAdmin is Epic's "UT3 WebAdmin" (2008–2009). With LinuxGSM the instance's settings
  are in `UTGame/Config/<instance>/`: `UTWeb.ini` `[IpDrv.WebServer]` `ListenPort` (8075 on
  the test server; the game's default is 8080), `UTGame.ini` `[Engine.AccessControl]`
  `AdminPassword`, user `admin`.
- **`bHttpAuth=true` in `[WebAdmin.WebAdmin]`** is needed: then the WebAdmin accepts HTTP
  Basic authentication. Without it there is only the login form (fields `token`,
  `username`, `password`, and a session cookie).
- The WebAdmin printed "No localization data. Please make sure the file
  Localization/INT/WebAdmin.int is up to date." on every page and its menu was missing;
  the pages LANPilot uses worked regardless.

### Status

- **No usable query.** UT3's LAN beacon (UDP 14001, `opengsq` `UT3`/`UDK`) only answers
  queries sent *from* port 14001; on the machine that runs the server that port is taken,
  and from elsewhere LANPilot would have to own it. GameSpy queries on the game port were not
  answered (what came back was game traffic). LANPilot therefore reads the status from the
  WebAdmin, which needs the password: a wrong password makes the server look offline, as on
  TrackMania Forever, and so does every map change (about 15 s without WebAdmin).
- `/ServerAdmin/current`: definition lists `currentGame` (Server Name, MOTD, Game type =
  name + class, Map, Mutators) and `currentRules` (Time Limit, Goal Score, **Players (Max)**
  as `0 (32)`, Spectators (Max)), then a players table. No password flag.

### Pages and requests

| What | Request | Answer |
|---|---|---|
| Game types | GET `/ServerAdmin/current/change`: select `gametype` (8 types, e.g. `UTGameContent.UTOnslaughtGame_Content` "Warfare") | also the form below |
| Maps of a game type | POST `/ServerAdmin/current/change/data` `ajax=1&gametype=<class>` | an HTML fragment with `<select id="map">` (no `name`): `DM-Deck` labelled `DM-Deck (Deck)`; Warfare also `WAR-Avalanche?LinkSetup=Alt` labelled `… (Avalanche Two Fronts)` |
| Change | POST `/ServerAdmin/current/change` `gametype`, `map`, `action=change`, `mutatorGroupCount`, `urlextra`, the `mutgroup*` fields | "Changing the current game with the following url:"; the WebAdmin is gone for about 15 s (connection reset, then 503) |

- The change form carries the running mutators (`mutgroup*` checkboxes and radio buttons)
  and, in the hidden `urlextra`, the running URL options
  (`?bIsDedicated=true?bIsLanMatch=true?…?TimeLimit=15?GoalScore=0`) — **including
  `?LinkSetup=Alt` of the running Warfare map**, which would carry over to the next map.
- **An unknown map is dangerous**: `DM-NoSuchMap` made the server travel to another map
  (`CTF-Reflection`) instead of doing nothing.
- Every request without the session cookie opens a new session, kept for a day
  (`ExpirationSecs 86400`); LANPilot keeps the cookie.
- There is no request for a round restart.

### What the adapter does with it

- `ut3` reads the status from `/current` and the map list from `/current/change` and its data
  request: 147 entries in 8 game types in 0.6 s, with map titles. A map with a layout is an
  entry of its own (key `WAR-Avalanche?LinkSetup=Alt`, name "Avalanche Two Fronts").
- A change posts the change form with the target's game type and map, the running mutators
  and `urlextra` **without** `?LinkSetup=`; the hard restart is a change to the running map
  with the layout taken from `urlextra`.
- The login form instead of a page means the credentials were refused; they are not sent
  again.

The adapter was run against this server on 2026-10-14: status, 147 map entries, change to
WAR-Avalanche "Two Fronts" (Warfare), reload keeping the layout, change to DM-Deck
(Deathmatch) dropping it — confirmed in the server log — and a wrong password. Through the
web interface: added, map changed.

### Players and console

Measured with bots (added through the WebAdmin's "Bots" page; UT3 bots do join an empty
server), the player actions taken from the WebAdmin's code (`handleCurrentPlayers` and
`getPlayerKey` in `UTGame/CookedPC/WebAdmin.u`, which carries its source text, and
`Web/ServerAdmin/current_players_row.inc`):

- The status page `/current` has a scoreboard table `#players` with the columns (team),
  "Player name", "Ranking", "Score", "Deaths", "Kills", "Ping", "Bot", "Admin", "Spec."; bots
  are listed with Bot = True. An empty server shows "There are no players" in one cell
  spanning all columns.
- `/current/players` lists **only human players** (bots were not on it), with the columns
  (team id), "Player name", "Ping", "IP", "Unique ID", "Client Hash", "Admin", "Spec.",
  "Controls". The controls are a form with hidden `playerid` and `playerkey` and a select
  `action` (`kick`, `sessionban`, `banip`, `banid`, `banhash`, voice and text mute).
- The key is `<PlayerID>_<unique id>_<CreationTime>`. Posting the form to `/current/players`
  looks the player up by key; every ban action also kicks. The answer is the player page;
  its messages read "No localization data …" on the test server (the WebAdmin's localisation
  file is missing), so they cannot be relied on.
- Console (measured): `/console`, POST `command`; the page repeats the command in
  `#consoleResults` ("> command") followed by the answer, HTML-escaped
  (`getall UTGame GoalScore` → "0) UTDeathmatch DM-Deck.TheWorld:PersistentLevel.
  UTDeathmatch_2.GoalScore = 0"; `say` answers nothing). `bConsoleEnabled=false` in
  `[WebAdmin.WebAdmin]` removes the page (404).

The adapter lists everyone from the scoreboard; human players get their key from the player
page and their score from the scoreboard when their name is unique there. Bots are listed
without buttons. A kick posts `kick`, a ban `banip` (by IP address, like the other games),
and the player must be gone from the answer.

### Not verified yet

- Kick and ban with human players connected (taken from the WebAdmin code).
- A server that is not a LAN match (online advertisement); the LAN beacon from another
  machine.

## Map images

All three adapters look their maps up in the Unreal Archive index shipped with LANPilot
(image source `unreal_archive`, see docs/configuration.md). The archive holds community
maps; for the test servers it had images for 33 of 81 UT99, 66 of 230 UT2004 and 1 of 60
UT3 maps. Stock maps need an uploaded image.

## Simulators

`lanpilot.testing.uweb.UnrealSimulator` (`game="ut2004"` or `"ut99"`) serves the WebAdmin
pages LANPilot uses (Basic auth, `current_game`, `defaults_maps`, `current_restart`,
`server_restart`) and the GameSpy status query, with the quirks above (case duplicates,
Onslaught options, `.unr`, `SwitchMap` keeping the game type, unknown maps accepted without
effect), the player page of either game with kick and ban, and the console with its message
log.

`lanpilot.testing.uweb.UT3Simulator` serves UT3's status page, change form and data request,
with the login form for wrong credentials, a new session for every request without cookie,
`urlextra` carrying the layout, the fallback travel for unknown maps, the scoreboard, the
player page with its kick and ban form, and the console.
