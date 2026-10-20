"""Map catalogue, online image sources and the image lookup. Nothing here uses the internet:
sources are tested with a fake fetcher, the fetcher itself against a local HTTP server."""

import asyncio
import io
import os
import time
from collections.abc import AsyncIterator, Callable, Mapping
from datetime import timedelta
from pathlib import Path
from types import TracebackType
from typing import Any

import pytest
from aiohttp import web
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lanpilot.auth.roles import Role
from lanpilot.config import ConfigError, Settings, load_settings
from lanpilot.core import catalog, image_lookup, image_sources, map_images
from lanpilot.core.adapters import MapRef
from lanpilot.core.adapters.registry import load_registry
from lanpilot.core.image_lookup import ImageLookup, Outcome, Reach, Wanted
from lanpilot.core.image_sources import (
    SOURCES,
    Fetcher,
    FetchError,
    GameBanana,
    GameTracker,
    ImageSource,
    LiandriArchives,
    SteamWorkshop,
    TrackmaniaExchange,
    UnrealArchive,
)
from lanpilot.core.map_images import SWEEP_MIN_AGE_SECONDS, MapImageIndex, sweep
from lanpilot.db.engine import create_engine, create_sessionmaker
from lanpilot.db.models import MapImage
from lanpilot.testing.goldsrc import GoldSrcSimulator
from tests.conftest import (
    ClientFactory,
    Prepare,
    Query,
    UserFactory,
    csrf_token,
    sign_in,
)
from tests.test_goldsrc import PASSWORD, background_server  # noqa: F401 - fixture

CS16 = {"gametracker": "cs", "gamebanana": "4254"}


def png(width: int = 64, height: int = 36) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (width, height), (200, 120, 40)).save(output, "PNG")
    return output.getvalue()


class FakeFetcher(Fetcher):
    """Answers from a table instead of the internet and records what was asked."""

    def __init__(self, answers: Mapping[str, object] | None = None) -> None:
        super().__init__(frozenset())
        self.answers = dict(answers or {})
        self.requests: list[str] = []

    async def __aenter__(self) -> "FakeFetcher":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    def _answer(self, url: str) -> Any:
        self.requests.append(url)
        answer = self.answers.get(url)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def get_json(self, url: str, params: Mapping[str, str] | None = None) -> Any:
        return self._answer(url)

    async def post_json(self, url: str, data: Mapping[str, str]) -> Any:
        return self._answer(url)

    async def get_image(self, url: str) -> bytes | None:
        answer = self._answer(url)
        return answer if isinstance(answer, bytes) else None


# --- catalogue -----------------------------------------------------------------------------


def test_catalogue_names_stock_maps() -> None:
    names = catalog.load()

    assert {"cs16", "css", "csgo", "cs2"} <= set(names.games())
    assert names.name("cs2", "de_dust2") == "Dust II"
    assert names.name("cs2", "DE_DUST2") == "Dust II"
    assert names.name("cs2", "rush_001") is None
    assert names.name("nosuchgame", "de_dust2") is None


def test_catalogue_keeps_names_the_server_gave() -> None:
    names = catalog.load()

    assert names.named("css", MapRef("de_dust2")).name == "Dust II"
    assert names.named("css", MapRef("de_dust2", "Server name")).name == "Server name"
    assert names.named("css", MapRef("aim_map")).name is None


@pytest.mark.parametrize(
    "text",
    [
        "[cs2]\n'bad key' = 'x'\n",
        "[cs2]\nde_x = ''\n",
        "[cs2]\nde_x = 3\n",
        '[cs2]\nde_x = "line\\nbreak"\n',  # TOML turns \n into a line break here
        "cs2 = 'not a table'\n",
        "[cs2\n",
    ],
)
def test_malformed_catalogue_is_rejected(text: str) -> None:
    with pytest.raises(catalog.CatalogError):
        catalog.parse(text, "test.toml")


# --- configuration -------------------------------------------------------------------------


def test_image_settings(base_env: dict[str, str]) -> None:
    env = {k: v for k, v in base_env.items() if k != "LANPILOT_IMAGES__ONLINE"}
    defaults = load_settings(environ=env).images
    assert defaults.online is True
    assert defaults.sources == [
        "steam_workshop",
        "gamebanana",
        "gametracker",
        "tmx",
        "unreal_archive",
        "liandri",
    ]
    assert list(SOURCES) == defaults.sources  # every source is configurable

    chosen = load_settings(
        environ={**env, "LANPILOT_IMAGES__SOURCES": "gametracker, gametracker"}
    ).images
    assert chosen.sources == ["gametracker"]

    with pytest.raises(ConfigError, match="unknown source"):
        load_settings(environ={**env, "LANPILOT_IMAGES__SOURCES": "gametracker,google"})


# --- fetcher -------------------------------------------------------------------------------


@pytest.fixture
async def local_web() -> AsyncIterator[str]:
    async def ok(request: web.Request) -> web.Response:
        return web.json_response({"hello": "world"})

    async def image(request: web.Request) -> web.Response:
        return web.Response(body=png(), content_type="image/png")

    async def huge(request: web.Request) -> web.Response:
        return web.Response(body=b"x" * (image_sources.MAX_JSON_BYTES + 10))

    async def redirect(request: web.Request) -> web.Response:
        raise web.HTTPFound("http://127.0.0.1:1/elsewhere")

    async def status(request: web.Request) -> web.Response:
        return web.Response(status=int(request.match_info["code"]))

    app = web.Application()
    app.router.add_get("/ok", ok)
    app.router.add_post("/ok", ok)
    app.router.add_get("/image", image)
    app.router.add_get("/huge", huge)
    app.router.add_get("/redirect", redirect)
    app.router.add_get("/status/{code}", status)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    yield f"http://127.0.0.1:{port}"
    await runner.cleanup()


def local_fetcher() -> Fetcher:
    return Fetcher(frozenset({"127.0.0.1"}), scheme="http", timeout_seconds=2)


async def test_fetcher_reads_allowed_hosts(local_web: str) -> None:
    async with local_fetcher() as fetcher:
        assert await fetcher.get_json(f"{local_web}/ok") == {"hello": "world"}
        assert await fetcher.post_json(f"{local_web}/ok", {"a": "b"}) == {"hello": "world"}
        assert await fetcher.get_image(f"{local_web}/image") == png()


async def test_fetcher_refuses_other_hosts_schemes_and_credentials(local_web: str) -> None:
    port = local_web.rsplit(":", 1)[1]
    async with local_fetcher() as fetcher:
        for url in (
            f"http://localhost:{port}/ok",  # same server, but not on the list
            f"https://127.0.0.1:{port}/ok",  # another scheme
            f"http://user:pw@127.0.0.1:{port}/ok",
            "file:///etc/passwd",
        ):
            assert await fetcher.get_json(url) is None


async def test_fetcher_never_follows_redirects(local_web: str) -> None:
    async with local_fetcher() as fetcher:
        assert await fetcher.get_json(f"{local_web}/redirect") is None


async def test_fetcher_limits_size_and_tells_misses_from_failures(local_web: str) -> None:
    async with local_fetcher() as fetcher:
        assert await fetcher.get_json(f"{local_web}/huge") is None
        assert await fetcher.get_json(f"{local_web}/status/404") is None
        for code in (429, 500, 503):
            with pytest.raises(FetchError):
                await fetcher.get_json(f"{local_web}/status/{code}")
        with pytest.raises(FetchError):
            await fetcher.get_json("http://127.0.0.1:1/unreachable")


# --- sources -------------------------------------------------------------------------------


async def test_steam_workshop_needs_an_id_and_the_right_game() -> None:
    answer = {
        "response": {
            "publishedfiledetails": [
                {"result": 1, "consumer_app_id": 730, "preview_url": "https://x/preview.jpg"}
            ]
        }
    }
    fetcher = FakeFetcher({SteamWorkshop.endpoint: answer})
    source = SteamWorkshop()
    workshop_map = MapRef("bhop_water", extra={"workshop_id": "3070288532"})

    assert await source.find(fetcher, {"steam_app": "730"}, workshop_map) == "https://x/preview.jpg"
    assert await source.find(fetcher, {"steam_app": "240"}, workshop_map) is None
    assert await source.find(fetcher, {}, workshop_map) is None
    assert await source.find(fetcher, {"steam_app": "730"}, MapRef("bhop_water")) is None
    evil = MapRef("x", extra={"workshop_id": "1&publishedfileids[1]=2"})
    assert await source.find(fetcher, {"steam_app": "730"}, evil) is None
    assert len(fetcher.requests) == 2


def banana(
    name: str, game: int = 4254, category: str = "Maps", file: str = "abc.jpg"
) -> dict[str, Any]:
    return {
        "_sName": name,
        "_aRootCategory": {"_sName": category},
        "_aGame": {"_idRow": game},
        "_aPreviewMedia": {"_aImages": [{"_sBaseUrl": GameBanana.image_base, "_sFile": file}]},
    }


async def test_gamebanana_takes_only_exact_matches() -> None:
    records = [
        banana("de_dust2_remake", file="wrong.jpg"),
        banana("aim_redline", category="Skins", file="skin.jpg"),
        banana("aim_redline", game=2, file="other-game.jpg"),
        banana("AIM_Redline", file="right.jpg"),
    ]
    fetcher = FakeFetcher({GameBanana.endpoint: {"_aRecords": records}})
    found = await GameBanana().find(fetcher, CS16, MapRef("aim_redline"))

    assert found == f"{GameBanana.image_base}/right.jpg"


@pytest.mark.parametrize(
    "record",
    [
        {"_sName": "aim_redline"},  # incomplete
        banana("aim_redline", file="../../evil.jpg"),
        {**banana("aim_redline"), "_aPreviewMedia": {"_aImages": [{"_sBaseUrl": "https://evil"}]}},
        "not a record",
    ],
)
async def test_gamebanana_ignores_odd_records(record: object) -> None:
    fetcher = FakeFetcher({GameBanana.endpoint: {"_aRecords": [record]}})

    assert await GameBanana().find(fetcher, CS16, MapRef("aim_redline")) is None


async def test_gametracker_builds_the_url_from_safe_parts_only() -> None:
    fetcher = FakeFetcher()
    source = GameTracker()

    url = await source.find(fetcher, CS16, MapRef("De_Dust2"))
    assert url == "https://image.gametracker.com/images/maps/160x120/cs/de_dust2.jpg"
    assert await source.find(fetcher, CS16, MapRef("../../etc")) is None
    assert await source.find(fetcher, {"gametracker": "c/s"}, MapRef("de_dust2")) is None
    assert await source.find(fetcher, {}, MapRef("de_dust2")) is None
    assert fetcher.requests == []


TMX_UID = "RVEoquUninLI8FqVo_ZCo4lr5X9"
TMX_SITES = {"tmx": "tmnf.exchange,tmuf.exchange"}


def tmx_answer(uid: str = TMX_UID, track_id: object = 2312, **flags: bool) -> object:
    return {"Results": [{"TrackId": track_id, "UId": uid, **flags}]}


async def test_tmx_finds_tracks_by_uid_on_the_listed_sites() -> None:
    nations, united = "https://tmnf.exchange/api/tracks", "https://tmuf.exchange/api/tracks"
    track = MapRef("B11-Race.Challenge.Gbx", extra={"uid": TMX_UID})
    source = TrackmaniaExchange()

    fetcher = FakeFetcher({nations: tmx_answer(HasImages=True)})
    assert await source.find(fetcher, TMX_SITES, track) == (
        "https://tmnf.exchange/trackshow/2312/image/1"
    )
    # Not on the Nations site: the United site is asked; no screenshot, but a thumbnail.
    fetcher = FakeFetcher({nations: {"Results": []}, united: tmx_answer(HasThumbnail=True)})
    assert await source.find(fetcher, TMX_SITES, track) == (
        "https://tmuf.exchange/trackshow/2312/thumbnail"
    )
    assert fetcher.requests == [nations, united]


@pytest.mark.parametrize(
    "answer",
    [
        tmx_answer(uid="SomeOtherTrackUId00000000", HasImages=True),  # not this track
        tmx_answer(track_id="2312", HasImages=True),
        tmx_answer(track_id=-1, HasImages=True),
        tmx_answer(),  # neither images nor a thumbnail: TMX would send a placeholder
        {"Results": "nope"},
        None,
    ],
)
async def test_tmx_never_guesses_an_image_url(answer: object) -> None:
    fetcher = FakeFetcher({"https://tmnf.exchange/api/tracks": answer})
    track = MapRef("x.Challenge.Gbx", extra={"uid": TMX_UID})

    assert await TrackmaniaExchange().find(fetcher, {"tmx": "tmnf.exchange"}, track) is None


async def test_tmx_needs_a_uid_and_known_sites() -> None:
    fetcher = FakeFetcher()
    source = TrackmaniaExchange()

    assert await source.find(fetcher, TMX_SITES, MapRef("x.Challenge.Gbx")) is None
    evil = MapRef("x", extra={"uid": "../../x&uid=y"})
    assert await source.find(fetcher, TMX_SITES, evil) is None
    other_site = {"tmx": "evil.example,tmnf.exchange.evil.example"}
    assert await source.find(fetcher, other_site, MapRef("x", extra={"uid": TMX_UID})) is None
    assert fetcher.requests == []


def test_every_source_url_stays_on_its_hosts() -> None:
    for source in SOURCES.values():
        for url in (getattr(source, "endpoint", None), getattr(source, "image_base", None)):
            if url:
                assert url.startswith("https://")
                assert url.split("/")[2] in source.hosts


# --- the lookup ----------------------------------------------------------------------------


class FixedSource(ImageSource):
    hosts = frozenset({"example.org"})

    def __init__(self, source_id: str, url: str | None, *, custom_only: bool = False) -> None:
        self.id = source_id  # type: ignore[misc]
        self.title = source_id  # type: ignore[misc]
        self.url = url
        self.custom_maps_only = custom_only  # type: ignore[misc]
        self.asked: list[str] = []

    async def find(self, fetcher: Fetcher, lookup: Mapping[str, str], ref: MapRef) -> str | None:
        self.asked.append(ref.key)
        return self.url


LookupFactory = Callable[..., ImageLookup]


@pytest.fixture
async def lookups(
    settings: Settings, prepare_database: Prepare
) -> AsyncIterator[tuple[LookupFactory, async_sessionmaker[AsyncSession], MapImageIndex]]:
    prepare_database(settings)
    engine = create_engine(settings.database_path)
    sessionmaker = create_sessionmaker(engine)
    index = MapImageIndex()

    def factory(sources: Mapping[str, ImageSource], **image_settings: object) -> ImageLookup:
        configured = settings.images.model_copy(
            update={"online": True, "sources": list(sources), **image_settings}
        )
        return ImageLookup(
            configured,
            index=index,
            sessionmaker=sessionmaker,
            data_dir=settings.data_dir,
            registry=load_registry(),
            sources=sources,
            pause_seconds=0,
        )

    yield factory, sessionmaker, index
    await engine.dispose()


def wanted(key: str, reach: Reach = Reach.AUTOMATIC, adapter_id: str = "cs16") -> Wanted:
    return Wanted(adapter_id, CS16, MapRef(key), reach)


async def test_first_usable_image_wins(lookups: Any) -> None:
    factory, sessionmaker, index = lookups
    sources = {
        "a": FixedSource("a", None),
        "b": FixedSource("b", "https://example.org/broken.jpg"),
        "c": FixedSource("c", "https://example.org/good.png"),
        "d": FixedSource("d", "https://example.org/never.png"),
    }
    fetcher = FakeFetcher(
        {"https://example.org/broken.jpg": b"not an image", "https://example.org/good.png": png()}
    )
    lookup = factory(sources)

    assert await lookup.lookup(fetcher, wanted("aim_map")) is Outcome.FOUND
    assert index.filename("cs16", "AIM_MAP") is not None
    assert sources["d"].asked == []
    async with sessionmaker() as db:
        row = (await db.execute(select(MapImage))).scalar_one()
    assert (row.provider, row.source_url, row.width) == ("c", "https://example.org/good.png", 64)

    # With an image in place nothing is asked any more.
    assert await lookup.lookup(fetcher, wanted("aim_map")) is Outcome.SKIPPED
    assert sources["a"].asked == ["aim_map"]


async def test_community_sources_are_not_asked_for_stock_maps(lookups: Any) -> None:
    factory, _, _ = lookups
    community = FixedSource("community", "https://example.org/remake.png", custom_only=True)
    lookup = factory({"community": community})

    assert await lookup.lookup(FakeFetcher(), wanted("de_dust2")) is Outcome.NOT_FOUND
    assert community.asked == []


async def test_misses_are_remembered_according_to_reach(lookups: Any) -> None:
    factory, sessionmaker, _ = lookups
    source = FixedSource("a", None)
    lookup = factory({"a": source})
    fetcher = FakeFetcher()

    assert await lookup.lookup(fetcher, wanted("aim_map")) is Outcome.NOT_FOUND
    assert await lookup.lookup(fetcher, wanted("aim_map")) is Outcome.SKIPPED
    assert await lookup.lookup(fetcher, wanted("aim_map", Reach.MISSING)) is Outcome.NOT_FOUND
    assert len(source.asked) == 2

    async with sessionmaker() as db:
        await image_lookup.remember_miss(db, "cs16", "aim_map", "removed")
        await db.commit()
    assert await lookup.lookup(fetcher, wanted("aim_map", Reach.MISSING)) is Outcome.SKIPPED
    assert await lookup.lookup(fetcher, wanted("aim_map", Reach.ONE)) is Outcome.NOT_FOUND
    assert len(source.asked) == 3


async def test_old_misses_are_asked_again(lookups: Any) -> None:
    factory, sessionmaker, _ = lookups
    source = FixedSource("a", None)
    lookup = factory({"a": source}, retry_days=1)
    await lookup.lookup(FakeFetcher(), wanted("aim_map"))
    async with sessionmaker() as db:
        miss = (await image_lookup.misses(db, "cs16"))["aim_map"]
        miss.checked_at = miss.checked_at - timedelta(days=2)
        await db.commit()

    assert await lookup.lookup(FakeFetcher(), wanted("aim_map")) is Outcome.NOT_FOUND
    assert len(source.asked) == 2


async def test_not_found_is_forgotten_at_start_up(lookups: Any) -> None:
    factory, sessionmaker, _ = lookups
    lookup = factory({"a": FixedSource("a", None)})
    await lookup.lookup(FakeFetcher(), wanted("aim_one"))
    async with sessionmaker() as db:
        await image_lookup.remember_miss(db, "cs16", "aim_two", "removed")
        await db.commit()
        assert await image_lookup.forget_not_found(db) == 1
        assert list(await image_lookup.misses(db, "cs16")) == ["aim_two"]


async def test_unreachable_sources_are_not_remembered_as_misses(lookups: Any) -> None:
    factory, sessionmaker, _ = lookups
    lookup = factory({"a": FixedSource("a", "https://example.org/x.png")})
    fetcher = FakeFetcher({"https://example.org/x.png": FetchError("down")})

    assert await lookup.lookup(fetcher, wanted("aim_map")) is Outcome.FAILED
    async with sessionmaker() as db:
        assert await image_lookup.misses(db, "cs16") == {}
    # The poll does not ask again for a while; an administrator may.
    assert not lookup.want("cs16", MapRef("aim_map"))
    assert lookup.want("cs16", MapRef("aim_map"), Reach.ONE)


async def test_queue_finds_images_in_the_background(lookups: Any) -> None:
    factory, _, index = lookups
    lookup = factory({"a": FixedSource("a", "https://example.org/x.png")})
    lookup._fetcher = lambda hosts: FakeFetcher({"https://example.org/x.png": png()})
    found = asyncio.Event()
    lookup.on_found = found.set
    lookup.start()
    try:
        assert lookup.want("cs16", MapRef("aim_one"))
        assert not lookup.want("cs16", MapRef("aim_one"))  # already queued
        assert not lookup.want("nosuchgame", MapRef("aim_two"))
        async with asyncio.timeout(5):
            await found.wait()
    finally:
        await lookup.stop()

    assert index.filename("cs16", "aim_one") is not None
    assert not lookup.want("cs16", MapRef("aim_one"))  # has an image now


async def test_poll_does_not_queue_a_map_without_image_again_and_again(lookups: Any) -> None:
    factory, _, _ = lookups
    source = FixedSource("a", None)
    lookup = factory({"a": source})
    lookup._fetcher = lambda hosts: FakeFetcher()
    lookup.start()
    try:
        assert lookup.want("cs16", MapRef("aim_map"))
        async with asyncio.timeout(5):
            while lookup._queued:  # noqa: ASYNC110 - nothing signals "not found"
                await asyncio.sleep(0.01)
        assert not lookup.want("cs16", MapRef("aim_map"))  # quiet for a while
        assert lookup.want("cs16", MapRef("aim_map"), Reach.MISSING)  # unless asked for
    finally:
        await lookup.stop()
    assert source.asked == ["aim_map"]


async def test_nothing_is_queued_when_switched_off(lookups: Any) -> None:
    factory, _, _ = lookups
    lookup = factory({"a": FixedSource("a", None)}, online=False)

    assert not lookup.enabled
    assert not lookup.want("cs16", MapRef("aim_map"), Reach.ONE)


# --- through the web interface -------------------------------------------------------------


def test_current_map_gets_an_image_automatically(
    background_server: GoldSrcSimulator,  # noqa: F811 - fixture
    make_client: ClientFactory,
    make_user: UserFactory,
    query: Query,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gametracker = "https://image.gametracker.com/images/maps/160x120/cs/de_dust2.jpg"
    fetchers: list[FakeFetcher] = []

    def fake_fetcher(hosts: frozenset[str]) -> FakeFetcher:
        fetchers.append(FakeFetcher({gametracker: png(160, 120)}))
        return fetchers[-1]

    monkeypatch.setattr(image_sources, "Fetcher", fake_fetcher)
    make_user("root", Role.ADMIN)
    client: TestClient = make_client(LANPILOT_IMAGES__ONLINE="true")
    sign_in(client, "root")

    response = client.post(
        "/admin/servers/new",
        data={
            "adapter": "cs16",
            "name": "CS Arena",
            "enabled": "1",
            "c_host": "127.0.0.1",
            "c_port": str(background_server.port),
            "c_rcon_password": PASSWORD,
            "csrf_token": csrf_token(client),
        },
    )
    assert response.status_code == 303
    [(server_id,)] = query("SELECT id FROM servers")

    deadline = time.monotonic() + 10
    while "/map-images/" not in client.get(f"/servers/{server_id}/card").text:
        assert time.monotonic() < deadline, "no image appeared"
        time.sleep(0.1)

    assert query("SELECT map_key, provider, width FROM map_images") == [
        ("de_dust2", "gametracker", 160)
    ]
    # Stock map: GameBanana was not asked, only GameTracker.
    assert [url for fetcher in fetchers for url in fetcher.requests] == [gametracker]


def test_a_current_map_the_server_does_not_list_is_not_looked_up(
    background_server: GoldSrcSimulator,  # noqa: F811 - fixture
    make_client: ClientFactory,
    make_user: UserFactory,
    query: Query,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The status names the current map; a server that keeps reporting new names must not
    # make LANPilot ask the image sources (and remember a miss) for each of them.
    fetchers: list[FakeFetcher] = []

    def fake_fetcher(hosts: frozenset[str]) -> FakeFetcher:
        fetchers.append(FakeFetcher())
        return fetchers[-1]

    monkeypatch.setattr(image_sources, "Fetcher", fake_fetcher)
    background_server.current_map = "fy_invented_1"
    make_user("root", Role.ADMIN)
    client: TestClient = make_client(
        LANPILOT_IMAGES__ONLINE="true", LANPILOT_POLLING__INTERVAL_SECONDS="2"
    )
    sign_in(client, "root")
    response = client.post(
        "/admin/servers/new",
        data={
            "adapter": "cs16",
            "name": "CS Arena",
            "enabled": "1",
            "c_host": "127.0.0.1",
            "c_port": str(background_server.port),
            "c_rcon_password": PASSWORD,
            "csrf_token": csrf_token(client),
        },
    )
    assert response.status_code == 303
    [(server_id,)] = query("SELECT id FROM servers")

    deadline = time.monotonic() + 10
    while "fy_invented_1" not in client.get(f"/servers/{server_id}/card").text:
        assert time.monotonic() < deadline, "the map did not show up"
        time.sleep(0.1)
    background_server.current_map = "fy_invented_2"
    while "fy_invented_2" not in client.get(f"/servers/{server_id}/card").text:
        assert time.monotonic() < deadline, "the map did not show up"
        time.sleep(0.1)
    time.sleep(0.5)

    assert [url for fetcher in fetchers for url in fetcher.requests] == []
    assert query("SELECT map_key FROM map_image_misses") == []


def test_admin_page_offers_online_lookup(
    make_client: ClientFactory,
    make_user: UserFactory,
    query: Query,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(image_sources, "Fetcher", lambda hosts: FakeFetcher())
    make_user("root", Role.ADMIN)
    client = make_client(LANPILOT_IMAGES__ONLINE="true", LANPILOT_ENABLE_DEMO_ADAPTER="true")
    sign_in(client, "root")
    client.post(
        "/admin/servers/new",
        data={
            "adapter": "demo",
            "name": "Demo",
            "enabled": "1",
            "c_host": "127.0.0.1",
            "c_password": "demo",
            "c_behaviour": "normal",
            "csrf_token": csrf_token(client),
        },
    )
    [(server_id,)] = query("SELECT id FROM servers")
    deadline = time.monotonic() + 10
    while not query("SELECT id FROM server_maps"):
        assert time.monotonic() < deadline
        time.sleep(0.1)
    [(map_id, map_key), *_] = query("SELECT id, key FROM server_maps ORDER BY id")

    page = client.get(f"/admin/servers/{server_id}/maps").text
    assert "Look for all missing images online" in page and "Look online" in page

    for path in ("maps/fetch-images", f"maps/{map_id}/image/fetch"):
        response = client.post(
            f"/admin/servers/{server_id}/{path}", data={"csrf_token": csrf_token(client)}
        )
        assert response.status_code == 303
        assert "notice=images_requested" in response.headers["location"]
    actions = [row[0] for row in query("SELECT action FROM audit_log ORDER BY id")]
    assert actions[-2:] == ["map_image.fetch_missing", "map_image.fetch"]

    # Removing an image is remembered, so the lookup does not bring it back.
    upload = client.post(
        f"/admin/servers/{server_id}/maps/{map_id}/image",
        data={"csrf_token": csrf_token(client)},
        files={"image": ("x.png", png(), "image/png")},
    )
    assert upload.status_code == 303
    client.post(
        f"/admin/servers/{server_id}/maps/{map_id}/image/delete",
        data={"csrf_token": csrf_token(client)},
    )
    assert query("SELECT map_key, reason FROM map_image_misses") == [(map_key.lower(), "removed")]
    assert "image removed" in client.get(f"/admin/servers/{server_id}/maps").text


def test_online_lookup_switched_off(
    make_client: ClientFactory, make_user: UserFactory, query: Query, tmp_path: Path
) -> None:
    make_user("root", Role.ADMIN)
    client = make_client(LANPILOT_ENABLE_DEMO_ADAPTER="true")
    sign_in(client, "root")
    client.post(
        "/admin/servers/new",
        data={
            "adapter": "demo",
            "name": "Demo",
            "enabled": "1",
            "c_host": "127.0.0.1",
            "c_password": "demo",
            "c_behaviour": "normal",
            "csrf_token": csrf_token(client),
        },
    )
    [(server_id,)] = query("SELECT id FROM servers")

    page = client.get(f"/admin/servers/{server_id}/maps").text
    assert "switched off" in page and "Look for all missing images online" not in page
    response = client.post(
        f"/admin/servers/{server_id}/maps/fetch-images", data={"csrf_token": csrf_token(client)}
    )
    assert response.status_code == 409


# --- command line --------------------------------------------------------------------------


def test_prefetch_command(
    make_client: ClientFactory,
    make_user: UserFactory,
    query: Query,
    base_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from lanpilot.cli import main

    # A server with a map list, created through the web interface.
    make_user("root", Role.ADMIN)
    client = make_client(LANPILOT_ENABLE_DEMO_ADAPTER="true")
    sign_in(client, "root")
    client.post(
        "/admin/servers/new",
        data={
            "adapter": "demo",
            "name": "Demo",
            "enabled": "1",
            "c_host": "127.0.0.1",
            "c_password": "demo",
            "c_behaviour": "normal",
            "csrf_token": csrf_token(client),
        },
    )
    deadline = time.monotonic() + 10
    while not query("SELECT id FROM server_maps"):
        assert time.monotonic() < deadline
        time.sleep(0.1)
    [(map_count,)] = query("SELECT count(*) FROM server_maps")

    for name, value in base_env.items():
        monkeypatch.setenv(name, value)
    assert main(["images", "prefetch"]) == 1
    assert "switched off" in capsys.readouterr().err

    # The demo game declares no image identifiers, so every source comes back empty.
    monkeypatch.setattr(image_sources, "Fetcher", lambda hosts: FakeFetcher())
    monkeypatch.setattr(image_lookup, "PAUSE_SECONDS", 0)
    monkeypatch.setenv("LANPILOT_IMAGES__ONLINE", "true")
    assert main(["images", "prefetch", "--server", "999"]) == 0
    assert "0 found, 0 not found" in capsys.readouterr().out
    assert main(["images", "prefetch"]) == 0
    assert f"0 found, {map_count} not found" in capsys.readouterr().out
    assert len(query("SELECT id FROM map_image_misses")) == map_count


async def test_every_game_has_its_own_images(lookups: Any) -> None:
    factory, _, index = lookups
    lookup = factory({"a": FixedSource("a", "https://example.org/x.png")})
    fetcher = FakeFetcher({"https://example.org/x.png": png()})

    assert await lookup.lookup(fetcher, wanted("de_dust2", adapter_id="cs16")) is Outcome.FOUND
    assert index.filename("cs16", "de_dust2") is not None
    assert index.filename("cs2", "de_dust2") is None  # same name, another game
    assert await lookup.lookup(fetcher, wanted("de_dust2", adapter_id="cs2")) is Outcome.FOUND
    assert index.filename("cs2", "de_dust2") != index.filename("cs16", "de_dust2")


def test_sweep_deletes_only_old_orphans(tmp_path: Path) -> None:
    index = MapImageIndex()
    index._set("cs16", "de_dust2", "k" * 24 + ".webp")
    known, orphan, fresh, other = (
        tmp_path / ("k" * 24 + ".webp"),
        tmp_path / ("o" * 24 + ".webp"),
        tmp_path / ("f" * 24 + ".webp"),
        tmp_path / "notes.txt",
    )
    for path in (known, orphan, fresh, other):
        path.write_bytes(b"x")
    old = time.time() - 2 * SWEEP_MIN_AGE_SECONDS
    for path in (known, orphan, other):
        os.utime(path, (old, old))

    assert sweep(tmp_path, index) == 1
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(
        [known.name, fresh.name, other.name]
    )
    assert sweep(tmp_path / "missing", index) == 0


# --- Unreal Archive --------------------------------------------------------------------------


async def test_unreal_archive_looks_up_the_bundled_index(monkeypatch: pytest.MonkeyPatch) -> None:
    image = "https://unreal-archive-img.s3.sgp.io.cloud.ovh.net/x/dm-zizeh_shot_1.png"
    index = {
        "Unreal Tournament 2004": {
            "dm-zizeh": [image, "Zizeh"],
            "dm-evil": ["https://evil.example/x.png", "Evil"],
        }
    }
    monkeypatch.setattr(image_sources, "unreal_index", lambda: index)
    source = UnrealArchive()
    fetcher = Fetcher(source.hosts)
    lookup = {"unreal_archive": "Unreal Tournament 2004"}

    assert await source.find(fetcher, lookup, MapRef("DM-Zizeh")) == image
    assert await source.find(fetcher, lookup, MapRef("DM-Zizeh?LinkSetup=Default")) == image
    assert await source.find(fetcher, lookup, MapRef("DM-Evil")) is None  # host not allowed
    assert await source.find(fetcher, lookup, MapRef("DM-Missing")) is None
    assert await source.find(fetcher, {}, MapRef("DM-Zizeh")) is None


def test_the_bundled_unreal_index_loads() -> None:
    index = image_sources.unreal_index()

    assert set(index) == {"Unreal Tournament", "Unreal Tournament 2004", "Unreal Tournament 3"}
    assert len(index["Unreal Tournament"]) > 1000
    url, title = index["Unreal Tournament"]["ctf-face"]
    assert url.startswith("https://unreal-archive-img.") and title == "Facing Worlds"


async def test_liandri_looks_up_the_bundled_index(monkeypatch: pytest.MonkeyPatch) -> None:
    base = "https://unrealarchive.org/wikis/the-liandri-archives/"
    index = {
        "Unreal Tournament 2004": {
            "dm-deck17": "w/images/DM-Deck17.gif",
            "dm-evil": "../../evil.gif",
        }
    }
    monkeypatch.setattr(image_sources, "liandri_index", lambda: (base, index))
    source = LiandriArchives()
    fetcher = Fetcher(source.hosts)
    lookup = {"liandri": "Unreal Tournament 2004"}
    expected = base + "w/images/DM-Deck17.gif"

    assert await source.find(fetcher, lookup, MapRef("DM-Deck17")) == expected
    assert await source.find(fetcher, lookup, MapRef("DM-Deck17?LinkSetup=Default")) == expected
    assert await source.find(fetcher, lookup, MapRef("DM-Evil")) is None  # not a plain file path
    assert await source.find(fetcher, lookup, MapRef("DM-Missing")) is None
    assert await source.find(fetcher, {}, MapRef("DM-Deck17")) is None


def test_sources_may_send_gif_but_uploads_may_not() -> None:
    buffer = io.BytesIO()
    Image.new("P", (64, 32)).save(buffer, "GIF")

    assert map_images.process(buffer.getvalue(), formats=map_images.SOURCE_FORMATS).width == 64
    with pytest.raises(map_images.ImageError):
        map_images.process(buffer.getvalue())


def test_the_bundled_liandri_index_loads() -> None:
    base, index = image_sources.liandri_index()

    assert base.startswith("https://unrealarchive.org/")
    assert set(index) == {"Unreal Tournament", "Unreal Tournament 2004", "Unreal Tournament 3"}
    assert index["Unreal Tournament 2004"]["dm-deck17"] == "w/images/DM-Deck17.gif"
    assert all(path.startswith("w/images/") for maps in index.values() for path in maps.values())


def test_liandri_index_builder() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "build_liandri_index", Path(__file__).parent.parent / "scripts" / "build_liandri_index.py"
    )
    assert spec is not None and spec.loader is not None
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)

    category = (
        '<a href="./Category:Stubs.html">x</a><a href="./Unreal_Tournament_2004.html">x</a>'
        '<a href="./DM-Deck17.html">a</a><a href="./DM-Deck16%5D%5B.html">b</a>'
        '<a href="./Help:Contents.html">c</a>'
    )
    assert builder.members(category) == ["DM-Deck17", "DM-Deck16]["]

    page = (
        '<a href="w/images/Placeholder.png">p</a><a href="w/images/Deck17-Pickups.png">w</a>'
        '<a href="w/images/DM-Deck17.gif">s</a>'
    )
    assert builder.screenshot(page) == builder.BASE + "w/images/DM-Deck17.gif"
    assert builder.screenshot('<a href="w/images/Placeholder.png">p</a>') is None


def test_unreal_index_builder(tmp_path: Path) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "build_unreal_index", Path(__file__).parent.parent / "scripts" / "build_unreal_index.py"
    )
    assert spec is not None and spec.loader is not None
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    host = "https://unreal-archive-img.s3.sgp.io.cloud.ovh.net"

    def upload(name: str, files: list[str], image: str, **fields: str) -> None:
        folder = tmp_path / "Unreal Tournament 2004" / "Maps" / "DeathMatch"
        folder.mkdir(parents=True, exist_ok=True)
        lines = ["--- !<MAP>", 'contentType: "MAP"']
        lines += [f'{key}: "{value}"' for key, value in fields.items()]
        lines += ["attachments:", '- type: "IMAGE"', f'  name: "{name}.png"', f'  url: "{image}"']
        lines += ["files:"] + [f'- name: "{file}"\n  fileSize: 1' for file in files]
        (folder / f"{name}.yml").write_text("\n".join(lines) + "\n")

    upload("variation", ["DM-Zizeh.ut2"], f"{host}/b.png", variationOf="x", firstIndex="2018")
    upload("original", ["DM-Zizeh.ut2", "Textures.utx"], f"{host}/a.png", firstIndex="2019")
    upload("deleted", ["DM-Gone.ut2"], f"{host}/c.png", deleted="true")
    upload("foreign", ["DM-Foreign.ut2"], "https://evil.example/d.png")
    upload("pack", ["DM-One.ut2", "DM-Two.ut2"], f"{host}/e.png", title="Two in one")

    games = builder.build(tmp_path)

    assert games["Unreal Tournament 2004"] == {
        "dm-zizeh": [f"{host}/a.png", ""],  # the original wins over the variation
        "dm-one": [f"{host}/e.png", "Two in one"],
        "dm-two": [f"{host}/e.png", "Two in one"],
    }
    assert games["Unreal Tournament"] == {} and games["Unreal Tournament 3"] == {}
