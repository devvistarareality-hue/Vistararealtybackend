"""Pratishtha price book.

Every unit is derived from a rate: flats from a per-sq.yd flat rate, shops from a
per-sq.ft rate. This module is the single source of those rules and the per-unit
areas they are applied to.

Three groups:

* **Ground floor shops (Shop1–Shop12)** — area x rate, with each extra a stated
  percentage or per-sq.ft charge (see SHOP_RULES).
* **1st floor flats (101–107)** — the only flats with a private terrace, so each has its
  own terrace area. The terrace is priced at exactly half the flat rate.
* **Typical flats (201–1307)** — identical on every floor, one rate per facing.

Facing matters: road-facing commands a premium over garden-facing.
"""

import math


def _r(x):
    """Round half *up*, matching JavaScript's Math.round. Python's built-in round()
    is banker's rounding, so 136192.5 would go to 136192 in the backend and 136193 in
    the booking form — the two must agree to the rupee."""
    return int(math.floor(float(x) + 0.5))


# ── Ground floor shops ────────────────────────────────────────────────────────
# Everything below the area is derived, so a rate change only needs SHOP_AREAS edited.
SHOP_RATE = 10000            # Rs per sq.ft
SHOP_LOAN_PCT = 0.50         # loan amount = 50% of the unit amount
SHOP_RULES = {
    'stamp_duty_reg': ('pct_of_loan', 0.06),      # 6% of loan amount
    'gst':            ('pct_of_loan', 0.05),      # 5% of loan amount
    'auda':           ('per_sqft', 400),          # Rs 400 per sq.ft
    'maint_adv_6m':   ('per_sqft_month', 1.5, 6),   # Rs 1.5/sq.ft/month x 6
    'maint_dep_12m':  ('per_sqft_month', 1.5, 12),  # Rs 1.5/sq.ft/month x 12
    'legal':          ('flat', 10000),
}
SHOP_AREAS = {
    'Shop1': 700, 'Shop2': 525, 'Shop3': 255, 'Shop4': 255,
    'Shop5': 330, 'Shop6': 425, 'Shop7': 695, 'Shop8': 520,
    'Shop9': 400, 'Shop10': 400, 'Shop11': 520, 'Shop12': 700,
}


def shop_price_book(number, sq_feet):
    amount = sq_feet * SHOP_RATE
    loan = round(amount * SHOP_LOAN_PCT)
    extras = {
        'stamp_duty_reg':  round(loan * SHOP_RULES['stamp_duty_reg'][1]),
        'gst':             round(loan * SHOP_RULES['gst'][1]),
        'auda':            round(sq_feet * SHOP_RULES['auda'][1]),
        'maint_adv_6m':    round(sq_feet * 1.5 * 6),
        'maint_dep_12m':   round(sq_feet * 1.5 * 12),
        'legal':           SHOP_RULES['legal'][1],
    }
    total_extra = sum(extras.values())
    return {
        'kind': 'shop', 'unit': number,
        'sq_feet': sq_feet, 'rate': SHOP_RATE,
        'amount': amount, 'loan_amount': loan,
        **extras,
        'total_extra': total_extra,
        'grand_total': amount + total_extra,
    }


# ── Flats ─────────────────────────────────────────────────────────────────────
# Every flat line follows from the flat rate and the token:
#
#   Flat Price       = Flat Area x Flat Rate
#   Terrace Price    = Terrace Area x (Flat Rate / 2)
#   Box Price        = Flat Price + Terrace Price
#   Bank Loan        = Box Price - Token
#   Bank Processing  = Bank Loan x 4.5%
#   Dastavej Value   = (Box Price - Bank Processing) / 1.07
#   Stamp Duty + Reg = Dastavej Value x 6%
#   GST              = Dastavej Value x 1%
#   Total            = Processing + Dastavej + Stamp Duty + GST, which in exact
#                      arithmetic is precisely the Box Price.
#
# Road-facing commands a premium over garden-facing. The rates below reproduce the
# sanctioned box prices for the standard 84 sq.yd flat: 27,00,000 and 28,00,000.
# Keep in step with vistaraweb/src/lib/pratishthaFlat.js and the Vistarafront copy,
# which do the same arithmetic on the booking form so a rate change previews live.
STD_FLAT_AREA = 84           # sq.yd
FLAT_TOKEN = 11000
FLAT_RATE = {
    'garden': 2700000 / STD_FLAT_AREA,   # 32,142.86 per sq.yd
    'road':   2800000 / STD_FLAT_AREA,   # 33,333.33 per sq.yd
}
FLAT_RULES = {
    'terrace_rate_divisor': 2,
    'bank_processing_pct': 0.045,
    'dastavej_divisor': 1.07,
    'stamp_duty_reg_pct': 0.06,
    'gst_pct': 0.01,
}

# Private terrace area (sq.yd) by 1st-floor unit — the only flats that have one.
FIRST_FLOOR_TERRACE = {
    '101': 21, '102': 51, '103': 30, '104': 30, '105': 51, '106': 21, '107': 0,
}
# 1st-floor facing, read off the floor plate.
FIRST_FLOOR_FACING = {
    '101': 'garden', '102': 'road', '103': 'road', '104': 'road',
    '105': 'road', '106': 'garden', '107': 'garden',
}


def flat_price_book(number, facing, flat_area=None, terrace_area=0, rate=None, token=FLAT_TOKEN):
    """Price book for a flat. Rounded at each step, not only at the end — that is what
    reproduces the originally sanctioned figures to the rupee."""
    R = FLAT_RULES
    area = STD_FLAT_AREA if flat_area is None else flat_area
    flat_rate = FLAT_RATE[facing] if rate is None else rate
    terrace_rate = flat_rate / R['terrace_rate_divisor']

    flat_price = _r(area * flat_rate)
    terrace_price = _r((terrace_area or 0) * terrace_rate)
    box = flat_price + terrace_price
    loan = box - token
    bank_processing = _r(loan * R['bank_processing_pct'])
    dastavej = _r((box - bank_processing) / R['dastavej_divisor'])
    return {
        'kind': 'flat', 'unit': number, 'facing': facing,
        'flat_area': area, 'terrace_area': terrace_area or 0,
        'flat_rate': flat_rate, 'terrace_rate': terrace_rate,
        'flat_price': flat_price, 'terrace_price': terrace_price,
        'box_price': box, 'token': token, 'bank_loan': loan,
        'bank_processing': bank_processing,
        'dastavej_value': dastavej,
        'stamp_duty_reg': _r(dastavej * R['stamp_duty_reg_pct']),
        'gst': _r(dastavej * R['gst_pct']),
        # See the note above: the four components sum to the box price in exact
        # arithmetic, so the box price is the total actually quoted and booked.
        'total': box,
    }


def first_floor_price_book(number):
    return flat_price_book(number, FIRST_FLOOR_FACING[number],
                           terrace_area=FIRST_FLOOR_TERRACE[number])


def typical_price_book(number, facing):
    return flat_price_book(number, facing)


# Facing by position in a typical floor's run of 7 — read off the floor plate:
#   x01 Garden | x02 Road | x03 Road | x04 Road | x05 Road | x06 Garden | x07 Garden
TYPICAL_FACING = {1: 'garden', 2: 'road', 3: 'road', 4: 'road', 5: 'road',
                  6: 'garden', 7: 'garden'}


def price_book_for(number):
    """The price book for a Pratishtha unit, or None if the number isn't one."""
    n = str(number).strip()
    if n in SHOP_AREAS:
        return shop_price_book(n, SHOP_AREAS[n])
    if n in FIRST_FLOOR_FACING:
        return first_floor_price_book(n)
    if n.isdigit() and len(n) in (3, 4):
        floor, pos = int(n[:-2]), int(n[-2:])
        if 2 <= floor <= 13 and pos in TYPICAL_FACING:
            return typical_price_book(n, TYPICAL_FACING[pos])
    return None
