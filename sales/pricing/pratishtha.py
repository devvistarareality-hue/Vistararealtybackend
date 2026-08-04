"""Pratishtha price book.

Unlike Kalrav / Ankhol / Industrial, Pratishtha is not priced from a rate formula —
every unit carries a fixed, pre-agreed all-inclusive "box price" and its LOI renders
those figures verbatim. This module is the single source of those numbers.

Three groups:

* **Ground floor shops (Shop1–Shop12)** — the only group that IS derived: area x rate,
  with each extra a stated percentage or per-sq.ft charge (see SHOP_RULES).
* **1st floor flats (101–107)** — the only flats with a private terrace, so each has its
  own terrace area and terrace price. Terrace pricing is negotiated per unit rather
  than a flat rate (21 sq.yd -> 3,37,500 works out at ~16,071/sq.yd while 30 and 51
  sq.yd both price at ~16,667), so these are stated per unit, not computed.
* **Typical flats (201–1307)** — identical on every floor, one price per facing.

Facing matters: road-facing commands a premium over garden-facing.
"""

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


# ── 1st floor flats (the only ones with a terrace) ────────────────────────────
# facing, flat area (sq.yd), terrace area (sq.yd), flat price, terrace price
FIRST_FLOOR = {
    '101': ('garden', 84, 21, 2700000,  337500),
    '102': ('road',   84, 51, 2800000,  850000),
    '103': ('road',   84, 30, 2800000,  500000),
    '104': ('road',   84, 30, 2800000,  500000),
    '105': ('road',   84, 51, 2800000,  850000),
    '106': ('garden', 84, 21, 2700000,  337500),
    '107': ('garden', 84,  0, 2700000,       0),
}
# The remaining lines are stated per unit rather than derived — they come off the
# sanctioned figures, not a formula.
FIRST_FLOOR_LINES = {
    #        token, bank_loan, dastavej, stamp_duty_reg,  gst, bank_processing
    '101': (11000, 3026500, 2711502, 162690, 27115, 136193),
    '102': (11000, 3639000, 3258173, 195490, 32582, 163755),
    '103': (11000, 3289000, 2945790, 176747, 29458, 148005),
    '104': (11000, 3289000, 2945790, 176747, 29458, 148005),
    '105': (11000, 3639000, 3258173, 195490, 32582, 163755),
    '106': (11000, 3026500, 2711502, 162690, 27115, 136193),
    '107': (11000, 2689000, 2410276, 144617, 24103, 121005),
}


def first_floor_price_book(number):
    facing, area, terrace, flat_price, terrace_price = FIRST_FLOOR[number]
    token, loan, dastavej, sdreg, gst, proc = FIRST_FLOOR_LINES[number]
    box = flat_price + terrace_price
    return {
        'kind': 'flat', 'unit': number, 'facing': facing,
        'flat_area': area, 'terrace_area': terrace,
        'flat_price': flat_price, 'terrace_price': terrace_price,
        'box_price': box, 'token': token, 'bank_loan': loan,
        'dastavej_value': dastavej, 'stamp_duty_reg': sdreg, 'gst': gst,
        'bank_processing': proc, 'total': box,
    }


# ── Typical flats 201–1307 — identical on every floor, priced by facing ───────
TYPICAL = {
    'garden': {
        'box_price': 2700000, 'flat_area': 84, 'token': 11000,
        'bank_loan': 2689000, 'dastavej_value': 2410280,
        'stamp_duty_reg': 144617, 'gst': 24103, 'bank_processing': 121000,
    },
    'road': {
        'box_price': 2800000, 'flat_area': 84, 'token': 11000,
        'bank_loan': 2789000, 'dastavej_value': 2500000,
        'stamp_duty_reg': 150000, 'gst': 25000, 'bank_processing': 125000,
    },
}


def typical_price_book(number, facing):
    t = TYPICAL[facing]
    return {
        'kind': 'flat', 'unit': number, 'facing': facing,
        'flat_area': t['flat_area'], 'terrace_area': 0,
        'flat_price': t['box_price'], 'terrace_price': 0,
        'box_price': t['box_price'], 'token': t['token'],
        'bank_loan': t['bank_loan'], 'dastavej_value': t['dastavej_value'],
        'stamp_duty_reg': t['stamp_duty_reg'], 'gst': t['gst'],
        'bank_processing': t['bank_processing'], 'total': t['box_price'],
    }


# Facing by position in a typical floor's run of 7 — read off the floor plate:
#   x01 Garden | x02 Road | x03 Road | x04 Road | x05 Road | x06 Garden | x07 Garden
TYPICAL_FACING = {1: 'garden', 2: 'road', 3: 'road', 4: 'road', 5: 'road',
                  6: 'garden', 7: 'garden'}


def price_book_for(number):
    """The price book for a Pratishtha unit, or None if the number isn't one."""
    n = str(number).strip()
    if n in SHOP_AREAS:
        return shop_price_book(n, SHOP_AREAS[n])
    if n in FIRST_FLOOR:
        return first_floor_price_book(n)
    if n.isdigit() and len(n) in (3, 4):
        floor, pos = int(n[:-2]), int(n[-2:])
        if 2 <= floor <= 13 and pos in TYPICAL_FACING:
            return typical_price_book(n, TYPICAL_FACING[pos])
    return None
