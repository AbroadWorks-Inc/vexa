"""Which webhook subscription URLs admin-api accepts on save (§2.7).

The rules are meeting-api's send-time SSRF guard (``meeting_api/webhooks/ssrf.py``) plus the
private-host allow-list; the two services share no code, so both test suites read the same vectors
(``core/identity/contracts/webhook-subscriptions/url-guard.vectors.json``):

- only ``http`` and ``https``, and a host is required;
- a URL that does not parse is refused, and so is a port that is given but is not a number from 1
  to 65535, allow-listed host or not;
- a host in ``WEBHOOK_PRIVATE_HOST_ALLOWLIST`` (comma-separated, compared case-insensitively) is
  accepted as is, without resolving it;
- the internal hostnames below are refused;
- a literal address is refused when it is private, loopback, link-local, multicast, "this
  network", unspecified, shared address space (CGNAT), benchmarking, NAT64 or IPv4-compatible
  (``_BLOCKED_NETWORKS``); an IPv4-mapped IPv6 address (``::ffff:a.b.c.d``) is judged as the IPv4
  address it maps;
- a DNS name is resolved, and refused when it resolves to nothing or when ANY of its addresses
  is refused.

A refusal says why in words, never echoing the URL.

Resolving a name blocks, so request handlers call ``check_subscription_url_off_loop``: the check
runs in a worker thread under a total timeout, and a check that outlasts it is a refusal. admin-api
also answers the gateway's token validation, which a resolver hanging on the event loop would stall.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Callable, Collection, List, Optional
from urllib.parse import urlparse

__all__ = [
    "DEFAULT_PRIVATE_HOST_ALLOWLIST",
    "UrlRefused",
    "Resolver",
    "parse_allowlist",
    "check_subscription_url",
    "check_subscription_url_off_loop",
]

DEFAULT_PRIVATE_HOST_ALLOWLIST = "portal.notetaker.svc.cluster.local"

Resolver = Callable[[str], List[str]]

# meeting-api's ssrf.py holds the same list.
_BLOCKED_NETWORKS = [
    ipaddress.ip_network(net)
    for net in (
        "0.0.0.0/8",  # this network
        "10.0.0.0/8",  # private
        "100.64.0.0/10",  # shared address space (CGNAT)
        "127.0.0.0/8",  # loopback
        "169.254.0.0/16",  # link-local, cloud metadata
        "172.16.0.0/12",  # private
        "192.168.0.0/16",  # private
        "198.18.0.0/15",  # benchmarking
        "224.0.0.0/4",  # multicast
        "::/96",  # unspecified (::) and IPv4-compatible (::a.b.c.d)
        "::1/128",  # loopback
        "64:ff9b::/96",  # NAT64 (64:ff9b::a.b.c.d)
        "fc00::/7",  # unique local
        "fe80::/10",  # link-local
        "ff00::/8",  # multicast
    )
]

_BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "metadata.google.internal",
        "metadata.amazonaws.com",
        "metadata",
        "api-gateway",
        "admin-api",
        "meeting-api",
        "runtime-api",
        "transcription-collector",
        "redis",
        "postgres",
        "mcp",
    }
)

_PRIVATE = "the URL can't point at an internal or private network"


class UrlRefused(ValueError):
    """The URL may not be a webhook target; the message says why."""


def parse_allowlist(value: Optional[str]) -> frozenset[str]:
    """``WEBHOOK_PRIVATE_HOST_ALLOWLIST`` as a set of lower-cased hosts."""
    return frozenset(
        host.strip().lower() for host in (value or "").split(",") if host.strip()
    )


def _is_blocked_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return True
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return any(ip.version == net.version and ip in net for net in _BLOCKED_NETWORKS)


def _resolve(hostname: str) -> List[str]:
    try:
        results = socket.getaddrinfo(hostname, None)
    except OSError:
        return []
    addresses: List[str] = []
    for *_, sockaddr in results:
        address = str(sockaddr[0])
        if address and address not in addresses:
            addresses.append(address)
    return addresses


def check_subscription_url(
    url: str,
    *,
    allowlist: Collection[str],
    resolver: Optional[Resolver] = None,
) -> None:
    """Raise ``UrlRefused`` unless ``url`` may receive webhooks."""
    try:
        parsed = urlparse(url)
    except ValueError:
        raise UrlRefused("the URL could not be parsed") from None
    if parsed.scheme not in ("http", "https"):
        raise UrlRefused("the URL must use http or https")
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        raise UrlRefused("the URL must have a host")
    try:
        port = parsed.port
    except ValueError:
        port = 0
    if port == 0:
        raise UrlRefused("the URL's port must be a number from 1 to 65535")
    if hostname in {host.strip().lower() for host in allowlist}:
        return
    if hostname in _BLOCKED_HOSTNAMES:
        raise UrlRefused(_PRIVATE)
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        addresses = (resolver or _resolve)(hostname)
        if not addresses:
            raise UrlRefused("the URL's host could not be resolved")
    else:
        addresses = [hostname]
    if any(_is_blocked_ip(address) for address in addresses):
        raise UrlRefused(_PRIVATE)


async def check_subscription_url_off_loop(
    url: str,
    *,
    allowlist: Collection[str],
    resolver: Optional[Resolver] = None,
    timeout_s: float,
) -> None:
    """``check_subscription_url`` in a worker thread; past ``timeout_s`` the URL is refused."""
    try:
        await asyncio.wait_for(
            asyncio.to_thread(
                check_subscription_url, url, allowlist=allowlist, resolver=resolver
            ),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError as exc:
        raise UrlRefused("the URL's host could not be resolved in time") from exc
