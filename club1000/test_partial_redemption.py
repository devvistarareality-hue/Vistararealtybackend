"""Taking part of the money out early, and leaving the rest invested.

Redemption used to be all or nothing: the whole principal out, every pending
payout deleted, the investment closed. An investor who wants half of it back
had no path that did not close the deal.

The money is split by what it did. The part withdrawn earns the scheme's
premature rate for the DAYS it actually sat invested: the per-month rate is
annualised and spread over 365, the same actual-day-count basis the payout
schedule uses. Whole months alone threw away the part-month at the end — a
redemption on the 19th lost eighteen days the money had really been invested
for. The part left behind is not penalised at all: it keeps the scheme's
normal return and the original maturity date, and only its payout schedule is
resized to the smaller principal.

Because this changes what is owed, it goes through approval with a signed LOI
rather than taking effect on the spot, the same way a revision does — so a
rejected redemption leaves the investment untouched.
"""
from datetime import date
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from club1000.models import Investor, Payout, Scheme
from club1000.services import generate_payout_schedule
from companies.models import Company


class PartialRedemptionTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='PRD', name='Redeem Co', is_active=True)
        cls.admin = User.objects.create_user(
            'admin@prd.com', company=cls.co, user_code='PRD001', password='p',
            name='Club Admin', role='Admin', modules=['Club 1000'])

    def setUp(self):
        self.scheme = Scheme.objects.create(
            company=self.co, name='RISE', tenure_months=12, min_ticket_size=100000,
            interest_payout_options=['quarterly'], premature_redemption_allowed=True,
            premature_redemption_lock_months=0,
            premature_redemption_rate_pct_per_month=Decimal('1.00'))
        self.start = date(2026, 1, 1)
        self.inv = Investor.objects.create(
            company=self.co, scheme=self.scheme, added_by=self.admin,
            name='An Investor', phone='+919000000777',
            amount_invested=Decimal('1000000'), total_return_pct=Decimal('15'),
            investment_date=self.start, maturity_date=self.start + relativedelta(months=12),
            interest_payout='quarterly', status='active', approval_status='approved')
        generate_payout_schedule(self.inv)
        self.api = APIClient()
        self.api.force_authenticate(user=self.admin)
        self.on = date(2026, 7, 1)          # six months in

    LOI = {'name': 'loi.pdf', 'type': 'application/pdf', 'data': 'eA=='}

    def _quote(self, amount, on=None):
        r = self.api.get(f'/api/club1000/investors/{self.inv.pk}/redeem/quote/'
                         f'?amount={amount}&redemption_date={on or self.on}')
        self.assertEqual(r.status_code, 200, r.data)
        return r.data

    def _request(self, amount, on=None, **extra):
        body = {'amount': str(amount), 'redemption_date': str(on or self.on),
                'loi_file': self.LOI}
        body.update(extra)
        return self.api.post(f'/api/club1000/investors/{self.inv.pk}/redeem/request/',
                             body, format='json')

    def _approve(self):
        return self.api.post(f'/api/club1000/investors/{self.inv.pk}/action/',
                             {'action': 'approve'}, format='json')

    def _fresh(self):
        return Investor.objects.get(pk=self.inv.pk)

    # ------------------------------------------------------------ the working

    def test_the_redeemed_part_earns_the_premature_rate_for_the_days_held(self):
        """5,00,000 taken out on 01-07-2026, invested 01-01-2026: 181 days at
        1% a month annualised over 365 — Rs 164.38 a day, 29,753.42 in all."""
        q = self._quote('500000')
        self.assertEqual(q['days_held'], 181)
        self.assertEqual(Decimal(q['daily_interest']), Decimal('164.38'))
        self.assertEqual(Decimal(q['premature_interest']), Decimal('29753.42'))
        self.assertEqual(Decimal(q['payable']), Decimal('529753.42'))

    def test_the_part_month_at_the_end_is_not_thrown_away(self):
        """The reported gap: redeeming on the 19th counted nine whole months and
        dropped the eighteen days after them. Priced by the day, those days are
        worth more than the rounding they used to vanish into."""
        q = self._quote('1000000', on=date(2026, 10, 19))
        self.assertEqual(q['days_held'], 291)
        self.assertEqual(Decimal(q['daily_interest']), Decimal('328.77'))
        self.assertEqual(Decimal(q['premature_interest']), Decimal('95671.23'))
        # Nine whole months at 1% would have been 90,000 — the days are the
        # difference, and they are no longer free.
        self.assertGreater(Decimal(q['premature_interest']), Decimal('90000'))

    def test_a_day_more_invested_is_a_day_more_interest(self):
        a = self._quote('500000', on=date(2026, 7, 1))
        b = self._quote('500000', on=date(2026, 7, 2))
        self.assertEqual(b['days_held'] - a['days_held'], 1)
        # The total is rounded once at the end rather than per day, so a day's
        # step can land a paisa either side of the per-day figure shown. That
        # is the right way round: rounding each day first would drift over a
        # long holding.
        step = Decimal(b['premature_interest']) - Decimal(a['premature_interest'])
        self.assertLessEqual(abs(step - Decimal(a['daily_interest'])), Decimal('0.01'), step)

    def test_the_interest_is_on_the_redeemed_part_only(self):
        # Not on the whole investment — that was the point of the instruction.
        # 2,00,000 for 181 days at Rs 65.75 a day.
        q = self._quote('200000')
        self.assertEqual(Decimal(q['daily_interest']), Decimal('65.75'))
        self.assertEqual(Decimal(q['premature_interest']), Decimal('11901.37'))

    def test_the_rest_is_reported_as_staying_invested(self):
        q = self._quote('500000')
        self.assertEqual(Decimal(q['remaining_principal']), Decimal('500000.00'))
        self.assertFalse(q['is_full'])
        self.assertEqual(Decimal(q['normal_return_pct']), Decimal('15.00'))

    def test_redeeming_everything_is_marked_as_full(self):
        q = self._quote('1000000')
        self.assertTrue(q['is_full'])
        self.assertEqual(Decimal(q['remaining_principal']), Decimal('0.00'))

    def test_interest_already_paid_on_that_share_is_shown_but_not_deducted(self):
        """Reported for whoever signs, deliberately not subtracted — the
        payable is the gross figure specified."""
        q = self._quote('500000')
        self.assertIn('interest_paid_share', q)
        self.assertEqual(Decimal(q['payable']),
                         Decimal(q['redeem_amount']) + Decimal(q['premature_interest']))

    # ------------------------------------------------------- it needs approval

    def test_a_request_does_not_change_the_investment_yet(self):
        self.assertEqual(self._request('500000').status_code, 201)
        fresh = self._fresh()
        self.assertEqual(fresh.approval_status, 'pending')
        self.assertEqual(fresh.pending_revision_type, 'redeem')
        self.assertEqual(fresh.amount_invested, Decimal('1000000'))
        self.assertEqual(fresh.status, 'active')

    def test_a_request_needs_a_signed_loi(self):
        r = self.api.post(f'/api/club1000/investors/{self.inv.pk}/redeem/request/',
                          {'amount': '500000', 'redemption_date': str(self.on)}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('loi_file', str(r.data))

    def test_a_rejected_redemption_leaves_the_investment_untouched(self):
        self._request('500000')
        before = Payout.objects.filter(investor=self.inv, status='pending').count()
        r = self.api.post(f'/api/club1000/investors/{self.inv.pk}/action/',
                          {'action': 'reject', 'reason': 'not agreed'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        fresh = self._fresh()
        self.assertEqual(fresh.amount_invested, Decimal('1000000'))
        self.assertEqual(fresh.redeemed_principal, Decimal('0'))
        self.assertEqual(Payout.objects.filter(investor=fresh, status='pending').count(), before)
        self.assertFalse(Payout.objects.filter(investor=fresh,
                                               payout_type='premature_redemption').exists())

    # ------------------------------------------------------- once approved

    def test_approval_pays_out_and_shrinks_the_principal(self):
        self._request('500000', notes='partial withdrawal')
        self.assertEqual(self._approve().status_code, 200)
        fresh = self._fresh()
        self.assertEqual(fresh.amount_invested, Decimal('500000.00'))
        self.assertEqual(fresh.redeemed_principal, Decimal('500000.00'))
        payout = Payout.objects.get(investor=fresh, payout_type='premature_redemption')
        self.assertEqual(payout.amount_due, Decimal('529753.42'))
        self.assertEqual(payout.due_date, self.on)

    def test_the_redemption_payout_survives_the_schedule_rebuild(self):
        """It is created alongside a wholesale delete of pending payouts, and
        was briefly swept away by it — the rest of this file would still have
        passed."""
        self._request('500000')
        self._approve()
        self.assertEqual(
            Payout.objects.filter(investor=self.inv, payout_type='premature_redemption').count(), 1)

    def test_the_part_left_behind_keeps_its_term_and_rate(self):
        self._request('500000')
        self._approve()
        fresh = self._fresh()
        self.assertEqual(fresh.status, 'active', 'a partial redemption must not close the deal')
        self.assertEqual(fresh.maturity_date, self.start + relativedelta(months=12))
        self.assertEqual(fresh.total_return_pct, Decimal('15.00'))

    def test_the_remaining_schedule_is_resized_to_the_smaller_principal(self):
        self._request('500000')
        self._approve()
        interest = Payout.objects.filter(investor=self.inv, payout_type='interest')
        total = sum((p.amount_due for p in interest), Decimal('0'))
        # 15% of what is left, within the per-instalment rounding.
        self.assertLessEqual(abs(total - Decimal('75000')), Decimal('0.05'), total)

    def test_redeeming_everything_closes_the_investment(self):
        self._request('1000000')
        self._approve()
        fresh = self._fresh()
        self.assertEqual(fresh.status, 'premature_redeemed')
        self.assertEqual(fresh.amount_invested, Decimal('0.00'))
        self.assertFalse(Payout.objects.filter(investor=fresh, status='pending',
                                               payout_type='interest').exists())

    def test_two_partial_redemptions_accumulate(self):
        self._request('300000')
        self._approve()
        self.assertEqual(self._fresh().redemption_count, 1)
        self._request('200000', on=date(2026, 8, 1))
        self._approve()
        fresh = self._fresh()
        self.assertEqual(fresh.redeemed_principal, Decimal('500000.00'))
        self.assertEqual(fresh.amount_invested, Decimal('500000.00'))
        # Numbered among redemptions, which is what the approvals card shows —
        # a pending third would read "Redemption 3", not "R7".
        self.assertEqual(fresh.redemption_count, 2)

    def test_a_redemption_does_not_rewrite_the_last_renewal(self):
        """Reported from the LOI: after a redemption, "Renewal 3" grew a
        "Principal paid back to him" line and claimed it had renewed into the
        smaller figure. past_terms read the running principal as it stands now
        rather than as it started, so money redeemed later was attributed to
        the renewal before it."""
        from club1000.services import past_terms
        self.inv.past_terms = [{
            'start': '2025-01-01', 'maturity': '2026-01-01',
            'principal': '900000', 'return_pct': '15', 'interest_payout': 'maturity',
        }]
        self.inv.save(update_fields=['past_terms'])
        before = past_terms(self._fresh())[-1]
        self.assertEqual(before['next'], Decimal('1000000'))
        self.assertEqual(before['returned'], Decimal('0.00'))

        self._request('300000')
        self._approve()

        after = past_terms(self._fresh())[-1]
        self.assertEqual(after['next'], Decimal('1000000'),
                         'the renewal now claims it renewed into the post-redemption figure')
        self.assertEqual(after['returned'], Decimal('0.00'),
                         'a phantom "principal paid back" line appeared on the renewal')

    def test_an_earlier_redemption_is_not_wiped_by_the_next_one(self):
        """Reported from the ledger: four redemptions approved, one payout row
        left. Rebuilding the schedule deleted every pending payout, and an
        unpaid redemption is pending — so each redemption erased the one before
        it, taking the amount owed and its signed LOI with it."""
        self._request('200000')
        self._approve()
        self._request('300000', on=date(2026, 8, 1))
        self._approve()
        reds = Payout.objects.filter(investor=self.inv, payout_type='premature_redemption')
        self.assertEqual(reds.count(), 2, 'the first redemption was deleted by the second')
        # 2,00,000 held 181 days and 3,00,000 held 212 days, each at the
        # per-day rate — both still owed, neither overwritten.
        self.assertEqual(sorted(r.amount_due for r in reds),
                         [Decimal('211901.37'), Decimal('320909.59')])
        # And each keeps its own paperwork and working.
        self.assertTrue(all(r.redemption_detail for r in reds))

    def test_the_redemption_keeps_its_own_working(self):
        """Without this, an earlier redemption could only ever be shown as one
        total — amount_due cannot be taken apart again into principal, days and
        interest."""
        self._request('300000')
        self._approve()
        d = Payout.objects.get(investor=self.inv, payout_type='premature_redemption').redemption_detail
        self.assertEqual(d['redeem_amount'], '300000.00')
        self.assertEqual(d['days_held'], 181)
        self.assertEqual(d['daily_interest'], '98.63')
        self.assertEqual(d['remaining_principal'], '700000.00')

    def test_the_ledger_hands_that_working_back(self):
        self._request('300000')
        self._approve()
        r = self.api.get(f'/api/club1000/investors/{self.inv.pk}/ledger/')
        self.assertEqual(r.status_code, 200, r.data)
        red = [e for e in r.data['entries'] if e['type'] == 'premature_redemption']
        self.assertEqual(len(red), 1)
        self.assertEqual(red[0]['redemption']['days_held'], 181)

    def test_the_redemption_keeps_its_own_signed_loi(self):
        """The investor's loi_document is replaced by whatever is signed next,
        so without a copy on the payout the ledger could not show this
        redemption's paper again after the next revision."""
        self._request('300000')
        self._approve()
        payout = Payout.objects.get(investor=self.inv, payout_type='premature_redemption')
        self.assertTrue(payout.loi_document, 'the signed redemption LOI was not kept')

    def test_that_loi_is_served_for_that_payout(self):
        self._request('300000')
        self._approve()
        payout = Payout.objects.get(investor=self.inv, payout_type='premature_redemption')
        r = self.api.get(f'/api/club1000/investors/{self.inv.pk}/loi-url/?payout={payout.pk}')
        # Either a signed URL, or storage being unavailable in tests — what
        # matters is that it is not "this payout has no LOI".
        self.assertNotEqual(r.status_code, 404, r.data)

    def test_a_payout_with_no_loi_of_its_own_says_so(self):
        interest = Payout.objects.filter(investor=self.inv, payout_type='interest').first()
        r = self.api.get(f'/api/club1000/investors/{self.inv.pk}/loi-url/?payout={interest.pk}')
        self.assertEqual(r.status_code, 404)

    def test_a_rejected_redemption_does_not_count(self):
        self._request('300000')
        self.api.post(f'/api/club1000/investors/{self.inv.pk}/action/',
                      {'action': 'reject', 'reason': 'no'}, format='json')
        self.assertEqual(self._fresh().redemption_count, 0)

    # --------------------------------------------------------------- refusals

    def test_more_than_is_invested_is_refused(self):
        self.assertFalse(self._quote('1500000')['can_redeem'])
        self.assertEqual(self._request('1500000').status_code, 400)

    def test_leaving_less_than_the_scheme_minimum_is_refused(self):
        """Leaving 50,000 behind under a 1,00,000 floor is not a smaller
        investment, it is one the scheme would never have taken."""
        q = self._quote('950000')
        self.assertFalse(q['can_redeem'])
        self.assertIn('minimum', q['blocked_reason'])

    def test_a_locked_in_scheme_is_refused_until_the_lock_passes(self):
        Scheme.objects.filter(pk=self.scheme.pk).update(premature_redemption_lock_months=9)
        # Six months in, still locked; nine months in, free. The later date has
        # to be in the past — a redemption cannot be dated into the future.
        self.assertFalse(self._quote('500000')['can_redeem'])
        self.assertTrue(self._quote('500000', on=date(2026, 10, 15))['can_redeem'])

    def test_a_scheme_that_forbids_it_is_refused(self):
        Scheme.objects.filter(pk=self.scheme.pk).update(premature_redemption_allowed=False)
        self.assertFalse(self._quote('500000')['can_redeem'])
        self.assertEqual(self._request('500000').status_code, 400)

    def test_a_date_before_the_investment_is_refused(self):
        self.assertEqual(self._request('500000', on=date(2025, 12, 1)).status_code, 400)

    def test_another_company_cannot_redeem_it(self):
        other = Company.objects.create(code='PRE', name='Elsewhere', is_active=True)
        outsider = User.objects.create_user(
            'a@pre.com', company=other, user_code='PRE001', password='p',
            name='Outsider', role='Admin', modules=['Club 1000'])
        api = APIClient()
        api.force_authenticate(user=outsider)
        r = api.post(f'/api/club1000/investors/{self.inv.pk}/redeem/request/',
                     {'amount': '500000', 'redemption_date': str(self.on), 'loi_file': self.LOI},
                     format='json')
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self._fresh().amount_invested, Decimal('1000000'))
