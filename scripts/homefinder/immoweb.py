"""Immoweb adapter.

Immoweb serves its search results as JSON from
`/en/search-results/{types}/{transaction}` and honours real filter params, so
this adapter is a thin translation layer. Verified param names live in
REFERENCE.md; the response echoes back a `criteria` object, which is the
cheapest way to confirm a param was understood.
"""

from __future__ import annotations

import math
from urllib.parse import urlencode

from .model import APARTMENT, HOUSE, Criteria, Listing

BASE = "https://www.immoweb.be/en/search-results"
PER_PAGE = 31

_TYPE_SLUG = {
    (HOUSE,): "house",
    (APARTMENT,): "apartment",
    (APARTMENT, HOUSE): "house-and-apartment",
    (HOUSE, APARTMENT): "house-and-apartment",
}
_TXN_SLUG = {"sale": "for-sale", "rent": "for-rent"}
_TYPE_BACK = {"HOUSE": HOUSE, "APARTMENT": APARTMENT}

_SORT = {"price": "cheapest", "price_per_m2": "cheapest",
         "surface": "relevance", "bedrooms": "relevance"}


def _slug(types: list[str]) -> str:
    return _TYPE_SLUG.get(tuple(types), "house-and-apartment")


def build_url(c: Criteria, postcode: str, page: int) -> str:
    params = {
        "countries": "BE",
        "postalCodes": "BE-" + postcode,
        "page": page,
        "orderBy": _SORT.get(c.sort, "cheapest"),
    }
    for key, val in (("minPrice", c.min_price), ("maxPrice", c.max_price),
                     ("minBedroomCount", c.min_bedrooms),
                     ("maxBedroomCount", c.max_bedrooms),
                     ("minSurface", c.min_surface),
                     ("maxSurface", c.max_surface),
                     ("minLandSurface", c.min_land)):
        if val is not None:
            params[key] = val
    return "%s/%s/%s?%s" % (BASE, _slug(c.property_types),
                            _TXN_SLUG.get(c.transaction, "for-sale"),
                            urlencode(params))


def parse_results(payload: dict) -> list[Listing]:
    """Map one page of the Immoweb JSON payload onto Listings."""
    out = []
    for r in payload.get("results") or []:
        prop = r.get("property") or {}
        loc = prop.get("location") or {}
        price = (r.get("price") or {}).get("mainValue")

        # New-build "projects" are clusters of units with a price range rather
        # than a price. They aren't a house you can go and view, so skip them.
        if price is None:
            continue

        street = " ".join(str(x) for x in (loc.get("street"), loc.get("number")) if x)
        pics = [p.get("mediumUrl") or p.get("largeUrl")
                for p in (r.get("media") or {}).get("pictures") or []]

        out.append(Listing(
            sources={"immoweb": {
                "id": r.get("id"),
                "url": "https://www.immoweb.be/en/classified/%s" % r.get("id"),
            }},
            price=_int(price),
            property_type=_TYPE_BACK.get(prop.get("type")),
            bedrooms=_int(prop.get("bedroomCount")),
            habitable_m2=_int(prop.get("netHabitableSurface")),
            land_m2=_int(prop.get("landSurface")),
            street=street or None,
            postcode=str(loc.get("postalCode")) if loc.get("postalCode") else None,
            locality=loc.get("locality"),
            lat=_float(loc.get("latitude")),
            lon=_float(loc.get("longitude")),
            agency=r.get("customerName"),
            epc=None,  # not present in the list payload; Zimmo supplies it on merge
            images=[p for p in pics if p][:3],
            promoted=bool((r.get("publication") or {}).get("visualisationOption")),
        ))
    return out


def search(c: Criteria, fetcher, warn) -> list[Listing]:
    listings: list[Listing] = []
    for postcode in c.postcodes:
        try:
            first = fetcher.get_json(build_url(c, postcode, 1))
        except Exception as e:
            warn("immoweb: %s page 1 failed: %s" % (postcode, e))
            continue

        total = first.get("totalItems") or 0
        listings += parse_results(first)
        pages = min(math.ceil(total / PER_PAGE) if total else 1, c.max_pages)
        if total and math.ceil(total / PER_PAGE) > c.max_pages:
            warn("immoweb: %s has %d results, capped at %d pages (%d listings)"
                 % (postcode, total, c.max_pages, c.max_pages * PER_PAGE))

        fetcher.log("  immoweb %s: %d results, %d page(s)" % (postcode, total, pages))
        for page in range(2, pages + 1):
            try:
                listings += parse_results(fetcher.get_json(build_url(c, postcode, page)))
            except Exception as e:
                warn("immoweb: %s page %d failed: %s" % (postcode, page, e))
                break
    return listings


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
