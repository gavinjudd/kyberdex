"""Bounded public lore links shared by the archive and Discord card details."""
import re
from urllib.parse import urlsplit


def lore_references(card):
    """Expose only ordinary HTTPS references, without credentials or query data."""
    sources = card.get("lore_sources", [])
    if not isinstance(sources, list):
        return []
    references = []
    seen = set()
    for source in sources:
        if not isinstance(source, str) or len(source) > 400 or re.search(r"[\s\x00-\x1f\x7f<>\\\[\]()]", source):
            continue
        try:
            url = urlsplit(source)
            host = url.hostname or ""
            if (url.scheme != "https" or url.username is not None or url.password is not None
                    or url.port not in (None, 443) or url.query or url.fragment
                    or not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z][a-z0-9-]*", host)
                    or host.endswith((".local", ".localhost", ".internal"))):
                continue
        except ValueError:
            continue
        if source not in seen:
            references.append({"url": source, "label": host.removeprefix("www.")})
            seen.add(source)
        if len(references) == 3:
            break
    return references
