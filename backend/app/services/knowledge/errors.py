"""User-facing crawl failure messages.

``knowledge_sources.error_code`` is a stable machine-readable reason; ``error_message`` is always
taken from this fixed catalogue. Raw exception text, URLs from server logs, response bodies and
stack traces never reach the database or the API.
"""

ERROR_MESSAGES: dict[str, str] = {
    "blocked_url": (
        "This address cannot be crawled. Only public websites on standard web ports are supported."
    ),
    "dns_error": "We could not find this website. Check the address and try again.",
    "unreachable": (
        "We could not connect to this website. It may be down or may be blocking our crawler."
    ),
    "timeout": "The website took too long to respond.",
    "tls_error": "The website's security certificate could not be verified.",
    "too_large": "The website's first page is too large to process.",
    "too_many_redirects": "The website redirected too many times.",
    "redirect_off_site": (
        "The website redirects to a different domain. Add that domain's address instead."
    ),
    "not_html": "That address did not return a web page.",
    "http_error": "The website returned an error when we tried to open it.",
    "robots_disallowed": "The website's robots.txt does not allow us to crawl it.",
    "robots_unavailable": (
        "The website's robots.txt could not be retrieved, so we did not crawl it. "
        "Please try again later."
    ),
    "no_content": (
        "We could not find readable text on this website. Pages that rely on JavaScript to "
        "display their content are not supported yet."
    ),
    "embedding_unconfigured": (
        "Embeddings are not configured on the server. Please contact support."
    ),
    "embedding_failed": (
        "The embedding service failed while indexing this website. Please try again later."
    ),
    "interrupted": "The crawl was interrupted. Please start it again.",
    "queue_unavailable": "The crawl queue is unavailable. Please try again in a moment.",
    "internal_error": "Something went wrong while crawling this website. Please try again.",
}


def error_message(code: str) -> str:
    return ERROR_MESSAGES.get(code, ERROR_MESSAGES["internal_error"])
