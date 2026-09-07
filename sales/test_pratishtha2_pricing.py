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
        """No facing given, so no premium — the plain rate arithmetic."""
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
        b = pratishtha2.price_book_for('A-101', flat_area=84)
        self.assertEqual(b['terrace_price'], 0)
        self.assertEqual(b['terrace_rate'], 0)
        self.assertEqual(b['box_price'], b['flat_price'])

    def test_the_original_project_still_halves_its_terrace(self):
        o = pratishtha.price_book_for('101')          # 1st floor, 21 sq.yd terrace
        self.assertEqual(o['terrace_rate'], o['flat_rate'] / 2)


class FacingPremiumTests(SimpleTestCase):
    """Road facing adds a lump sum to the Flat Price — it is not a higher rate."""

    def _book(self, facing):
        return pratishtha2.price_book_for(
            'E-104', flat_area=60, terrace_area=35, facing=facing)

    def test_road_adds_the_premium(self):
        b = self._book('road')
        self.assertEqual(b['facing_premium'], 50_000)
        self.assertEqual(b['flat_price'], 1_950_000)      # 60 x 31,666.6667 + 50,000
        self.assertEqual(b['box_price'], 2_370_000)       # + 35 x 12,000

    def test_garden_and_blank_pay_no_premium(self):
        for facing in ('garden', '', None):
            b = self._book(facing)
            self.assertEqual(b['facing_premium'], 0, facing)
            self.assertEqual(b['flat_price'], 1_900_000, facing)

    def test_the_rate_itself_does_not_change_with_facing(self):
        self.assertEqual(self._book('road')['flat_rate'],
                         self._book('garden')['flat_rate'])

    def test_the_premium_does_not_touch_the_terrace(self):
        self.assertEqual(self._book('road')['terrace_price'],
                         self._book('garden')['terrace_price'])

    def test_facing_is_recorded_on_the_book(self):
        self.assertEqual(self._book('road')['facing'], 'road')
        self.assertEqual(self._book(None)['facing'], '')

    def test_the_original_still_prices_facing_by_rate(self):
        """Untouched: it charges different per-sq.yd rates, with no premium."""
        self.assertNotEqual(pratishtha.FLAT_RATE['road'], pratishtha.FLAT_RATE['garden'])
        self.assertNotIn('facing_premium', pratishtha.price_book_for('101'))


class FinalUnitPriceTests(SimpleTestCase):
    """Pratishtha 2 has no 1.07 divisor: Final Unit Price = Box Price - Bank
    Processing. The Box Price stays the total the customer pays — stamp duty and GST
    are sale-deed figures inside it, not charges added on top."""

    def _book(self):
        return pratishtha2.price_book_for(
            'E-104', flat_area=60, terrace_area=35, facing='road')

    def test_final_unit_price_is_box_minus_processing(self):
        b = self._book()
        self.assertEqual(b['dastavej_value'], b['box_price'] - b['bank_processing'])
        self.assertEqual(b['dastavej_value'], 2_263_845)

    def test_no_divisor_is_recorded_on_the_book(self):
        self.assertEqual(self._book()['dastavej_divisor'], 1)

    def test_the_box_price_remains_the_total(self):
        b = self._book()
        self.assertEqual(b['total'], b['box_price'])
        self.assertEqual(b['total'], 2_370_000)

    def test_the_rows_deliberately_do_not_sum_to_the_total(self):
        """They did under 1.07. They no longer do, and that is correct — pinned so
        nobody 'repairs' it back into an all-inclusive breakdown."""
        b = self._book()
        four = (b['dastavej_value'] + b['stamp_duty_reg']
                + b['gst'] + b['bank_processing'])
        self.assertGreater(four, b['box_price'])

    def test_the_original_still_divides_by_107(self):
        o = pratishtha.price_book_for('101')
        four = (o['dastavej_value'] + o['stamp_duty_reg']
                + o['gst'] + o['bank_processing'])
        self.assertAlmostEqual(four, o['box_price'], delta=2)
        self.assertNotIn('dastavej_divisor', o)


class FloorRateTests(SimpleTestCase):
    """The rate steps down with height: 31,666.6667 on floors 1-3, 30,000 on 4-7.
    Facing does not move the rate, it adds a lump sum on top."""

    def _price(self, unit, facing='garden'):
        b = pratishtha2.price_book_for(unit, flat_area=60, facing=facing)
        return b['flat_rate'], b['flat_price']

    def test_floors_1_to_3(self):
        for unit in ('E-101', 'E-201', 'E-301'):
            rate, price = self._price(unit)
            self.assertAlmostEqual(rate, 31666.6666666667)
            self.assertEqual(price, 1_900_000, unit)

    def test_floors_4_to_7(self):
        for unit in ('E-401', 'E-501', 'E-601', 'E-701'):
            rate, price = self._price(unit)
            self.assertEqual(rate, 30_000)
            self.assertEqual(price, 1_800_000, unit)

    def test_the_road_premium_is_the_same_on_every_floor(self):
        self.assertEqual(self._price('E-104', 'road')[1], 1_950_000)
        self.assertEqual(self._price('E-404', 'road')[1], 1_850_000)

    def test_floors_8_to_10(self):
        for unit in ('E-801', 'E-901', 'E-1001'):
            rate, price = self._price(unit)
            self.assertAlmostEqual(rate, 28333.3333333333)
            self.assertEqual(price, 1_700_000, unit)

    def test_an_unknown_floor_gets_no_book_rather_than_a_guess(self):
        """Block E runs 1-10; an 11th floor has no rate. A guessed one would be a
        wrong price that looks right — the failure this work exists to stop."""
        self.assertIsNone(pratishtha2.price_book_for('E-1101', flat_area=60))

    def test_the_rate_steps_down_with_height(self):
        floors = [self._price(f'E-{f}01')[0] for f in (1, 4, 8)]
        self.assertEqual(floors, sorted(floors, reverse=True))

    def test_tenth_floor_terraces_price_at_the_quoted_rate(self):
        """Straight off the sheet's BOX PRICE column."""
        for unit, terr, box in (('E-1001', 60, 2_420_000), ('E-1002', 86, 2_782_000),
                                ('E-1010', 20, 1_940_000), ('E-1011', 41, 2_192_000)):
            facing = 'road' if unit == 'E-1002' else 'garden'
            b = pratishtha2.price_book_for(unit, flat_area=60, terrace_area=terr,
                                           facing=facing)
            self.assertEqual(b['terrace_price'], terr * 12_000, unit)
            self.assertEqual(b['box_price'], box, unit)

    def test_floor_is_read_off_the_unit_number(self):
        self.assertEqual(pratishtha2.floor_of('104'), 1)
        self.assertEqual(pratishtha2.floor_of('1001'), 10)
        self.assertIsNone(pratishtha2.floor_of('Shop3'))

    def test_an_explicit_floor_beats_the_number(self):
        """The Plot row's own `floor` wins, so a renumbered unit still prices right."""
        b = pratishtha2.price_book_for('E-104', flat_area=60, floor=4)
        self.assertEqual(b['flat_rate'], 30_000)


class BlockABCDTests(SimpleTestCase):
    """Blocks A-D: 84 sq.yd flats over 12 floors on one shared rate ladder, with a
    road premium that doubles on the top two floors."""

    BANDS = {1: (3000000, 3050000), 2: (3000000, 3050000), 3: (3000000, 3050000),
             4: (2900000, 2950000), 5: (2900000, 2950000), 6: (2900000, 2950000),
             7: (2900000, 2950000), 8: (2800000, 2850000), 9: (2800000, 2850000),
             10: (2800000, 2850000), 11: (2700000, 2800000), 12: (2700000, 2800000)}

    def test_every_floor_matches_the_sheet(self):
        for block in ('A', 'B', 'C', 'D'):
            for floor, (garden, road) in self.BANDS.items():
                for facing, expected in (('garden', garden), ('road', road)):
                    b = pratishtha2.price_book_for(
                        '%s-%d01' % (block, floor), flat_area=84,
                        facing=facing, floor=floor)
                    self.assertEqual(b['flat_price'], expected,
                                     '%s floor %d %s' % (block, floor, facing))

    def test_the_premium_doubles_on_floors_11_and_12(self):
        for block in ('A', 'B', 'C', 'D'):
            for floor, expected in ((10, 50_000), (11, 100_000), (12, 100_000)):
                b = pratishtha2.price_book_for('%s-%d02' % (block, floor),
                                               flat_area=84, facing='road', floor=floor)
                self.assertEqual(b['facing_premium'], expected,
                                 '%s floor %d' % (block, floor))

    def test_block_e_keeps_its_own_ladder(self):
        """A-D must not have moved E's rates."""
        for unit, floor, price in (('E-101', 1, 1_900_000), ('E-401', 4, 1_800_000),
                                   ('E-801', 8, 1_700_000)):
            b = pratishtha2.price_book_for(unit, flat_area=60, facing='garden',
                                           floor=floor)
            self.assertEqual(b['flat_price'], price, unit)

    def test_e_keeps_the_flat_50k_premium_on_every_floor(self):
        for floor in (1, 8, 10):
            b = pratishtha2.price_book_for('E-%d04' % floor, flat_area=60,
                                           facing='road', floor=floor)
            self.assertEqual(b['facing_premium'], 50_000)

    def test_a_floor_above_the_block_gets_no_book(self):
        self.assertIsNone(pratishtha2.price_book_for('A-1301', flat_area=84, floor=13))
        self.assertIsNone(pratishtha2.price_book_for('E-1101', flat_area=60, floor=11))

    def test_a_25_sqyd_terrace_always_charges(self):
        """The B-102 / D-102 case. Both sheets printed 0 against a 25 sq.yd terrace
        while their mirror units (A-106 / C-106) charged 3,00,000 for the same area;
        confirmed as a sheet error, so the rate applies uniformly."""
        for unit in ('A-106', 'B-102', 'C-106', 'D-102'):
            b = pratishtha2.price_book_for(unit, flat_area=84, terrace_area=25,
                                           facing='garden', floor=1)
            self.assertEqual(b['terrace_price'], 300_000, unit)

    def test_floor_1_terraces_price_at_the_quoted_rate(self):
        for unit, terr, price in (('A-105', 55, 660_000), ('A-102', 30, 360_000),
                                  ('A-106', 25, 300_000), ('B-103', 55, 660_000)):
            b = pratishtha2.price_book_for(unit, flat_area=84, terrace_area=terr,
                                           facing='road' if unit != 'A-106' else 'garden',
                                           floor=1)
            self.assertEqual(b['terrace_price'], price, unit)


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


class GroundFloorShopTests(SimpleTestCase):
    """Block E numbers its ground-floor shops E-1..E-16, with no 'Shop' in the
    name — so shop-ness comes off floor 0, not the number. Figures are the sheet's
    own columns for the six distinct sizes in the block."""

    SHEET = {  # sq.ft -> (rate, amount, loan, stamp, gst, auda, m6, m12, extra, box)
        700: (11000, 7700000, 3850000, 231000, 192500, 280000, 6300, 12600, 732400, 8432400),
        690: (11000, 7590000, 3795000, 227700, 189750, 276000, 6210, 12420, 722080, 8312080),
        420: (12000, 5040000, 2520000, 151200, 126000, 168000, 3780, 7560, 466540, 5506540),
        405: (12000, 4860000, 2430000, 145800, 121500, 162000, 3645, 7290, 450235, 5310235),
        402: (12000, 4824000, 2412000, 144720, 120600, 160800, 3618, 7236, 446974, 5270974),
        255: (12000, 3060000, 1530000, 91800, 76500, 102000, 2295, 4590, 287185, 3347185),
    }

    def test_floor_0_is_a_shop_even_without_shop_in_the_name(self):
        b = pratishtha2.price_book_for('E-1', sq_feet=700, floor=0)
        self.assertEqual(b['kind'], 'shop')

    def test_every_column_matches_the_sheet(self):
        for sq, exp in self.SHEET.items():
            b = pratishtha2.price_book_for('E-1', sq_feet=sq, floor=0)
            got = (b['rate'], b['amount'], b['loan_amount'], b['stamp_duty_reg'],
                   b['gst'], b['auda'], b['maint_adv_6m'], b['maint_dep_12m'],
                   b['total_extra'], b['grand_total'])
            self.assertEqual(got, exp, '%s sq.ft' % sq)

    def test_a_shop_without_an_area_still_gets_no_book(self):
        self.assertIsNone(pratishtha2.price_book_for('E-1', floor=0))

    def test_a_flat_floor_is_not_treated_as_a_shop(self):
        self.assertEqual(
            pratishtha2.price_book_for('E-101', flat_area=60, floor=1)['kind'], 'flat')


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
