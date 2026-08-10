# find-homes — design

**Date:** 2026-08-10
**Status:** implemented

Search Immoweb and Zimmo for Belgian homes by postcode plus requirements, and
produce an HTML report of photo cards with map thumbnails. The output format must
be identical on every run.

## Decisions

| Decision | Choice | Why |
|---|---|---|
| Dependencies | **Python stdlib only** | Nothing was installed and there is no `uv`. A skill that needs an install step breaks on the machine where the install fails. Costs ~100 lines of parsing. |
| Two sources | **Merge and dedupe**, badge each card with its sources | Agencies post to both sites. Seeing the same house twice is the thing a house-hunter least wants. |
| Filters | **Core only** — price, bedrooms, surface, land, house/apartment | Everything else needs a detail-page fetch per listing. Keeps a 300-listing search to seconds. |
| Report assets | **Fully embedded** (base64) | Works offline, survives delisting, safe to email. Costs 6–10 MB per 60 listings. |
| Card layout | **Wide comparison rows**, 340×210 map at zoom 15 | Chosen from three rendered mockups of real listings. Prices line up in a column for scanning; the map earns its size because location is a first-class criterion. |

## Architecture

Two commands with one hard boundary: **scraping never renders, rendering never
scrapes.**

```
search_homes.py  ──►  listings.json  ──►  build_report.py  ──►  report.html
   (network)          (the contract)         (assets only)
```

That split is what buys consistent output. Restyling re-runs only step 2 against
frozen JSON — no re-hitting the sites, no different result set. When Zimmo
changes their payload, exactly one file is wrong.

```
SKILL.md          invocation table; the rule that the agent never writes HTML
REFERENCE.md      site quirks, verified params, tile math, caching
scripts/
  search_homes.py    CLI → Criteria → sources → merge → JSON
  build_report.py    JSON + templates → self-contained HTML
  homefinder/
    model.py         Criteria + Listing, normalisation, dedupe keys
    fetch.py         UA, retry/backoff, per-host throttle, disk cache
    immoweb.py       adapter: search(criteria, fetch, warn) -> [Listing]
    zimmo.py         adapter: same signature
    merge.py         two-phase dedupe, ranking
    maps.py          lat/lon → minimal OSM tile window
    render.py        {{placeholder}} fill, asset embedding, tile registry
templates/
  report.html     page shell + CSS
  card.html       the row
tests/            unittest + saved fixtures, no network
```

Both adapters share `search(criteria, fetcher, warn) -> list[Listing]`. A third
site is one new file plus a registry entry.

## Data contract

`listings.json` is the deliverable of step 1 and the only input to step 2. It
carries `generated_at`, `criteria`, `criteria_summary`, `counts`, `warnings`, and
`listings[]`.

`counts` is deliberately verbose — `{immoweb, zimmo, raw_total, merged,
after_filter, shown}` — so a 3-result report can distinguish "nothing exists"
from "your filter was too tight".

## Filtering

Filters are pushed to the sites where supported, then **re-applied locally after
normalisation**. The local pass is the contract; the server-side params are only
a bandwidth optimisation.

This is not defensive theatre. Immoweb, queried for `minLandSurface=100&
maxPrice=500000&orderBy=cheapest`, returns as its *first* result a €499,000 house
with zero land. Both sites inject promoted listings that ignore the criteria.

**A missing value never excludes a listing.** A house that doesn't publish its
bedroom count still appears in a `--min-bedrooms 3` search, with a gap on the
card. Dropping real houses over an unpublished field is the worse failure mode.

## Dedupe

Two passes, because the two keys carry very different confidence:

1. **Address** — normalised `street+number+postcode`. Requires a digit; a bare
   street name is not an address. Everything with an address merges on address
   alone.
2. **Specs** — `postcode+price+bedrooms+surface`. Only ever used to attach a
   listing that has *no* address onto one that does.

An addressed listing never matches via specs. That was a real bug in the first
implementation: registering both keys for every listing would merge two different
houses that happen to share a postcode, price, bedroom count and surface.

On merge, Immoweb wins for coordinates and numbers (structured data); Zimmo
supplies the EPC label, which Immoweb's list payload lacks entirely.

## Normalisation

Street folding must handle Dutch concatenation: `Sint-Pietersnieuwstraat 41` and
`St. Pietersnieuwstr. 41` are the same address. Word-boundary regexes never fire
inside `Pietersnieuwstraat`, so rules apply to the punctuation-free token as
substrings. `straat→str` is safe anywhere; `laan→ln` is not — it appears inside
`Vlaanderenstraat`.

Unit suffixes (`bus 3`, `- C`, `/2`) are stripped, because Immoweb splits number
and box into separate fields while Zimmo writes them into one string.

`price`, `bedrooms` and `habitable_m2` of zero are normalised to `None` on the
record. Zimmo writes `prijs: "0"` for price-on-request; left alone those sort to
the top of a cheapest-first report as free houses. `land_m2` of zero is kept —
that's a true fact about an apartment.

## Maps

No API key and no image library. `maps.window()` computes the smallest rectangle
of 256×256 OSM tiles covering the viewport around the point; CSS shifts the
mosaic so the point is centred and the pin is an absolutely positioned dot.

Tiles are deduplicated across the whole report and emitted once each as a CSS
class — a real 60-listing Gent report needs only **51 unique tiles** for 60 maps.
`tile.openstreetmap.org` gets a dedicated 1 req/s lane and a one-year cache, per
their usage policy. Attribution renders on every map and in the footer.

## Error handling

A broken source must never produce a report that looks complete.

| Failure | Behaviour |
|---|---|
| One adapter errors or parses 0 cards | Warning in JSON, banner on the report, other source still renders |
| Both sources return nothing | Exit 1, no report written |
| 0 results after filtering | Report still generated, showing criteria and pre-filter counts |
| Image or tile fetch fails | Placeholder / flat cell, card still renders |
| No coordinates | "no location" instead of a map of the wrong place |
| Unfilled template placeholder | `render()` raises — a silently blank card is worse than a loud failure |

Exit codes: `0` fine, `1` hard failure, `2` usage error.

## Testing

55 `unittest` tests, no network, against saved real responses in
`tests/fixtures/`. The adapter tests are the canary for silent rot. Also covered:
the false-merge case, the promoted-listing filter, zero-price normalisation, tile
arithmetic (cross-checked against `asinh` for independence from the
implementation), and an end-to-end offline report build asserting no `{{` remains.

`search_homes.py --self-check` is a live PASS/FAIL probe per source, for
answering "is it me or is it them".

## Install

The repo *is* the skill root. Installed to `~/.claude/skills/find-homes` as a
directory junction (`mklink /J`, no admin needed on Windows), so repo edits are
live in the skill.
