import io
import re
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from lanpilot.config import Settings
from lanpilot.core import map_images
from tests.conftest import Query, csrf_token
from tests.test_live import add_server, map_id, post, site, switch_user

__all__ = ["site"]  # fixture re-used from the live tests


def picture(size: tuple[int, int] = (64, 48), fmt: str = "PNG", **save: object) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(buffer, fmt, **save)
    return buffer.getvalue()


def upload(
    client: TestClient, server_id: int, row_id: str, data: bytes, name: str = "map.png"
) -> tuple[int, str]:
    response = client.post(
        f"/admin/servers/{server_id}/maps/{row_id}/image",
        data={"csrf_token": csrf_token(client)},
        files={"image": (name, data, "application/octet-stream")},
    )
    return response.status_code, response.text


def images_dir(settings: Settings) -> Path:
    return settings.data_dir / "map-images"


def image_url(page: str) -> str:
    match = re.search(r'<img src="(/map-images/[^"]+)"', page)
    assert match, "no map image on the page"
    return match.group(1)


def test_upload_is_reencoded_stored_privately_and_shown(
    site: TestClient, query: Query, settings: Settings
) -> None:
    server_id = add_server(site, query)

    status, _ = upload(site, server_id, map_id(query, server_id, "dm_foundry"), picture())

    assert status == 303
    [(game, key, filename, provider, width, height)] = query(
        "SELECT game, map_key, filename, provider, width, height FROM map_images"
    )
    assert (game, key, provider, width, height) == ("demo", "dm_foundry", "upload", 64, 48)
    stored = images_dir(settings) / filename
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600
    assert stat.S_IMODE(stored.parent.stat().st_mode) == 0o700
    with Image.open(stored) as image:
        assert image.format == "WEBP"

    # Foundry is the current map, so the dashboard card and the server page show the image.
    for path in ("/", f"/servers/{server_id}", f"/servers/{server_id}/card"):
        assert image_url(site.get(path).text) == f"/map-images/{filename}"

    response = site.get(f"/map-images/{filename}")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/webp"
    assert response.headers["cache-control"] == "private, max-age=31536000, immutable"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_metadata_and_trailing_content_do_not_survive(
    site: TestClient, query: Query, settings: Settings
) -> None:
    server_id = add_server(site, query)
    exif = Image.Exif()
    exif[0x010E] = "SECRET-GPS-AND-CAMERA-DATA"  # ImageDescription
    payload = picture(fmt="JPEG", exif=exif) + b"<script>alert('polyglot')</script>"
    assert b"SECRET-GPS-AND-CAMERA-DATA" in payload

    assert upload(site, server_id, map_id(query, server_id, "dm_foundry"), payload)[0] == 303

    [(filename,)] = query("SELECT filename FROM map_images")
    stored = (images_dir(settings) / filename).read_bytes()
    assert b"SECRET-GPS-AND-CAMERA-DATA" not in stored
    assert b"<script>" not in stored


def test_large_images_are_scaled_down(site: TestClient, query: Query) -> None:
    server_id = add_server(site, query)

    upload(site, server_id, map_id(query, server_id, "dm_foundry"), picture((4000, 1000)))

    assert query("SELECT width, height FROM map_images") == [(1280, 320)]


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"", "Choose an image file."),
        (b"this is not an image at all", "not a JPEG, PNG or WebP image"),
        (b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>", "not a JPEG"),
        (picture(fmt="GIF"), "not a JPEG, PNG or WebP image"),
        (picture()[:40], "not a JPEG, PNG or WebP image"),
        (picture((9000, 9000), compress_level=9), "too many pixels"),
    ],
)
def test_unusable_uploads_are_rejected(
    site: TestClient, query: Query, settings: Settings, data: bytes, message: str
) -> None:
    server_id = add_server(site, query)

    status, page = upload(site, server_id, map_id(query, server_id, "dm_foundry"), data)

    assert status == 400
    assert message in page
    assert query("SELECT count(*) FROM map_images") == [(0,)]
    assert list(images_dir(settings).glob("*")) == []


def test_oversized_upload_is_refused_before_it_is_read(site: TestClient, query: Query) -> None:
    server_id = add_server(site, query)
    row_id = map_id(query, server_id, "dm_foundry")

    status, page = upload(site, server_id, row_id, b"x" * (12 * 1024 * 1024))

    assert status == 413
    assert "larger than 11 MB" in page
    assert query("SELECT count(*) FROM map_images") == [(0,)]


def test_file_just_over_the_image_limit_is_rejected(site: TestClient, query: Query) -> None:
    server_id = add_server(site, query)
    data = picture() + b"\0" * map_images.MAX_UPLOAD_BYTES

    status, page = upload(site, server_id, map_id(query, server_id, "dm_foundry"), data)

    assert status == 400
    assert "larger than 10 MB" in page


def test_replacing_and_removing_an_image(
    site: TestClient, query: Query, settings: Settings
) -> None:
    server_id = add_server(site, query)
    row_id = map_id(query, server_id, "dm_foundry")
    upload(site, server_id, row_id, picture())
    [(first,)] = query("SELECT filename FROM map_images")

    upload(site, server_id, row_id, picture((80, 80)))

    [(second,)] = query("SELECT filename FROM map_images")
    assert second != first
    assert [path.name for path in images_dir(settings).iterdir()] == [second]
    assert site.get(f"/map-images/{first}").status_code == 404
    assert site.get(f"/map-images/{second}").status_code == 200

    assert post(site, f"/admin/servers/{server_id}/maps/{row_id}/image/delete")[0] == 303

    assert query("SELECT count(*) FROM map_images") == [(0,)]
    assert list(images_dir(settings).iterdir()) == []
    assert site.get(f"/map-images/{second}").status_code == 404
    assert "<img" not in site.get(f"/servers/{server_id}/card").text


def test_image_is_shared_by_servers_of_the_same_game(site: TestClient, query: Query) -> None:
    first = add_server(site, query, "First")
    second = add_server(site, query, "Second", c_host="other.lan")

    upload(site, first, map_id(query, first, "dm_foundry"), picture())

    assert "<img" in site.get(f"/servers/{second}/card").text


def test_images_are_listed_on_the_management_page(site: TestClient, query: Query) -> None:
    server_id = add_server(site, query)
    upload(site, server_id, map_id(query, server_id, "ctf_canyon"), picture())

    page = site.get(f"/admin/servers/{server_id}/maps").text

    assert page.count("<img") == 1
    assert page.count("Upload image") == 5
    assert page.count("Replace image") == 1
    assert "Twin Forts" in page


def test_only_generated_file_names_are_served(
    site: TestClient, query: Query, settings: Settings
) -> None:
    server_id = add_server(site, query)
    upload(site, server_id, map_id(query, server_id, "dm_foundry"), picture())
    # A file that exists in the cache directory but was not registered by LANPilot.
    stray = images_dir(settings) / ("s" * 24 + ".webp")
    stray.write_bytes(picture(fmt="WEBP"))

    for name in (stray.name, "lanpilot.db", "..%2Flanpilot.db", "%2e%2e%2f%2e%2e%2f.env", "x.webp"):
        assert site.get(f"/map-images/{name}").status_code == 404


def test_images_need_a_session(site: TestClient, query: Query) -> None:
    server_id = add_server(site, query)
    upload(site, server_id, map_id(query, server_id, "dm_foundry"), picture())
    [(filename,)] = query("SELECT filename FROM map_images")

    switch_user(site, "vera")
    assert site.get(f"/map-images/{filename}").status_code == 200

    site.cookies.clear()
    assert site.get(f"/map-images/{filename}").status_code == 303


@pytest.mark.parametrize("username", ["olga", "vera"])
def test_only_admins_manage_images(site: TestClient, query: Query, username: str) -> None:
    server_id = add_server(site, query)
    row_id = map_id(query, server_id, "dm_foundry")
    switch_user(site, username)

    assert site.get(f"/admin/servers/{server_id}/maps").status_code == 403
    assert upload(site, server_id, row_id, picture())[0] == 403
    assert post(site, f"/admin/servers/{server_id}/maps/{row_id}/image/delete")[0] == 403
    assert query("SELECT count(*) FROM map_images") == [(0,)]
    assert "Map images" not in site.get(f"/servers/{server_id}").text


def test_upload_needs_csrf_token_and_a_map_of_that_server(site: TestClient, query: Query) -> None:
    first = add_server(site, query, "First")
    second = add_server(site, query, "Second", c_host="other.lan")
    foreign = map_id(query, second, "dm_foundry")

    no_token = site.post(
        f"/admin/servers/{first}/maps/{map_id(query, first, 'dm_foundry')}/image",
        files={"image": ("map.png", picture(), "image/png")},
    )
    assert no_token.status_code == 403
    assert upload(site, first, foreign, picture())[0] == 404
    assert upload(site, 999, foreign, picture())[0] == 404
    assert query("SELECT count(*) FROM map_images") == [(0,)]


def test_uploads_are_audited(site: TestClient, query: Query) -> None:
    server_id = add_server(site, query)
    row_id = map_id(query, server_id, "dm_foundry")
    upload(site, server_id, row_id, picture())
    post(site, f"/admin/servers/{server_id}/maps/{row_id}/image/delete")

    rows = query(
        "SELECT action, actor_name, target_id FROM audit_log WHERE action LIKE 'map_image.%'"
        " ORDER BY id"
    )

    assert rows == [
        ("map_image.upload", "root", "demo/dm_foundry"),
        ("map_image.remove", "root", "demo/dm_foundry"),
    ]


def test_body_without_declared_length_is_refused(site: TestClient) -> None:
    def chunks() -> object:
        yield b"username=root&password=x"

    response = site.post(
        "/login",
        content=chunks(),  # type: ignore[arg-type]
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 411


def test_process_keeps_small_images_and_normalises_keys() -> None:
    result = map_images.process(picture((10, 20)))

    assert (result.width, result.height) == (10, 20)
    assert map_images.normalise_key("  DM-Deck16][ ") == "dm-deck16]["
