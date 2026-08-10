#!/usr/bin/env python3
"""Search Immoweb and Zimmo for homes and write a normalised listings.json.

This script only gathers data. Rendering lives in build_report.py, so you can
re-style a report without re-running a search (and without re-hitting the
sites). Run with --help for the flags.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from homefinder import immoweb, zimmo
from homefinder.fetch import Fetcher, default_cache_dir
from homefinder.merge import merge, rank
from homefinder.model import APARTMENT, HOUSE, Criteria

SOURCES = {"immoweb": immoweb.search, "zimmo": zimmo.search}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="search_homes.py",
        description="Search Immoweb + Zimmo by postcode and write listings.json")
    p.add_argument("--postcode", action="append", dest="postcodes", metavar="CODE",
                   help="Belgian postcode; repeat for several (e.g. --postcode 9000 "
                        "--postcode 9030)")
    p.add_argument("--type", action="append", dest="types",
                   choices=[HOUSE, APARTMENT],
                   help="house and/or apartment (default: both)")
    p.add_argument("--transaction", choices=["sale", "rent"], default="sale")
    p.add_argument("--min-price", type=int)
    p.add_argument("--max-price", type=int)
    p.add_argument("--min-bedrooms", type=int)
    p.add_argument("--max-bedrooms", type=int)
    p.add_argument("--min-surface", type=int, help="minimum habitable m2")
    p.add_argument("--max-surface", type=int, help="maximum habitable m2")
    p.add_argument("--min-land", type=int, help="minimum land m2")
    p.add_argument("--sort", choices=["price", "price_per_m2", "surface", "bedrooms"],
                   default="price")
    p.add_argument("--limit", type=int, default=0,
                   help="cap the number of listings kept (default 0 = no cap; "
                        "every match is reported)")
    p.add_argument("--max-pages", type=int, default=25,
                   help="page cap per source per postcode (default 25)")
    p.add_argument("--source", action="append", choices=sorted(SOURCES),
                   help="restrict to one source; repeat to add (default: both)")
    p.add_argument("--out", default="listings.json", help="output JSON path")
    p.add_argument("--cache-dir", default=None)
    p.add_argument("--no-cache", action="store_true", help="ignore cached pages")
    p.add_argument("--delay", type=float, default=1.0,
                   help="seconds between requests to the same host (default 1.0)")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--self-check", action="store_true",
                   help="probe both sites with a tiny query and report PASS/FAIL")
    return p


def run_search(c: Criteria, fetcher, sources) -> dict:
    warnings: list[str] = []

    def warn(msg: str) -> None:
        warnings.append(msg)
        fetcher.log("  ! " + msg)

    raw, counts = [], {}
    for name in sources:
        fetcher.log("%s..." % name)
        try:
            found = SOURCES[name](c, fetcher, warn)
        except Exception as e:                     # an adapter bug must not
            warn("%s: adapter failed: %s" % (name, e))   # take the other source down
            found = []
        counts[name] = len(found)
        raw += found

    merged = merge(raw)
    kept = [x for x in merged if x.matches(c)]
    ranked = rank(kept, c)
    # No cap by default: a house-hunter wants every match, and an arbitrary
    # cutoff hides listings without saying which ones.
    limited = ranked[:c.limit] if c.limit else ranked

    counts.update({"raw_total": len(raw), "merged": len(merged),
                   "after_filter": len(kept), "shown": len(limited)})
    if len(ranked) > len(limited):
        warnings.append("%d listings matched but only %d shown (--limit %d)"
                        % (len(ranked), len(limited), c.limit))

    return {
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "criteria": c.to_dict(),
        "criteria_summary": c.summary(),
        "counts": counts,
        "warnings": warnings,
        "listings": [x.to_dict() for x in limited],
    }


def self_check(fetcher) -> int:
    """Live smoke test. Scrapers rot silently; this says so out loud."""
    c = Criteria(postcodes=["9000"], property_types=[HOUSE], limit=5, max_pages=1)
    failures = 0
    for name, fn in SOURCES.items():
        notes: list[str] = []
        try:
            found = fn(c, fetcher, notes.append)
        except Exception as e:
            found, notes = [], notes + ["exception: %s" % e]
        ok = len(found) > 0 and any(x.price and x.lat for x in found)
        print("%-9s %s  %d listing(s)%s"
              % (name, "PASS" if ok else "FAIL", len(found),
                 "  " + "; ".join(notes) if notes else ""))
        failures += 0 if ok else 1
    return 1 if failures else 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    fetcher = Fetcher(cache_dir=args.cache_dir or default_cache_dir(),
                      delay=args.delay, verbose=not args.quiet)
    if args.no_cache:
        fetcher._read_cache = lambda url, ttl: None

    if args.self_check:
        return self_check(fetcher)

    if not args.postcodes:
        build_parser().error("--postcode is required (repeat it for several)")

    c = Criteria(
        postcodes=[p.strip() for p in args.postcodes],
        transaction=args.transaction,
        property_types=sorted(set(args.types)) if args.types else [HOUSE, APARTMENT],
        min_price=args.min_price, max_price=args.max_price,
        min_bedrooms=args.min_bedrooms, max_bedrooms=args.max_bedrooms,
        min_surface=args.min_surface, max_surface=args.max_surface,
        min_land=args.min_land, sort=args.sort, limit=args.limit,
        max_pages=args.max_pages,
    )
    sources = sorted(set(args.source)) if args.source else sorted(SOURCES)

    fetcher.log("Searching: %s" % c.summary())
    result = run_search(c, fetcher, sources)

    if result["counts"].get("raw_total", 0) == 0:
        print("Both sources returned nothing -- not writing a report.", file=sys.stderr)
        for w in result["warnings"]:
            print("  ! " + w, file=sys.stderr)
        return 1

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")

    counts = result["counts"]
    fetcher.log("\n%s" % out)
    fetcher.log("  raw %d (immoweb %s, zimmo %s) -> merged %d -> matching %d -> shown %d"
                % (counts["raw_total"], counts.get("immoweb", "-"),
                   counts.get("zimmo", "-"), counts["merged"],
                   counts["after_filter"], counts["shown"]))
    fetcher.log("  cache: %d hit, %d fetched" % (fetcher.stats["hits"],
                                                 fetcher.stats["misses"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
