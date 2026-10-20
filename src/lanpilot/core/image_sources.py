"""Online sources of map preview images, and the only code that fetches from the internet.

Every request goes through ``Fetcher``: HTTPS only, to a fixed list of hosts, redirects are
never followed, answers are size-limited and time-limited. What comes back is untrusted:
JSON is picked apart defensively and images go through the same re-encoding as uploads
(``map_images.process``).

A source turns a map into the URL of a candidate image. It knows nothing about games; an
adapter declares the identifiers a source needs in ``GameAdapter.image_lookup`` (for
example GameTracker's slug ``css``). Endpoints and their observed behaviour are described in
docs/research/map-image-sources.md.
"""

from __future__ import annotations

import functools
import gzip
import json
import re
from abc import ABC, abstractmethod
from collections.abc import Mapping
from importlib import resources
from types import TracebackType
from typing import Any, ClassVar
from urllib.parse import urlsplit

import aiohttp

from lanpilot import __version__
from lanpilot.core.adapters import MapRef
from lanpilot.core.map_images import MAX_UPLOAD_BYTES

USER_AGENT = f"LANPilot/{__version__} (+https://github.com/lan-dot-party/LANPilot)"
MAX_JSON_BYTES = 2 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 15.0
# Map keys that can be put into a URL path or a search without further escaping.
SAFE_KEY = re.compile(r"[A-Za-z0-9_.-]{1,64}")


class FetchError(Exception):
    """The source could not be asked right now (network, server error, rate limit). Unlike
    a plain miss, this is worth trying again later."""


class Fetcher:
    """HTTP client restricted to an allow-list of hosts. Use as an async context manager."""

    def __init__(
        self,
        allowed_hosts: frozenset[str],
        *,
        timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
        scheme: str = "https",
    ) -> None:
        self._allowed_hosts = allowed_hosts
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._scheme = scheme  # tests use plain HTTP against a local server
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> Fetcher:
        self._session = aiohttp.ClientSession(
            timeout=self._timeout,
            headers={"User-Agent": USER_AGENT},
            # Proxies from the environment would be another way out of the allow-list.
            trust_env=False,
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def allows(self, url: str) -> bool:
        parts = urlsplit(url)
        return (
            parts.scheme == self._scheme
            and parts.hostname in self._allowed_hosts
            and parts.username is None
            and parts.password is None
        )

    async def _request(
        self,
        method: str,
        url: str,
        limit: int,
        *,
        params: Mapping[str, str] | None = None,
        data: Mapping[str, str] | None = None,
    ) -> bytes | None:
        """The body of a 200 answer; ``None`` for anything that means "not here"."""
        if self._session is None:
            raise RuntimeError("Fetcher is not open")
        if not self.allows(url):
            return None
        try:
            async with self._session.request(
                method, url, params=params, data=data, allow_redirects=False
            ) as response:
                if response.status == 429 or response.status >= 500:
                    raise FetchError(f"{urlsplit(url).hostname} answered {response.status}")
                if response.status != 200:
                    return None  # 404, and redirects, which are never followed
                body = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    body += chunk
                    if len(body) > limit:
                        return None
                return bytes(body)
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise FetchError(f"{urlsplit(url).hostname} could not be reached") from exc

    async def get_json(self, url: str, params: Mapping[str, str] | None = None) -> Any:
        return _json(await self._request("GET", url, MAX_JSON_BYTES, params=params))

    async def post_json(self, url: str, data: Mapping[str, str]) -> Any:
        return _json(await self._request("POST", url, MAX_JSON_BYTES, data=data))

    async def get_image(self, url: str) -> bytes | None:
        return await self._request("GET", url, MAX_UPLOAD_BYTES)


def _json(body: bytes | None) -> Any:
    if body is None:
        return None
    try:
        return json.loads(body)
    except ValueError:
        return None


def _dig(data: Any, *path: str | int) -> Any:
    """``data[path[0]][path[1]]…``, or ``None`` as soon as something does not fit."""
    for step in path:
        if isinstance(step, int):
            if not isinstance(data, list) or len(data) <= step:
                return None
        elif not isinstance(data, dict):
            return None
        data = data[step] if isinstance(step, int) else data.get(step)
    return data


class ImageSource(ABC):
    #: Name used in the configuration (``images.sources``) and stored with the image.
    id: ClassVar[str]
    #: Name shown to users.
    title: ClassVar[str]
    #: Hosts this source fetches from.
    hosts: ClassVar[frozenset[str]]
    #: Only for maps that are not stock maps: a community site has remakes, not originals.
    custom_maps_only: ClassVar[bool] = False

    @abstractmethod
    async def find(self, fetcher: Fetcher, lookup: Mapping[str, str], ref: MapRef) -> str | None:
        """The URL of an image of this map, or ``None``. ``lookup`` is the adapter's
        ``image_lookup``. Raises ``FetchError`` when the source cannot be asked."""


class SteamWorkshop(ImageSource):
    """Preview image of a Steam Workshop item; needs the item id in ``MapRef.extra``."""

    id = "steam_workshop"
    title = "Steam Workshop"
    hosts = frozenset(
        {"api.steampowered.com", "images.steamusercontent.com", "steamuserimages-a.akamaihd.net"}
    )
    endpoint = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"

    async def find(self, fetcher: Fetcher, lookup: Mapping[str, str], ref: MapRef) -> str | None:
        item = ref.extra.get("workshop_id", "")
        app = lookup.get("steam_app")
        if not app or not item.isdigit() or len(item) > 20:
            return None
        answer = await fetcher.post_json(
            self.endpoint, {"itemcount": "1", "publishedfileids[0]": item}
        )
        details = _dig(answer, "response", "publishedfiledetails", 0)
        if _dig(details, "result") != 1 or str(_dig(details, "consumer_app_id")) != app:
            return None
        url = _dig(details, "preview_url")
        return url if isinstance(url, str) else None


class GameBanana(ImageSource):
    """First screenshot of a GameBanana map whose name is exactly the map key."""

    id = "gamebanana"
    title = "GameBanana"
    hosts = frozenset({"gamebanana.com", "images.gamebanana.com"})
    custom_maps_only = True
    endpoint = "https://gamebanana.com/apiv11/Util/Search/Results"
    image_base = "https://images.gamebanana.com/img/ss/mods"
    image_file = re.compile(r"[A-Za-z0-9_.-]{1,100}")

    async def find(self, fetcher: Fetcher, lookup: Mapping[str, str], ref: MapRef) -> str | None:
        game = lookup.get("gamebanana")
        if not game or not game.isdigit() or not SAFE_KEY.fullmatch(ref.key):
            return None
        answer = await fetcher.get_json(
            self.endpoint,
            {
                "_sSearchString": ref.key,
                "_idGameRow": game,
                "_sModelName": "Mod",
                "_sOrder": "best_match",
            },
        )
        records = _dig(answer, "_aRecords")
        for record in records if isinstance(records, list) else []:
            # The search is fuzzy; only an upload named exactly like the map counts.
            if (
                str(_dig(record, "_sName")).lower() != ref.key.lower()
                or _dig(record, "_aRootCategory", "_sName") != "Maps"
                or str(_dig(record, "_aGame", "_idRow")) != game
            ):
                continue
            image = _dig(record, "_aPreviewMedia", "_aImages", 0)
            base, filename = _dig(image, "_sBaseUrl"), _dig(image, "_sFile")
            if (
                base == self.image_base
                and isinstance(filename, str)
                and self.image_file.fullmatch(filename)
            ):
                return f"{base}/{filename}"
        return None


class GameTracker(ImageSource):
    """GameTracker's map thumbnails (160x120), by game slug and map name."""

    id = "gametracker"
    title = "GameTracker"
    hosts = frozenset({"image.gametracker.com"})

    async def find(self, fetcher: Fetcher, lookup: Mapping[str, str], ref: MapRef) -> str | None:
        slug = lookup.get("gametracker")
        if not slug or not SAFE_KEY.fullmatch(slug) or not SAFE_KEY.fullmatch(ref.key):
            return None
        key = ref.key.lower()
        return f"https://image.gametracker.com/images/maps/160x120/{slug}/{key}.jpg"


class TrackmaniaExchange(ImageSource):
    """Screenshot or thumbnail of a track on TrackMania Exchange, found by its UId.

    The adapter names the TMX sites to ask, comma-separated (``tmnf.exchange``,
    ``tmuf.exchange``). Image URLs are only built from the id TMX returned for exactly this
    UId: for an unknown id TMX answers with a placeholder picture, not with 404.
    """

    id = "tmx"
    title = "TrackMania Exchange"
    hosts = frozenset({"tmnf.exchange", "tmuf.exchange"})
    uid = re.compile(r"[A-Za-z0-9_-]{20,40}")

    async def find(self, fetcher: Fetcher, lookup: Mapping[str, str], ref: MapRef) -> str | None:
        uid = ref.extra.get("uid", "")
        if not self.uid.fullmatch(uid):
            return None
        for site in lookup.get("tmx", "").split(","):
            if site not in self.hosts:
                continue
            answer = await fetcher.get_json(
                f"https://{site}/api/tracks",
                {"fields": "TrackId,UId,HasImages,HasThumbnail", "uid": uid},
            )
            found = _dig(answer, "Results", 0)
            track_id = _dig(found, "TrackId")
            if _dig(found, "UId") != uid or not isinstance(track_id, int) or track_id <= 0:
                continue
            if _dig(found, "HasImages") is True:
                return f"https://{site}/trackshow/{track_id}/image/1"
            if _dig(found, "HasThumbnail") is True:
                return f"https://{site}/trackshow/{track_id}/thumbnail"
        return None


UNREAL_INDEX = "unreal-archive.json.gz"


@functools.cache
def unreal_index() -> dict[str, dict[str, list[str]]]:
    """The bundled index game -> lower-case map name -> [image URL, title], built from
    Unreal Archive's public-domain metadata by scripts/build_unreal_index.py."""
    try:
        data = resources.files("lanpilot.catalog").joinpath(UNREAL_INDEX).read_bytes()
        games = json.loads(gzip.decompress(data))["games"]
    except (OSError, ValueError, KeyError):
        return {}
    return games if isinstance(games, dict) else {}


class UnrealArchive(ImageSource):
    """First screenshot of an Unreal Archive upload of the map, from the bundled index.

    Looking up needs no request; only the image is fetched. The archive collects community
    maps, so stock maps are mostly missing.
    """

    id = "unreal_archive"
    title = "Unreal Archive"
    hosts = frozenset(
        {"unreal-archive-img.s3.sgp.io.cloud.ovh.net", "ua-img.s3.us-west-002.backblazeb2.com"}
    )

    async def find(self, fetcher: Fetcher, lookup: Mapping[str, str], ref: MapRef) -> str | None:
        game = lookup.get("unreal_archive")
        if not game:
            return None
        found = unreal_index().get(game, {}).get(ref.key.split("?")[0].lower())
        url = found[0] if isinstance(found, list) and found else None
        return url if isinstance(url, str) and fetcher.allows(url) else None


LIANDRI_INDEX = "liandri.json.gz"
LIANDRI_HOST = "unrealarchive.org"


@functools.cache
def liandri_index() -> tuple[str, dict[str, dict[str, str]]]:
    """The bundled index (base URL, game -> lower-case map name -> path below the base),
    built from the Liandri Archives mirror by scripts/build_liandri_index.py."""
    try:
        data = resources.files("lanpilot.catalog").joinpath(LIANDRI_INDEX).read_bytes()
        document = json.loads(gzip.decompress(data))
        base, games = document["base"], document["games"]
    except (OSError, ValueError, KeyError):
        return "", {}
    if not isinstance(base, str) or not isinstance(games, dict):
        return "", {}
    return base, games


class LiandriArchives(ImageSource):
    """Screenshot from the Liandri Archives (the Unreal wiki), from the bundled index.

    Looking up needs no request; only the image is fetched. Unlike Unreal Archive the wiki
    describes the stock maps, but hardly any community maps.
    """

    id = "liandri"
    title = "Liandri Archives"
    hosts = frozenset({LIANDRI_HOST})

    async def find(self, fetcher: Fetcher, lookup: Mapping[str, str], ref: MapRef) -> str | None:
        game = lookup.get("liandri")
        if not game:
            return None
        base, games = liandri_index()
        path = games.get(game, {}).get(ref.key.split("?")[0].lower())
        if not isinstance(path, str) or not re.fullmatch(r"w/images/[A-Za-z0-9_.\-]+", path):
            return None
        url = base + path
        return url if fetcher.allows(url) else None


SOURCES: dict[str, ImageSource] = {
    source.id: source
    for source in (
        SteamWorkshop(),
        GameBanana(),
        GameTracker(),
        TrackmaniaExchange(),
        UnrealArchive(),
        LiandriArchives(),
    )
}
