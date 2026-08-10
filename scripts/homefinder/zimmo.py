"""Zimmo adapter.

Zimmo looks like an HTML-scraping job but isn't: every search page embeds its
full result set as JSON inside an `app.start({... properties: [...] })` call,
including lat/lon, an image list and the EPC label. We pull that array out and
never touch the DOM.

Two constraints shape this adapter:

* The `?search=<base64 json>` param that appears in Zimmo's own pagination links
  is ignored by the server -- the URL *path* selects place and category. So
  price/bedroom/surface filters cannot be pushed to Zimmo at all and are applied
  locally by `Listing.matches`.
* The path needs the town's slug (`/nl/gent-9000/`). A wrong slug silently
  redirects to the homepage. Slugs come from Zimmo's own gazetteer at
  geo-api.zimmo.be/places, fetched once and cached for a year.
"""

from __future__ import annotations

import json

from .fetch import TTL_ASSET
from .model import APARTMENT, HOUSE, Criteria, Listing

PLACES_URL = "https://geo-api.zimmo.be/places"
BASE = "https://www.zimmo.be/nl"
PER_PAGE = 21

_TXN_SLUG = {"sale": "te-koop", "rent": "te-huur"}
_CATEGORY_SLUG = {HOUSE: "huis", APARTMENT: "appartement"}
_TYPE_BACK = {"huis": HOUSE, "woning": HOUSE, "appartement": APARTMENT}


# --------------------------------------------------------------------------
# Place slugs
# --------------------------------------------------------------------------

def load_places(fetcher) -> dict[str, list[str]]:
    """postcode -> every Zimmo town slug using it, from Zimmo's gazetteer."""
    data = fetcher.get_json(PLACES_URL, ttl=TTL_ASSET)
    return parse_places(data)


def parse_places(data: dict) -> dict[str, list[str]]:
    """One postcode can map to several Zimmo slugs, and each is a separate page.

    9050 is both `gentbrugge` and `ledeberg`; 9070 is both `destelbergen` and
    `heusden`. Keeping a single slug per postcode silently loses every listing
    in the other half of the postcode, so all of them are returned and searched.
    Broader administrative levels come first, since they carry the most listings.
    """
    out: dict[str, list[str]] = {}
    seen: set[tuple[str, str]] = set()
    for place in (data.get("places") or {}).values():
        area = place.get("administrativeArea") or {}
        postcode = area.get("postalCode")
        slug = (place.get("slugs") or {}).get("nl") or area.get("slug")
        if not postcode or not slug or (postcode, slug) in seen:
            continue
        seen.add((postcode, slug))
        out.setdefault(postcode, []).append((area.get("level") or 99, slug))

    return {pc: [slug for _, slug in sorted(entries)]
            for pc, entries in out.items()}


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------

def build_url(slug: str, postcode: str, transaction: str,
              category: str | None, page: int) -> str:
    parts = [BASE, "%s-%s" % (slug, postcode), _TXN_SLUG.get(transaction, "te-koop")]
    if category:
        parts.append(category)
    url = "/".join(parts) + "/"
    return url + ("?p=%d" % page if page > 1 else "")


def extract_properties(html: str) -> list[dict] | None:
    """Pull the `properties: [...]` array out of the page's app.start(...) call.

    Returns `[]` for a page that genuinely has no listings, and `None` when the
    array could not be found or parsed. The caller must not conflate the two:
    a small town with no rentals is normal, a parse failure means Zimmo changed
    their page and the adapter needs fixing.

    Uses raw_decode rather than a regex: the array contains nested braces and
    free-text descriptions, so no non-greedy pattern terminates reliably.
    """
    marker = "properties:"
    i = html.find(marker)
    if i < 0:
        return None
    tail = html[i + len(marker):].lstrip()
    try:
        value, _ = json.JSONDecoder().raw_decode(tail)
    except ValueError:
        return None
    return value if isinstance(value, list) else None


def total_results(html: str) -> int | None:
    import re
    m = re.search(r"van ([0-9.]+) re", html)
    return int(m.group(1).replace(".", "")) if m else None


def parse_property(p: dict) -> Listing:
    subtype = (p.get("subtype_naam") or p.get("type") or "").strip().lower()
    images = [u for u in (p.get("firstImages") or []) if u]
    if not images and p.get("hoofdFoto"):
        images = [p["hoofdFoto"]]
    url = p.get("url") or p.get("pand_url") or ""
    label = (p.get("energyLabel") or "").strip().upper() or None

    return Listing(
        sources={"zimmo": {
            "code": p.get("code"),
            "url": "https://www.zimmo.be" + url if url.startswith("/") else url,
        }},
        price=_int(p.get("prijs")),
        property_type=_TYPE_BACK.get(subtype),
        bedrooms=_int(p.get("slaapkamers")),
        bedrooms_source="listed" if (_int(p.get("slaapkamers")) or 0) > 0 else None,
        habitable_m2=_int(p.get("b_woonopp")),
        land_m2=None,           # not exposed in the list payload
        street=(p.get("address") or "").strip() or None,
        postcode=str(p.get("postcode")) if p.get("postcode") else None,
        locality=p.get("gemeente"),
        lat=_float(p.get("lat")),
        lon=_float(p.get("lon")),
        agency=((p.get("advertiser") or {}).get("name")),
        epc=label,
        images=images[:3],
        promoted=bool(p.get("isPromoted")),
    )


def enrich_bedrooms(listings: list[Listing], fetcher, warn,
                    max_fetches: int = 40) -> dict:
    """Fill in bedroom counts from each listing's own detail page.

    Only called for listings that already pass every other criterion and still
    have no bedroom count, so the request count is proportional to the gap
    rather than to the result set. One page per listing, throttled like any
    other fetch and cached, so a re-run costs nothing.
    """
    from . import enrich

    todo = [x for x in listings
            if x.bedrooms is None and (x.sources.get("zimmo") or {}).get("url")]
    stats = {"considered": len(todo), "fetched": 0, "resolved": 0, "capped": 0}

    if len(todo) > max_fetches:
        stats["capped"] = len(todo) - max_fetches
        warn("zimmo: %d listings lack a bedroom count; only the first %d were "
             "looked up (--max-enrich)" % (len(todo), max_fetches))
        todo = todo[:max_fetches]

    for listing in todo:
        url = listing.sources["zimmo"]["url"]
        try:
            page = fetcher.get_text(url)
        except Exception as e:
            warn("zimmo: could not read %s: %s" % (url, e))
            continue
        stats["fetched"] += 1
        count, source = enrich.bedrooms_from_page(page)
        if count is not None:
            listing.bedrooms = count
            listing.bedrooms_source = source
            stats["resolved"] += 1
    return stats


def search(c: Criteria, fetcher, warn) -> list[Listing]:
    try:
        places = load_places(fetcher)
    except Exception as e:
        warn("zimmo: could not load place gazetteer (%s); skipping Zimmo" % e)
        return []

    categories = [_CATEGORY_SLUG[t] for t in c.property_types
                  if t in _CATEGORY_SLUG] or [None]

    listings: list[Listing] = []
    for postcode in c.postcodes:
        slugs = places.get(postcode) or []
        if not slugs:
            warn("zimmo: no town slug for postcode %s; skipped" % postcode)
            continue

        for slug, category in [(s, cat) for s in slugs for cat in categories]:
            try:
                html = fetcher.get_text(build_url(slug, postcode, c.transaction,
                                                  category, 1))
            except Exception as e:
                warn("zimmo: %s/%s page 1 failed: %s" % (slug, category, e))
                continue

            props = extract_properties(html)
            if props is None:
                warn("zimmo: could not parse %s/%s -- the embedded JSON format "
                     "may have changed" % (slug, category))
                continue
            if not props:
                continue          # a town with no listings is not a problem

            total = total_results(html) or len(props)
            listings += [parse_property(p) for p in props]
            pages = min(-(-total // PER_PAGE), c.max_pages)
            if -(-total // PER_PAGE) > c.max_pages:
                warn("zimmo: %s/%s has %d results, capped at %d pages (%d listings)"
                     % (slug, category, total, c.max_pages, c.max_pages * PER_PAGE))

            fetcher.log("  zimmo %s-%s/%s: %d results, %d page(s)"
                        % (slug, postcode, category, total, pages))
            for page in range(2, pages + 1):
                try:
                    more = fetcher.get_text(build_url(slug, postcode, c.transaction,
                                                      category, page))
                except Exception as e:
                    warn("zimmo: %s/%s page %d failed: %s" % (slug, category, page, e))
                    break
                page_props = extract_properties(more)
                if not page_props:
                    break
                listings += [parse_property(p) for p in page_props]
    return listings


def _int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError, AttributeError):
        return None


def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
