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

from .pratishtha import _r, FLAT_RULES, FLAT_TOKEN, SHOP_LOAN_PCT, SHOP_RULES

# Rs per sq.yd, the same for every facing.
FLAT_RATE = 31666.6666666667
# Facing is a flat premium on the Flat Price, NOT a different rate — the original
# prices road and garden at different per-sq.yd rates, Pratishtha 2 adds a lump sum
# to road-facing units instead. It lands on the Flat Price only: the terrace is
# priced off TERRACE_RATE and is unaffected.
FACING_PREMIUM = {'road': 50000}
# The original divides by 1.07 to strip the 7% (6% stamp + 1% GST) back out of an
# all-inclusive box price. Pratishtha 2's Final Unit Price is Box Price - Bank
# Processing flat, so there is no divisor. The Box Price remains the total the
# customer pays: stamp duty and GST are sale-deed figures already inside it, not
# charges added on top — which is why the four rows no longer sum to it.
DASTAVEJ_DIVISOR = 1


def facing_premium_for(facing):
    return FACING_PREMIUM.get(str(facing or '').strip().lower(), 0)
# Rs per sq.yd of private terrace. Units without a terrace price nothing for it.
TERRACE_RATE = 12000
# Shops keep the original's charge rules (6% stamp on loan, 5% GST, AUDA 400/sq.ft,
# 50% loan) but price on a size band of their own: the smaller the shop, the higher
# the per-sq.ft rate.
SHOP_RATE_SMALL = 12000      # Rs per sq.ft, under SHOP_SMALL_BELOW
SHOP_RATE_LARGE = 11000      # Rs per sq.ft, at or above it
SHOP_SMALL_BELOW = 500       # sq.ft


def shop_rate_for(sq_feet):
    """Rs 12,000/sq.ft under 500 sq.ft, Rs 11,000 at 500 and above."""
    return SHOP_RATE_SMALL if float(sq_feet or 0) < SHOP_SMALL_BELOW else SHOP_RATE_LARGE


def shop_price_book(number, sq_feet, rate=None):
    """Price book for a Pratishtha 2 shop.

    Same shape and charge rules as the original's shop_price_book, but the rate
    comes from the size band rather than a single project-wide SHOP_RATE — and can
    be overridden, which is what the booking form's editable Rate field passes in.
    """
    sq = float(sq_feet or 0)
    rate = shop_rate_for(sq) if rate in (None, '') else float(rate)
    amount = _r(sq * rate)
    loan = _r(amount * SHOP_LOAN_PCT)
    extras = {
        'stamp_duty_reg': _r(loan * SHOP_RULES['stamp_duty_reg'][1]),
        'gst':            _r(loan * SHOP_RULES['gst'][1]),
        'auda':           _r(sq * SHOP_RULES['auda'][1]),
        'maint_adv_6m':   _r(sq * 1.5 * 6),
        'maint_dep_12m':  _r(sq * 1.5 * 12),
        'legal':          SHOP_RULES['legal'][1],
    }
    total_extra = sum(extras.values())
    return {
        'kind': 'shop', 'unit': number,
        'sq_feet': sq, 'rate': rate,
        'amount': amount, 'loan_amount': loan,
        **extras,
        'total_extra': total_extra,
        'grand_total': amount + total_extra,
    }

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


def flat_price_book(number, flat_area, terrace_area=0, facing=None, token=FLAT_TOKEN,
                    rate=None):
    """Price book for a Pratishtha 2 flat, rounded at each step like the original."""
    R = FLAT_RULES
    area = float(flat_area or 0)
    terr = float(terrace_area or 0)
    flat_rate = float(rate) if rate not in (None, '') else FLAT_RATE
    premium = facing_premium_for(facing)
    flat_price = _r(area * flat_rate) + premium
    terrace_rate = TERRACE_RATE if terr else 0
    terrace_price = _r(terr * terrace_rate)
    box = flat_price + terrace_price
    loan = box - token
    bank_processing = _r(loan * R['bank_processing_pct'])
    dastavej = _r((box - bank_processing) / DASTAVEJ_DIVISOR)
    return {
        # `facing` is carried the way the original's books carry it — the booking form
        # and the LOI both surface it, and it records which rate the unit was priced on.
        'kind': 'flat', 'unit': number, 'facing': (facing or ''),
        'flat_area': area, 'terrace_area': terr,
        # `flat_rate` is the BASE rate; `facing_premium` is the lump sum on top. Both
        # are carried so the booking form can show the base rate as an editable field
        # and add the premium back when it recomputes the price from it.
        'flat_rate': flat_rate, 'terrace_rate': terrace_rate,
        'facing_premium': premium,
        'dastavej_divisor': DASTAVEJ_DIVISOR,
        'flat_price': flat_price, 'terrace_price': terrace_price,
        'box_price': box, 'token': token, 'bank_loan': loan,
        'bank_processing': bank_processing,
        'dastavej_value': dastavej,
        'stamp_duty_reg': _r(dastavej * R['stamp_duty_reg_pct']),
        'gst': _r(dastavej * R['gst_pct']),
        'total': box,
    }


def price_book_for(number, flat_area=None, terrace_area=0, sq_feet=None, facing=None):
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
        return shop_price_book(str(number), float(sq))
    if not flat_area:
        return None
    return flat_price_book(str(number), flat_area, terrace_area, facing=facing)
