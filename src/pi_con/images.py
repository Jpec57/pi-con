"""Find a picture on the web for the AI to show on the Pi's screen ("montre-moi un pikachu").

Sources: Wikipedia (French, then English: the lead image of the best matching article) and Wikimedia Commons. All free, no
key. The only hosts contacted are fixed ones, and the answer is only used if the image really is on wikimedia.org, so the
words of the assistant (or of whoever talks to it) can never make the Pi fetch or the browser load an arbitrary address.
"""
from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from collections.abc import Callable
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

USER_AGENT = "pi-con/0.1 (home voice assistant on a Raspberry Pi)"  # Wikimedia asks for an identifying agent
TIMEOUT = 6.0
WIDTH = 900  # pixels: sharp on the Pi's screen, small to load
WIKIPEDIAS = (("fr", "Wikipédia"), ("en", "Wikipedia"))

FetchJson = Callable[[str], dict]


def fetch_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as reply:
        return json.loads(reply.read(2_000_000))


def _api(host: str, **params: str) -> str:
    return f"https://{host}/w/api.php?" + urllib.parse.urlencode({"format": "json", "action": "query", **params})


def trusted(url: object) -> bool:
    """Only https images served by Wikimedia are shown."""
    if not isinstance(url, str):
        return False
    parts = urlsplit(url)
    return parts.scheme == "https" and (parts.hostname or "").endswith(".wikimedia.org")


def _in_order(pages: dict) -> list[dict]:
    return sorted(pages.values(), key=lambda page: page.get("index", 0))


def _from_wikipedia(query: str, lang: str, name: str, fetch: FetchJson) -> dict | None:
    data = fetch(_api(
        f"{lang}.wikipedia.org", generator="search", gsrsearch=query, gsrlimit="5", prop="pageimages|info",
        piprop="thumbnail", pithumbsize=str(WIDTH), pilicense="any", inprop="url",
    ))
    for page in _in_order((data.get("query") or {}).get("pages") or {}):
        thumb = (page.get("thumbnail") or {}).get("source")
        if trusted(thumb):
            return {"url": thumb, "title": page.get("title", query), "source": name, "page": page.get("fullurl", "")}
    return None


def _from_commons(query: str, fetch: FetchJson) -> dict | None:
    data = fetch(_api(
        "commons.wikimedia.org", generator="search", gsrsearch=f"{query} filetype:bitmap", gsrnamespace="6", gsrlimit="5",
        prop="imageinfo|info", iiprop="url|mime", iiurlwidth=str(WIDTH), inprop="url",
    ))
    for page in _in_order((data.get("query") or {}).get("pages") or {}):
        info = (page.get("imageinfo") or [{}])[0]
        thumb = info.get("thumburl") or info.get("url")
        if trusted(thumb) and str(info.get("mime", "")).startswith("image/"):
            title = str(page.get("title", query)).removeprefix("File:").rsplit(".", 1)[0]
            return {"url": thumb, "title": title, "source": "Wikimedia Commons", "page": page.get("fullurl", "")}
    return None


def find_image(query: str, fetch: FetchJson = fetch_json) -> dict | None:
    """{url, title, source, page} for the best picture of `query`, or None. Never raises: a network problem means no picture."""
    query = " ".join(query.split())[:100]
    if not query:
        return None
    attempts = [lambda lang=lang, name=name: _from_wikipedia(query, lang, name, fetch) for lang, name in WIKIPEDIAS]
    attempts.append(lambda: _from_commons(query, fetch))
    for attempt in attempts:
        try:
            found = attempt()
        except Exception as error:  # offline, timeout, a changed API: try the next source
            logger.warning("Image search failed (%s)", error)
            continue
        if found:
            return found
    return None
