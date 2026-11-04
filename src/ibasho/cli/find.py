"""Finding places: `places`, the grid's place search through the viewer as the logged-in account (Moderate places
included), or Second Life's public web search (and its Destination Guide) when the viewer can't, with a SLURL for
each result that `tp` takes as it is."""

from __future__ import annotations

import json

from .. import places as search
from ..api import ApiError, Down
from .common import Ctx, Fail

DESCRIPTION_CHARS = 200


def register(sub):
    p = sub.add_parser("places", help="search the grid's places (in world as this account, else Second Life's public "
                                      "search page): names, where, rating, traffic; each with a SLURL for `tp`")
    p.add_argument("query", nargs="*", help="words to look for (in world, 3 letters or more); none (or *) lists "
                                            "everything from the web search, best with --sort traffic or a "
                                            "--category")
    p.add_argument("--category", "-c", help="a category by name (a word of it will do): places have Hangout, "
                                            "Arts & Culture, Newcomer Friendly, Parks & Nature...; the guide has "
                                            "Voice Chat Hot Spots, What's Hot Now, Music, Beaches... An unknown "
                                            "name lists them")
    p.add_argument("--sort", choices=list(search.SORTS), default="relevance",
                   help="traffic: busiest first (Second Life's daily visit count); quiet: least visited first. The "
                        "search in world has no relevance and sorts by traffic, and quiet is the web search's only")
    p.add_argument("--guide", action="store_true", help="Linden Lab's Destination Guide (picked places, "
                                                        "described) instead of all places; always the web page")
    p.add_argument("--page", type=int, default=1, help="20 a page")
    p.set_defaults(fn=places)


def in_world(ctx: Ctx, query: str, a) -> tuple[search.Results | None, str]:
    """The grid's own place search through the viewer (`IbashoAgent findPlaces`), as the logged-in account: (results,
    "") or, where the viewer can't search, (None, why: "" when it isn't there to ask)."""
    try:
        st = ctx.api.is_up()
    except ApiError:
        st = None
    if not st or not st.get("logged_in") or "findPlaces" not in (st.get("ops") or {}).get("IbashoAgent", []):
        return None, ""
    if a.sort not in search.VIEWER_SORTS:
        return None, "it can't sort quietest first"
    cfg = ctx.cfg
    label, value = "", None
    if a.category:
        cats = {k: v for k, v in search.DIRECTORY_CATEGORIES.items()
                if v != "adult" or cfg.caps.maturity == "adult"}
        value = search.pick_category(a.category, cats)
        if value is None:
            raise Fail(f"no category {a.category!r}; places' categories: " + ", ".join(cats))
        label = next(k for k, v in cats.items() if v == value)
    req = search.viewer_request(query, maturity=cfg.caps.maturity, category=value, sort=a.sort, page=a.page)
    try:
        reply = ctx.api.op("IbashoAgent", "findPlaces", req, timeout=req["timeout"] + 15)
    except Down:
        return None, ""
    except ApiError as e:
        why = str(e).removeprefix("IbashoAgent.findPlaces: ")
        if query == "*" and "longer words" in why:
            why = "it needs words to look for, or a category with them"
        return None, why
    return search.from_viewer(reply, a.page, cfg.caps.maturity, label, by_name=req["sort"] == "name"), ""


def places(ctx: Ctx, a):
    cfg = ctx.cfg
    query = " ".join(a.query).strip() or "*"
    a.page = max(a.page, 1)
    r, why = (None, "") if a.guide else in_world(ctx, query, a)
    web = r is None
    if web:
        if not cfg.linden_grid:
            raise Fail((why or "`places` searches in world once Ibasho is logged in (`ibasho up`)") +
                       f"; Second Life's web search knows only Linden Lab's grid, and this install is on {cfg.grid}")
        try:
            r = search.search(query, maturity=cfg.caps.maturity, guide=a.guide, category=a.category, sort=a.sort,
                              page=a.page)
        except search.SearchError as e:
            raise Fail(str(e) + (f" (and the search in world: {why})" if why else "")) from None
    what = "Destination Guide entries" if a.guide else "places"
    head = f"{what} for {json.dumps(query, ensure_ascii=False)}"
    if a.category:
        head += f" in {a.category}"
    if not r.places:
        print(f"no {head}" + (f" on page {r.page}" if r.page > 1 else ""))
    else:
        first = (r.page - 1) * 20 + 1
        print(f"{head}, {first}-{first + len(r.places) - 1}" + (f" (--page {r.page + 1} for more)" if r.more else ""))
    for n, p in enumerate(r.places, (r.page - 1) * 20 + 1):
        print(fmt_place(n, p))
    if r.places:
        print("(names and descriptions are their owners' words; `tp` takes a SLURL as it is)")
    if why:
        print(f"(these are from Second Life's web search; the search in world: {why})")
    held_back = search.RATING_LEVEL.get(r.maturity, 0) < search.RATING_LEVEL.get(cfg.caps.maturity, 0)
    if held_back and web:
        print("(General places only: Second Life's public web search shows Moderate and Adult ones only to someone "
              "logged in on its website, so many of the busiest places aren't here" +
              ("" if a.guide or why else "; logged in, `places` searches in world, as the account") + ")")
    elif held_back:
        print(f"({r.maturity.capitalize()} places at most: what this account's own maturity setting allows)")


def fmt_place(n: int, p: search.Place) -> str:
    """One result in two or three lines: what it is, where it is with its SLURL, and the owner's description."""
    facts = [x for x in (p.category if p.kind != "region" and p.category != "All Categories" else "",
                         p.rating, f"traffic {p.traffic}" if p.traffic is not None else "") if x]
    kind = {"region": "region ", "guide": ""}.get(p.kind, "")
    line = f"{n}. {kind}{json.dumps(p.name, ensure_ascii=False)}" + (f" ({', '.join(facts)})" if facts else "")
    if p.region:
        x, y, z = p.pos or (128, 128, 0)
        line += f"\n   {p.region} {x:g},{y:g},{z:g}  {p.slurl}"
    if p.description:
        d = p.description if len(p.description) <= DESCRIPTION_CHARS else p.description[:DESCRIPTION_CHARS] + "..."
        line += f"\n   {json.dumps(d, ensure_ascii=False)}"
    return line
