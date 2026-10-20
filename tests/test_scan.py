"""The on-demand LAN scan: discovery helpers, the scanner, its page and the CLI command."""

import asyncio
import socket
from collections.abc import Sequence
from ipaddress import IPv4Network
from typing import ClassVar, cast

import pytest
from fastapi.testclient import TestClient

from lanpilot.auth.roles import Role
from lanpilot.cli import main
from lanpilot.config import ConfigError, ScanSettings, load_settings
from lanpilot.core import discovery
from lanpilot.core.adapters import (
    Capability,
    ConnectionSettings,
    DiscoveredServer,
    GameAdapter,
    ServerStatus,
)
from lanpilot.core.adapters.registry import AdapterRegistry
from lanpilot.core.scan import Found, ScanBusyError, Scanner, ScanResult
from tests.conftest import ClientFactory, Query, UserFactory, csrf_token, sign_in

LOCAL = [IPv4Network("127.0.0.1/32")]


# --- helpers -----------------------------------------------------------------------------


def test_targets_are_broadcast_addresses_or_single_hosts() -> None:
    networks = discovery.parse_networks(["10.20.30.7/23", "192.168.1.0/24", "10.0.0.5/32"])
    assert discovery.targets(networks) == ["10.20.31.255", "192.168.1.255", "10.0.0.5"]


def test_inside_accepts_only_addresses_of_the_networks() -> None:
    networks = discovery.parse_networks(["10.20.30.0/23"])
    assert discovery.inside("10.20.31.4", networks)
    assert not discovery.inside("10.20.32.1", networks)
    assert not discovery.inside("not-an-address", networks)


def test_clean_text_keeps_printable_text_only() -> None:
    assert discovery.clean_text("  My\x00 Server\n ") == "My Server"
    assert discovery.clean_text("\x01\x02") is None
    assert discovery.clean_text("x" * 100) == "x" * discovery.MAX_TEXT_LENGTH


class _Echo(asyncio.DatagramProtocol):
    def __init__(self, answer: bytes | None) -> None:
        self.answer = answer
        self.received: list[bytes] = []

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        # Python 3.11's selector transport does not derive from DatagramTransport.
        self.transport = cast(asyncio.DatagramTransport, transport)

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.received.append(data)
        if self.answer is not None:
            self.transport.sendto(self.answer, addr)


async def _listen(answer: bytes | None) -> tuple[asyncio.DatagramTransport, _Echo, int]:
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: _Echo(answer), local_addr=("127.0.0.1", 0), family=socket.AF_INET
    )
    return transport, protocol, transport.get_extra_info("sockname")[1]


async def test_broadcast_collects_answers_from_inside_the_networks() -> None:
    transport, protocol, port = await _listen(b"here")
    try:
        replies = await discovery.broadcast(LOCAL, [(b"who", port)], 0.2)
    finally:
        transport.close()
    assert protocol.received == [b"who"]
    assert replies == [discovery.Reply(b"here", "127.0.0.1", port)]


def test_answers_from_outside_the_networks_and_beyond_the_limits_are_dropped() -> None:
    collector = discovery._Collector([IPv4Network("127.0.0.0/8")])
    collector.datagram_received(b"stray", ("10.0.0.1", 27015))
    for port in range(100):  # one host, many answers
        collector.datagram_received(b"ok", ("127.0.0.1", port))
    assert len(collector.replies) == discovery.MAX_REPLIES_PER_HOST
    for number in range(discovery.MAX_REPLIES + 5):
        collector.datagram_received(b"ok", (f"127.1.{number // 250}.{number % 250 + 1}", 1))
    assert len(collector.replies) == discovery.MAX_REPLIES
    assert all(reply.host.startswith("127.") for reply in collector.replies)


async def test_follow_ups_are_bounded_in_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(discovery, "FOLLOW_UP_SECONDS", 0.05)

    async def slow() -> int:
        await asyncio.sleep(10)
        return 1

    async def quick() -> int:
        return 2

    assert await discovery.gather_found([slow(), quick()]) == [2]


async def test_broadcast_can_use_a_fixed_source_port() -> None:
    transport, protocol, port = await _listen(None)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("0.0.0.0", 0))  # noqa: S104 - like the scan socket
        free = probe.getsockname()[1]
    try:
        await discovery.broadcast(LOCAL, [(b"beacon", port)], 0.1, source_port=free)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as taken:
            taken.bind(("0.0.0.0", 0))  # noqa: S104 - like the scan socket
            with pytest.raises(OSError):
                await discovery.broadcast(
                    LOCAL, [(b"x", port)], 0.1, source_port=taken.getsockname()[1]
                )
    finally:
        transport.close()
    assert protocol.received == [b"beacon"]


async def test_gather_found_drops_failures_and_nothing() -> None:
    async def value(result: object) -> object:
        if isinstance(result, Exception):
            raise result
        return result

    assert await discovery.gather_found([value(1), value(None), value(ValueError("x"))]) == [1]


# --- configuration ---------------------------------------------------------------------


def test_scan_networks_are_normalised_and_checked(base_env: dict[str, str]) -> None:
    settings = load_settings(
        environ={**base_env, "LANPILOT_SCAN__NETWORKS": "10.20.30.212/23, 10.20.30.0/23"}
    )
    assert settings.scan.networks == ["10.20.30.0/23"]
    assert load_settings(environ=base_env).scan.networks == []
    wide = load_settings(environ={**base_env, "LANPILOT_SCAN__NETWORKS": "10.0.0.0/16"})
    assert wide.scan.networks == ["10.0.0.0/16"]
    with pytest.raises(ConfigError, match="IPv4 network"):
        load_settings(environ={**base_env, "LANPILOT_SCAN__NETWORKS": "fd00::/64"})


# --- scanner ----------------------------------------------------------------------------


class _Connection(ConnectionSettings):
    port: int = 1


class _Findable(GameAdapter):
    id = "findable"
    name = "Findable"
    family = "test"
    capabilities = frozenset({Capability.STATUS, Capability.DISCOVER})
    connection_schema = _Connection
    finds: ClassVar[list[DiscoveredServer]] = []
    calls: ClassVar[list[tuple[list[str], float]]] = []
    gate: ClassVar[asyncio.Event | None] = None

    async def query(self) -> ServerStatus:
        return ServerStatus(name="x", players=0, max_players=1)

    @classmethod
    async def discover(
        cls, networks: Sequence[IPv4Network], wait: float
    ) -> Sequence[DiscoveredServer]:
        cls.calls.append(([str(network) for network in networks], wait))
        if cls.gate is not None:
            await cls.gate.wait()
        return cls.finds


class _Broken(_Findable):
    id = "broken"
    name = "Broken"

    @classmethod
    async def discover(
        cls, networks: Sequence[IPv4Network], wait: float
    ) -> Sequence[DiscoveredServer]:
        raise RuntimeError("bug")


class _Hidden(_Findable):
    id = "hidden"
    name = "Hidden"
    capabilities = frozenset({Capability.STATUS})

    @classmethod
    async def discover(
        cls, networks: Sequence[IPv4Network], wait: float
    ) -> Sequence[DiscoveredServer]:
        raise AssertionError("not declared, never asked")


def _scanner(*adapters: type[GameAdapter]) -> Scanner:
    registry = AdapterRegistry()
    for adapter in adapters:
        registry._adapters[adapter.id] = adapter  # _Hidden would fail validation on purpose
    return Scanner(registry, ScanSettings(networks=["10.0.0.0/24"], timeout_seconds=1))


@pytest.fixture(autouse=True)
def _reset_findable() -> None:
    _Findable.finds = []
    _Findable.calls = []
    _Findable.gate = None


async def test_scanner_reports_finds_marks_known_servers_and_drops_strays() -> None:
    _Findable.finds = [
        DiscoveredServer("findable", "10.0.0.9", 27015, name="B"),
        DiscoveredServer("findable", "10.0.0.10", 27015, name="C"),
        DiscoveredServer("findable", "10.0.0.9", 27015, name="duplicate"),
        DiscoveredServer("findable", "10.0.0.3", None, name="A"),
        DiscoveredServer("findable", "192.168.0.1", 27015, name="outside"),
        DiscoveredServer("other", "10.0.0.4", 27015, name="other game"),
    ]
    result = await _scanner(_Findable, _Broken, _Hidden).run([("findable", "10.0.0.10")])

    assert _Findable.calls == [(["10.0.0.0/24"], 1.0)]
    assert [(item.server.name, item.configured) for item in result.found] == [
        ("A", False),
        ("B", False),
        ("C", True),
    ]
    assert all(item.game == "Findable" for item in result.found)
    assert result.failed == ("Broken",)
    assert result.networks == ("10.0.0.0/24",)


async def test_only_one_scan_runs_at_a_time() -> None:
    scanner = _scanner(_Findable)
    _Findable.gate = asyncio.Event()
    first = asyncio.create_task(scanner.run())
    await asyncio.sleep(0)
    assert scanner.running
    with pytest.raises(ScanBusyError):
        await scanner.run()
    _Findable.gate.set()
    await first
    assert not scanner.running


async def test_a_slow_discovery_is_reported_as_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("lanpilot.core.scan.FOLLOW_UP_SECONDS", 0.0)
    scanner = Scanner(
        _scanner(_Findable)._registry, ScanSettings(networks=["10.0.0.0/24"], timeout_seconds=0.5)
    )
    _Findable.gate = asyncio.Event()
    result = await scanner.run()
    assert result.failed == ("Findable",)


# --- page -------------------------------------------------------------------------------


FOUND = ScanResult(
    networks=("10.20.30.0/23",),
    found=(
        Found(
            DiscoveredServer(
                "ut2004",
                "10.20.30.5",
                None,
                name="<b>Fun</b> & Frags",
                game_type="CTF",
                values={"query_port": "7787"},
            ),
            "Unreal Tournament 2004",
            False,
        ),
        Found(
            DiscoveredServer("cs16", "10.20.30.6", 27015, name="Known"), "Counter-Strike 1.6", True
        ),
    ),
    failed=("TrackMania Forever",),
    seconds=2.3,
)


@pytest.fixture
def scanning_admin(
    make_client: ClientFactory, make_user: UserFactory, monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    async def run(self: Scanner, configured: object = ()) -> ScanResult:
        return FOUND

    monkeypatch.setattr(Scanner, "run", run)
    make_user("root", Role.ADMIN)
    client = make_client(LANPILOT_SCAN__NETWORKS="10.20.30.0/23")
    sign_in(client, "root")
    return client


def test_scan_page_lists_finds_with_add_links(scanning_admin: TestClient, query: Query) -> None:
    page = scanning_admin.get("/admin/scan")
    assert page.status_code == 200
    assert "10.20.30.0/23" in page.text and "Scan now" in page.text

    response = scanning_admin.post("/admin/scan", data={"csrf_token": csrf_token(scanning_admin)})
    assert response.status_code == 200
    assert "&lt;b&gt;Fun&lt;/b&gt; &amp; Frags" in response.text  # escaped, never markup
    assert "<b>Fun</b>" not in response.text
    assert (
        "/admin/servers/new?adapter=ut2004&amp;c_host=10.20.30.5&amp;"
        "name=%3Cb%3EFun%3C%2Fb%3E+%26+Frags&amp;c_query_port=7787"
    ) in response.text
    assert "already added" in response.text
    assert "TrackMania Forever did not finish" in response.text
    [(_, detail)] = query("SELECT action, detail FROM audit_log WHERE action = 'scan.run'")
    assert '"found": 2' in detail


def test_scan_needs_csrf_and_an_administrator(
    scanning_admin: TestClient, make_client: ClientFactory, make_user: UserFactory
) -> None:
    assert scanning_admin.post("/admin/scan", data={}).status_code == 403
    make_user("operator", Role.OPERATOR)
    operator = make_client(LANPILOT_SCAN__NETWORKS="10.20.30.0/23")
    sign_in(operator, "operator")
    assert operator.get("/admin/scan").status_code == 403


def test_scan_without_networks_explains_the_setting(
    make_client: ClientFactory, make_user: UserFactory
) -> None:
    make_user("root", Role.ADMIN)
    client = make_client()
    sign_in(client, "root")
    assert "No networks are configured" in client.get("/admin/scan").text
    response = client.post("/admin/scan", data={"csrf_token": csrf_token(client)})
    assert response.status_code == 409


def test_a_busy_scanner_is_reported(
    make_client: ClientFactory, make_user: UserFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def busy(self: Scanner, configured: object = ()) -> ScanResult:
        raise ScanBusyError

    monkeypatch.setattr(Scanner, "run", busy)
    make_user("root", Role.ADMIN)
    client = make_client(LANPILOT_SCAN__NETWORKS="10.20.30.0/23")
    sign_in(client, "root")
    response = client.post("/admin/scan", data={"csrf_token": csrf_token(client)})
    assert response.status_code == 409
    assert "already running" in response.text


def test_add_link_fills_in_the_form_but_never_a_password(
    make_client: ClientFactory, make_user: UserFactory
) -> None:
    make_user("root", Role.ADMIN)
    client = make_client()
    sign_in(client, "root")
    page = client.get(
        "/admin/servers/new",
        params={
            "adapter": "ut2004",
            "name": "LAN <CTF>",
            "c_host": "10.20.30.5",
            "c_query_port": "7790",
            "c_password": "stolen",
        },
    ).text
    assert 'value="LAN &lt;CTF&gt;"' in page
    assert 'value="10.20.30.5"' in page
    assert 'value="7790"' in page
    assert "stolen" not in page
    assert "Check the host before you enter a password" in page
    plain = client.get("/admin/servers/new", params={"adapter": "ut2004"}).text
    assert "Check the host" not in plain


# --- command line -----------------------------------------------------------------------


def test_cli_scan_prints_finds(
    base_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def run(self: Scanner, configured: object = ()) -> ScanResult:
        assert self.networks == ("10.0.0.0/24",)
        return FOUND

    monkeypatch.setattr(Scanner, "run", run)
    for key, value in base_env.items():
        monkeypatch.setenv(key, value)
    assert main(["scan", "--network", "10.0.0.7/24"]) == 0
    out = capsys.readouterr().out
    assert "10.20.30.5" in out and "(CTF)" in out and "query_port=7787" in out
    assert "10.20.30.6:27015" in out
    assert "did not finish: TrackMania Forever" in out


def test_cli_scan_needs_networks(base_env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in base_env.items():
        monkeypatch.setenv(key, value)
    assert main(["scan"]) == 1
    assert main(["scan", "--network", "fd00::/64"]) == 1
