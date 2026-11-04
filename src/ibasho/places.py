"""Finding places: the grid's own place search through the viewer (`IbashoAgent findPlaces`, the directory's
search, with the account's maturity access); Second Life's public place search (search.secondlife.com, the page the
viewer's own search shows), read as a visitor without a login; and SLURLs, the links that name a place in Second Life.

Everything in the results except the numbers is other people's text (parcel names, descriptions): it's handed on as
quoted text and never acted on.
"""

from __future__ import annotations

import html
import http.cookiejar
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass

from . import __version__

SEARCH_URL = "https://search.secondlife.com/"
USER_AGENT = f"Ibasho/{__version__} (+https://github.com/studio-quire/ibasho)"
MAX_PAGE_BYTES = 4 << 20
TIMEOUT = 20

# The page's maturity filter, as the config names maturity. A visitor without a login gets General whatever it asks.
MATURITY_PARAM = {"general": "g", "moderate": "gm", "adult": "gma"}
RATING_LEVEL = {"general": 0, "moderate": 1, "adult": 2}
SORTS = {"relevance": "", "traffic": "popularity_desc", "quiet": "popularity_asc", "name": "parcel_name_asc"}
# The directory search has no relevance: it sorts by traffic unless asked otherwise, as the viewer's does. Nor
# quietest first: Second Life's sends the busiest whatever the order asked for (2026-11-04), so that's the web's.
VIEWER_SORTS = {"relevance": "traffic", "traffic": "traffic", "name": "name"}
PAGE = 20

# Parcel categories as the directory search names them (LLParcel's), with the web page's labels for them.
DIRECTORY_CATEGORIES = {
    "Linden Location": "linden", "Arts & Culture": "arts", "Business": "store", "Educational": "educational",
    "Gaming": "game", "Hangout": "gather", "Newcomer Friendly": "newcomer", "Parks & Nature": "park",
    "Residential": "home", "Shopping": "shopping", "Stage": "stage", "Other": "other", "Rental": "rental",
    "Adult": "adult",
}


class SearchError(Exception):
    """The search page couldn't be had or read; the message says why."""


@dataclass
class Place:
    kind: str                 # "parcel", "region", "guide" (the Destination Guide) or the page's own word
    name: str
    region: str = ""
    pos: tuple[float, float, float] | None = None
    rating: str = ""          # General, Moderate, Adult
    category: str = ""
    traffic: int | None = None
    description: str = ""

    @property
    def slurl(self) -> str:
        if not self.region:
            return ""
        x, y, z = self.pos or (128, 128, 0)
        return (f"https://maps.secondlife.com/secondlife/{urllib.parse.quote(self.region)}/"
                f"{x:g}/{y:g}/{z:g}")


@dataclass
class Results:
    places: list[Place]
    page: int
    more: bool                # a next page exists
    maturity: str             # what the page searched: general, moderate or adult
    categories: dict[str, str]   # the collection's categories: label -> the page's value


# ---------- SLURLs ----------

SLURL = re.compile(
    r"^(?:(?:https?://)?(?:maps\.secondlife\.com|slurl\.com)/secondlife/"
    r"|secondlife://(?:/app/(?:teleport|region|worldmap)/)?)"
    r"(?P<region>[^/?#]+)(?:/(?P<x>-?[\d.]+)(?:/(?P<y>-?[\d.]+)(?:/(?P<z>-?[\d.]+))?)?)?/?(?:[?#].*)?$",
    re.I)


def looks_like_slurl(s: str) -> bool:
    return bool(re.match(r"^\s*(?:https?://)?(?:maps\.secondlife\.com|slurl\.com)/|^\s*secondlife://", s, re.I))


def parse_slurl(s: str) -> tuple[str, float, float, float] | None:
    """(region, x, y, z) from a SLURL: https://maps.secondlife.com/secondlife/REGION/X/Y/Z, secondlife://REGION/X/Y/Z
    or secondlife:///app/teleport/REGION/X/Y/Z. Missing coordinates are the region's middle, on the ground, as the
    viewer takes them. None for anything else (an avatar's or a group's link, say)."""
    m = SLURL.match(s.strip())
    if not m:
        return None
    region = m.group("region")
    for _ in range(2):   # the search page encodes some twice (%2520)
        if not re.search(r"%[0-9A-Fa-f]{2}", region):
            break
        region = urllib.parse.unquote(region)
    region = " ".join(region.split())
    if not region or region.lower() == "app":
        return None
    try:
        x = float(m.group("x")) if m.group("x") else 128.0
        y = float(m.group("y")) if m.group("y") else 128.0
        z = float(m.group("z")) if m.group("z") else 0.0
    except ValueError:
        return None
    return region, min(max(x, 0.0), 256.0), min(max(y, 0.0), 256.0), min(max(z, 0.0), 4096.0)


# ---------- the search page ----------

class _SameSiteRedirects(urllib.request.HTTPRedirectHandler):
    """The page sends a first visit through Linden Lab's login service (`checklogin ... anonOK=true`) and back,
    setting a cookie on the way: follow that, but only to Linden Lab's own sites over https."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        u = urllib.parse.urlsplit(newurl)
        host = (u.hostname or "").lower()
        if u.scheme != "https" or not (host == "secondlife.com" or host.endswith(".secondlife.com")):
            raise SearchError(f"the search page sent us to {u.scheme}://{host}, which isn't Second Life's")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(params: dict) -> str:
    """The search page for these parameters. A fresh cookie jar each time, kept only in memory: nothing about
    the agent goes with the request but the search itself."""
    url = SEARCH_URL + "?" + urllib.parse.urlencode(params)
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
                                         _SameSiteRedirects())
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
    try:
        with opener.open(req, timeout=TIMEOUT) as r:
            body = r.read(MAX_PAGE_BYTES + 1)
    except SearchError:
        raise
    except Exception as e:   # URLError, HTTPError, timeouts, TLS: one line for the agent
        raise SearchError(f"Second Life's search didn't answer ({getattr(e, 'reason', None) or e})") from None
    if len(body) > MAX_PAGE_BYTES:
        raise SearchError("Second Life's search sent a page too large to read")
    return body.decode("utf-8", errors="replace")


def plain(s: str) -> str:
    """Someone's text as one line: control and direction characters and runs of space folded."""
    s = re.sub(r"[\x00-\x1f\x7f​-‏‪-‮⁦-⁩]", " ", s)
    return " ".join(s.split())


def text_of(fragment: str) -> str:
    """Markup to one line of plain text: tags gone, entities read, control characters and runs of space folded."""
    return plain(html.unescape(re.sub(r"<[^>]+>", " ", fragment)))


def _field(block: str, label: str) -> str:
    m = re.search(r'<span class="font-bold">\s*' + re.escape(label) + r'\s*:?\s*</span>(.*?)</p>', block, re.S)
    return text_of(m.group(1)) if m else ""


KINDS = {"parcel": "parcel", "region": "region", "v2_destinationguideentry": "guide", "event": "event"}


def parse_place(block: str) -> Place | None:
    m = re.search(r'data-result-type="([^"]*)"', block)
    kind = KINDS.get((m.group(1) if m else "").lower(), (m.group(1) if m else "").lower() or "?")
    m = re.search(r'<h2 class="text-accent[^"]*">(.*?)</h2>', block, re.S)
    name = text_of(m.group(1)) if m else ""
    if not name:
        return None
    p = Place(kind=kind, name=name)
    m = re.search(r'href="(https?://maps\.secondlife\.com/secondlife/[^"]+)"', block)
    where = parse_slurl(html.unescape(m.group(1))) if m else None
    if where is None:
        m = re.search(r'href="(secondlife://[^"]+)"', block)
        where = parse_slurl(html.unescape(m.group(1))) if m else None
    if where:
        p.region, p.pos = where[0], where[1:]
    m = re.search(r'font-semibold text-sm">\s*(General|Moderate|Adult)\s*<', block)
    p.rating = m.group(1) if m else ""
    p.category = _field(block, "Category")
    traffic = _field(block, "Traffic")
    p.traffic = int(traffic) if traffic.isdigit() else None
    m = re.search(r'search-result__content__description">(.*?)</p>', block, re.S)
    p.description = text_of(m.group(1)) if m else ""
    return p


def parse(page: str, collection: str) -> Results:
    blocks = re.split(r'<div id="result_', page)[1:]
    places = [p for b in blocks if (p := parse_place(b.split("<!-- Pagination -->")[0]))]
    # (not the page's count of results: it said 4 above 22 of them, 2026-11-03)
    m = re.search(r'div_role_pagination.*?cursor-default">\s*(\d+)\s*<', page, re.S)
    current = int(m.group(1)) if m else 1
    more = bool(re.search(r'[?&;]page=%d(?:&|")' % (current + 1), page))
    maturity = "general"
    m = re.search(r'name="maturity".*?</select>', page, re.S)
    if m:
        sel = re.search(r'value="(\w+)"\s*selected', m.group(0))
        maturity = {"g": "general", "gm": "moderate", "gma": "adult", "a": "adult"}.get(sel.group(1) if sel else "",
                                                                                      "general")
    categories = {}
    m = re.search(r'<select name="%s_category".*?</select>' % re.escape(collection), page, re.S)
    if m:
        for value, label in re.findall(r'<option value="([^"]*)"[^>]*>([^<]*)</option>', m.group(0)):
            if value not in ("", "0"):
                categories[text_of(label)] = value
    return Results(places, current, more, maturity, categories)


def search(query: str, *, maturity: str = "moderate", guide: bool = False, category: str | None = None,
           sort: str = "relevance", page: int = 1) -> Results:
    """One page of results (20 at most). query "*" lists everything (the page needs some query). category: a label
    from the page's own list (case and partial words do) or its value. maturity: the most the config allows; results
    rated above it are dropped even if the page sends them."""
    collection = "destinations" if guide else "places"
    params = {"search_type": "standard", "collection_chosen": collection, "query_term": query.strip() or "*",
              "maturity": MATURITY_PARAM.get(maturity, "g")}
    if SORTS.get(sort):
        params["sort"] = SORTS[sort]
    if page > 1:
        params["page"] = str(page)
    if category:
        first = parse(fetch(params), collection)   # the page's own list says what the categories are called
        value = pick_category(category, first.categories)
        if value is None:
            raise SearchError(f"no category {category!r}; " + ("the Destination Guide's" if guide else "places'") +
                              " categories: " + ", ".join(first.categories))
        params[f"{collection}_category"] = value
    return capped(parse(fetch(params), collection), maturity)


def capped(r: Results, maturity: str) -> Results:
    """Only what's rated at most `maturity`, the config's, whatever the search sent."""
    cap = RATING_LEVEL.get(maturity, 0)
    r.places = [p for p in r.places if RATING_LEVEL.get(p.rating.lower(), 0) <= cap]
    if RATING_LEVEL.get(r.maturity, 0) > cap:
        r.maturity = maturity
    return r


# ---------- through the viewer ----------

def viewer_request(query: str, *, maturity: str, category: str | None = None, sort: str = "relevance",
                   page: int = 1) -> dict:
    """The `IbashoAgent findPlaces` request for one page. category: a value of DIRECTORY_CATEGORIES."""
    q = query.strip()
    r = {"query": "" if q == "*" else q, "maturity": maturity, "sort": VIEWER_SORTS[sort],
         "start": (max(page, 1) - 1) * PAGE, "limit": PAGE, "timeout": 20}
    if category:
        r["category"] = category
    return r


def _number(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def from_viewer(reply: dict, page: int, maturity: str, category_label: str = "", by_name: bool = False) -> Results:
    """findPlaces' reply as Results, capped at `maturity`. Names, regions and descriptions are other people's text,
    made one plain line each; anything malformed is dropped. by_name: sorted by name, where Second Life sends every
    place's traffic as 0, so a 0 means nothing (2026-11-04)."""
    out = []
    for p in reply.get("places") or []:
        if not isinstance(p, dict) or not isinstance(p.get("name"), str):
            continue
        name = plain(p["name"])
        if not name:
            continue
        place = Place(kind="parcel", name=name, category=category_label)
        pos = p.get("pos")
        xyz = [_number(v) for v in pos] if isinstance(pos, list) and len(pos) == 3 else []
        region = plain(p["region"]) if isinstance(p.get("region"), str) else ""
        if region and len(xyz) == 3 and None not in xyz:
            place.region = region
            place.pos = (float(min(max(round(xyz[0]), 0), 256)), float(min(max(round(xyz[1]), 0), 256)),
                         float(min(max(round(xyz[2]), 0), 4096)))
        if p.get("rating") in ("General", "Moderate", "Adult"):
            place.rating = p["rating"]
        if (t := _number(p.get("traffic"))) is not None and not (by_name and t <= 0):
            place.traffic = int(t)
        if isinstance(p.get("description"), str):
            place.description = plain(p["description"])
        out.append(place)
    searched = reply.get("maturity") if reply.get("maturity") in RATING_LEVEL else "general"
    return capped(Results(out, max(page, 1), bool(reply.get("more")), searched, dict(DIRECTORY_CATEGORIES)),
                  maturity)


def pick_category(want: str, categories: dict[str, str]) -> str | None:
    w = " ".join(want.lower().replace("&", " ").split())
    if not w:
        return None
    for label, value in categories.items():
        if w in (value.lower(), label.lower()):
            return value
    words = w.split()
    hits = [value for label, value in categories.items()
            if all(any(t.startswith(x) for t in label.lower().replace("&", " ").split()) for x in words)]
    return hits[0] if len(hits) == 1 else None
