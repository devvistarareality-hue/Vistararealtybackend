"""Parity with the company's Excel macro.

`vba_interest` below is a line-by-line port of CalculatePlotInterest_Detailed
from the AR workbook (Payment Plan / Payment Tracker VBA) — including its
insertion sorts and its FIFO loop — kept deliberately literal, NOT reusing any
of engine.py. The tests run both on the workbook's own plots, on the live ERP
booking, and on thousands of random plans, and require the same interest.

Three documented differences are outside the macro and excluded here:
  * overpaid money: the workbook reads a hand-kept "Net Overpaid interst" column;
    engine.py computes 1%/month to date instead;
  * an installment with no due date: the macro has no such case (an empty Excel
    date is 30/12/1899), engine.py charges nothing until a date is set;
  * Legal & Other Charges paid early earn no credit in engine.py (company
    decision, 21/09/26); the macro gives 1%/month on them like any installment.
"""
import random
from datetime import date, timedelta
from decimal import Decimal as D

from django.test import SimpleTestCase

from .engine import PlanLine, Receipt, compute, rupees

RATE = 0.02      # Const RATE As Double = 0.02
ADV_RATE = 0.01  # Const ADV_RATE As Double = 0.01


def vba_interest(plan, payments, today):
    """plan: [(inst, due_date, amount)], payments: [(paid_date, amount)] — the
    macro's pInst/pDue/pAmt and aDate/aRemain arrays."""
    p_inst = [p[0] for p in plan]
    p_due = [p[1] for p in plan]
    p_amt = [float(p[2]) for p in plan]
    a_date = [a[0] for a in payments if a[1] != 0]
    a_remain = [float(a[1]) for a in payments if a[1] != 0]
    if not p_inst:
        return 0.0

    # SortByDate — insertion sort; `If d(j) < kd Then Exit Do`
    for i in range(1, len(p_due)):
        kd, ka, ki = p_due[i], p_amt[i], p_inst[i]
        j = i - 1
        while j >= 0:
            if p_due[j] < kd:
                break
            p_due[j + 1], p_amt[j + 1], p_inst[j + 1] = p_due[j], p_amt[j], p_inst[j]
            j -= 1
        p_due[j + 1], p_amt[j + 1], p_inst[j + 1] = kd, ka, ki

    # SortPaymentsByDate — insertion sort; `dt(j) > kd`
    for i in range(1, len(a_date)):
        kd, ka = a_date[i], a_remain[i]
        j = i - 1
        while j >= 0 and a_date[j] > kd:
            a_date[j + 1], a_remain[j + 1] = a_date[j], a_remain[j]
            j -= 1
        a_date[j + 1], a_remain[j + 1] = kd, ka

    total = 0.0
    ai = 0
    for pi in range(len(p_inst)):
        due_remain = p_amt[pi]
        while due_remain > 0 and ai < len(a_date):
            if a_remain[ai] <= 0:
                ai += 1
            else:
                alloc = due_remain if a_remain[ai] >= due_remain else a_remain[ai]
                dly = (a_date[ai] - p_due[pi]).days          # DateDiff("d", pDue, aDate)
                if dly < 0:
                    intr = alloc * ADV_RATE * (dly / 30)
                elif dly <= 10:
                    intr = 0
                else:
                    intr = alloc * RATE * (dly / 30)
                total += intr
                a_remain[ai] -= alloc
                due_remain -= alloc
                if a_remain[ai] <= 1e-07:
                    ai += 1
        if due_remain > 0 and p_due[pi] <= today:            # pending portion
            dly = (today - p_due[pi]).days
            intr = 0 if dly <= 10 else due_remain * RATE * (dly / 30)
            total += intr
    return total


def engine_installment_interest(plan, payments, today):
    """engine.py's interest on installments only (overpaid credit excluded, as the
    macro reads that from a sheet column instead of computing it)."""
    lines = [PlanLine(str(i), str(inst), '', due, D(str(amt))) for i, (inst, due, amt) in enumerate(plan)]
    recs = [Receipt(i, d, D(str(a))) for i, (d, a) in enumerate(payments)]
    r = compute(lines, recs, today)
    return float(sum((row.interest for row in r.rows), D('0')))


EXCEL_PLOT10 = [(1, date(2025, 8, 1), 100000), (2, date(2025, 9, 12), 4050000), (3, date(2025, 9, 30), 2500000),
                (4, date(2025, 10, 12), 4875000), (5, date(2025, 11, 12), 4875000), (6, date(2025, 12, 12), 316923)]
# The live ERP booking for Kalrav 2 plot 10 (booking 150), which is the correct data.
ERP_PLOT10 = [('N1', date(2025, 8, 1), 100000), ('N2', date(2025, 9, 25), 4050000), ('N3', date(2025, 9, 30), 2500000),
              ('N4', date(2025, 10, 12), 4875000), ('1', date(2025, 11, 12), 2889000), ('N5', date(2025, 11, 12), 1985999),
              ('L', date(2027, 3, 31), 640804)]
PLOT10_PAID = [(date(2025, 8, 1), 100000), (date(2025, 9, 12), 4050000), (date(2025, 9, 28), 3000000),
               (date(2025, 11, 25), 2000000), (date(2025, 12, 20), 1500000), (date(2025, 12, 26), 2000000),
               (date(2026, 1, 3), 500000), (date(2026, 2, 1), 1000000), (date(2026, 3, 31), 1000000),
               (date(2026, 7, 16), 1567000)]
LEDGER_DATE = date(2026, 9, 21)


class VbaParityTests(SimpleTestCase):
    def test_excel_plot10_both_give_the_workbook_figure(self):
        vba = vba_interest(EXCEL_PLOT10, PLOT10_PAID, LEDGER_DATE)
        eng = engine_installment_interest(EXCEL_PLOT10, PLOT10_PAID, LEDGER_DATE)
        self.assertAlmostEqual(vba, eng, places=6)
        # + the sheet's hand-kept "Net Overpaid interst" (-2) = Plot Master's 6,15,052
        self.assertEqual(round(vba - 2), 615052)

    def test_erp_plot10_both_give_the_same_figure(self):
        vba = vba_interest(ERP_PLOT10, PLOT10_PAID, LEDGER_DATE)
        eng = engine_installment_interest(ERP_PLOT10, PLOT10_PAID, LEDGER_DATE)
        self.assertAlmostEqual(vba, eng, places=6)
        self.assertEqual(round(vba), 524604)

    def test_alindra_plot57(self):
        plan = [(1, date(2026, 7, 30), 77500), (2, date(2026, 9, 30), 232500),
                (3, date(2026, 10, 31), 310000), (4, date(2026, 11, 30), 1000000)]
        paid = [(date(2026, 4, 1), 11000)]
        vba = vba_interest(plan, paid, LEDGER_DATE)
        self.assertAlmostEqual(vba, engine_installment_interest(plan, paid, LEDGER_DATE), places=6)
        self.assertEqual(round(vba), 1910)   # the Alindra Plot Master's Net Interest

    def test_random_plans_agree(self):
        """5,000 random plots: installments, part payments, lumps that span several
        installments, early money, overpayment, duplicate due dates, same-day
        payments — the macro and the engine must agree on every one."""
        rng = random.Random(20260921)
        start = date(2024, 1, 1)
        for case in range(5000):
            n_inst = rng.randint(1, 9)
            plan = []
            for k in range(n_inst):
                due = start + timedelta(days=rng.randint(0, 900))
                if plan and rng.random() < 0.15:          # a shared due date, like 1 and N5
                    due = plan[-1][1]
                plan.append((k + 1, due, rng.choice([rng.randint(1, 60) * 25000, rng.randint(10000, 5000000)])))
            total = sum(p[2] for p in plan)
            payments, paid = [], 0
            for _ in range(rng.randint(0, 12)):
                amt = rng.choice([rng.randint(1000, 3000000), rng.choice(plan)[2]])
                if rng.random() < 0.1:                    # sometimes overpay the whole deal
                    amt = total - paid + rng.randint(1, 50000)
                payments.append((start + timedelta(days=rng.randint(-60, 1000)), amt))
                paid += amt
            today = start + timedelta(days=rng.randint(0, 1200))
            vba = vba_interest(plan, payments, today)
            eng = engine_installment_interest(plan, payments, today)
            self.assertAlmostEqual(vba, eng, delta=0.01, msg=f'case {case}: plan={plan} paid={payments} today={today}')

    def test_erp_plot10_with_the_no_legal_credit_rule(self):
        """The same plot with the legal line marked as legal: the 3,17,000 paid
        258 days before 31/03/2027 no longer earns 27,262 of credit."""
        lines = [PlanLine(str(i), str(n), '', due, D(str(amt)), 'legal' if n == 'L' else 'inst')
                 for i, (n, due, amt) in enumerate(ERP_PLOT10)]
        recs = [Receipt(i, d, D(str(a))) for i, (d, a) in enumerate(PLOT10_PAID)]
        r = compute(lines, recs, LEDGER_DATE)
        self.assertEqual(rupees(r.net_interest), 551867)   # 5,24,604 + 27,262.3 of credit, rounded
