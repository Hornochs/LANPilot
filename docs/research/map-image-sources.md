# Research: map image sources

_Collected 2026-10-13; the Counter-Strike part was checked again on 2026-10-14. Endpoints
marked **[verified]** answered as described on that day; everything else is
**[unverified]** and must be checked before it is relied upon._

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
- TMNF, checked again 2026-10-14: the stock Nadeo tracks are on TMX with the same UId
  (`B11-Race` → TrackId 2312). `/trackshow/<id>/image/1` returned a 320×240 JPEG when the
  track `HasImages`, otherwise the 256×256 thumbnail; `/trackshow/<id>/thumbnail` is the
  thumbnail. **An id that does not exist returns 200 with an 800×800 placeholder**, so image
  URLs must only be built from an id the API returned. [verified]
- 2026-10-14: `sunrise.tm-exchange.com`, `original.tm-exchange.com` and
  `nations.tm-exchange.com` no longer resolve. Many Sunrise tracks are on `tmuf.exchange`
  with their original UId (named "TMS …"); 45 of 56 Sunrise eXtreme tracks were found there.
  The URL pattern on tmuf.exchange is the same as on tmnf.exchange. [verified]
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
- 2026-10-14: the repository is about 900 MB with history; a shallow sparse clone of the
  three games' `Maps` folders is 160 MB (29 739 UT99, 7 877 UT2004, 2 520 UT3 uploads).
  Image URLs are on `unreal-archive-img.s3.sgp.io.cloud.ovh.net` (110 on
  `ua-img.s3.us-west-002.backblazeb2.com`); screenshots were 256×256 to 640×512. [verified]
- **Stock maps are mostly missing** (no DM-Deck16][, DM-Deck17, DM-Deck): the archive collects
  community content. Of the test servers' maps it had 33 of 81 (UT99), 66 of 230 (UT2004)
  and 1 of 60 (UT3). `MapPacks` (bonus packs) have screenshots of the whole pack only, not
  per map. [verified]
- GameTracker has no working image pattern for Unreal Tournament. [verified, all probes 404]
- Extracting the embedded screenshot from map packages would need file access and is out of
  scope (LANPilot is protocol-only).

## Counter-Strike

Ranked, best first:

1. **Steam Workshop** (CS:GO/CS2 workshop maps):
   `POST https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/` with
   `itemcount=1&publishedfileids[0]=<id>` works without a key and returns `preview_url`.
   [verified] The preview URL pointed to `images.steamusercontent.com` (JPEG). [verified
   2026-10-14 with item 3070288532] Neither CS2 nor CS:GO reports workshop ids over RCON in
   a usable form (see `games/counter-strike.md`), so this source only helps for maps whose
   adapter puts a `workshop_id` into `MapRef.extra`. Name search (`IPublishedFileService/QueryFiles`) needs an API
   key. [verified]
2. **GameBanana API v11** (custom maps):
   `GET https://gamebanana.com/apiv11/Util/Search/Results?_sSearchString=<map>&_idGameRow=<id>&_sModelName=Mod&_sOrder=best_match`.
   Game ids: Counter-Strike: Source = 2, Counter-Strike 1.6 = 4254, CS:GO = 4660,
   Counter-Strike 2 = 18134 (found with `_sModelName=Game`, 2026-10-14). Results carry
   `_aPreviewMedia._aImages[]` with `_sBaseUrl` (`https://images.gamebanana.com/img/ss/mods`)
   and `_sFile`; the constructed image URL loads (JPEG, up to a few hundred KB). [verified]
   The search is fuzzy: `de_dust2` returns `de_d2_remake`, `de_dust2_xmas08` and the like,
   so LANPilot only takes uploads named exactly like the map and does not ask for stock maps.
   Rate limits and terms are [unverified].
3. **GameTracker image CDN** (stock and popular maps, 160x120 only):
   `https://image.gametracker.com/images/maps/160x120/<game>/<map>.jpg`. Working game slugs:
   `cs`, `css`, `csgo`, `cod`, `cod2`, `cod4`, `q3`, `bf2`, `tf2`. [verified] There is no `cs2`
   slug; LANPilot uses the `csgo` pictures for CS2. Missing maps return 404. [verified again
   2026-10-14: 34 of 38 stock CS:GO maps had a picture] Terms of use are [unverified]; this is unofficial hotlinking,
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
