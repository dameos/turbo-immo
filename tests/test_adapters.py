"""Adapter tests against saved real responses.

These are the canary for silent breakage: if either site changes its payload
shape, refresh the fixture and watch these fail.
"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from homefinder import immoweb, zimmo
from homefinder.model import APARTMENT, HOUSE, Criteria

FIX = ROOT / "tests" / "fixtures"


class TestImmoweb(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = json.loads((FIX / "immoweb_search.json").read_text(encoding="utf-8"))
        cls.listings = immoweb.parse_results(cls.payload)

    def test_parses_every_result_in_the_payload(self):
        self.assertEqual(len(self.listings), len(self.payload["results"]))

    def test_core_fields_present(self):
        first = self.listings[0]
        self.assertIsInstance(first.price, int)
        self.assertEqual(first.property_type, HOUSE)
        self.assertEqual(first.postcode, "9000")
        self.assertTrue(first.street)
        self.assertTrue(50 < first.lat < 52 and 2 < first.lon < 6)
        self.assertTrue(first.images and first.images[0].startswith("https://"))
        self.assertIn("immoweb", first.sources)
        self.assertIn(str(first.sources["immoweb"]["id"]),
                      first.sources["immoweb"]["url"])

    def test_every_listing_has_a_price(self):
        # price-less entries are new-build project clusters, not viewable homes
        self.assertTrue(all(x.price for x in self.listings))

    def test_at_most_three_images(self):
        self.assertTrue(all(len(x.images) <= 3 for x in self.listings))

    def test_url_carries_verified_param_names(self):
        c = Criteria(postcodes=["9000"], property_types=[HOUSE],
                     min_price=200000, max_price=500000, min_bedrooms=3,
                     min_surface=120, min_land=100)
        url = immoweb.build_url(c, "9000", 2)
        for expected in ("search-results/house/for-sale", "postalCodes=BE-9000",
                         "minPrice=200000", "maxPrice=500000", "minBedroomCount=3",
                         "minSurface=120", "minLandSurface=100", "page=2"):
            self.assertIn(expected, url)

    def test_type_slug_for_both_types(self):
        c = Criteria(postcodes=["9000"], property_types=[HOUSE, APARTMENT])
        self.assertIn("house-and-apartment", immoweb.build_url(c, "9000", 1))


class TestZimmo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (FIX / "zimmo_search.html").read_text(encoding="utf-8")
        cls.props = zimmo.extract_properties(cls.html)
        cls.listings = [zimmo.parse_property(p) for p in cls.props]

    def test_extracts_embedded_json(self):
        self.assertEqual(len(self.props), 4)

    def test_survives_braces_and_quotes_in_free_text(self):
        """The fixture's first description contains {braces}, escaped quotes and
        a newline. That free text is why extraction uses raw_decode rather than
        a non-greedy regex, which would terminate early on it."""
        self.assertIn("{", self.props[0]["a_beschrijf"])
        self.assertEqual(len(self.props), 4)   # nothing lost to early termination

    def test_reads_total_result_count(self):
        self.assertEqual(zimmo.total_results(self.html), 911)

    def test_core_fields_present(self):
        first = self.listings[0]
        self.assertIsInstance(first.price, int)
        self.assertEqual(first.postcode, "9000")
        self.assertTrue(50 < first.lat < 52 and 2 < first.lon < 6)
        self.assertIn("zimmo", first.sources)
        self.assertTrue(first.sources["zimmo"]["url"].startswith("https://www.zimmo.be/"))

    def test_supplies_epc_which_immoweb_lacks(self):
        self.assertTrue(any(x.epc for x in self.listings))

    def test_property_types_recognised(self):
        kinds = {x.property_type for x in self.listings}
        self.assertTrue(kinds & {HOUSE, APARTMENT})

    def test_unparseable_page_is_distinguished_from_an_empty_one(self):
        """A small town with no rentals must not raise the same alarm as Zimmo
        changing their page format -- one is normal, the other needs a fix."""
        self.assertIsNone(zimmo.extract_properties("<html>nothing here</html>"))
        self.assertIsNone(zimmo.extract_properties("properties: not-json"))
        self.assertIsNone(zimmo.extract_properties('properties: {"a": 1}'))
        self.assertEqual(zimmo.extract_properties("properties: []"), [])

    def test_place_slug_lookup(self):
        data = json.loads((FIX / "zimmo_places.json").read_text(encoding="utf-8"))
        places = zimmo.parse_places(data)
        self.assertEqual(places.get("9000"), ["gent"])
        # 8000 covers Brugge plus two sub-towns; the municipality sorts first
        # because it carries by far the most listings.
        self.assertEqual(places.get("8000")[0], "brugge")
        self.assertIn("koolkerke", places.get("8000"))

    def test_postcode_with_several_towns_returns_all_of_them(self):
        """9050 is Gentbrugge AND Ledeberg; 9070 is Destelbergen AND Heusden.
        Keeping one slug per postcode silently loses half the postcode."""
        data = json.loads((FIX / "zimmo_places.json").read_text(encoding="utf-8"))
        places = zimmo.parse_places(data)
        self.assertEqual(sorted(places["9050"]), ["gentbrugge", "ledeberg"])
        self.assertEqual(sorted(places["9070"]), ["destelbergen", "heusden"])

    def test_duplicate_slug_for_one_postcode_is_not_repeated(self):
        """The gazetteer lists 'gent' twice for 9000 at two admin levels."""
        data = json.loads((FIX / "zimmo_places.json").read_text(encoding="utf-8"))
        slugs = zimmo.parse_places(data)["9000"]
        self.assertEqual(len(slugs), len(set(slugs)))

    def test_url_shape(self):
        self.assertEqual(zimmo.build_url("gent", "9000", "sale", "huis", 1),
                         "https://www.zimmo.be/nl/gent-9000/te-koop/huis/")
        self.assertEqual(zimmo.build_url("gent", "9000", "sale", "huis", 3),
                         "https://www.zimmo.be/nl/gent-9000/te-koop/huis/?p=3")
        self.assertEqual(zimmo.build_url("brugge", "8000", "rent", None, 1),
                         "https://www.zimmo.be/nl/brugge-8000/te-huur/")


if __name__ == "__main__":
    unittest.main()
