"""Recover facts that a search payload omits, by reading the listing's own page.

Two things live here, sharing the detail-page parsing and the fetch cache:
a bedroom count (which changes who passes a filter) and an availability date
(display only). Both are per-listing HTTP requests, so both are capped.

Bedroom counts
--------------

Zimmo's search payload sets `slaapkamers` to "0" for a large minority of
listings, which normalises to "unknown". Those listings then pass a
`--min-bedrooms` filter untouched (a missing value never excludes), so a
2-bedroom search fills up with studios.

The listing's own detail page almost always settles it, via three signals in
descending reliability:

1. **The feature table** -- `Slaapkamers: 3`. The published figure, just absent
   from the search payload. Not a guess.
2. **The property subtype** -- `Type: Studio (Appartement)`. A studio has no
   separate bedroom, so this is a hard 0 even when the count is "op aanvraag".
   This is the signal that does the most work in practice.
3. **The description prose** -- "Dit 2 slaapkamer appartement...". A real guess,
   and marked as one.

Anything derived here is tagged in `Listing.bedrooms_source` so the report can
show it as an estimate rather than passing it off as published data.

Availability
------------

Neither site puts an availability date in its search results (verified against a
live rent search, not just the fixtures), so it too costs a page read. Unlike a
bedroom count it is never inferred from prose: both sites have a dedicated field,
and if that field says nothing then neither do we.
"""

from __future__ import annotations

import html as _html
import re
import unicodedata
from datetime import date

FEATURE_RE = re.compile(
    r'<strong class="feature-label">\s*([^<]+?)\s*</strong>\s*'
    r'<span class="feature-value">\s*(.*?)\s*</span>', re.S)
META_DESC_RE = re.compile(r'<meta name="description" content="([^"]*)"', re.I)
TAG_RE = re.compile(r"<[^>]+>")

# "op aanvraag" (on request) is Zimmo's placeholder for an unpublished value.
_ABSENT = ("op aanvraag", "sur demande", "on request", "-", "")

_WORD_NUMBERS = {"een": 1, "één": 1, "1": 1, "twee": 2, "drie": 3, "vier": 4,
                 "vijf": 5, "zes": 6, "zeven": 7, "acht": 8,
                 "un": 1, "une": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5,
                 "one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
_ADJECTIVES = r"(?:ruime|mooie|grote|lichte|volwaardige|apart[e]?|slaap|beaux?|belles?|spacieuses?|large|good-sized)\s+"
_NOUN = r"(?:slaapkamers?|slpk\.?|chambres?(?:\s+à\s+coucher)?|bedrooms?|beds?)"

_COUNT_PATTERNS = [
    # "3 slaapkamers", "2 ruime slaapkamers", "3 chambres", "2 bedrooms"
    re.compile(r"\b(\d{1,2})\s*(?:%s)?%s" % (_ADJECTIVES, _NOUN), re.I),
    # "3-slaapkamerappartement", "2-bedroom"
    re.compile(r"\b(\d{1,2})\s*-\s*(?:slaapkamer|bedroom|chambre)", re.I),
    # "twee slaapkamers", "drie ruime slaapkamers"
    re.compile(r"\b(%s)\s+(?:%s)?%s"
               % ("|".join(_WORD_NUMBERS), _ADJECTIVES, _NOUN), re.I),
]

_STUDIO_RE = re.compile(r"\bstudio\b|\bkot\b", re.I)


def parse_features(page: str) -> dict[str, str]:
    """The detail page's label/value table, as plain text."""
    out = {}
    for label, value in FEATURE_RE.findall(page):
        text = _html.unescape(TAG_RE.sub(" ", value))
        text = re.sub(r"\s+", " ", text).replace("»", "").strip()
        out[_html.unescape(label).strip()] = text
    return out


def description_text(page: str) -> str:
    m = META_DESC_RE.search(page)
    return _html.unescape(m.group(1)) if m else ""


def bedrooms_from_text(text: str) -> int | None:
    """First plausible bedroom count mentioned, or None.

    Takes the first match rather than the largest: descriptions lead with the
    headline count ("appartement met 2 slaapkamers") and only then walk through
    rooms individually, so a later number is usually describing one of them.
    """
    if not text:
        return None
    for pattern in _COUNT_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        token = m.group(1).lower()
        value = _WORD_NUMBERS.get(token) if not token.isdigit() else int(token)
        if value is not None and 0 <= value <= 10:
            return value
    return None


def bedrooms_from_page(page: str) -> tuple[int | None, str | None]:
    """Best available bedroom count for a Zimmo detail page.

    Returns (count, source) where source is 'detail', 'subtype' or
    'description'; (None, None) when the page settles nothing.
    """
    features = parse_features(page)

    published = features.get("Slaapkamers", "")
    if published.lower() not in _ABSENT:
        m = re.search(r"\d{1,2}", published)
        if m:
            return int(m.group()), "detail"

    # A studio has no separate bedroom, whatever the count field says.
    if _STUDIO_RE.search(features.get("Type", "")):
        return 0, "subtype"

    guess = bedrooms_from_text(description_text(page))
    if guess is not None:
        return guess, "description"
    return None, None


# --------------------------------------------------------------------------
# Availability
# --------------------------------------------------------------------------

# Zimmo's own row label, and its wording for "you can move in today".
_ZIMMO_AVAILABILITY_LABEL = "Vrij op"
_IMMEDIATE_WORDS = ("onmiddellijk", "immediat", "immediately")

# Day-first, because both sites are Belgian. Read as month-first, 01/10/2026
# would move a flat a month earlier and look entirely plausible doing it.
_DMY_RE = re.compile(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})\b")

_IW_DATE_RE = re.compile(r'"availabilityDate"\s*:\s*"(\d{4})-(\d{2})-(\d{2})')
_IW_PERIOD_RE = re.compile(r'"availabilityPeriodType"\s*:\s*"([A-Z_]+)"')


def _iso(year, month, day) -> str | None:
    """ISO date, or None if those numbers aren't one."""
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return None


def availability_from_zimmo_page(page: str) -> tuple[str | None, bool]:
    """Zimmo's `Vrij op` row, as (iso_date, immediately).

    Returns (None, False) for anything that isn't a date or a word meaning
    "now" -- "in overleg" (by arrangement) is a common value that names no date,
    and turning it into one would be inventing the fact the user cares most about.
    """
    value = parse_features(page).get(_ZIMMO_AVAILABILITY_LABEL, "")
    text = _html.unescape(value).strip().lower()
    if text in _ABSENT:
        return None, False

    m = _DMY_RE.search(text)
    if m:
        iso = _iso(m.group(3), m.group(2), m.group(1))
        if iso:
            return iso, False

    folded = "".join(c for c in unicodedata.normalize("NFD", text)
                     if unicodedata.category(c) != "Mn")
    if any(w in folded for w in _IMMEDIATE_WORDS):
        return None, True
    return None, False


def availability_from_immoweb_page(page: str) -> tuple[str | None, bool]:
    """Immoweb's `transaction` block, as (iso_date, immediately).

    Matches the unescaped JSON only. The page also carries an HTML-escaped copy
    of the whole classified blob and a translation dictionary defining an
    `available_date` *label*; a looser pattern would happily return either.
    """
    m = _IW_DATE_RE.search(page)
    if m:
        iso = _iso(m.group(1), m.group(2), m.group(3))
        if iso:
            return iso, False

    m = _IW_PERIOD_RE.search(page)
    # Only IMMEDIATELY is a date-equivalent. AFTER_DEED and friends are
    # conditions, and rendering them as "free now" would be a lie.
    if m and m.group(1) == "IMMEDIATELY":
        return None, True
    return None, False


# Which source to read a listing's availability from, best first. Immoweb
# publishes an ISO date in a structured field; Zimmo publishes DD/MM/YYYY as
# display text. Reading only the first available one keeps this at one request
# per listing rather than one per source.
_AVAILABILITY_READERS = (
    ("immoweb", availability_from_immoweb_page),
    ("zimmo", availability_from_zimmo_page),
)


def fill_availability(listings: list, fetcher, warn,
                      max_fetches: int = 200) -> dict:
    """Read an availability date off each listing's own detail page.

    Display-only, so unlike bedroom recovery this runs *after* filtering and
    ranking -- there is no point paying for a page that won't appear on the
    report. Pages are cached and shared with bedroom recovery, so a listing
    looked up for both costs one request, not two.

    A page that publishes nothing is the normal case (about half of Zimmo's
    rentals) and is not warned about; only a failed fetch is.
    """
    todo = []
    for listing in listings:
        for name, reader in _AVAILABILITY_READERS:
            url = (listing.sources.get(name) or {}).get("url")
            if url:
                todo.append((listing, url, reader))
                break

    stats = {"considered": len(todo), "fetched": 0, "resolved": 0, "capped": 0}

    if len(todo) > max_fetches:
        stats["capped"] = len(todo) - max_fetches
        warn("availability: %d listings to look up; only the first %d were read "
             "(--max-availability)" % (len(todo), max_fetches))
        todo = todo[:max_fetches]

    for listing, url, reader in todo:
        try:
            page = fetcher.get_text(url)
        except Exception as e:
            warn("availability: could not read %s: %s" % (url, e))
            continue
        stats["fetched"] += 1
        iso, immediately = reader(page)
        if iso or immediately:
            listing.available_from = iso
            listing.available_immediately = immediately
            stats["resolved"] += 1
    return stats
