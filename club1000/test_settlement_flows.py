"""Redeem, maturity Payout and Renew — the figures shown and what each one saves."""
from datetime import date, timedelta
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from club1000.models import Investor, Payout, Scheme
from companies.models import Company


class SettlementFlowTests(TestCase):
    def setUp(self):
        self.co = Company.objects.create(code='STF', name='Settle Co', is_active=True)
        self.admin = User.objects.create_user('a@stf.com', company=self.co, user_code='STF1', password='p',
                                              name='Admin', role='Admin', modules=['Club 1000'])
        self.scheme = Scheme.objects.create(company=self.co, name='RISE', tenure_months=24, min_ticket_size=1,
                                            premature_redemption_allowed=True, premature_redemption_lock_months=6,
                                            premature_redemption_rate_pct_per_month=Decimal('1.00'))
        self.api = APIClient(); self.api.force_authenticate(self.admin)

    def _inv(self, invested, months_tenure=24):
        inv = Investor.objects.create(company=self.co, scheme=self.scheme, added_by=self.admin, name='I', phone='9800000002',
                                      amount_invested=Decimal('100000'), investment_date=invested,
                                      maturity_date=invested + relativedelta(months=months_tenure),
                                      total_return_pct=Decimal('30'), interest_payout='quarterly',
                                      status='active', approval_status='approved')
        return inv

    def test_preview_subtracts_interest_already_paid(self):
        inv = self._inv(date.today() - relativedelta(months=10))
        Payout.objects.create(investor=inv, payout_type='interest', due_date=date.today() - timedelta(days=30),
                              amount_due=Decimal('3000'), paid_amount=Decimal('3000'), status='paid')
        Payout.objects.create(investor=inv, payout_type='maturity', due_date=inv.maturity_date, amount_due=Decimal('100000'))
        d = self.api.get(f'/api/club1000/investors/{inv.pk}/settlement/').data
        self.assertEqual(d['months_elapsed'], 10)
        self.assertEqual(Decimal(d['redemption_interest']), Decimal('10000.00'))
        self.assertEqual(Decimal(d['payable']), Decimal('107000.00'))   # 1,00,000 + 10,000 − 3,000
        self.assertTrue(d['can_redeem'])

    def test_redeem_inside_lock_in_refused(self):
        inv = self._inv(date.today() - relativedelta(months=2))
        r = self.api.post(f'/api/club1000/investors/{inv.pk}/redeem/', {}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('locked in', r.data['detail'])

    def test_redeem_replaces_pending_and_can_pay_now(self):
        inv = self._inv(date.today() - relativedelta(months=8))
        Payout.objects.create(investor=inv, payout_type='interest', due_date=date.today() + timedelta(days=20), amount_due=Decimal('3000'))
        Payout.objects.create(investor=inv, payout_type='maturity', due_date=inv.maturity_date, amount_due=Decimal('100000'))
        r = self.api.post(f'/api/club1000/investors/{inv.pk}/redeem/', {
            'amount': '105000', 'notes': 'Needs money',
            'pay_now': {'paid_date': str(date.today()), 'amount': '105000', 'notes': 'UTR 5'}}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        inv.refresh_from_db()
        self.assertEqual(inv.status, 'premature_redeemed')
        self.assertFalse(inv.payouts.filter(status='pending').exists())
        p = inv.payouts.get(payout_type='premature_redemption')
        self.assertEqual((p.status, p.paid_amount, p.notes), ('paid', Decimal('105000'), 'UTR 5'))

    def test_maturity_payout_pays_the_sent_rows_and_closes(self):
        inv = self._inv(date.today() - relativedelta(months=25))
        i = Payout.objects.create(investor=inv, payout_type='interest', due_date=date.today() - timedelta(days=40), amount_due=Decimal('7500'))
        m = Payout.objects.create(investor=inv, payout_type='maturity', due_date=inv.maturity_date, amount_due=Decimal('100000'))
        r = self.api.post(f'/api/club1000/investors/{inv.pk}/mature-payout/', {
            'payments': [{'id': m.pk, 'amount': '100000', 'paid_date': str(date.today()), 'notes': 'cheque 1'}]}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        inv.refresh_from_db(); i.refresh_from_db(); m.refresh_from_db()
        self.assertEqual(inv.status, 'redeemed')
        self.assertEqual((m.status, m.notes), ('paid', 'cheque 1'))
        self.assertEqual(i.status, 'pending')   # not sent — still owed, still on Payouts

    def test_renewal_keeps_old_unpaid_interest_and_pays_out_a_reduction(self):
        inv = self._inv(date.today() - relativedelta(months=25))
        old_int = Payout.objects.create(investor=inv, payout_type='interest', due_date=date.today() - timedelta(days=40), amount_due=Decimal('7500'))
        Payout.objects.create(investor=inv, payout_type='maturity', due_date=inv.maturity_date, amount_due=Decimal('100000'))
        Investor.objects.filter(pk=inv.pk).update(
            approval_status='pending', pending_revision_type='renew',
            pending_revision={'amount_invested': '80000', 'investment_date': str(date.today()), 'interest_payout': 'maturity'})
        r = self.api.post(f'/api/club1000/investors/{inv.pk}/action/', {'action': 'approve'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertTrue(Payout.objects.filter(pk=old_int.pk, status='pending').exists(), 'old unpaid interest was wiped')
        old_maturity = inv.maturity_date
        returned = Payout.objects.filter(investor=inv, payout_type='maturity', due_date=old_maturity, status='pending')
        self.assertEqual([p.amount_due for p in returned], [Decimal('20000.00')])     # on the finished term's maturity
        inv.refresh_from_db()
        self.assertEqual([t['start'] for t in inv.past_terms], [str(date.today() - relativedelta(months=25))])

    def test_renewing_an_at_maturity_term_keeps_its_interest(self):
        inv = self._inv(date.today() - relativedelta(months=25))
        Investor.objects.filter(pk=inv.pk).update(interest_payout='maturity')
        Payout.objects.create(investor=inv, payout_type='maturity', due_date=inv.maturity_date, amount_due=Decimal('130000'))
        Investor.objects.filter(pk=inv.pk).update(
            approval_status='pending', pending_revision_type='renew',
            pending_revision={'amount_invested': '100000', 'investment_date': str(date.today()), 'interest_payout': 'maturity'})
        old_maturity = inv.maturity_date
        self.assertEqual(self.api.post(f'/api/club1000/investors/{inv.pk}/action/', {'action': 'approve'}, format='json').status_code, 200)
        old = list(Payout.objects.filter(investor=inv, due_date=old_maturity).values_list('payout_type', 'amount_due'))
        self.assertEqual(old, [('interest', Decimal('30000.00'))])                   # the 30% earned is not lost
        d = self.api.get(f'/api/club1000/investors/{inv.pk}/ledger/').data
        self.assertEqual([t['label'].split(' · ')[0] for t in d['terms']], ['Original', 'Renewal 1'])
        self.assertEqual({e['term'] for e in d['entries'] if e.get('due_date') == str(old_maturity)}, {0})

    def test_history_counts_renewals_and_lists_payments(self):
        inv = self._inv(date.today() - relativedelta(months=25))
        p = Payout.objects.create(investor=inv, payout_type='interest', due_date=date.today() - timedelta(days=40), amount_due=Decimal('7500'))
        self.api.post(f'/api/club1000/payouts/{p.pk}/mark-paid/', {'amount': '7000', 'notes': 'UTR 7'}, format='json')
        Investor.objects.filter(pk=inv.pk).update(
            approval_status='pending', pending_revision_type='renew',
            pending_revision={'amount_invested': '100000', 'investment_date': str(date.today()), 'interest_payout': 'maturity'})
        self.api.post(f'/api/club1000/investors/{inv.pk}/action/', {'action': 'approve'}, format='json')
        inv.refresh_from_db()
        self.assertEqual(inv.renewal_count, 1)
        d = self.api.get(f'/api/club1000/investors/{inv.pk}/history/').data
        self.assertEqual(d['renewal_count'], 1)
        kinds = [e['kind'] for e in d['events']]
        self.assertIn('paid', kinds)
        self.assertIn('added', kinds)
        paid = next(e for e in d['events'] if e['kind'] == 'paid')
        self.assertEqual(Decimal(paid['amount']), Decimal('7000'))
        self.assertIn('UTR 7', paid['detail'])

    def test_interest_periods_are_fy_quarters_with_days(self):
        from club1000.services import generate_payout_schedule
        inv = self._inv(date(2026, 10, 17), months_tenure=12)
        inv.total_return_pct = Decimal('15'); inv.amount_invested = Decimal('2000000'); inv.save()
        generate_payout_schedule(inv)
        rows = self.api.get('/api/club1000/payouts/').data
        ints = sorted((r for r in rows if r['payout_type'] == 'interest'), key=lambda r: r['due_date'])
        labels = [r['period']['label'] for r in ints]
        self.assertEqual(labels, ['Q3 FY26-27 (part)', 'Q4 FY26-27', 'Q1 FY27-28', 'Q2 FY27-28', 'Q3 FY27-28 (part)'])
        first = ints[0]['period']
        self.assertEqual((first['from'], first['to'], first['days']), ('2026-10-18', '2026-12-31', 75))
        self.assertEqual(Decimal(ints[0]['amount_due']), Decimal(first['computed']))

    def test_manager_deletes_an_investor_with_nothing_paid(self):
        inv = self._inv(date.today() - relativedelta(months=1))
        Payout.objects.create(investor=inv, payout_type='maturity', due_date=inv.maturity_date, amount_due=Decimal('100000'))
        r = self.api.delete(f'/api/club1000/investors/{inv.pk}/')
        self.assertEqual(r.status_code, 204, getattr(r, 'data', None))
        self.assertFalse(Investor.objects.filter(pk=inv.pk).exists())
        self.assertFalse(Payout.objects.filter(investor_id=inv.pk).exists())

    def test_delete_refused_once_money_has_moved(self):
        inv = self._inv(date.today() - relativedelta(months=4))
        Payout.objects.create(investor=inv, payout_type='interest', due_date=date.today() - timedelta(days=5),
                              amount_due=Decimal('3000'), status='paid', paid_amount=Decimal('3000'))
        r = self.api.delete(f'/api/club1000/investors/{inv.pk}/')
        self.assertEqual(r.status_code, 409)
        self.assertTrue(Investor.objects.filter(pk=inv.pk).exists())

    def test_staff_cannot_delete_an_investor(self):
        from accounts.models import User
        staff = User.objects.create_user('st@stf.com', company=self.co, user_code='STF2', password='p',
                                         name='Staff', role='Employee', modules=['Club 1000'])
        inv = self._inv(date.today() - relativedelta(months=1))
        Investor.objects.filter(pk=inv.pk).update(added_by=staff)
        api = APIClient(); api.force_authenticate(staff)
        self.assertEqual(api.delete(f'/api/club1000/investors/{inv.pk}/').status_code, 403)
        self.assertTrue(Investor.objects.filter(pk=inv.pk).exists())
