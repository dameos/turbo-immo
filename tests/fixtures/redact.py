#!/usr/bin/env python3
"""Regenerate the redacted test fixtures from live captures.

The fixtures must keep the *structure* the adapters depend on -- field names,
nesting, the `app.start({... properties: [...]})` wrapper, the messy free-text
that makes a regex parser fail -- while carrying none of Immoweb's or Zimmo's
actual content. Structure is what the tests assert on; the real addresses,
agency names, phone numbers and photo URLs are not needed and are not ours to
republish.

Usage (only when a site's format changes and the fixtures need refreshing):

    python tests/fixtures/redact.py <live_immoweb.json> <live_zimmo.html> <live_places.json>

Capture the live inputs by hand first; this script never hits the network.
"""

import json
import re
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent
KEEP = 4                       # listings retained per fixture

STREETS = ["Voorbeeldstraat", "Proefdreef", "Testkaai", "Modellaan",
           "Steekproefweg", "Sjabloonplein"]
AGENCIES = ["Agency Alpha", "Agency Beta", "Agency Gamma", "Agency Delta"]


def fake_street(i):
    return "%s %d" % (STREETS[i % len(STREETS)], 10 + i * 7)


def fake_photo(i, n, size):
    return "https://example.invalid/photos/%d/%d/%s.jpg" % (i, n, size)


def redact_immoweb(src: Path) -> None:
    d = json.loads(src.read_text(encoding="utf-8"))

    # `resultsList` is ~850 KB of pre-rendered result HTML and `resultsStorage`
    # repeats every id on the page -- both carry the full unredacted result set
    # and neither is read by the adapter. The SEO blobs are Immoweb's copy.
    for key in ("resultsList", "breadcrumbs", "seoFooterContent",
                "metaTitle", "metaDescription", "customer"):
        d.pop(key, None)
    d["results"] = d["results"][:KEEP]
    d["resultsStorage"] = [10000001 + i for i in range(KEEP)]
    for i, r in enumerate(d["results"]):
        loc = r["property"]["location"]
        loc["street"] = fake_street(i)
        loc["number"] = str(10 + i * 7)
        loc["box"] = None
        # City-level precision only: enough for the tile tests, not enough to
        # point at a specific front door.
        for k in ("latitude", "longitude"):
            if loc.get(k) is not None:
                loc[k] = round(float(loc[k]), 2)
        r["customerName"] = AGENCIES[i % len(AGENCIES)]
        r["customerLogoUrl"] = "https://example.invalid/logo.png"
        r["advertisementId"] = "REDACTED"
        r["id"] = 10000001 + i
        for n, pic in enumerate(r.get("media", {}).get("pictures") or []):
            for size in ("smallUrl", "mediumUrl", "largeUrl", "extralargeUrl"):
                if size in pic:
                    pic[size] = fake_photo(i, n, size)
    (OUT / "immoweb_search.json").write_text(
        json.dumps(d, indent=1, ensure_ascii=False), encoding="utf-8")
    print("immoweb_search.json  ->", len(d["results"]), "listings")


def redact_zimmo(src: Path) -> None:
    """Rebuild a minimal page that is still hostile to a naive parser.

    Deliberately retained: the app.start(...) wrapper, trailing script content
    after the array, and a description containing braces and quotes -- that
    free text is exactly why extract_properties uses raw_decode instead of a
    non-greedy regex, so the fixture has to keep testing it.
    """
    html = src.read_text(encoding="utf-8", errors="replace")
    i = html.index("properties:") + len("properties:")
    props = json.JSONDecoder().raw_decode(html[i:].lstrip())[0][:KEEP]

    for n, p in enumerate(props):
        p["code"] = "TEST%02d" % n
        p["uuid"] = "00000000-0000-0000-0000-%012d" % n
        p["address"] = fake_street(n)
        p["hoofdFoto"] = fake_photo(n, 0, "main")
        p["firstImages"] = [fake_photo(n, k, "list") for k in range(3)]
        p["logo"] = p["propertyItemLogo"] = "https://example.invalid/logo.png"
        p["url"] = p["pand_url"] = "/nl/gent-9000/te-koop/appartement/TEST%02d/" % n
        p["advertiser"] = {"name": AGENCIES[n % len(AGENCIES)], "phone": None,
                           "mobile": None, "showEmail": 0, "officebox": False}
        p["zimmo_kantoor_id"] = "0"
        for k in ("lat", "lon"):
            if p.get(k):
                p[k] = str(round(float(p[k]), 2))
    # nested braces + quotes + an escaped newline, in the field that carries
    # free text on the real site
    props[0]["a_beschrijf"] = (
        'Ruim {2 slaapkamers} appartement, \\"instapklaar\\", '
        'zie {"details": "op aanvraag"}.\\nTweede regel.')

    search = {"paging": {"from": 0, "size": 21},
              "sorting": [{"type": "RANKING_SCORE", "order": "DESC"}],
              "filter": {"status": {"in": ["FOR_SALE", "TAKE_OVER"]},
                         "placeId": {"in": [1506]}}}

    page = """<!DOCTYPE html>
<html lang="nl"><head><title>Immo te koop in Gent (9000) | Zimmo</title></head>
<body>
<div class="list-options-container"><div class="results">
<strong>1 - %d</strong> van 911 resultaten</div></div>
<script>
$(function () {
  app.start({ search: %s, properties: %s, extra: {"trailing": "content"} });
});
</script>
</body></html>
""" % (KEEP, json.dumps(search, separators=(",", ":")),
       json.dumps(props, ensure_ascii=False))
    (OUT / "zimmo_search.html").write_text(page, encoding="utf-8")
    print("zimmo_search.html    ->", len(props), "listings")


def redact_places(src: Path) -> None:
    """Keep only the postcodes the tests exercise, including the multi-slug
    cases (9050 = gentbrugge + ledeberg, 9070 = destelbergen + heusden,
    8000 = brugge + sub-towns) that a single-slug lookup used to silently drop."""
    keep = {"9000", "9040", "9050", "9070", "8000", "1000", "2000", "3000", "4000"}
    d = json.loads(src.read_text(encoding="utf-8"))
    places = {k: v for k, v in d["places"].items()
              if ((v.get("administrativeArea") or {}).get("postalCode") in keep)}
    (OUT / "zimmo_places.json").write_text(
        json.dumps({"places": places}, indent=1, ensure_ascii=False), encoding="utf-8")
    print("zimmo_places.json    ->", len(places), "places")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit(__doc__)
    redact_immoweb(Path(sys.argv[1]))
    redact_zimmo(Path(sys.argv[2]))
    redact_places(Path(sys.argv[3]))
