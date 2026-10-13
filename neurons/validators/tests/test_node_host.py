import socket

import aiohttp
import pytest
from aiohttp import web

from neurons.validators.src.services import node_host
from neurons.validators.src.services.node_host import (
    NodeHostError,
    PublicHostResolver,
    open_public_connection,
    public_host_connector,
    public_host_sock,
)


def _resolves_to(monkeypatch, *addresses, port=None):
    async def fake_getaddrinfo(self, host, requested_port, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port or requested_port)) for a in addresses
        ]

    monkeypatch.setattr("asyncio.BaseEventLoop.getaddrinfo", fake_getaddrinfo)


@pytest.fixture
def listener():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    yield server
    server.close()


@pytest.mark.asyncio
async def test_ip_literal_is_not_resolved(monkeypatch):
    # Arrange
    _resolves_to(monkeypatch)  # would raise if consulted

    # Act
    extra = await public_host_sock("10.0.0.5")

    # Assert
    assert extra == {}


@pytest.mark.asyncio
async def test_public_name_returns_a_socket_connected_to_the_validated_address(monkeypatch, listener):
    # Arrange: the name resolves to the local listener, treated as public for this test
    port = listener.getsockname()[1]
    _resolves_to(monkeypatch, "127.0.0.1", port=port)
    monkeypatch.setattr("ipaddress.IPv4Address.is_global", property(lambda self: True))

    # Act
    extra = await public_host_sock("gpu-node1.example.net", port, connect_timeout=5)

    # Assert
    sock = extra["sock"]
    try:
        assert sock.getpeername() == ("127.0.0.1", port)
    finally:
        sock.close()


@pytest.mark.asyncio
async def test_open_public_connection_reaches_the_validated_address(monkeypatch, listener):
    # Arrange: the name resolves to the local listener, treated as public for this test
    port = listener.getsockname()[1]
    _resolves_to(monkeypatch, "127.0.0.1", port=port)
    monkeypatch.setattr("ipaddress.IPv4Address.is_global", property(lambda self: True))

    # Act
    _, writer = await open_public_connection("gpu-node1.example.net", port)

    # Assert
    try:
        assert writer.get_extra_info("peername") == ("127.0.0.1", port)
    finally:
        writer.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["10.0.0.5", "127.0.0.1", "169.254.169.254", "192.168.1.2"])
async def test_name_with_a_non_public_address_is_refused(monkeypatch, bad):
    # Arrange
    _resolves_to(monkeypatch, "8.8.8.8", bad)

    # Act / Assert
    with pytest.raises(NodeHostError, match="non-public"):
        await public_host_sock("gpu-node1.example.net")


@pytest.mark.asyncio
async def test_unresolvable_name_is_refused(monkeypatch):
    # Arrange
    async def fail(self, host, port, **kwargs):
        raise socket.gaierror(-2, "Name or service not known")

    monkeypatch.setattr("asyncio.BaseEventLoop.getaddrinfo", fail)

    # Act / Assert
    with pytest.raises(NodeHostError, match="does not resolve"):
        await public_host_sock("gone.example.net")


def test_is_ip_literal():
    assert node_host.is_ip_literal("203.0.113.9") and node_host.is_ip_literal("[2001:db8::1]")
    assert not node_host.is_ip_literal("gpu-node1.example.net")


@pytest.mark.asyncio
async def test_resolver_returns_only_the_checked_public_addresses(monkeypatch):
    # Arrange
    _resolves_to(monkeypatch, "8.8.8.8", "1.1.1.1")

    # Act
    results = await PublicHostResolver().resolve("gpu-node1.example.net", 8080, family=socket.AF_UNSPEC)

    # Assert
    assert sorted(r["host"] for r in results) == ["1.1.1.1", "8.8.8.8"]
    assert {(r["hostname"], r["port"]) for r in results} == {("gpu-node1.example.net", 8080)}


@pytest.mark.asyncio
async def test_resolver_refuses_a_name_with_a_metadata_address(monkeypatch):
    # Arrange
    _resolves_to(monkeypatch, "8.8.8.8", "169.254.169.254")

    # Act / Assert
    with pytest.raises(NodeHostError, match="non-public"):
        await PublicHostResolver().resolve("gpu-node1.example.net", 80)


@pytest.mark.asyncio
async def test_http_request_to_a_name_goes_to_the_checked_address_with_the_name_as_host(monkeypatch):
    # Arrange: a local server stands in for the node; the name resolves to it, treated as public
    seen_hosts = []

    async def handler(request):
        seen_hosts.append(request.host)
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    lookups = []

    async def fake_getaddrinfo(self, host, requested_port, **kwargs):
        lookups.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", requested_port))]

    monkeypatch.setattr("asyncio.BaseEventLoop.getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr("ipaddress.IPv4Address.is_global", property(lambda self: True))

    # Act
    try:
        async with aiohttp.ClientSession(connector=public_host_connector()) as session:
            async with session.get(f"http://gpu-node1.example.net:{port}/") as response:
                body = await response.text()
    finally:
        await runner.cleanup()

    # Assert
    assert body == "ok"
    assert seen_hosts == [f"gpu-node1.example.net:{port}"]
    assert lookups == ["gpu-node1.example.net"]


@pytest.mark.asyncio
async def test_http_request_to_a_name_with_a_private_address_is_never_sent(monkeypatch):
    # Arrange
    _resolves_to(monkeypatch, "10.0.0.5")

    # Act / Assert
    async with aiohttp.ClientSession(connector=public_host_connector()) as session:
        with pytest.raises(aiohttp.ClientConnectorError):
            await session.get("http://gpu-node1.example.net:8080/")
