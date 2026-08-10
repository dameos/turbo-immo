"""Search criteria, the normalised Listing record, and the dedupe keys.

This module is the contract between the two source adapters and everything
downstream. Adapters translate their site's shape into `Listing`; nothing after
that point knows or cares which site a listing came from.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field, asdict

HOUSE = "house"
APARTMENT = "apartment"
SALE = "sale"
RENT = "rent"


# --------------------------------------------------------------------------
# Criteria
# --------------------------------------------------------------------------

@dataclass
class Criteria:
    """What the user asked for. Pushed to the sites where supported, and always
    re-applied locally by `Listing.matches` -- see the note on that method."""

    postcodes: list[str]
    transaction: str = SALE
    property_types: list[str] = field(default_factory=lambda: [HOUSE, APARTMENT])
    min_price: int | None = None
    max_price: int | None = None
    min_bedrooms: int | None = None
    max_bedrooms: int | None = None
    min_surface: int | None = None
    max_surface: int | None = None
    min_land: int | None = None
    sort: str = "price"
    limit: int = 0          # 0 = keep everything that matched
    max_pages: int = 25
    max_enrich: int = 40   # listing pages read to recover missing bedroom counts

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Criteria":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    def summary(self) -> str:
        """One-line human description, shown in the report header."""
        bits = ["/".join(self.property_types), "for " + self.transaction,
                "in " + ", ".join(self.postcodes)]
        if self.min_price or self.max_price:
            bits.append("price %s-%s" % (_money(self.min_price) or "any",
                                         _money(self.max_price) or "any"))
        if self.min_bedrooms or self.max_bedrooms:
            bits.append("%s-%s bedrooms" % (self.min_bedrooms or "any",
                                            self.max_bedrooms or "any"))
        if self.min_surface or self.max_surface:
            bits.append("%s-%s m2 living" % (self.min_surface or "any",
                                             self.max_surface or "any"))
        if self.min_land:
            bits.append("min %s m2 land" % self.min_land)
        return " | ".join(bits)


def _money(v):
    return None if v is None else "EUR {:,}".format(v).replace(",", ".")


# --------------------------------------------------------------------------
# Listing
# --------------------------------------------------------------------------

@dataclass
class Listing:
    sources: dict = field(default_factory=dict)   # {"immoweb": {"id":..,"url":..}}
    price: int | None = None
    property_type: str | None = None
    bedrooms: int | None = None
    habitable_m2: int | None = None
    land_m2: int | None = None
    street: str | None = None
    postcode: str | None = None
    locality: str | None = None
    lat: float | None = None
    lon: float | None = None
    agency: str | None = None
    epc: str | None = None
    images: list[str] = field(default_factory=list)
    promoted: bool = False
    # Where `bedrooms` came from: "listed" (in the search results), or one of
    # "detail" / "subtype" / "description" when recovered from the listing page.
    # Anything but "listed" is shown as an estimate rather than a fact.
    bedrooms_source: str | None = None

    def __post_init__(self):
        """Normalise "absent" sentinels to None.

        Zimmo writes prijs="0" for price-on-request listings, which otherwise
        sort to the top of a cheapest-first report as free houses. Bedrooms and
        surface get the same treatment. `land_m2` deliberately does not -- zero
        land is a true fact about an apartment, not a missing value.
        """
        for name in ("price", "habitable_m2"):
            if (getattr(self, name) or 0) <= 0:
                setattr(self, name, None)
        # Zero bedrooms is "unpublished" when it came straight off a search
        # result, but a real fact once something established it -- a studio
        # genuinely has none, and that 0 must survive a JSON round-trip.
        if (self.bedrooms or 0) <= 0 and not self.bedrooms_source:
            self.bedrooms = None

    # -- derived ----------------------------------------------------------

    @property
    def price_per_m2(self) -> int | None:
        if self.price and self.habitable_m2:
            return round(self.price / self.habitable_m2)
        return None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["price_per_m2"] = self.price_per_m2
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Listing":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    # -- filtering --------------------------------------------------------

    def matches(self, c: Criteria) -> bool:
        """Local filter, applied to every listing regardless of what the site
        was asked for.

        Both sites inject promoted listings that ignore the search criteria
        (verified: Immoweb returns a 499k listing with 0 m2 land for a query of
        minLandSurface=100, maxPrice=500000 sorted cheapest). Server-side params
        are a bandwidth optimisation; this method is the actual contract.
        """
        if self.property_type and self.property_type not in c.property_types:
            return False
        if c.postcodes and self.postcode and self.postcode not in c.postcodes:
            return False
        if not _within(self.price, c.min_price, c.max_price):
            return False
        if not _within(self.bedrooms, c.min_bedrooms, c.max_bedrooms):
            return False
        if not _within(self.habitable_m2, c.min_surface, c.max_surface):
            return False
        # Land is only a floor, and apartments legitimately have none: only
        # enforce it when the user asked for it and the listing reports a value.
        if c.min_land and (self.land_m2 or 0) < c.min_land:
            return False
        return True

    # -- dedupe -----------------------------------------------------------

    def address_key(self) -> str | None:
        """Strong identity: normalised street+number+postcode.

        Requires a digit, because a bare street name is not an address -- half
        of Ham street is not the same property as the other half.
        """
        street = norm_street(self.street)
        if street and self.postcode and _has_digit(street):
            return "addr:%s:%s" % (self.postcode, street)
        return None

    def spec_key(self) -> str | None:
        """Weak identity: postcode+price+bedrooms+surface.

        Only ever used to attach a listing that has NO address (in practice a
        Zimmo card that omitted the house number) onto one that does. A listing
        with an address never matches via this key, because two genuinely
        different houses on the same street can share all four values.
        """
        if self.postcode and self.price and self.bedrooms and self.habitable_m2:
            return "spec:%s:%s:%s:%s" % (self.postcode, self.price,
                                         self.bedrooms, self.habitable_m2)
        return None


def _within(value, lo, hi) -> bool:
    """Range check that treats a missing value as "not excluded".

    A listing with no bedroom count shouldn't vanish from a min_bedrooms search;
    we'd rather show it with a gap than silently drop a real house.
    """
    if value is None:
        return True
    if lo is not None and value < lo:
        return False
    if hi is not None and value > hi:
        return False
    return True


def _has_digit(s: str) -> bool:
    return any(ch.isdigit() for ch in s)


# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------

# Applied to the punctuation-free token, so these are substring rules, not word
# rules: Dutch street names concatenate ("Pietersnieuwstraat"), which is exactly
# why a \b-anchored pattern never fires on the part that matters.
#
# Anywhere-rules must be unambiguous suffixes. "laan" is deliberately absent --
# it appears inside "Vlaanderenstraat" and would mangle it.
_ANYWHERE = [("straat", "str"), ("steenweg", "stwg"), ("dreef", "drf")]

# Prefix-only, because these are French address articles that are safe at the
# front and dangerous in the middle ("rue" inside "Bruel").
_PREFIX = [("sint", "st"), ("saint", "st"), ("boulevard", "bd"),
           ("avenue", "av"), ("chaussee", "ch"), ("place", "pl"), ("rue", "r")]


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")


def norm_street(s: str | None) -> str:
    """Fold a street line to a comparable form.

    'Sint-Pietersnieuwstraat 41 bus 3' and 'St. Pietersnieuwstr. 41/3' both
    collapse to 'stpietersnieuwstr41'. Unit suffixes are dropped because the two
    sites disagree about them constantly -- Immoweb splits number and box into
    separate fields, Zimmo writes 'Ham 47 - C' into one string.
    """
    if not s:
        return ""
    s = strip_accents(s).lower()

    # Unit suffixes, stripped while the separators are still present.
    s = re.sub(r"\s*\b(bus|bte|box|b)\b\.?\s*[\w-]+\s*$", "", s)
    s = re.sub(r"\s*[/-]\s*\w{1,3}\s*$", "", s)

    s = re.sub(r"[^a-z0-9]", "", s)

    for old, new in _PREFIX:
        if s.startswith(old):
            s = new + s[len(old):]
            break
    for old, new in _ANYWHERE:
        s = s.replace(old, new)
    return s
