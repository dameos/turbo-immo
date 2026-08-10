"""Recover a bedroom count for listings whose search result omits one.

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
"""

from __future__ import annotations

import html as _html
import re

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
