"""SSRF protection primitives: which IP addresses a crawl may ever connect to.

The crawler fetches user-supplied URLs from inside our network, so the destination must be proven
public *at connection time*, not inferred from the URL text. The rules:

* Resolve the host name ourselves and require EVERY returned address to be public. A name with one
  public and one private record is refused rather than gambled on.
* The address that was validated is the address that is connected to (see fetcher.py), so a DNS
  answer that changes between "check" and "connect" (DNS rebinding) cannot redirect the connection.
* IPv4-in-IPv6 encodings (::ffff:a.b.c.d, NAT64, 6to4) are unwrapped and the embedded IPv4 address
  is judged, so they cannot be used to smuggle a private address past an IPv6-only check.
"""

import ipaddress
import socket
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int], Sequence[str]]
IpValidator = Callable[[IPAddress], bool]

DNS_TIMEOUT_SECONDS = 5.0

# Belt and braces on top of ipaddress.is_global: ranges that must never be reachable even if a
# future Python release changes how is_global classifies them.
_BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",  # "this network"
        "10.0.0.0/8",  # private
        "100.64.0.0/10",  # carrier-grade NAT (also Alibaba Cloud metadata 100.100.100.200)
        "127.0.0.0/8",  # loopback
        "169.254.0.0/16",  # link-local, cloud metadata (169.254.169.254)
        "172.16.0.0/12",  # private
        "192.0.0.0/24",  # IETF protocol assignments (incl. Oracle Cloud metadata 192.0.0.192)
        "192.0.2.0/24",  # documentation
        "192.168.0.0/16",  # private
        "198.18.0.0/15",  # benchmarking
        "198.51.100.0/24",  # documentation
        "203.0.113.0/24",  # documentation
        "224.0.0.0/4",  # multicast
        "240.0.0.0/4",  # reserved, incl. broadcast
        "::/128",  # unspecified
        "::1/128",  # loopback
        "100::/64",  # discard-only
        "2001::/32",  # Teredo (embeds an IPv4 address)
        "2001:db8::/32",  # documentation
        "fc00::/7",  # unique local (incl. AWS IPv6 metadata fd00:ec2::254)
        "fe80::/10",  # link-local
        "ff00::/8",  # multicast
    )
)
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_NAT64_LOCAL = ipaddress.ip_network("64:ff9b:1::/48")
_6TO4 = ipaddress.ip_network("2002::/16")


class AddressBlockedError(Exception):
    """A host resolved to an address the crawler must not contact."""


class DnsError(Exception):
    """The host name could not be resolved."""


def is_public_ip(ip: IPAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return is_public_ip(ip.ipv4_mapped)
        if ip in _NAT64:
            return is_public_ip(ipaddress.IPv4Address(ip.packed[-4:]))
        if ip in _NAT64_LOCAL:
            return False
        if ip in _6TO4:
            return is_public_ip(ipaddress.IPv4Address(ip.packed[2:6]))
    if any(ip in network for network in _BLOCKED_NETWORKS):
        return False
    return bool(
        ip.is_global
        and not ip.is_private
        and not ip.is_loopback
        and not ip.is_link_local
        and not ip.is_multicast
        and not ip.is_reserved
        and not ip.is_unspecified
    )


def system_resolver(host: str, port: int) -> list[str]:
    """Resolve through the OS with a hard timeout (getaddrinfo itself has none)."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM)
        try:
            infos = future.result(timeout=DNS_TIMEOUT_SECONDS)
        except FutureTimeout:
            raise DnsError("DNS lookup timed out") from None
        except OSError:
            raise DnsError("DNS lookup failed") from None
    return [str(info[4][0]) for info in infos]


def resolve_public_addresses(
    host: str,
    port: int,
    *,
    resolver: Resolver = system_resolver,
    validator: IpValidator = is_public_ip,
) -> list[str]:
    """Resolve ``host`` and return its addresses, all verified public.

    Raises AddressBlockedError if any address is not public, DnsError if nothing resolves.
    """
    try:
        literal: IPAddress | None = ipaddress.ip_address(host)
    except ValueError:
        literal = None

    raw = [str(literal)] if literal is not None else list(resolver(host, port))
    addresses: list[str] = []
    for item in raw:
        text = item.split("%", 1)[0]  # drop an IPv6 zone id such as fe80::1%eth0
        try:
            ip = ipaddress.ip_address(text)
        except ValueError:
            raise AddressBlockedError("unparseable address") from None
        if not validator(ip):
            raise AddressBlockedError("non-public address")
        if str(ip) not in addresses:
            addresses.append(str(ip))
    if not addresses:
        raise DnsError("no addresses")
    return addresses
