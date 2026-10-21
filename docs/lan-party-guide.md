# Running LANPilot at a LAN party

A checklist for organisers, from the week before to the evening after. Installation itself
is in [installation.md](installation.md), every setting in
[configuration.md](configuration.md).

## Before the event

**Where LANPilot runs.** On a machine in the same network as the game servers: the LAN scan
uses broadcasts, which routers do not forward, and the remote protocols send passwords in
clear text, so keep the way from LANPilot to the servers short. If the event network allows
it, put LANPilot and the game servers' admin ports into a network players cannot reach.

**HTTPS.** Put a reverse proxy with a certificate in front of LANPilot
([reverse-proxy.md](reverse-proxy.md)). Over plain HTTP, anyone on the network can read
session cookies and passwords, and LANPilot shows a warning on every page.

**Accounts.** Create the first administrator on the command line
(`lanpilot user create NAME`), then the others under "Users":

| Role | For | May |
|---|---|---|
| `viewer` | everyone who should only look | see servers, maps and players |
| `operator` | helpers at the admin desk | also change maps, restart matches, reload levels, kick players |
| `admin` | the organisers | also ban players, manage servers, users and map images, read the audit log, use the console |

Every action is recorded in the audit log with who did it and from where. Each user picks
the language (English or German) on their "Account" page; otherwise the browser's
preference decides.

**Game servers.** Every server needs its remote control switched on, with a password of its
own (do not reuse one: LANPilot stores them encrypted, but the protocols send them in clear
text):

| Game | Switch on | LANPilot reaches |
|---|---|---|
| Counter-Strike 1.6 | `rcon_password` in `server.cfg` | UDP game port (27015) |
| CS:Source, CS:GO, CS2 | `rcon_password` | game port, UDP (status) and TCP (RCON) |
| TrackMania Forever, Sunrise | `<authorization_levels>` in `dedicated_cfg.txt`; `xmlrpc_allowremote` for LANPilot's address | TCP XML-RPC port (5000) |
| UT99, UT2004 | WebAdmin (`UWeb.WebServer`, `ListenPort`), `AdminPassword` | TCP WebAdmin port, UDP GameSpy query port (UT99: game port + 1, UT2004: + 10) |
| UT3 | WebAdmin with `bHttpAuth=true` | TCP WebAdmin port |
| Quake III Arena, Call of Duty, Call of Duty 4, World at War | `rconpassword` (CoD4 and WaW: `rcon_password`) in the server's config, without spaces or quotes | UDP game port (27960, 28960) |

Details per game, as measured: [games/](games/).

**The LAN scan** finds these servers without passwords. Set `[scan] networks` to the
event's networks, then "Server setup" → "Scan the network" lists what answers; "Add" opens
the form with host and ports filled in, and only the passwords remain to be entered. What
the scan sends:

| Game | Scan |
|---|---|
| Counter-Strike | UDP broadcast to 27015–27020 and 26900–26905 |
| TrackMania | UDP broadcast to 2350–2353, then TCP to the game port |
| UT99 | UDP broadcast to 8777–8786, then the GameSpy query |
| UT2004 | UDP broadcast to 10777; the answer and a follow-up query use the query port (game port + 1) |
| UT3 | UDP broadcast from and to 14001 |
| Quake III Arena, Call of Duty, Call of Duty 4, World at War | UDP broadcast to 27960–27963 and 28960–28963 |

A firewall on the game servers has to let these in; on the LANPilot host the answers are
replies and pass a stateful firewall.

**Map images** come from the internet. If the event has no internet access, load them
beforehand: add the servers at home or the day before, let LANPilot read their map lists,
then run `lanpilot images prefetch` (or "Look for all missing images online" per server).
The images are stored locally and shown without internet. Maps without an online image get
an uploaded one on the server's "Map images" page.

**Try everything once:** "Test connection" for every server, one map change, one restart.
A wrong password shows up on the server's page while its status keeps working.

## During the event

- The dashboard updates by itself; nobody needs to reload.
- Map changes go only to maps in the server's list. A map that was copied onto a server
  later appears after "Read the map list from the server again".
- Kick and ban act on the player who is on the server at that moment. Counter-Strike bans
  by IP address, because LAN servers give every player the same Steam ID; TrackMania bans
  last until the server restarts. (Kick and ban have not yet been tried with real players;
  please report what you see.)
- The raw console is off unless `enable_console = true` is set, and then only for
  administrators. Leave it off unless someone needs it.
- If a server shows "offline", LANPilot keeps asking, less often, and picks it up again when
  it answers.

## After the event

- Disable or delete the helpers' accounts; their sessions end at once.
- The audit log keeps the record of the event.
- Back up `/var/lib/lanpilot` (database, images) and `/etc/lanpilot` (configuration, key)
  together: without the key, the stored server passwords cannot be read.
