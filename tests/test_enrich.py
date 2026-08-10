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


if __name__ == "__main__":
    unittest.main()
