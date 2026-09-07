"""Pratishtha 2 price book.

Same charge structure as the original Pratishtha (see pratishtha.py — that module
holds FLAT_RULES, SHOP_RULES and the rounding rule), but two things differ:

* **One flat rate, not one per facing.** Pratishtha 2 quotes Rs 31,666.6667 per
  sq.yd for every flat regardless of facing.
* **The terrace is quoted, not derived.** The original prices a terrace at exactly
  half the flat rate; Pratishtha 2 quotes Rs 12,000 per sq.yd, which is NOT half of
  31,666.6667 (that would be 15,833.33). The book therefore carries an explicit
  `terrace_rate` and the booking form honours it — see the fallback in
  lib/pratishthaFlat.js, which keeps the half-rate rule for books that omit the key.

Areas are NOT hardcoded here. The original's 103 units are a fixed floor plate, so
its areas live in the module; Pratishtha 2 has 537 units across five blocks and its
areas are per-unit data on the Plot rows (`size`, `terrace_area`).
"""

import re

from .pratishtha import _r, FLAT_RULES, FLAT_TOKEN, shop_price_book

# Rs per sq.yd, every flat, any facing.
FLAT_RATE = 31666.6666666667
# Rs per sq.yd of private terrace. Units without a terrace price nothing for it.
TERRACE_RATE = 12000
# Shops reuse the original's rules and rate. ASSUMED: not separately confirmed for
# Pratishtha 2 — change SHOP_RATE in pratishtha.py's terms here if it differs.
SHOP_RATE_ASSUMED = True

# A-1001 / E-104 / B-Shop3 — an optional block letter, then a unit number. The
# original's price_book_for() tests n.isdigit(), so every one of these fell through
# and returned None, which is why all 537 units had an empty price book.
_NUM = re.compile(r'^\s*(?:([A-Za-z])\s*-\s*)?(Shop\s*\d+|\d+)\s*$', re.I)


def parse_unit(number):
    """('E', '104') for 'E-104', ('B', 'Shop3') for 'B-Shop3', or None."""
    m = _NUM.match(str(number or ''))
    if not m:
        return None
    block, unit = m.group(1), re.sub(r'\s+', '', m.group(2))
    if unit.lower().startswith('shop'):
        unit = 'Shop' + unit[4:]
    return ((block or '').upper(), unit)


def flat_price_book(number, flat_area, terrace_area=0, token=FLAT_TOKEN):
    """Price book for a Pratishtha 2 flat, rounded at each step like the original."""
    R = FLAT_RULES
    area = float(flat_area or 0)
    terr = float(terrace_area or 0)
    flat_price = _r(area * FLAT_RATE)
    terrace_rate = TERRACE_RATE if terr else 0
    terrace_price = _r(terr * terrace_rate)
    box = flat_price + terrace_price
    loan = box - token
    bank_processing = _r(loan * R['bank_processing_pct'])
    dastavej = _r((box - bank_processing) / R['dastavej_divisor'])
    return {
        'kind': 'flat', 'unit': number,
        'flat_area': area, 'terrace_area': terr,
        'flat_rate': FLAT_RATE, 'terrace_rate': terrace_rate,
        'flat_price': flat_price, 'terrace_price': terrace_price,
        'box_price': box, 'token': token, 'bank_loan': loan,
        'bank_processing': bank_processing,
        'dastavej_value': dastavej,
        'stamp_duty_reg': _r(dastavej * R['stamp_duty_reg_pct']),
        'gst': _r(dastavej * R['gst_pct']),
        'total': box,
    }


def price_book_for(number, flat_area=None, terrace_area=0, sq_feet=None):
    """The price book for a Pratishtha 2 unit, or None when its areas are unknown.

    Shops price per sq.ft on the original's shop rules; flats per sq.yd on the rates
    above. Returns None rather than a zero-priced book when the area is missing — a
    book with no area would price the unit at nothing, which is the exact failure
    this whole change exists to prevent.
    """
    parsed = parse_unit(number)
    if not parsed:
        return None
    _block, unit = parsed
    if unit.lower().startswith('shop'):
        # Deliberately NO fallback to the original's SHOP_AREAS. Pratishtha 2's
        # A-SHOP1, C-Shop1 and the original's Shop1 are three different shops in
        # three different blocks; borrowing the original's floor plate would have
        # priced 24 units off another building's areas, silently and plausibly.
        sq = sq_feet
        if not sq:
            return None
        book = shop_price_book(unit, float(sq))
        book['unit'] = str(number)      # keep the block prefix the plot is named by
        return book
    if not flat_area:
        return None
    return flat_price_book(str(number), flat_area, terrace_area)
