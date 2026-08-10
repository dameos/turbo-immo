---
name: find-homes
description: Search Immoweb and Zimmo for houses and apartments by Belgian postcode plus requirements (price, bedrooms, surface, land), and produce a self-contained HTML report with photo cards and map thumbnails. Use when the user wants to find homes, house-hunt, compare listings, or asks about property for sale or rent in Belgium.
---

# Find Homes

Searches **immoweb.be** and **zimmo.be**, merges the results, and writes an HTML
report of photo cards with a map thumbnail per home.

## The one rule

**Run the scripts. Never hand-write the HTML, and never replace the report with a
chat summary.**

The whole point of this skill is that the output format is identical every time.
Two scripts produce it deterministically. If a card looks wrong, fix the template
or the Python — never patch the generated file.

## Usage

Always both steps, in order:

```bash
python scripts/search_homes.py --postcode 9000 [filters] --out reports/<slug>/listings.json
python scripts/build_report.py reports/<slug>/listings.json --out reports/<slug>/report.html
```

Use `reports/YYYY-MM-DD-<place>/` for `<slug>`. Then tell the user the counts and
the path to the report.

Searching and rendering are separate on purpose: re-running `build_report.py`
restyles a report without re-hitting the sites, and every asset is cached, so the
second run is fast.

## Translating a request into flags

| The user says | Flag |
|---|---|
| "in Gent" / "9000" | `--postcode 9000` (repeat for several) |
| "houses only" / "apartments only" | `--type house` / `--type apartment` (default: both) |
| "to rent" | `--transaction rent` (default: sale) |
| "under 500k", "max 500000" | `--max-price 500000` |
| "at least 3 bedrooms" | `--min-bedrooms 3` |
| "at least 120 m²" | `--min-surface 120` |
| "with a garden" / "some land" | `--min-land 100` (approximate — see Limits) |
| "cheapest first" (default) | `--sort price` |
| "best value" | `--sort price_per_m2` |
| "biggest first" | `--sort surface` |
| "just the top 20" | `--limit 20` (no cap by default) |

Postcodes are Belgian 4-digit codes. If the user names a town you don't have a
postcode for, ask rather than guess — a wrong postcode silently returns another
town's houses.

## Reading the output

`search_homes.py` prints the funnel, and the report repeats it in the header:

```
raw 814 (immoweb 122, zimmo 692) -> merged 568 -> matching 155 -> shown 155
```

- **raw → merged**: cross-site duplicates and repeated promoted listings collapsing
- **merged → matching**: your filters applied
- **matching → shown**: identical unless you passed `--limit`

If `matching` is 0, say so plainly and quote the pre-filter counts — that
distinguishes "nothing like this exists" from "your filter was too tight".

Any warnings appear both in `listings.json` and as a banner on the report. Repeat
them to the user; a report with a broken source must never be presented as
complete.

## When something breaks

```bash
python scripts/search_homes.py --self-check    # live PASS/FAIL per source
python -m unittest discover -s tests           # offline, against saved fixtures
```

Scrapers rot. `--self-check` is the fast answer to "is it me or is it them".
If a source fails, see REFERENCE.md for how each site is accessed and what to
re-verify.

## Limits worth stating up front

- **`--min-land` is approximate.** Only Immoweb publishes land surface; Zimmo-only
  listings have no value, and the filter does not drop listings whose value is
  unknown (see below).
- **A missing value never excludes a listing.** A house that doesn't publish its
  bedroom count still appears in a `--min-bedrooms 3` search, with a gap on the
  card. Dropping real houses over a missing field is the worse failure.
- **Missing bedroom counts are recovered before filtering.** For listings that
  pass every other criterion but have no bedroom count, the listing page is read
  for its feature table, its subtype (a studio is a hard 0) and finally its
  description. Capped by `--max-enrich` (default 40, `0` disables). Recovered
  figures render as `~2 bed` in orange with a tooltip naming their origin --
  never as if the site had published them.
- **EPC is display-only**, not filterable. It comes from Zimmo; Immoweb-only
  listings won't have one.
- **Zimmo has no server-side price filter**, so a broad postcode means fetching
  every page for that town. `--max-pages` (default 25) caps it and warns when it
  bites.
- **Every match is reported.** There is no default cap — `--limit N` is there if
  you want one. Budget roughly **150 KB per listing**, since photos and map tiles
  are embedded: ~15 MB for 100 listings, ~45 MB for 300. That's the price of a
  report that works offline and survives delisting. Warn the user before
  generating a report over a few hundred listings, and suggest tightening the
  filters instead of capping — a cap hides matches without saying which.
