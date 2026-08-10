"""Recovering bedroom counts that the search results omit.

The bar here is asymmetric: a wrong count silently corrupts a filter, while no
count merely leaves a visible gap. So these tests care as much about what the
inference *refuses* to answer as about what it gets right.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from homefinder import enrich
from homefinder.merge import merge
from homefinder.model import Criteria, Listing


def page(features: dict, description: str = "") -> str:
    rows = "".join(
        '<li><strong class="feature-label">%s</strong>'
        '<span class="feature-value">%s</span></li>' % (k, v)
        for k, v in features.items())
    return ('<html><head><meta name="description" content="%s"></head>'
            '<body><ul>%s</ul></body></html>' % (description, rows))


class TestFeatureTable(unittest.TestCase):
    def test_published_count_is_used_verbatim(self):
        self.assertEqual(
            enrich.bedrooms_from_page(page({"Type": "Appartement", "Slaapkamers": "3"})),
            (3, "detail"))

    def test_op_aanvraag_is_not_a_number(self):
        """Zimmo's placeholder for an unpublished value; parsing a digit out of
        it would be inventing data."""
        count, _ = enrich.bedrooms_from_page(
            page({"Type": "Appartement", "Slaapkamers": "op aanvraag &raquo;"}))
        self.assertIsNone(count)

    def test_entities_and_whitespace_are_cleaned(self):
        feats = enrich.parse_features(
            page({"Woonopp.": "120 m&sup2; &raquo;", "Type": "  Huis  "}))
        self.assertEqual(feats["Type"], "Huis")
        self.assertNotIn("&raquo;", feats["Woonopp."])


class TestStudioSubtype(unittest.TestCase):
    """The signal that does the most real work: 5 of the 8 unresolved listings
    in a live Gent rental search were studios."""

    def test_studio_has_no_bedrooms(self):
        self.assertEqual(
            enrich.bedrooms_from_page(
                page({"Type": "Studio (Appartement)", "Slaapkamers": "op aanvraag"})),
            (0, "subtype"))

    def test_studio_with_a_sleeping_nook_is_still_a_studio(self):
        self.assertEqual(
            enrich.bedrooms_from_page(
                page({"Type": "Studio met slaaphoek (Appartement)",
                      "Slaapkamers": "op aanvraag"})),
            (0, "subtype"))

    def test_a_published_count_outranks_the_subtype(self):
        count, source = enrich.bedrooms_from_page(
            page({"Type": "Studio (Appartement)", "Slaapkamers": "1"}))
        self.assertEqual((count, source), (1, "detail"))

    def test_plain_apartment_is_not_a_studio(self):
        count, _ = enrich.bedrooms_from_page(
            page({"Type": "Appartement", "Slaapkamers": "op aanvraag"}))
        self.assertIsNone(count)


class TestProse(unittest.TestCase):
    def test_dutch_digit_forms(self):
        for text, expected in [
                ("Dit 1 slaapkamer appartement van 56 m2", 1),
                ("Ruim appartement met 3 slaapkamers", 3),
                ("Instapklaar, 2 ruime slaapkamers en een terras", 2),
                ("Een 4-slaapkamerwoning met tuin", 4),
                ("Appartement met 2 slpk.", 2)]:
            self.assertEqual(enrich.bedrooms_from_text(text), expected, text)

    def test_dutch_word_numbers(self):
        self.assertEqual(enrich.bedrooms_from_text("met twee slaapkamers"), 2)
        self.assertEqual(enrich.bedrooms_from_text("drie ruime slaapkamers"), 3)

    def test_french_and_english(self):
        self.assertEqual(enrich.bedrooms_from_text("Appartement avec 2 chambres"), 2)
        self.assertEqual(enrich.bedrooms_from_text("Bright flat with 3 bedrooms"), 3)

    def test_takes_the_headline_count_not_a_later_room_by_room_mention(self):
        """Descriptions lead with the total, then walk through rooms; a later
        number is usually describing one of them."""
        self.assertEqual(enrich.bedrooms_from_text(
            "Appartement met 3 slaapkamers. De eerste slaapkamer is 14 m2, "
            "de tweede slaapkamer 11 m2."), 3)

    def test_refuses_when_nothing_is_stated(self):
        for text in ("", "Prachtig appartement met terras en garage",
                     "Ruime living, keuken, badkamer"):
            self.assertIsNone(enrich.bedrooms_from_text(text))

    def test_ignores_implausible_counts(self):
        self.assertIsNone(enrich.bedrooms_from_text("residentie met 240 slaapkamers"))

    def test_description_is_the_last_resort(self):
        count, source = enrich.bedrooms_from_page(
            page({"Type": "Appartement", "Slaapkamers": "op aanvraag"},
                 "Dit 2 slaapkamer appartement is instapklaar"))
        self.assertEqual((count, source), (2, "description"))

    def test_page_with_no_signal_at_all_returns_nothing(self):
        self.assertEqual(enrich.bedrooms_from_page(page({"Type": "Appartement"})),
                         (None, None))


class TestProvenanceSurvives(unittest.TestCase):
    def test_zero_bedrooms_survives_a_json_round_trip_when_established(self):
        """A studio's 0 is a fact. Without provenance it would be normalised
        back to "unknown" on reload and start passing bedroom filters again."""
        studio = Listing(bedrooms=0, bedrooms_source="subtype")
        self.assertEqual(studio.bedrooms, 0)
        self.assertEqual(Listing.from_dict(studio.to_dict()).bedrooms, 0)

    def test_bare_zero_from_a_search_result_is_still_unknown(self):
        self.assertIsNone(Listing(bedrooms=0).bedrooms)

    def test_a_studio_fails_a_two_bedroom_filter(self):
        c = Criteria(postcodes=["9000"], min_bedrooms=2)
        self.assertFalse(Listing(postcode="9000", bedrooms=0,
                                 bedrooms_source="subtype").matches(c))
        self.assertTrue(Listing(postcode="9000", bedrooms=2,
                                bedrooms_source="listed").matches(c))

    def test_merge_carries_the_count_and_its_provenance_together(self):
        a = Listing(sources={"immoweb": {"url": "u"}}, street="Ham 47",
                    postcode="9000", bedrooms=None)
        b = Listing(sources={"zimmo": {"url": "v"}}, street="Ham 47",
                    postcode="9000", bedrooms=2, bedrooms_source="description")
        out = merge([a, b])[0]
        self.assertEqual(out.bedrooms, 2)
        self.assertEqual(out.bedrooms_source, "description")

    def test_a_published_count_keeps_its_listed_provenance(self):
        a = Listing(sources={"immoweb": {"url": "u"}}, street="Ham 47",
                    postcode="9000", bedrooms=3, bedrooms_source="listed")
        b = Listing(sources={"zimmo": {"url": "v"}}, street="Ham 47",
                    postcode="9000", bedrooms=2, bedrooms_source="description")
        out = merge([a, b])[0]
        self.assertEqual((out.bedrooms, out.bedrooms_source), (3, "listed"))


# --------------------------------------------------------------------------
# Availability
# --------------------------------------------------------------------------

def immoweb_page(period="null", date="null") -> str:
    """The fragment of a detail page that carries availability.

    Deliberately keeps the surrounding noise a real page has: the translation
    dictionary defines an `available_date` label, and the whole classified blob
    appears a second time HTML-escaped. A parser that matched either would
    report a label as a date.
    """
    blob = ('"transaction":{"type":"FOR_RENT","subtype":"RENT_REGULAR",'
            '"availabilityPeriodType":%s,"availabilityDate":%s,'
            '"certificates":null}' % (period, date))
    return ('<html><body><script>window.classified={%s};</script>'
            '<script>var t={"available_date":"Available date",'
            '"immediately_available":"Immediately available"};</script>'
            '<div data-x="&quot;availabilityDate&quot;:&quot;1999-01-01&quot;">'
            '</div></body></html>' % blob)


class TestZimmoAvailability(unittest.TestCase):
    def test_published_date_becomes_iso(self):
        self.assertEqual(
            enrich.availability_from_zimmo_page(page({"Vrij op": "01/10/2026"})),
            ("2026-10-01", False))

    def test_onmiddellijk_is_immediate_not_a_date(self):
        self.assertEqual(
            enrich.availability_from_zimmo_page(page({"Vrij op": "onmiddellijk"})),
            (None, True))

    def test_absent_row_yields_nothing(self):
        self.assertEqual(
            enrich.availability_from_zimmo_page(page({"Type": "Appartement"})),
            (None, False))

    def test_unrecognised_wording_is_not_guessed_at(self):
        """'in overleg' (by arrangement) is a real Zimmo value that names no
        date. Rendering it as available-now would invent a fact."""
        self.assertEqual(
            enrich.availability_from_zimmo_page(page({"Vrij op": "in overleg"})),
            (None, False))

    def test_immediate_is_recognised_in_the_other_site_languages(self):
        """Zimmo serves /fr/ and /en/ too, and only the wording changes."""
        for word in ("onmiddellijk", "immédiatement", "immediately"):
            with self.subTest(word=word):
                self.assertEqual(
                    enrich.availability_from_zimmo_page(page({"Vrij op": word})),
                    (None, True))

    def test_day_and_month_are_not_transposed(self):
        """A European DD/MM date read as MM/DD silently moves a flat by months.
        13 cannot be a month, so this fails loudly if the order ever flips."""
        self.assertEqual(
            enrich.availability_from_zimmo_page(page({"Vrij op": "13/09/2026"})),
            ("2026-09-13", False))


class TestImmowebAvailability(unittest.TestCase):
    def test_availability_date_is_read(self):
        self.assertEqual(
            enrich.availability_from_immoweb_page(immoweb_page(date='"2026-09-01"')),
            ("2026-09-01", False))

    def test_immediately_period_has_no_date(self):
        self.assertEqual(
            enrich.availability_from_immoweb_page(immoweb_page(period='"IMMEDIATELY"')),
            (None, True))

    def test_both_null_yields_nothing(self):
        self.assertEqual(enrich.availability_from_immoweb_page(immoweb_page()),
                         (None, False))

    def test_other_period_types_are_not_treated_as_immediate(self):
        """Only IMMEDIATELY means "move in now". AFTER_DEED and friends are
        conditions, not dates, and must not render as either."""
        self.assertEqual(
            enrich.availability_from_immoweb_page(immoweb_page(period='"AFTER_DEED"')),
            (None, False))

    def test_a_real_date_wins_over_the_escaped_copy(self):
        got, _ = enrich.availability_from_immoweb_page(
            immoweb_page(date='"2026-09-01"'))
        self.assertEqual(got, "2026-09-01")


class FakeFetcher:
    """Records what was asked for, so the tests can assert on request count --
    the whole cost of this feature is one request per listing."""

    def __init__(self, pages: dict, fail: set = frozenset()):
        self.pages = pages
        self.fail = fail
        self.asked: list[str] = []

    def get_text(self, url, **kw):
        self.asked.append(url)
        if url in self.fail:
            raise OSError("boom")
        return self.pages.get(url, "<html></html>")

    def log(self, msg):
        pass


def both_sites(iw_url="https://immoweb/1", zi_url="https://zimmo/1") -> Listing:
    return Listing(sources={"immoweb": {"url": iw_url}, "zimmo": {"url": zi_url}},
                   postcode="9000", price=1000)


class TestFillAvailability(unittest.TestCase):
    def test_date_and_immediate_flag_land_on_the_listing(self):
        l = Listing(sources={"zimmo": {"url": "https://zimmo/1"}})
        f = FakeFetcher({"https://zimmo/1": page({"Vrij op": "01/09/2026"})})
        enrich.fill_availability([l], f, lambda m: None)
        self.assertEqual((l.available_from, l.available_immediately),
                         ("2026-09-01", False))

    def test_a_listing_on_both_sites_costs_one_request_not_two(self):
        l = both_sites()
        f = FakeFetcher({"https://immoweb/1": immoweb_page(date='"2026-09-01"')})
        enrich.fill_availability([l], f, lambda m: None)
        self.assertEqual(f.asked, ["https://immoweb/1"])
        self.assertEqual(l.available_from, "2026-09-01")

    def test_immoweb_is_preferred_because_its_date_needs_no_locale_guess(self):
        """Both sites have the field; Immoweb publishes ISO, Zimmo publishes
        DD/MM/YYYY. Given the choice, read the one that cannot be transposed."""
        l = both_sites()
        f = FakeFetcher({"https://immoweb/1": immoweb_page(period='"IMMEDIATELY"'),
                         "https://zimmo/1": page({"Vrij op": "01/09/2026"})})
        enrich.fill_availability([l], f, lambda m: None)
        self.assertTrue(l.available_immediately)
        self.assertNotIn("https://zimmo/1", f.asked)

    def test_falls_back_to_zimmo_when_there_is_no_immoweb_listing(self):
        l = Listing(sources={"zimmo": {"url": "https://zimmo/2"}})
        f = FakeFetcher({"https://zimmo/2": page({"Vrij op": "onmiddellijk"})})
        enrich.fill_availability([l], f, lambda m: None)
        self.assertEqual(f.asked, ["https://zimmo/2"])
        self.assertTrue(l.available_immediately)

    def test_a_listing_with_no_url_is_skipped_not_fetched(self):
        l = Listing(sources={})
        f = FakeFetcher({})
        stats = enrich.fill_availability([l], f, lambda m: None)
        self.assertEqual(f.asked, [])
        self.assertEqual(stats["considered"], 0)

    def test_a_dead_page_warns_and_leaves_the_rest_alone(self):
        """One 404 must not cost the other listings their dates."""
        dead = Listing(sources={"zimmo": {"url": "https://zimmo/dead"}})
        live = Listing(sources={"zimmo": {"url": "https://zimmo/live"}})
        f = FakeFetcher({"https://zimmo/live": page({"Vrij op": "01/09/2026"})},
                        fail={"https://zimmo/dead"})
        warnings = []
        enrich.fill_availability([dead, live], f, warnings.append)
        self.assertEqual(live.available_from, "2026-09-01")
        self.assertIsNone(dead.available_from)
        self.assertEqual(len(warnings), 1)

    def test_a_page_that_publishes_nothing_is_not_a_warning(self):
        """Half of Zimmo's rentals omit the row. That is normal, not a fault."""
        l = Listing(sources={"zimmo": {"url": "https://zimmo/3"}})
        warnings = []
        enrich.fill_availability([l], FakeFetcher({}), warnings.append)
        self.assertEqual(warnings, [])
        self.assertIsNone(l.available_from)

    def test_the_cap_truncates_and_says_which_flag_did_it(self):
        ls = [Listing(sources={"zimmo": {"url": "https://zimmo/%d" % i}})
              for i in range(5)]
        f = FakeFetcher({})
        warnings = []
        stats = enrich.fill_availability(ls, f, warnings.append, max_fetches=2)
        self.assertEqual(len(f.asked), 2)
        self.assertEqual(stats["capped"], 3)
        self.assertTrue(any("--max-availability" in w for w in warnings))

    def test_stats_count_what_was_actually_resolved(self):
        got = Listing(sources={"zimmo": {"url": "https://zimmo/a"}})
        blank = Listing(sources={"zimmo": {"url": "https://zimmo/b"}})
        f = FakeFetcher({"https://zimmo/a": page({"Vrij op": "01/09/2026"})})
        stats = enrich.fill_availability([got, blank], f, lambda m: None)
        self.assertEqual((stats["considered"], stats["fetched"], stats["resolved"]),
                         (2, 2, 1))


class TestAvailabilitySurvivesJson(unittest.TestCase):
    """`listings.json` is the contract between searching and rendering, so a
    field that doesn't round-trip is a field the report never sees."""

    def test_round_trip_keeps_both_fields(self):
        l = Listing(available_from="2026-09-01", available_immediately=False)
        self.assertEqual(Listing.from_dict(l.to_dict()).available_from,
                         "2026-09-01")

    def test_round_trip_keeps_the_immediate_flag(self):
        l = Listing(available_immediately=True)
        self.assertTrue(Listing.from_dict(l.to_dict()).available_immediately)

    def test_default_is_no_availability_at_all(self):
        l = Listing()
        self.assertEqual((l.available_from, l.available_immediately),
                         (None, False))


if __name__ == "__main__":
    unittest.main()
