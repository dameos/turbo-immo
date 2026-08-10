"""Tile arithmetic, template filling, and an end-to-end offline report build."""

import json
import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build_report
from homefinder import maps
from homefinder.model import Listing
from homefinder.render import TileRegistry, data_uri, render, sniff_mime

# Elfjulistraat 63, Gent. Tile numbers cross-checked against the canonical OSM
# formula written with asinh -- a different code path from maps.project's
# log(tan + sec) -- so this test is independent of the implementation.
LAT, LON, ZOOM = 51.0283363, 3.7210309, 15
TILE_X, TILE_Y = 16722, 10965


class TestTileMath(unittest.TestCase):
    def test_projection_lands_on_the_expected_tile(self):
        wx, wy = maps.project(LAT, LON, ZOOM)
        self.assertEqual(int(wx // maps.TILE), TILE_X)
        self.assertEqual(int(wy // maps.TILE), TILE_Y)

    def test_window_covers_the_viewport(self):
        w, h = 340, 210
        win = maps.window(LAT, LON, w, h, ZOOM)
        # the mosaic must start at or before the viewport's top-left...
        self.assertLessEqual(win.offset_x, 0)
        self.assertLessEqual(win.offset_y, 0)
        # ...and extend at or past its bottom-right, or the map shows bare
        # background where a tile should be
        self.assertGreaterEqual(win.offset_x + win.nx * maps.TILE, w)
        self.assertGreaterEqual(win.offset_y + win.ny * maps.TILE, h)

    def test_window_is_minimal(self):
        win = maps.window(LAT, LON, 340, 210, ZOOM)
        self.assertLessEqual(win.nx, 3)
        self.assertLessEqual(win.ny, 2)

    def test_pin_tile_is_inside_the_window(self):
        win = maps.window(LAT, LON, 340, 210, ZOOM)
        xs = [x for _, _, x, _ in win.tiles()]
        ys = [y for _, _, _, y in win.tiles()]
        self.assertIn(TILE_X, xs)
        self.assertIn(TILE_Y, ys)

    def test_extreme_latitudes_do_not_explode(self):
        for lat in (-89.9, 89.9, 0.0):
            win = maps.window(lat, 0.0, 340, 210, ZOOM)
            self.assertTrue(list(win.tiles()) or win.ny >= 1)

    def test_tile_urls_are_well_formed(self):
        url = maps.TILE_URL.format(z=ZOOM, x=TILE_X, y=TILE_Y)
        self.assertEqual(url, "https://tile.openstreetmap.org/15/16722/10965.png")


class TestPlaceholderTileDetection(unittest.TestCase):
    """OSM serves its "access blocked" notice as a PNG with HTTP 200, so it
    reaches the report looking exactly like a successful fetch. These guard the
    two independent defences against that."""

    BLOCKED = bytes.fromhex("89504e470d0a1a0a") + b"pretend blocked tile"

    def setUp(self):
        self._real = set(maps.BLOCKED_TILE_SHA256)
        import hashlib
        maps.BLOCKED_TILE_SHA256.add(hashlib.sha256(self.BLOCKED).hexdigest())

    def tearDown(self):
        maps.BLOCKED_TILE_SHA256.clear()
        maps.BLOCKED_TILE_SHA256.update(self._real)

    def test_known_placeholder_is_recognised(self):
        self.assertTrue(maps.is_placeholder_tile(self.BLOCKED))

    def test_empty_response_is_a_placeholder(self):
        self.assertTrue(maps.is_placeholder_tile(b""))

    def test_a_real_tile_is_not(self):
        self.assertFalse(maps.is_placeholder_tile(b"\x89PNG\r\n\x1a\n" + b"x" * 9000))

    def test_the_real_osm_blocked_tile_hash_is_registered(self):
        self.assertIn("99465c86e84cdbcdf88f207e48c2e8ce70b144d12e8f029a5b15bbee0b34df4a",
                      self._real)

    def test_uniform_tiles_are_flagged_even_if_the_hash_is_unknown(self):
        """Provider-agnostic backstop: distinct coordinates returning identical
        bytes is never real map data."""
        class OneImageEmbedder:
            stats = type("S", (), {"failed": 0, "embedded": 0, "bytes": 0})()
            def uri(self, url, reject=None):
                return "data:image/png;base64,SAMEBYTES"

        reg = TileRegistry(OneImageEmbedder())
        for n in range(4):
            reg.class_for("https://tiles/15/100%d/200.png" % n)
        self.assertTrue(reg.suspect_uniform())

    def test_varied_tiles_are_not_flagged(self):
        class VariedEmbedder:
            stats = type("S", (), {"failed": 0, "embedded": 0, "bytes": 0})()
            def uri(self, url, reject=None):
                return "data:image/png;base64," + url[-9:]

        reg = TileRegistry(VariedEmbedder())
        for n in range(4):
            reg.class_for("https://tiles/15/100%d/200.png" % n)
        self.assertFalse(reg.suspect_uniform())


class TestTileUserAgent(unittest.TestCase):
    def test_osm_gets_an_identifying_user_agent_not_the_browser_one(self):
        from homefinder.fetch import UA, Fetcher
        osm_ua = Fetcher.HOST_UA["tile.openstreetmap.org"]
        self.assertNotEqual(osm_ua, UA)
        self.assertNotIn("Mozilla", osm_ua)
        self.assertIn("find-homes", osm_ua)


class TestRender(unittest.TestCase):
    def test_escapes_by_default_and_raw_opts_out(self):
        out = render("<i>{{A}}</i><b>{{B|raw}}</b>", {"A": "<x>", "B": "<x>"})
        self.assertEqual(out, "<i>&lt;x&gt;</i><b><x></b>")

    def test_none_renders_empty(self):
        self.assertEqual(render("[{{A}}]", {"A": None}), "[]")

    def test_missing_placeholder_raises_rather_than_blanking(self):
        with self.assertRaises(KeyError):
            render("{{A}} {{B}}", {"A": 1})

    def test_sniffs_image_types(self):
        self.assertEqual(sniff_mime(b"\xff\xd8\xff\xe0rest"), "image/jpeg")
        self.assertEqual(sniff_mime(b"\x89PNG\r\n\x1a\nrest"), "image/png")
        self.assertTrue(data_uri(b"\x89PNG\r\n\x1a\n").startswith("data:image/png;base64,"))


class TestReportBuild(unittest.TestCase):
    """Full render with embedding disabled, so it never touches the network."""

    def setUp(self):
        self.data = {
            "generated_at": "2026-08-10T12:00:00+02:00",
            "criteria": {"postcodes": ["9000"]},
            "criteria_summary": "house for sale in 9000",
            "counts": {"immoweb": 10, "zimmo": 8, "merged": 15,
                       "after_filter": 4, "shown": 3},
            "warnings": ["zimmo: page 3 failed"],
            "listings": [
                {"sources": {"immoweb": {"id": 1, "url": "https://immoweb/1"},
                             "zimmo": {"code": "A", "url": "https://zimmo/A"}},
                 "price": 399000, "property_type": "house", "bedrooms": 3,
                 "habitable_m2": 150, "land_m2": 200, "street": "Ham 47",
                 "postcode": "9000", "locality": "Gent", "lat": LAT, "lon": LON,
                 "agency": "Test & Co", "epc": "B",
                 "images": ["https://x/1.jpg", "https://x/2.jpg", "https://x/3.jpg"]},
                {"sources": {"zimmo": {"code": "B", "url": "https://zimmo/B"}},
                 "price": 250000, "property_type": "apartment", "bedrooms": 2,
                 "habitable_m2": 80, "street": "Coupure 1", "postcode": "9000",
                 "locality": "Gent", "lat": None, "lon": None,
                 "images": ["https://x/only.jpg"]},
                {"sources": {"immoweb": {"id": 3, "url": "https://immoweb/3"}},
                 "price": None, "property_type": "house", "street": None,
                 "postcode": "9000", "locality": "Gent", "lat": LAT, "lon": LON,
                 "images": []},
            ],
        }
        self.html, _ = build_report.build(self.data, fetcher=None, embed_assets=False)

    def test_no_unfilled_placeholders_remain(self):
        self.assertNotIn("{{", self.html)

    def test_one_card_per_listing(self):
        self.assertEqual(self.html.count('<article class="card">'), 3)

    def test_shows_prices_and_epc(self):
        self.assertIn("€399.000", self.html)
        self.assertIn("EPC B", self.html)

    def test_warnings_surface_in_the_page(self):
        self.assertIn("zimmo: page 3 failed", self.html)
        self.assertIn("may be", self.html)

    def test_both_source_badges_render_for_a_merged_listing(self):
        self.assertIn("IMMOWEB", self.html)
        self.assertIn("ZIMMO", self.html)

    def test_single_image_listing_widens_the_hero(self):
        self.assertIn('class="hero wide"', self.html)

    def test_listing_without_photos_gets_a_placeholder(self):
        self.assertIn("no photo", self.html)

    def test_listing_without_coordinates_says_so_instead_of_drawing_a_map(self):
        self.assertIn("no location", self.html)

    def test_hidden_price_is_labelled(self):
        self.assertIn("Price on request", self.html)

    def test_agency_name_is_escaped(self):
        self.assertIn("Test &amp; Co", self.html)

    def test_empty_result_set_still_renders(self):
        data = dict(self.data, listings=[])
        html, _ = build_report.build(data, fetcher=None, embed_assets=False)
        self.assertIn("No listings matched", html)
        self.assertNotIn("{{", html)


class TestAvailabilityChip(unittest.TestCase):
    """The chip answers "when can I move in?", so a date already in the past
    must not read as a future one, and "now" must not be confused with silence."""

    TODAY = date(2026, 8, 10)

    def chip(self, **kw):
        return build_report.availability_chip(Listing(**kw), self.TODAY)

    def test_a_date_later_this_year_omits_the_year(self):
        text, title = self.chip(available_from="2026-09-01")
        self.assertEqual(text, "Free 1 Sep")
        self.assertIn("2026-09-01", title)

    def test_a_date_in_another_year_keeps_it(self):
        text, _ = self.chip(available_from="2027-01-15")
        self.assertEqual(text, "Free 15 Jan 2027")

    def test_immediately_reads_as_free_now(self):
        text, title = self.chip(available_immediately=True)
        self.assertEqual(text, "Free now")
        self.assertIn("immediately", title.lower())

    def test_a_date_that_has_passed_reads_as_free_now(self):
        """Zimmo really does serve 15/06/2026 in August. The flat is free; the
        advert is just stale. Printing the past date would look like a bug."""
        text, title = self.chip(available_from="2026-06-15")
        self.assertEqual(text, "Free now")
        self.assertIn("2026-06-15", title)

    def test_available_today_is_free_now_not_a_date(self):
        text, _ = self.chip(available_from="2026-08-10")
        self.assertEqual(text, "Free now")

    def test_nothing_published_means_no_chip(self):
        self.assertIsNone(self.chip())

    def test_an_unparseable_date_means_no_chip(self):
        """Rather than crash a whole report over one malformed field."""
        self.assertIsNone(self.chip(available_from="soon"))


class TestAvailabilityInChipRow(unittest.TestCase):
    TODAY = date(2026, 8, 10)

    def test_it_renders_beside_the_other_chips(self):
        out = build_report.chips_html(
            Listing(bedrooms=2, bedrooms_source="listed", habitable_m2=80,
                    epc="237", available_immediately=True), self.TODAY)
        for expected in ("2 bed", "80 m²", "EPC 237", "Free now"):
            self.assertIn(expected, out)

    def test_an_estimated_bedroom_count_does_not_swallow_it(self):
        """The estimated-bedroom path renders its chip separately and then
        appends the rest. A second copy of that list is how the availability
        chip would silently vanish from exactly the listings we enriched."""
        out = build_report.chips_html(
            Listing(bedrooms=2, bedrooms_source="description", epc="237",
                    available_from="2026-09-01"), self.TODAY)
        self.assertIn("~2 bed", out)
        self.assertIn("Free 1 Sep", out)


class TestAvailabilityCoverageIsStated(unittest.TestCase):
    def test_the_header_says_how_many_listings_published_a_date(self):
        """Roughly half of Zimmo's rentals publish nothing, so a card with no
        chip is ambiguous between 'no date' and 'not looked up'. The count
        disambiguates it for the whole report."""
        out = build_report.counts_html(
            {"merged": 20, "after_filter": 10, "shown": 10,
             "availability_found": 6})
        self.assertIn("6", out)
        self.assertIn("with a date", out)

    def test_a_sale_report_says_nothing_about_availability(self):
        out = build_report.counts_html({"merged": 20, "shown": 10})
        self.assertNotIn("with a date", out)


class TestReportDateIsNotTheClock(unittest.TestCase):
    def test_the_chip_is_dated_from_generated_at_not_today(self):
        """A report rebuilt months later must render identically -- which also
        means these tests never go stale as the real date moves."""
        data = {
            "generated_at": "2026-08-10T12:00:00+02:00",
            "criteria": {"postcodes": ["9000"]}, "counts": {}, "warnings": [],
            "listings": [{"sources": {"zimmo": {"url": "https://zimmo/A"}},
                          "price": 1000, "postcode": "9000",
                          "available_from": "2026-09-01", "images": []}],
        }
        html, _ = build_report.build(data, fetcher=None, embed_assets=False)
        self.assertIn("Free 1 Sep", html)

    def test_a_report_with_no_generation_date_still_builds(self):
        data = {"criteria": {}, "counts": {}, "warnings": [],
                "listings": [{"sources": {}, "available_immediately": True,
                              "images": []}]}
        html, _ = build_report.build(data, fetcher=None, embed_assets=False)
        self.assertIn("Free now", html)
        self.assertNotIn("{{", html)


if __name__ == "__main__":
    unittest.main()
