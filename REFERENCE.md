# How the two sites are accessed

Everything here was verified against the live sites on 2026-08-10. When a source
breaks, start by re-verifying the claims in this file, then refresh the fixture
in `tests/fixtures/` and re-run the tests.

---

## Immoweb

**Endpoint.** `GET https://www.immoweb.be/en/search-results/{types}/{transaction}`
with `Accept: application/json` returns the full search payload as JSON. No key,
no cookie, no bot check.

- `{types}` — `house` | `apartment` | `house-and-apartment`
- `{transaction}` — `for-sale` | `for-rent`

**Query params** (all confirmed — the response echoes a `criteria` object listing
exactly the params it understood, which is the cheapest way to test a new one):

| Param | Example |
|---|---|
| `countries` | `BE` |
| `postalCodes` | `BE-9000` |
| `minPrice` / `maxPrice` | `200000` / `500000` |
| `minBedroomCount` / `maxBedroomCount` | `3` / `5` |
| `minSurface` / `maxSurface` | habitable m², `120` / `400` |
| `minLandSurface` | `100` |
| `page` | 1-based, **31 results per page** |
| `orderBy` | `cheapest`, `relevance` |

`totalItems` gives the result count for pagination.

**Response shape.** `results[]`, each with `property.{type,bedroomCount,
netHabitableSurface,landSurface,location.{street,number,postalCode,locality,
latitude,longitude}}`, `price.mainValue`, `media.pictures[]` (four sizes:
`smallUrl` 100px, `mediumUrl` 300px, `largeUrl` 736px, `extralargeUrl` 2560px —
we take `mediumUrl`), and `customerName` for the agency.

**No EPC** in the list payload. It would need a detail-page fetch per listing;
we take it from Zimmo on merge instead.

**Promoted listings ignore the criteria.** A query for `minLandSurface=100&
maxPrice=500000&orderBy=cheapest` returns, as its first result, a €499,000 house
with `landSurface: 0`. This is why filters are re-applied locally.

**Price-less results** are new-build project clusters (`cluster.minPrice` etc.)
rather than individual homes. The adapter skips them.

---

## Zimmo

**It looks like an HTML-scraping job. It isn't.** Every search page embeds its
complete result set as JSON in an inline `app.start({ search: {...},
properties: [...] })` call. `zimmo.extract_properties` pulls the `properties`
array out with `json.JSONDecoder().raw_decode`, not a regex — the array contains
nested braces and free-text descriptions, so no non-greedy pattern terminates
reliably.

**URL shape.** `https://www.zimmo.be/nl/{slug}-{postcode}/{transaction}/{category}/?p={page}`

- `{slug}` — the town slug, **required**. A wrong slug silently 200-redirects to
  the homepage rather than erroring.
- `{transaction}` — `te-koop` | `te-huur`
- `{category}` — `huis` | `appartement`, or omitted for both
- `?p=N` — pagination, **21 results per page**. Verified to return different
  listings per page.

**Town slugs** come from `https://geo-api.zimmo.be/places` — a ~3,300-entry
gazetteer where each place carries `slugs.nl` and `administrativeArea.postalCode`.
It ignores any `?q=` parameter and always returns everything, so we fetch it once
and cache it for a year. Where several sub-municipalities share a postcode, prefer
the entry with `administrativeArea.level == 8` (the municipality), which is what
Zimmo's own URLs use.

**There is no server-side filtering beyond place, transaction and category.**
Zimmo's own pagination links carry a `?search=<base64 JSON>` parameter that looks
like a full Elasticsearch-style query:

```json
{"paging":{"from":0,"size":21},
 "sorting":[{"type":"RANKING_SCORE","order":"DESC"}],
 "filter":{"status":{"in":["FOR_SALE","TAKE_OVER"]},
           "category":{"in":["HOUSE"]},"placeId":{"in":[1506]}}}
```

**The server ignores it.** Replaying Zimmo's own base64 against a different path
returns that path's results, not the encoded query's; and injecting `price`,
`bedrooms` or `livingArea` filters under any of a dozen plausible key names
changes nothing. `paging.size` is likewise ignored — pages are always 21. The
path is the only thing that filters. Consequently every price/bedroom/surface
constraint on Zimmo is applied locally, and a broad postcode means fetching all
of that town's pages.

There is a real API at `search-api.zimmo.be` (referenced in the Angular bundle at
`/public/elements/main.*.js`), but no endpoint path was discoverable from the
bundle. If Zimmo's page format ever changes, that's the first place to look.

**Fields per property:** `prijs`, `slaapkamers`, `b_woonopp` (habitable m²),
`address`, `postcode`, `gemeente`, `lat`, `lon`, `energyLabel`, `firstImages[]`
(828×618), `advertiser.name`, `code`, `url`, `isPromoted`.

**`prijs: "0"` means price-on-request**, not free. Normalised to `None` in
`Listing.__post_init__`; left alone it sorts to the top of a cheapest-first
report. Same for `slaapkamers` and `b_woonopp`.

**No land surface** in the list payload.

---

## Map tiles

Static maps use no API key and no image library. `maps.window()` computes the
smallest rectangle of 256×256 OpenStreetMap tiles covering the viewport around
the point; the mosaic is then shifted by CSS so the point sits dead centre, with
the pin as an absolutely positioned dot. For a 340×210 viewport at zoom 15 that's
2×2 or 3×2 tiles.

Tile numbering is the standard slippy-map projection; `maps.project` uses
`log(tan + sec)`, cross-checked in the tests against `asinh` for independence.

**Tiles are deduplicated across the whole report.** Neighbouring listings share
most of their tiles — a real 60-listing Gent report needed only **51 unique
tiles**. Each is emitted once as a CSS class in `<style>`; inlining per card
would have multiplied the file size several times over.

`tile.openstreetmap.org` is a donated community resource with an explicit
no-bulk-downloading policy, so it gets a dedicated 1 req/s lane in
`Fetcher.HOST_DELAY` regardless of the global `--delay`, and tiles are cached for
a year. Attribution is required and is rendered on every map plus in the footer.

---

## Caching

`Fetcher` caches every response on disk under `%LOCALAPPDATA%\find-homes\cache`,
keyed by URL hash. Search pages expire after 6 hours; assets (photos, tiles, the
gazetteer) after a year, since their URLs are content-addressed. This is what
makes re-rendering a report nearly free, and it's why `--self-check` may pass
from cache — use `--no-cache` to force a live probe.
