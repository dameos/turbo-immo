# turbo-immo — find-homes

Searches [immoweb.be](https://www.immoweb.be) and [zimmo.be](https://www.zimmo.be)
for Belgian houses and apartments by postcode, merges and de-duplicates the
results, and renders a self-contained HTML report of photo cards with a map
thumbnail per home.

Also packaged as a [Claude Code](https://claude.com/claude-code) skill — see
[SKILL.md](SKILL.md).

## Requirements

Python 3.10+. **No third-party packages.** Standard library only: `urllib` for
HTTP, `json` for both sites' payloads, `string`/`re` for templating, and CSS for
the maps. Nothing to install, nothing to keep up to date.

## Usage

```bash
python scripts/search_homes.py --postcode 9000 --max-price 500000 \
    --min-bedrooms 3 --min-surface 120 --out reports/gent/listings.json

python scripts/build_report.py reports/gent/listings.json \
    --out reports/gent/report.html
```

Searching and rendering are deliberately separate. `listings.json` is the
contract between them, so you can restyle a report without re-running a search
or re-hitting the sites.

```
--postcode CODE        repeat for several
--type house|apartment default: both
--transaction sale|rent
--min-price / --max-price
--min-bedrooms / --max-bedrooms
--min-surface / --max-surface   habitable m²
--min-land
--sort price|price_per_m2|surface|bedrooms
--limit N              default 60
--self-check           live PASS/FAIL per source
```

## How it works

| | Immoweb | Zimmo |
|---|---|---|
| Access | JSON API at `/en/search-results/…` | Result set embedded as JSON in the page's `app.start(…)` call |
| Server-side filters | price, bedrooms, surface, land | **none** — place and category only |
| Coordinates | yes | yes |
| EPC label | no | yes |
| Per page | 31 | 21 |

Filters are pushed to the sites where supported and then **re-applied locally**,
because both inject promoted listings that ignore the query — Immoweb answers
"min 100 m² land, cheapest first" with a €499k house on zero land.

De-duplication runs in two passes: everything with a real street address merges
on address alone, then address-less listings attach via a weaker
postcode+price+bedrooms+surface key. Two addressed listings never merge on specs,
so houses that coincidentally share those four values stay separate.

Maps are OpenStreetMap tiles with **no API key and no image library**: the
smallest covering tile rectangle is laid out in a grid and shifted by CSS so the
point sits centred. Tiles are de-duplicated across the report — a 60-listing
report typically needs ~50 unique tiles — and embedded once each as a CSS class.

See [REFERENCE.md](REFERENCE.md) for the verified endpoint details and
[docs/specs](docs/specs) for the design rationale.

## Tests

```bash
python -m unittest discover -s tests     # 65 tests, no network
python scripts/search_homes.py --self-check   # live probe of both sites
```

Tests run against redacted fixtures (see
[tests/fixtures/README.md](tests/fixtures/README.md)). The adapter tests are the
canary for a site changing its format; `--self-check` answers "is it me or is it
them".

## Courtesy and limits

- One request per second per host, exponential backoff, and everything cached on
  disk (search pages 6 h, assets 1 year), so re-runs are nearly free.
- OpenStreetMap tiles get a dedicated 1 req/s lane and a User-Agent identifying
  this tool, per their [tile usage policy](https://operations.osmfoundation.org/policies/tiles/).
  Attribution is rendered on every map. Sending a browser User-Agent gets you
  blocked — the code deliberately does not.
- Built for personal house-hunting at human scale. Check Immoweb's and Zimmo's
  terms before using it for anything else, and don't republish their content.

## Licence

MIT — see [LICENSE](LICENSE). This covers the code only, not any data it fetches.
