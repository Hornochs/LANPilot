# Research: map image sources

_Collected 2026-10-13. Endpoints marked **[verified]** answered as described on that day;
everything else is **[unverified]** and must be checked before it is relied upon._

LANPilot shows a preview image of the current map. It knows the game, the map file name and,
for Trackmania, the track UId. Images are fetched on demand and cached; they are never
bundled, because screenshots are copyrighted by the publisher or the map author.

## Trackmania – TMX / ManiaExchange

Best source: lookup by UId is exact. No API key. Send a descriptive `User-Agent`; the API
documentation (<https://api.mania.exchange/>) requires a `fields=` parameter and encourages
caching.

| Title | Host | Endpoint |
|---|---|---|
| TMNF | `tmnf.exchange` | `/api/tracks` |
| TMUF | `tmuf.exchange` | `/api/tracks` |
| Nations ESWC | `nations.tm-exchange.com` | `/api/tracks` |
| Sunrise | `sunrise.tm-exchange.com` | `/api/tracks` |
| Original | `original.tm-exchange.com` | `/api/tracks` |
| TM2 | `tm.mania.exchange` | `/api/maps` |
| TM2020 | `trackmania.exchange` | `/api/maps` |

All hosts answered their endpoint with 200. [verified]

- Lookup TMNF: `GET https://tmnf.exchange/api/tracks?fields=TrackId,TrackName,UId,HasThumbnail,HasImages&uid=<UId>`
  returns `{"More":false,"Results":[{"TrackId":…}]}`. [verified]
- Lookup TM2020: `GET https://trackmania.exchange/api/maps?fields=MapId,Name,MapUid&uid=<MapUid>`;
  `name=` works as a filter too. [verified]
- Image TMNF: `https://tmnf.exchange/trackshow/<TrackId>/image/1` (JPEG). [verified]
- Image TM2020: `https://trackmania.exchange/mapimage/<MapId>/1` and `/mapthumb/<MapId>`. [verified]
- The image URL pattern on the Sunrise, Original, Nations and TMUF hosts is [unverified].
- Stock Nadeo tracks: `name=A01-Race` found a track on TMNF; full coverage is [unverified].

## Unreal Tournament – Unreal Archive

Best source for UT99, UT2003, UT2004 and UT3 (tens of thousands of maps).

- <https://unrealarchive.org> is a static site without a JSON API. [verified]
- Data repository: <https://github.com/unreal-archive/unreal-archive-data>, one YAML file per
  item, license **Unlicense** (public domain) for the metadata. [verified]
- Path pattern:
  `content/<Game>/Maps/<Gametype>/<letter>/<h>/<h>/<hash6>/<name>_[<hash8>].yml`. [verified]
- Fields: `contentType: MAP`, `game`, `name`, `author`, `files[]` (with the map file name) and
  `attachments: [{type: IMAGE, url}]`; the image URLs point to an object store and returned a
  PNG. [verified]
- A path cannot be derived from a map name, and several uploads can share a name.
  **LANPilot needs a script that builds a slim index** (`game + file name → image URL`) from
  the repository. The index can be shipped; the images are fetched on demand.
- The repository's size on disk is [unverified].
- GameTracker has no working image pattern for Unreal Tournament. [verified, all probes 404]
- Extracting the embedded screenshot from map packages would need file access and is out of
  scope (LANPilot is protocol-only).

## Counter-Strike

Ranked, best first:

1. **Steam Workshop** (CS:GO/CS2 workshop maps):
   `POST https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/` with
   `itemcount=1&publishedfileids[0]=<id>` works without a key and returns `preview_url`.
   [verified] The id comes from the map string `workshop/<id>/<name>` reported by the server.
   [unverified on a real server] Name search (`IPublishedFileService/QueryFiles`) needs an API
   key. [verified]
2. **GameBanana API v11** (custom maps):
   `GET https://gamebanana.com/apiv11/Util/Search/Results?_sSearchString=<map>&_idGameRow=<id>&_sModelName=Mod&_sOrder=best_match`.
   Game ids: Counter-Strike: Source = 2, Counter-Strike 1.6 = 4254. Results carry
   `_aPreviewMedia._aImages[]` with `_sBaseUrl` and `_sFile`. [verified] The search is fuzzy;
   prefer exact name matches in the "Maps" category. Loading the constructed image URL, rate
   limits and terms are [unverified].
3. **GameTracker image CDN** (stock and popular maps, 160x120 only):
   `https://image.gametracker.com/images/maps/160x120/<game>/<map>.jpg`. Working game slugs:
   `cs`, `css`, `csgo`, `cod`, `cod2`, `cod4`, `q3`, `bf2`, `tf2`. [verified] There is no `cs2`
   slug. Missing maps return 404. Terms of use are [unverified]; this is unofficial hotlinking,
   so it is the last resort and can be disabled.

Not usable: 17buddies (blocks automated requests) [verified]; HLstatsX:CE image packs (license
of the images unclear).

## Other games

- GameTracker slugs `cod`, `cod2`, `cod4`, `q3`, `bf2` work; `cod5` and `jk3` do not. [verified]
- No open-licensed multi-game image dataset was found.

## Rules for every provider

- Fetch only from an allow-list of hosts, with a size limit and a descriptive `User-Agent`.
- Rate-limit prefetch runs to a few requests per second.
- Store the image together with its provider and source URL; cache misses too.
- Every provider can be disabled in the configuration.
