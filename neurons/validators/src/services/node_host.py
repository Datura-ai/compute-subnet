"""A node registered by hostname (dynamic DNS) is resolved before the validator dials it.

The hostname stays the node's address everywhere (its identity does not change when the IP behind
it does); this check only refuses a name that points anywhere but the public internet.
"""

import asyncio
import ipaddress
import socket


class NodeHostError(OSError):
    """The node's hostname does not resolve to public addresses only.

    An OSError so the connect site classifies it like any other failed dial: the node is reported
    EXECUTOR_SSH_UNREACHABLE (an availability error) and hidden, not scored as a generic failure.
    """


def is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


async def resolve_public_host(host: str, port: int = 22) -> list:
    """The getaddrinfo results to dial for ``host``: [] for an IP literal (dial it as is), else every
    address the name resolves to, all of them global; NodeHostError otherwise."""
    if is_ip_literal(host):
        return []
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise NodeHostError(f"{host} does not resolve: {exc}") from exc
    addresses = {ipaddress.ip_address(info[4][0]) for info in infos}
    if not addresses:
        raise NodeHostError(f"{host} resolves to no address")
    blocked = sorted(str(a) for a in addresses if not a.is_global)
    if blocked:
        raise NodeHostError(f"{host} resolves to a non-public address: {', '.join(blocked)}")
    return infos


async def public_host_sock(host: str, port: int = 22, connect_timeout: float | None = None) -> dict:
    """Return the extra ``asyncssh.connect`` kwargs that make it dial only a validated address.

    An IP literal needs nothing ({}: unchanged behaviour). A name is resolved once and refused
    (NodeHostError) unless every address is global; a TCP socket is then opened to one of those
    addresses and returned as ``{"sock": ...}``. asyncssh runs over that socket instead of
    resolving the name again (which a rebinding DNS server could answer with a private address),
    while still matching known_hosts against ``host``, the name.
    """
    infos = await resolve_public_host(host, port)
    if not infos:
        return {}
    sock = await asyncio.wait_for(_dial_first(asyncio.get_running_loop(), infos), connect_timeout)
    return {"sock": sock}


async def open_public_connection(
    host: str, port: int, **kwargs
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """``asyncio.open_connection`` that dials a hostname only at an address it was validated against."""
    pinned = await public_host_sock(host, port)
    if pinned:
        return await asyncio.open_connection(**pinned, **kwargs)
    return await asyncio.open_connection(host, port, **kwargs)


def dial_validated(infos: list, timeout: float | None) -> socket.socket:
    """Blocking TCP connect to the first reachable address of ``resolve_public_host``'s results (no lookup)."""
    last_error: OSError | None = None
    for family, sock_type, proto, _, sockaddr in infos:
        sock = socket.socket(family, sock_type, proto)
        try:
            sock.settimeout(timeout)
            sock.connect(sockaddr)
        except OSError as exc:
            sock.close()
            last_error = exc
            continue
        except BaseException:
            sock.close()
            raise
        return sock
    raise last_error


async def _dial_first(loop: asyncio.AbstractEventLoop, infos: list) -> socket.socket:
    last_error: OSError | None = None
    for family, sock_type, proto, _, sockaddr in infos:
        sock = socket.socket(family, sock_type, proto)
        try:
            sock.setblocking(False)
            await loop.sock_connect(sock, sockaddr)
        except OSError as exc:
            sock.close()
            last_error = exc
            continue
        except BaseException:
            sock.close()
            raise
        return sock
    raise last_error
