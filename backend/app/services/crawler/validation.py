import ipaddress
from collections.abc import Collection

from app.services.crawler.ssrf import is_public_ip
from app.services.crawler.urls import UrlError, canonicalize_url, url_host, url_port


def validate_seed_url(raw: str, allowed_ports: Collection[int]) -> str:
    """Synchronous, network-free validation of a user-supplied website address.

    Returns the canonical URL or raises UrlError (message is safe to show). This gives immediate
    feedback for obvious problems (localhost, private IP literals, odd ports); it is NOT the SSRF
    defense. Host names are resolved and re-checked by the crawler at connection time, which is
    what actually protects the network.
    """
    url = canonicalize_url(raw)
    if url_port(url) not in allowed_ports:
        raise UrlError("Only standard web ports are supported.")
    host = url_host(url)
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return url
    if not is_public_ip(ip):
        raise UrlError("That address is not a public website.")
    return url
