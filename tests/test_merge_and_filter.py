"""Dedupe, filtering, ranking and street normalisation."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from homefinder.merge import merge, rank
from homefinder.model import APARTMENT, HOUSE, Criteria, Listing, norm_street


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
