# Test fixtures

**These are redacted samples, not live captures.** They preserve the structure
the adapters parse — field names, nesting, the `app.start({... properties: [...]})`
wrapper, and free text containing braces and quotes — but every identifying
value has been replaced:

| Real | In these fixtures |
|---|---|
| Street addresses | `Voorbeeldstraat 10`, `Proefdreef 17`, … |
| Agency names and phone numbers | `Agency Alpha`, phone `null` |
| Photo CDN URLs | `https://example.invalid/photos/…` |
| Listing ids / Zimmo codes | `10000001…`, `TEST00…` |
| Coordinates | rounded to 2 decimals (~1 km, city-level) |

Result sets are trimmed to 4 listings and the Zimmo gazetteer to the 24 places
the tests exercise. Enough to prove the parsers work; not a republication of
Immoweb's or Zimmo's content.

## Refreshing after a site changes format

`search_homes.py --self-check` is what tells you a site has changed. When it
does, capture the live responses by hand and re-run the redactor:

```bash
python tests/fixtures/redact.py <live_immoweb.json> <live_zimmo.html> <live_places.json>
```

Then fix the adapter until the tests pass again. Do not commit the live captures.
