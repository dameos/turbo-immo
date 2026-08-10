"""Dedupe, filtering, ranking and street normalisation."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from homefinder.merge import merge, rank
from homefinder.model import (APARTMENT, HOUSE, RENT, SALE, Criteria, Listing,
                              norm_street)


def iw(**kw):
    kw.setdefault("sources", {"immoweb": {"id": 1, "url": "https://immoweb/1"}})
    return Listing(**kw)


def zi(**kw):
    kw.setdefault("sources", {"zimmo": {"code": "AAA", "url": "https://zimmo/AAA"}})
    return Listing(**kw)


class TestNormaliseStreet(unittest.TestCase):
    def test_abbreviations_and_accents_collapse(self):
        self.assertEqual(norm_street("Sint-Pietersnieuwstraat 41"),
                         norm_street("St. Pietersnieuwstr. 41"))

    def test_box_suffix_ignored(self):
        self.assertEqual(norm_street("Ham 47 bus 3"), norm_street("Ham 47"))
        self.assertEqual(norm_street("Ham 47 - C"), norm_street("Ham 47"))

    def test_different_numbers_stay_different(self):
        self.assertNotEqual(norm_street("Ham 47"), norm_street("Ham 48"))


class TestMerge(unittest.TestCase):
    def test_same_address_across_sources_becomes_one_card(self):
        a = iw(street="Sint-Pietersnieuwstraat 41", postcode="9000", price=400000,
               bedrooms=3, habitable_m2=150, lat=51.04, lon=3.72)
        b = zi(street="St. Pietersnieuwstr. 41 bus 2", postcode="9000", price=400000,
               bedrooms=3, habitable_m2=150, epc="B")
        out = merge([a, b])
        self.assertEqual(len(out), 1)
        self.assertEqual(set(out[0].sources), {"immoweb", "zimmo"})

    def test_merge_takes_epc_from_zimmo_and_coords_from_immoweb(self):
        a = iw(street="Ham 47", postcode="9000", lat=51.05, lon=3.73,
               price=300000, bedrooms=2, habitable_m2=90)
        b = zi(street="Ham 47", postcode="9000", lat=99.9, lon=99.9, epc="A",
               price=300000, bedrooms=2, habitable_m2=90)
        out = merge([a, b])[0]
        self.assertEqual(out.epc, "A")
        self.assertEqual(out.lat, 51.05)   # immoweb wins for coordinates

    def test_different_houses_with_identical_specs_do_not_merge(self):
        """The false-merge case: same postcode, price, bedrooms and surface,
        different streets. These are two houses, not one."""
        a = iw(street="Ham 47", postcode="9000", price=400000, bedrooms=3,
               habitable_m2=150)
        b = zi(street="Coupure 12", postcode="9000", price=400000, bedrooms=3,
               habitable_m2=150)
        self.assertEqual(len(merge([a, b])), 2)

    def test_addressless_listing_attaches_via_specs(self):
        a = iw(street="Ham 47", postcode="9000", price=400000, bedrooms=3,
               habitable_m2=150)
        b = zi(street="Ham", postcode="9000", price=400000, bedrooms=3,
               habitable_m2=150, epc="C")           # no house number
        out = merge([a, b])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].epc, "C")

    def test_images_top_up_to_three_without_duplicates(self):
        a = iw(street="Ham 47", postcode="9000", images=["i1", "i2"])
        b = zi(street="Ham 47", postcode="9000", images=["i2", "z1", "z2"])
        out = merge([a, b])[0]
        self.assertEqual(out.images, ["i1", "i2", "z1"])

    def test_unrelated_listings_pass_through(self):
        self.assertEqual(len(merge([iw(street="A 1", postcode="9000"),
                                    zi(street="B 2", postcode="9000")])), 2)


class TestFilter(unittest.TestCase):
    def setUp(self):
        self.c = Criteria(postcodes=["9000"], property_types=[HOUSE],
                          min_price=200000, max_price=500000, min_bedrooms=3,
                          min_surface=120, min_land=100)

    def test_accepts_a_listing_that_meets_everything(self):
        self.assertTrue(iw(property_type=HOUSE, postcode="9000", price=350000,
                           bedrooms=3, habitable_m2=150, land_m2=200).matches(self.c))

    def test_rejects_the_promoted_listing_that_violates_land(self):
        """Immoweb really returns this: 499k, 0 m2 land, for a query that asked
        for at least 100 m2 of land."""
        self.assertFalse(iw(property_type=HOUSE, postcode="9000", price=499000,
                            bedrooms=4, habitable_m2=197, land_m2=0).matches(self.c))

    def test_rejects_out_of_range_price_and_wrong_type(self):
        base = dict(postcode="9000", bedrooms=3, habitable_m2=150, land_m2=200)
        self.assertFalse(iw(property_type=HOUSE, price=600000, **base).matches(self.c))
        self.assertFalse(iw(property_type=APARTMENT, price=350000, **base).matches(self.c))

    def test_rejects_other_postcode(self):
        self.assertFalse(iw(property_type=HOUSE, postcode="9030", price=350000,
                            bedrooms=3, habitable_m2=150, land_m2=200).matches(self.c))

    def test_missing_value_is_not_excluded(self):
        """A listing that doesn't publish its bedroom count should surface with a
        gap rather than disappear."""
        self.assertTrue(iw(property_type=HOUSE, postcode="9000", price=350000,
                           bedrooms=None, habitable_m2=150, land_m2=200).matches(self.c))


class TestAbsentValueSentinels(unittest.TestCase):
    def test_zero_price_is_treated_as_no_price(self):
        """Zimmo writes prijs="0" for price-on-request. Left as 0 it sorts to the
        top of a cheapest-first report as a free house."""
        self.assertIsNone(iw(price=0).price)

    def test_zero_bedrooms_and_surface_are_absent(self):
        listing = iw(bedrooms=0, habitable_m2=0)
        self.assertIsNone(listing.bedrooms)
        self.assertIsNone(listing.habitable_m2)

    def test_zero_land_is_kept_because_apartments_really_have_none(self):
        self.assertEqual(iw(land_m2=0).land_m2, 0)

    def test_priceless_listing_sorts_last_not_first(self):
        out = rank([iw(price=0), iw(price=250000)], Criteria(postcodes=["9000"]))
        self.assertEqual(out[0].price, 250000)


class TestResultCap(unittest.TestCase):
    """`--limit` is opt-in. An arbitrary default cutoff hid matching listings
    without saying which ones, which is the opposite of useful when house-hunting."""

    def setUp(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import search_homes
        self.mod = search_homes
        self.found = [iw(property_type=HOUSE, postcode="9000", price=100000 + i,
                         bedrooms=2, habitable_m2=100, street="Str %d" % i)
                      for i in range(25)]
        self.mod.SOURCES = {"stub": lambda c, f, w: list(self.found)}

    class _Fetcher:
        stats = {"hits": 0, "misses": 0, "errors": 0}
        def log(self, msg): pass

    def _run(self, **kw):
        c = Criteria(postcodes=["9000"], property_types=[HOUSE], **kw)
        return self.mod.run_search(c, self._Fetcher(), ["stub"])

    def test_default_keeps_every_match(self):
        out = self._run()
        self.assertEqual(out["counts"]["shown"], 25)
        self.assertEqual(out["counts"]["after_filter"], 25)
        self.assertEqual(out["warnings"], [])

    def test_explicit_limit_still_caps_and_says_so(self):
        out = self._run(limit=10)
        self.assertEqual(out["counts"]["shown"], 10)
        self.assertTrue(any("--limit 10" in w for w in out["warnings"]))

    def test_criteria_defaults_to_uncapped(self):
        self.assertEqual(Criteria(postcodes=["9000"]).limit, 0)


class TestAvailabilitySweep(unittest.TestCase):
    """Availability costs one request per listing, so *when* it runs matters as
    much as what it parses: rentals only, and only for listings that will
    actually appear on the report."""

    def setUp(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import search_homes
        self.mod = search_homes
        self.found = [
            Listing(sources={"zimmo": {"url": "https://zimmo/%d" % i}},
                    property_type=HOUSE, postcode="9000", price=1000 + i,
                    bedrooms=2, habitable_m2=100, street="Str %d" % i)
            for i in range(5)]
        self.mod.SOURCES = {"stub": lambda c, f, w: [
            Listing(**{k: v for k, v in vars(x).items()}) for x in self.found]}

    class _Fetcher:
        stats = {"hits": 0, "misses": 0, "errors": 0}

        def __init__(self):
            self.asked = []

        def log(self, msg):
            pass

        def get_text(self, url, **kw):
            self.asked.append(url)
            return ('<ul><li><strong class="feature-label">Vrij op</strong>'
                    '<span class="feature-value">01/09/2026</span></li></ul>')

    def _run(self, **kw):
        c = Criteria(postcodes=["9000"], property_types=[HOUSE], **kw)
        f = self._Fetcher()
        return self.mod.run_search(c, f, ["stub"]), f

    def test_a_rent_search_reads_availability(self):
        out, f = self._run(transaction=RENT)
        self.assertEqual(len(f.asked), 5)
        self.assertTrue(all(x["available_from"] == "2026-09-01"
                            for x in out["listings"]))

    def test_a_sale_search_does_not_pay_for_it(self):
        """An availability date on a sale listing is 'at deed' -- not worth a
        request per listing."""
        out, f = self._run(transaction=SALE)
        self.assertEqual(f.asked, [])
        self.assertTrue(all(x["available_from"] is None for x in out["listings"]))

    def test_zero_cap_turns_it_off(self):
        out, f = self._run(transaction=RENT, max_availability=0)
        self.assertEqual(f.asked, [])

    def test_only_listings_that_will_be_shown_are_looked_up(self):
        """It runs after --limit, unlike bedroom recovery which has to run
        before filtering because it changes who passes."""
        out, f = self._run(transaction=RENT, limit=2)
        self.assertEqual(len(f.asked), 2)

    def test_the_funnel_reports_what_it_cost_and_found(self):
        out, _ = self._run(transaction=RENT)
        self.assertEqual(out["counts"]["availability_lookups"], 5)
        self.assertEqual(out["counts"]["availability_found"], 5)

    def test_default_cap_is_generous_enough_for_a_normal_search(self):
        self.assertGreaterEqual(Criteria(postcodes=["9000"]).max_availability, 150)

    def test_no_availability_flag_is_the_same_as_a_zero_cap(self):
        args = self.mod.build_parser().parse_args(["--postcode", "9000",
                                             "--no-availability"])
        self.assertEqual(args.max_availability, 0)

    def test_the_cap_is_on_by_default_at_the_cli(self):
        args = self.mod.build_parser().parse_args(["--postcode", "9000"])
        self.assertGreaterEqual(args.max_availability, 150)


class TestRank(unittest.TestCase):
    def test_cheapest_first_with_unpriced_last(self):
        c = Criteria(postcodes=["9000"], sort="price")
        out = rank([iw(price=None), iw(price=300000), iw(price=100000)], c)
        self.assertEqual([x.price for x in out], [100000, 300000, None])

    def test_price_per_m2(self):
        c = Criteria(postcodes=["9000"], sort="price_per_m2")
        a = iw(price=400000, habitable_m2=100)   # 4000/m2
        b = iw(price=300000, habitable_m2=150)   # 2000/m2
        self.assertEqual(rank([a, b], c)[0], b)


if __name__ == "__main__":
    unittest.main()
