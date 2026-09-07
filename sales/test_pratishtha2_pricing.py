"""Pratishtha 2 price books.

Two things separate Pratishtha 2 from the original: one flat rate for every facing,
and a terrace that is quoted (Rs 12,000/sq.yd) rather than derived at half the flat
rate. The original's half-rate rule must keep working untouched.
"""
from django.test import SimpleTestCase

from sales.pricing import pratishtha, pratishtha2


class UnitNumberTests(SimpleTestCase):
    def test_block_prefixed_numbers_parse(self):
        self.assertEqual(pratishtha2.parse_unit('E-104'), ('E', '104'))
        self.assertEqual(pratishtha2.parse_unit('A-1001'), ('A', '1001'))
        self.assertEqual(pratishtha2.parse_unit('B-Shop3'), ('B', 'Shop3'))
        self.assertEqual(pratishtha2.parse_unit('A-SHOP1'), ('A', 'Shop1'))

    def test_the_originals_parser_rejects_them(self):
        """Why every one of the 537 units had an empty price book."""
        self.assertIsNone(pratishtha.price_book_for('E-104'))
        self.assertIsNotNone(pratishtha.price_book_for('104'))


class FlatPricingTests(SimpleTestCase):
    def test_e104_matches_the_quoted_rates(self):
        b = pratishtha2.price_book_for('E-104', flat_area=60, terrace_area=35)
        self.assertEqual(b['flat_price'], 1_900_000)      # 60 x 31,666.6667
        self.assertEqual(b['terrace_price'], 420_000)     # 35 x 12,000
        self.assertEqual(b['box_price'], 2_320_000)
        self.assertEqual(b['terrace_rate'], 12_000)

    def test_terrace_rate_is_not_half_the_flat_rate(self):
        """The whole reason the book carries an explicit terrace_rate."""
        b = pratishtha2.price_book_for('E-104', flat_area=60, terrace_area=35)
        self.assertNotEqual(b['terrace_rate'], b['flat_rate'] / 2)

    def test_no_terrace_means_no_terrace_charge(self):
        b = pratishtha2.price_book_for('A-1001', flat_area=84)
        self.assertEqual(b['terrace_price'], 0)
        self.assertEqual(b['terrace_rate'], 0)
        self.assertEqual(b['box_price'], b['flat_price'])

    def test_the_original_project_still_halves_its_terrace(self):
        o = pratishtha.price_book_for('101')          # 1st floor, 21 sq.yd terrace
        self.assertEqual(o['terrace_rate'], o['flat_rate'] / 2)


class MissingAreaTests(SimpleTestCase):
    def test_a_flat_with_no_area_gets_no_book(self):
        """Never a zero-priced book — that is the failure being prevented."""
        self.assertIsNone(pratishtha2.price_book_for('E-104', flat_area=None))
        self.assertIsNone(pratishtha2.price_book_for('E-104', flat_area=0))

    def test_a_shop_never_borrows_the_originals_areas(self):
        """A-SHOP1 and the original's Shop1 are different shops in different blocks."""
        self.assertIsNone(pratishtha2.price_book_for('A-SHOP1'))
        self.assertIn('Shop1', pratishtha.SHOP_AREAS)   # it exists there, and stays there

    def test_a_shop_prices_from_its_own_area(self):
        b = pratishtha2.price_book_for('A-SHOP1', sq_feet=700)
        self.assertEqual(b['kind'], 'shop')
        self.assertEqual(b['unit'], 'A-SHOP1')
        self.assertEqual(b['sq_feet'], 700)


class ShopRateBandTests(SimpleTestCase):
    """Rs 12,000/sq.ft under 500 sq.ft, Rs 11,000 at 500 and above."""

    def test_under_500_takes_the_higher_rate(self):
        self.assertEqual(pratishtha2.shop_rate_for(255), 12_000)
        self.assertEqual(pratishtha2.shop_rate_for(499), 12_000)

    def test_500_and_above_takes_the_lower_rate(self):
        self.assertEqual(pratishtha2.shop_rate_for(500), 11_000)
        self.assertEqual(pratishtha2.shop_rate_for(700), 11_000)

    def test_the_band_reaches_the_price_book(self):
        small = pratishtha2.price_book_for('A-SHOP3', sq_feet=255)
        big = pratishtha2.price_book_for('A-SHOP1', sq_feet=700)
        self.assertEqual(small['rate'], 12_000)
        self.assertEqual(small['amount'], 255 * 12_000)
        self.assertEqual(big['rate'], 11_000)
        self.assertEqual(big['amount'], 700 * 11_000)

    def test_an_explicit_rate_overrides_the_band(self):
        """What the booking form's editable Rate field passes in."""
        b = pratishtha2.shop_price_book('A-SHOP1', 700, rate=9_500)
        self.assertEqual(b['rate'], 9_500)
        self.assertEqual(b['amount'], 700 * 9_500)

    def test_the_band_is_a_cliff_and_that_is_intended(self):
        """499 sq.ft costs more than 500 — inherent to a size band, pinned so a
        later 'fix' is a deliberate decision rather than an accident."""
        just_under = pratishtha2.price_book_for('A-SHOP1', sq_feet=499)['amount']
        just_over = pratishtha2.price_book_for('A-SHOP1', sq_feet=500)['amount']
        self.assertGreater(just_under, just_over)

    def test_an_unrecognised_number_gets_no_book(self):
        self.assertIsNone(pratishtha2.price_book_for('random', flat_area=60))
