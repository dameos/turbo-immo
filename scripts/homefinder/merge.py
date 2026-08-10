"""Cross-source dedupe, ranking and capping."""

from __future__ import annotations

from .model import Criteria, Listing

# Fields where one source is simply better than the other.
#   Immoweb: structured numbers and real coordinates.
#   Zimmo:   the EPC label, which Immoweb's list payload doesn't carry at all.
_PREFER_IMMOWEB = ("lat", "lon", "land_m2", "habitable_m2", "bedrooms", "price")
# bedrooms and its provenance must travel together or a recovered count
# gets shown as if the site had published it.
_BEDROOM_FIELDS = ("bedrooms", "bedrooms_source")


def merge(listings: list[Listing]) -> list[Listing]:
    """Collapse listings that describe the same property.

    Two passes, because the two keys carry very different confidence:

    1. Everything with a real address merges on that address alone. Two
       addressed listings never merge on specs, so two different houses that
       happen to share postcode+price+bedrooms+surface stay separate.
    2. Listings with no address (a Zimmo card missing its house number) then
       attach to an addressed group via the weak spec key, or stand alone.
    """
    by_addr: dict[str, Listing] = {}
    by_spec: dict[str, Listing] = {}
    order: list[Listing] = []

    for listing in listings:
        key = listing.address_key()
        if key is None:
            continue
        if key in by_addr:
            _absorb(by_addr[key], listing)
        else:
            by_addr[key] = listing
            order.append(listing)

    for grouped in order:
        spec = grouped.spec_key()
        if spec:
            by_spec.setdefault(spec, grouped)

    for listing in listings:
        if listing.address_key() is not None:
            continue
        spec = listing.spec_key()
        if spec and spec in by_spec:
            _absorb(by_spec[spec], listing)
        else:
            order.append(listing)
            if spec:
                by_spec.setdefault(spec, listing)

    return order


def _absorb(target: Listing, other: Listing) -> None:
    """Fold `other` into `target` in place."""
    target.sources.update(other.sources)

    has_immoweb = "immoweb" in target.sources
    for fieldname in Listing.__dataclass_fields__:
        if fieldname in ("sources", "images", "promoted") + _BEDROOM_FIELDS:
            continue
        current = getattr(target, fieldname)
        incoming = getattr(other, fieldname)
        if incoming in (None, ""):
            continue
        if current in (None, ""):
            setattr(target, fieldname, incoming)
        elif fieldname in _PREFER_IMMOWEB and not has_immoweb:
            setattr(target, fieldname, incoming)

    # Bedrooms are merged as a unit with their provenance, and excluded from the
    # loop above: setting the count there would strip the source, and a recovered
    # figure would then render as if the site had published it.
    take_other = other.bedrooms is not None and (
        target.bedrooms is None
        or (not has_immoweb and other.bedrooms_source == "listed"))
    if take_other:
        target.bedrooms = other.bedrooms
        target.bedrooms_source = other.bedrooms_source

    # Immoweb images first (300px, ~4x smaller than Zimmo's 828px), then top up
    # from the other source if we're short of three.
    if len(target.images) < 3:
        for url in other.images:
            if url not in target.images and len(target.images) < 3:
                target.images.append(url)

    target.promoted = target.promoted and other.promoted


_SORTERS = {
    "price": lambda x: x.price,
    "price_per_m2": lambda x: x.price_per_m2,
    "surface": lambda x: -(x.habitable_m2 or 0),
    "bedrooms": lambda x: -(x.bedrooms or 0),
}


def rank(listings: list[Listing], c: Criteria) -> list[Listing]:
    """Sort, putting listings with no value for the sort key last rather than
    first -- a hidden price shouldn't win the top of a cheapest-first report."""
    key = _SORTERS.get(c.sort, _SORTERS["price"])

    def sort_key(x):
        v = key(x)
        return (v is None, v if v is not None else 0)

    return sorted(listings, key=sort_key)
