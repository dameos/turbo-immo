#!/usr/bin/env python3
"""Turn a listings.json into a self-contained HTML report.

Reads only the JSON and the templates. The one thing it fetches is assets --
photos and map tiles -- which it base64-embeds so the report keeps working
offline and after a listing is delisted.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from homefinder import maps
from homefinder.fetch import Fetcher, default_cache_dir
from homefinder.model import Listing
from homefinder.render import Embedder, TileRegistry, render

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
MAP_W, MAP_H, MAP_Z = 340, 210, 15

SOURCE_LABEL = {"immoweb": "Immoweb", "zimmo": "Zimmo"}


# --------------------------------------------------------------------------
# Fragments
# --------------------------------------------------------------------------

def money(v) -> str:
    return "€{:,}".format(int(v)).replace(",", ".") if v else "Price on request"


def photos_html(listing: Listing, embed) -> str:
    """Hero plus up to two thumbs, degrading when a listing has fewer images.

    Zimmo-only listings sometimes carry a single photo; an empty thumb slot
    reads as a broken card, so the hero widens instead.
    """
    uris = [u for u in (embed(src) for src in listing.images[:3]) if u]
    if not uris:
        return '<div class="noimg">no photo</div>'

    hero_class = "hero wide" if len(uris) == 1 else "hero"
    out = ['<img class="%s" src="%s" alt="" loading="lazy">' % (hero_class, uris[0])]
    if len(uris) > 1:
        thumb_class = "tall" if len(uris) == 2 else ""
        out.append('<div class="stack">' + "".join(
            '<img class="%s" src="%s" alt="" loading="lazy">' % (thumb_class, u)
            for u in uris[1:]) + "</div>")
    return "".join(out)


def chips_html(listing: Listing) -> str:
    chips = []
    if listing.bedrooms:
        chips.append(("", "%d bed" % listing.bedrooms))
    if listing.habitable_m2:
        chips.append(("", "%d m²" % listing.habitable_m2))
    if listing.land_m2:
        chips.append(("", "%d m² land" % listing.land_m2))
    if listing.epc:
        chips.append(("epc", "EPC %s" % listing.epc))
    return "".join('<span class="chip %s">%s</span>' % (cls, html.escape(text))
                   for cls, text in chips)


def links_html(listing: Listing) -> str:
    out = []
    for name in sorted(listing.sources):
        url = (listing.sources[name] or {}).get("url") or ""
        label = SOURCE_LABEL.get(name, name).upper()
        out.append('<a class="badge %s" href="%s" target="_blank" rel="noopener">%s</a>'
                   % (name, html.escape(url), label))
    return "".join(out)


def map_html(listing: Listing, tiles: TileRegistry) -> str:
    if listing.lat is None or listing.lon is None:
        return '<div class="map absent">no location</div>'

    win = maps.window(listing.lat, listing.lon, MAP_W, MAP_H, MAP_Z)
    cells = []
    for col, row, x, y in win.tiles():
        cls = tiles.class_for(maps.TILE_URL.format(z=win.z, x=x, y=y))
        if cls:
            cells.append('<div class="%s" style="left:%dpx;top:%dpx"></div>'
                         % (cls, col * maps.TILE, row * maps.TILE))
    if not cells:
        return '<div class="map absent">map unavailable</div>'

    return (
        '<div class="map">'
        '<div class="mosaic" style="left:%.1fpx;top:%.1fpx;width:%dpx;height:%dpx">%s</div>'
        '<div class="pin"></div><div class="attr">© OpenStreetMap</div></div>'
        % (win.offset_x, win.offset_y, win.nx * maps.TILE, win.ny * maps.TILE,
           "".join(cells))
    )


def counts_html(counts: dict) -> str:
    order = [("immoweb", "Immoweb"), ("zimmo", "Zimmo"), ("merged", "after dedupe"),
             ("after_filter", "matching"), ("shown", "shown")]
    return "".join('<span class="count"><b>%s</b> %s</span>' % (counts[k], label)
                   for k, label in order if k in counts)


def warnings_html(warnings: list) -> str:
    if not warnings:
        return ""
    items = "".join("<li>%s</li>" % html.escape(w) for w in warnings)
    return ('<div class="warnings"><strong>%d warning%s</strong> — this report may be '
            'incomplete.<ul>%s</ul></div>'
            % (len(warnings), "" if len(warnings) == 1 else "s", items))


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------

def build(data: dict, fetcher, embed_assets: bool = True) -> tuple[str, dict]:
    card_tpl = (TEMPLATES / "card.html").read_text(encoding="utf-8")
    page_tpl = (TEMPLATES / "report.html").read_text(encoding="utf-8")

    embedder = Embedder(fetcher, enabled=embed_assets)
    tiles = TileRegistry(embedder, reject=maps.is_placeholder_tile)

    cards = []
    for raw in data.get("listings", []):
        listing = Listing.from_dict(raw)
        cards.append(render(card_tpl, {
            "PHOTOS": photos_html(listing, embedder.uri),
            "PRICE": money(listing.price),
            "PRICE_PER_M2": ("%s/m²" % money(listing.price_per_m2)
                             if listing.price_per_m2 else ""),
            "STREET": listing.street or "Address not published",
            "CITY": " · ".join(x for x in ("%s %s" % (listing.postcode or "",
                                                      listing.locality or ""),
                                           listing.agency) if x and x.strip()),
            "CHIPS": chips_html(listing),
            "LINKS": links_html(listing),
            "MAP": map_html(listing, tiles),
        }))

    if not cards:
        cards = ['<div class="empty">No listings matched these criteria.</div>']

    counts = data.get("counts", {})
    stats = embedder.stats

    warnings = list(data.get("warnings", []))
    if tiles.rejected:
        warnings.append(
            "%d map tile(s) were rejected by the tile provider and are missing. "
            "OpenStreetMap blocks clients that do not send an identifying "
            "User-Agent -- see Fetcher.HOST_UA." % tiles.rejected)
    if tiles.suspect_uniform():
        warnings.append(
            "Every map tile came back byte-identical, which real map data never "
            "is. The maps in this report are almost certainly placeholder images.")

    page = render(page_tpl, {
        "TITLE": "Homes — %s" % ", ".join(data.get("criteria", {}).get("postcodes", [])),
        "SUMMARY": data.get("criteria_summary", ""),
        "GENERATED": data.get("generated_at", ""),
        "COUNTS": counts_html(counts),
        "WARNINGS": warnings_html(warnings),
        "CARDS": "\n".join(cards),
        "TILE_CSS": tiles.css(),
        "ATTRIBUTION": maps.ATTRIBUTION,
        "STATS": "%d assets embedded (%.1f MB)%s"
                 % (stats.embedded, stats.bytes / 1e6,
                    ", %d failed" % stats.failed if stats.failed else ""),
    })
    return page, {"embedded": stats.embedded, "failed": stats.failed,
                  "bytes": stats.bytes}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="build_report.py",
        description="Render listings.json into a self-contained HTML report")
    p.add_argument("listings", help="path to listings.json")
    p.add_argument("--out", default=None, help="output HTML (default: alongside input)")
    p.add_argument("--no-embed", action="store_true",
                   help="link assets instead of embedding (tiny file, needs internet)")
    p.add_argument("--cache-dir", default=None)
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args(argv)

    src = Path(args.listings)
    if not src.exists():
        print("no such file: %s" % src, file=sys.stderr)
        return 2
    data = json.loads(src.read_text(encoding="utf-8"))

    fetcher = Fetcher(cache_dir=args.cache_dir or default_cache_dir(),
                      delay=0.2, verbose=not args.quiet)
    fetcher.log("Embedding assets for %d listing(s)..." % len(data.get("listings", [])))

    page, stats = build(data, fetcher, embed_assets=not args.no_embed)

    out = Path(args.out) if args.out else src.with_suffix(".html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")

    fetcher.log("%s  (%.1f MB, %d assets%s)"
                % (out, out.stat().st_size / 1e6, stats["embedded"],
                   ", %d failed" % stats["failed"] if stats["failed"] else ""))
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
