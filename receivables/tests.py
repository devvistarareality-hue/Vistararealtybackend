"""AR engine tests.

The golden case is the company's own workbook: AR - KALRAV 2, Plot 10 (Jigar
Makwana), ledger date 21/09/26. Every figure asserted below is copied from that
sheet, not derived from the engine.
"""
from datetime import date
from decimal import Decimal as D

from django.test import SimpleTestCase

from .engine import PlanLine, Receipt, compute, rupees


def plot10():
    plan = [
        PlanLine('1', '1', 'Installment 1', date(2025, 8, 1),   D('100000')),
        PlanLine('2', '2', 'Installment 2', date(2025, 9, 12),  D('4050000')),
        PlanLine('3', '3', 'Installment 3', date(2025, 9, 30),  D('2500000')),
        PlanLine('4', '4', 'Installment 4', date(2025, 10, 12), D('4875000')),
        PlanLine('5', '5', 'Installment 5', date(2025, 11, 12), D('4875000')),
        # Legal & Other Charges (Legal 40,000 + Maintenance 2,76,923), dated 12/12/25.
        PlanLine('legal', '6', 'Legal & Other Charges', date(2025, 12, 12), D('316923'), 'legal'),
    ]
    receipts = [
        Receipt(1,  date(2025, 8, 1),   D('100000'),  'nbfc'),
        Receipt(2,  date(2025, 9, 12),  D('4050000'), 'nbfc'),
        Receipt(3,  date(2025, 9, 28),  D('3000000'), 'nbfc'),
        Receipt(4,  date(2025, 11, 25), D('2000000'), 'nbfc'),
        Receipt(5,  date(2025, 12, 20), D('1500000'), 'nbfc', 'DS COLLECTED'),
        Receipt(6,  date(2025, 12, 26), D('2000000'), 'bank', 'TD-CHQ'),
        Receipt(7,  date(2026, 1, 3),   D('500000'),  'bank', 'CJ-CHQ'),
        Receipt(8,  date(2026, 2, 1),   D('1000000'), 'nbfc', 'DS COLLECTED'),
        Receipt(9,  date(2026, 3, 31),  D('1000000'), 'nbfc', 'SM COLLECTED'),
        Receipt(10, date(2026, 7, 16),  D('1567000'), 'nbfc', 'COLLECTED BY PARTH'),
    ]
    return plan, receipts


class Plot10GoldenTest(SimpleTestCase):
    """Must match the Kalrav 2 workbook to the rupee."""

    def setUp(self):
        plan, receipts = plot10()
        self.r = compute(plan, receipts, date(2026, 9, 21))

    def test_ledger_summary(self):
        self.assertEqual(rupees(self.r.collectable), 16716923)        # Total Deal (b), stamp/reg 0
        self.assertEqual(rupees(self.r.received), 16717000)           # Total Payment Received (e)
        self.assertEqual(rupees(self.r.outstanding), -77)             # O/s (b-c-d-e)
        self.assertEqual(rupees(self.r.net_interest), 615052)         # Interest Due (f) / Net Interest
        self.assertEqual(rupees(self.r.os_with_interest), 614975)     # O/s with interest

    def test_interest_summary_rows(self):
        """The workbook's Interest Summary table, row by row."""
        expected = [  # (inst, due, paid, amount, days, interest) — from the sheet
            ('1', date(2025, 8, 1),   date(2025, 8, 1),   100000,  0,    0),
            ('2', date(2025, 9, 12),  date(2025, 9, 12),  4050000, 0,    0),
            ('3', date(2025, 9, 30),  date(2025, 9, 28),  2500000, -2,   -1667),
            ('4', date(2025, 10, 12), date(2025, 9, 28),  500000,  -14,  -2333),
            ('4', date(2025, 10, 12), date(2025, 11, 25), 2000000, 44,   58667),
            ('4', date(2025, 10, 12), date(2025, 12, 20), 1500000, 69,   69000),
            ('4', date(2025, 10, 12), date(2025, 12, 26), 875000,  75,   43750),
            ('5', date(2025, 11, 12), date(2025, 12, 26), 1125000, 44,   33000),
            ('5', date(2025, 11, 12), date(2026, 1, 3),   500000,  52,   17333),
            ('5', date(2025, 11, 12), date(2026, 2, 1),   1000000, 81,   54000),
            ('5', date(2025, 11, 12), date(2026, 3, 31),  1000000, 139,  92667),
            ('5', date(2025, 11, 12), date(2026, 7, 16),  1250000, 246,  205000),
            ('6', date(2025, 12, 12), date(2026, 7, 16),  316923,  216,  45637),
        ]
        got = [(r.inst_no, r.due, r.paid_on, rupees(r.amount), r.days, rupees(r.interest)) for r in self.r.rows]
        self.assertEqual(got, expected)
        self.assertEqual(sum(e[5] for e in expected), 615054)   # the sheet's TOTAL row

    def test_overpaid_credit(self):
        # ₹77 overpaid on 16/07/26 earns the 1% credit for 67 days: the sheet's -2.
        self.assertEqual(rupees(self.r.overpaid), 77)
        self.assertEqual(rupees(self.r.overpaid_credit), -2)

    def test_everything_completed(self):
        self.assertEqual({s.status for s in self.r.lines}, {'completed'})
        self.assertEqual(rupees(self.r.overdue), 0)
        self.assertEqual(rupees(self.r.not_due), 0)


class RuleTests(SimpleTestCase):
    AS_OF = date(2026, 1, 31)

    def one(self, due, paid, amount='100000'):
        plan = [PlanLine('1', '1', 'I1', due, D(amount))]
        rec = [Receipt(1, paid, D(amount))] if paid else []
        return compute(plan, rec, self.AS_OF)

    def test_grace_boundary_is_all_or_nothing(self):
        self.assertEqual(rupees(self.one(date(2026, 1, 1), date(2026, 1, 11)).net_interest), 0)   # 10 days: free
        # 11 days late: all 11 days are charged, not just the 1 beyond grace.
        self.assertEqual(rupees(self.one(date(2026, 1, 1), date(2026, 1, 12)).net_interest),
                         rupees(D('100000') * D('0.02') * 11 / 30))

    def test_early_payment_is_a_credit(self):
        self.assertEqual(rupees(self.one(date(2026, 1, 31), date(2026, 1, 1)).net_interest),
                         rupees(-D('100000') * D('0.01') * 30 / 30))

    def test_unpaid_past_due_accrues_to_as_of(self):
        r = self.one(date(2026, 1, 1), None)
        self.assertEqual(rupees(r.overdue), 100000)
        self.assertEqual(rupees(r.net_interest), rupees(D('100000') * D('0.02') * 30 / 30))
        self.assertEqual(rupees(r.ageing['16-30']), 100000)

    def test_undated_line_never_accrues(self):
        plan = [PlanLine('legal', 'L', 'Legal', None, D('50000'), 'legal')]
        r = compute(plan, [], self.AS_OF)
        self.assertEqual(rupees(r.net_interest), 0)
        self.assertEqual(rupees(r.not_due), 50000)
        self.assertEqual(r.month_forecast[-1], ('No date', D('50000')))

    def test_partial_status_and_spill(self):
        plan = [PlanLine('1', '1', 'I1', date(2026, 1, 1), D('100')),
                PlanLine('2', '2', 'I2', date(2026, 2, 1), D('100'))]
        r = compute(plan, [Receipt(1, date(2026, 1, 1), D('150'))], self.AS_OF)
        self.assertEqual([s.status for s in r.lines], ['completed', 'partial'])
        self.assertEqual(rupees(r.outstanding), 50)


class RealLoiShapeTests(SimpleTestCase):
    """Plans exactly as production LOIs store them (Kalrav 2 plot 10, read from the
    live database): sale-deed + NSD installments, and the extra charges as an
    installment numbered "Extra" that already includes stamp duty and registration."""

    class B:  # a stand-in with just the fields build_plan reads
        def __init__(self, **kw):
            self.__dict__.update(kw)

    def booking(self):
        return self.B(
            installments=[
                {'no': 1, 'date': '2025-11-12', 'amt': 2889000},
                {'no': 1, 'date': '2025-08-01', 'amt': 100000, 'isNsd': True},
                {'no': 2, 'date': '2025-09-25', 'amt': 4050000, 'isNsd': True},
                {'no': 3, 'date': '2025-09-30', 'amt': 2500000, 'isNsd': True},
                {'no': 4, 'date': '2025-10-12', 'amt': 4875000, 'isNsd': True},
                {'no': 5, 'date': '2025-11-12', 'amt': 1985999, 'isNsd': True},
                {'no': 'Extra', 'date': '2027-03-31', 'amt': 706555},
            ],
            extra_work_inst=[], total_extra=D('706555'), stamp_duty=D('53361'), reg_fees=D('12390'),
            final_amount=D('17106554'),
        )

    def test_extra_installment_is_the_legal_line_not_added_twice(self):
        from .services import build_plan, expected_collectable
        b = self.booking()
        plan = build_plan(b)
        self.assertEqual([l.no for l in plan], ['1', 'N1', 'N2', 'N3', 'N4', 'N5', 'L'])
        legal = plan[-1]
        self.assertEqual(legal.amount, D('640804'))            # 7,06,555 − 53,361 − 12,390
        self.assertEqual(legal.due, date(2027, 3, 31))          # the LOI's own date
        self.assertEqual(sum(l.amount for l in plan), expected_collectable(b))  # no mismatch

    def test_a_date_set_by_accounts_overrides_the_loi(self):
        from .services import build_plan
        self.assertEqual(build_plan(self.booking(), date(2026, 6, 30))[-1].due, date(2026, 6, 30))


class LegalNoEarlyCreditTests(SimpleTestCase):
    """Legal & Other Charges paid early earn no credit; paid late they are charged."""

    def test_early_legal_payment_earns_nothing(self):
        plan = [PlanLine('legal', 'L', 'Legal', date(2027, 3, 31), D('640804'), 'legal')]
        r = compute(plan, [Receipt(1, date(2026, 7, 16), D('317000'))], date(2026, 9, 21))
        self.assertEqual(rupees(r.net_interest), 0)          # was −27,262 (258 days early)

    def test_late_legal_payment_is_still_charged(self):
        plan = [PlanLine('legal', 'L', 'Legal', date(2026, 1, 1), D('100000'), 'legal')]
        r = compute(plan, [Receipt(1, date(2026, 1, 31), D('100000'))], date(2026, 9, 21))
        self.assertEqual(rupees(r.net_interest), rupees(D('100000') * D('0.02') * 30 / 30))

    def test_installments_still_get_early_credit(self):
        plan = [PlanLine('1', '1', 'I1', date(2026, 1, 31), D('100000'))]
        r = compute(plan, [Receipt(1, date(2026, 1, 1), D('100000'))], date(2026, 9, 21))
        self.assertEqual(rupees(r.net_interest), -1000)
