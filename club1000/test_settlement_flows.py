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
        self.assertTrue(Payout.objects.filter(pk=old_int.pk, status='paid').exists(), 'old interest must stay, settled at the renewal')
        old_maturity = inv.maturity_date
        returned = Payout.objects.filter(investor=inv, payout_type='maturity', due_date=old_maturity, status='paid')
        self.assertEqual([p.amount_due for p in returned], [Decimal('20000.00')])     # on the finished term's maturity, settled
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

    def test_ask_nexora_terms_match_the_ledger_tabs(self):
        from sales import assistant
        inv = self._inv(date.today() - relativedelta(months=25))
        Investor.objects.filter(pk=inv.pk).update(interest_payout='maturity')
        Payout.objects.create(investor=inv, payout_type='maturity', due_date=inv.maturity_date, amount_due=Decimal('130000'))
        Investor.objects.filter(pk=inv.pk).update(
            approval_status='pending', pending_revision_type='renew',
            pending_revision={'amount_invested': '100000', 'investment_date': str(date.today()), 'interest_payout': 'maturity'})
        self.api.post(f'/api/club1000/investors/{inv.pk}/action/', {'action': 'approve'}, format='json')
        ledger = self.api.get(f'/api/club1000/investors/{inv.pk}/ledger/').data['terms']
        rows = assistant.run_query(self.admin, None, {'entity': 'investment_terms', 'mode': 'list', 'limit': 50,
                                                      'fields': ['term', 'kind', 'interest', 'current']})['rows']
        by = {r['term']: r for r in rows}
        self.assertEqual(sorted(by), sorted(t['label'] for t in ledger))
        first = ledger[0]
        self.assertEqual(by[first['label']]['kind'], 'original')
        self.assertEqual(Decimal(str(by[first['label']]['interest']).replace('₹', '').replace(',', '')),
                         Decimal(first['summary']['interest']))

    def _renew_quarterly(self, amount, choice=None):
        """A quarterly investment whose term ended with 2 interest instalments unpaid, renewed."""
        inv = self._inv(date.today() - relativedelta(months=25))      # ₹1,00,000, 30% total, quarterly
        old_maturity = inv.maturity_date
        a = Payout.objects.create(investor=inv, payout_type='interest', due_date=old_maturity - relativedelta(months=3), amount_due=Decimal('3750'))
        b = Payout.objects.create(investor=inv, payout_type='interest', due_date=old_maturity, amount_due=Decimal('3750'))
        Payout.objects.create(investor=inv, payout_type='maturity', due_date=old_maturity, amount_due=Decimal('100000'))
        pr = {'amount_invested': str(amount), 'investment_date': str(old_maturity), 'interest_payout': 'quarterly'}
        if choice:
            pr['interest_choice'] = choice
        Investor.objects.filter(pk=inv.pk).update(approval_status='pending', pending_revision_type='renew', pending_revision=pr)
        r = self.api.post(f'/api/club1000/investors/{inv.pk}/action/', {'action': 'approve'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        inv.refresh_from_db(); a.refresh_from_db(); b.refresh_from_db()
        return inv, a, b, old_maturity

    def test_case1_everything_added_into_the_renewal(self):
        from club1000.services import money_in
        inv, a, b, _ = self._renew_quarterly(107500, 'reinvest')
        self.assertEqual((a.status, b.status), ('paid', 'paid'))                 # settled — not overdue
        self.assertIn('Added to renewal', a.notes)
        self.assertEqual(inv.past_terms[-1]['rolled_interest'], '7500.00')
        self.assertEqual(money_in(inv), Decimal('100000'))                      # no new money

    def test_case2_interest_paid_out_principal_renewed(self):
        inv, a, b, _ = self._renew_quarterly(100000, 'pay')
        self.assertEqual((a.status, b.status), ('paid', 'paid'))                 # handed over at the renewal
        self.assertTrue(a.notes.startswith('Settled at renewal R1'))
        self.assertFalse(Payout.objects.filter(investor=inv, payout_type='maturity', notes__icontains='returned').exists())

    def test_case3_some_principal_and_the_interest_paid_out(self):
        inv, a, b, old = self._renew_quarterly(60000, 'pay')
        self.assertEqual((a.status, b.status), ('paid', 'paid'))
        back = Payout.objects.get(investor=inv, payout_type='maturity', due_date=old, status='paid')
        self.assertEqual(back.amount_due, Decimal('40000.00'))

    def test_case4_more_added_on_top_of_principal_and_interest(self):
        from club1000.services import money_in
        inv, a, b, _ = self._renew_quarterly(150000, 'reinvest')
        self.assertEqual((a.status, b.status), ('paid', 'paid'))
        self.assertEqual(money_in(inv), Decimal('142500'))                      # 1,00,000 + 42,500 fresh

    def test_partly_added_leaves_the_rest_due(self):
        inv, a, b, _ = self._renew_quarterly(105000, 'reinvest')                 # only ₹5,000 of ₹7,500 added
        self.assertEqual(a.status, 'paid')
        self.assertEqual(b.paid_amount, Decimal('1250'))
        # the ₹2,500 he takes is handed over at the renewal too
        self.assertEqual([(x.amount_due, x.status) for x in Payout.objects.filter(investor=inv, balance_of=b)], [(Decimal('2500.00'), 'paid')])

    def test_undo_renewal_puts_the_ended_term_back(self):
        from django.core.files.base import ContentFile
        from club1000.models import ReferralReward
        inv, a, b, old_maturity = self._renew_quarterly(107500, 'reinvest')
        self.assertEqual((a.status, b.status), ('paid', 'paid'))
        ReferralReward.objects.create(investor=inv, reference_name='R', reference_phone='9800000009', amount=Decimal('268.75'), pct=Decimal('0.25'))
        d = self.api.get(f'/api/club1000/investors/{inv.pk}/ledger/').data
        self.assertTrue(d['can_undo_renewal'])
        r = self.api.post(f'/api/club1000/investors/{inv.pk}/undo-renewal/', {}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        inv.refresh_from_db(); a.refresh_from_db(); b.refresh_from_db()
        self.assertEqual((inv.renewal_count, inv.amount_invested, inv.maturity_date, inv.past_terms), (0, Decimal('100000'), old_maturity, None))
        self.assertEqual((a.status, b.status), ('pending', 'pending'))            # added interest is due again
        self.assertFalse(Payout.objects.filter(investor=inv, due_date__gt=old_maturity).exists())
        self.assertEqual([p.amount_due for p in Payout.objects.filter(investor=inv, payout_type='maturity')], [Decimal('100000.00')])
        self.assertFalse(ReferralReward.objects.filter(investor=inv, pct=Decimal('0.25')).exists())

    def test_undo_renewal_with_payments_asks_first(self):
        inv, a, b, old_maturity = self._renew_quarterly(100000, 'pay')
        p = Payout.objects.filter(investor=inv, due_date__gt=old_maturity, payout_type='interest').order_by('due_date').first()
        self.api.post(f'/api/club1000/payouts/{p.pk}/mark-paid/', {'amount': str(p.amount_due), 'paid_date': str(date.today())}, format='json')
        r = self.api.post(f'/api/club1000/investors/{inv.pk}/undo-renewal/', {}, format='json')
        self.assertEqual((r.status_code, r.data.get('code')), (409, 'has_paid'))
        r = self.api.post(f'/api/club1000/investors/{inv.pk}/undo-renewal/', {'confirm_paid': True}, format='json')
        self.assertEqual(r.status_code, 200, r.data)

    def test_part_interest_taken_and_fresh_money_added(self):
        """Takes ₹2,500 of the ₹7,500 interest and adds ₹50,000: ₹5,000 goes into the renewal."""
        from club1000.services import money_in
        inv = self._inv(date.today() - relativedelta(months=25))
        old = inv.maturity_date
        a = Payout.objects.create(investor=inv, payout_type='interest', due_date=old - relativedelta(months=3), amount_due=Decimal('3750'))
        b = Payout.objects.create(investor=inv, payout_type='interest', due_date=old, amount_due=Decimal('3750'))
        Payout.objects.create(investor=inv, payout_type='maturity', due_date=old, amount_due=Decimal('100000'))
        Investor.objects.filter(pk=inv.pk).update(approval_status='pending', pending_revision_type='renew', pending_revision={
            'amount_invested': '155000', 'investment_date': str(old), 'interest_payout': 'quarterly',
            'interest_choice': 'reinvest', 'interest_added': '5000'})
        self.assertEqual(self.api.post(f'/api/club1000/investors/{inv.pk}/action/', {'action': 'approve'}, format='json').status_code, 200)
        inv.refresh_from_db(); a.refresh_from_db(); b.refresh_from_db()
        self.assertEqual(inv.past_terms[-1]['rolled_interest'], '5000.00')
        self.assertEqual((a.status, b.paid_amount), ('paid', Decimal('1250')))          # ₹2,500 of b handed to him at renewal
        self.assertEqual(money_in(inv), Decimal('150000'))                              # ₹50,000 fresh

    def test_rejected_renewal_rolls_back_the_number_and_is_listed(self):
        inv = self._inv(date.today() - relativedelta(months=25))
        Investor.objects.filter(pk=inv.pk).update(approval_status='pending', pending_revision_type='renew', revision_no=1,
                                                  pending_revision={'amount_invested': '150000', 'investment_date': str(date.today())})
        r = self.api.post(f'/api/club1000/investors/{inv.pk}/action/', {'action': 'reject', 'reason': 'again sign'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        inv.refresh_from_db()
        self.assertEqual((inv.approval_status, inv.revision_no, inv.amount_invested), ('approved', 0, Decimal('100000')))
        self.assertEqual((inv.rejections[0]['kind'], inv.rejections[0]['no'], inv.rejections[0]['reason']), ('renewal', 1, 'again sign'))
        rows = self.api.get('/api/club1000/investors/?approval_status=rejected').data
        rows = rows if isinstance(rows, list) else rows.get('results', [])
        self.assertEqual([x['id'] for x in rows], [inv.pk])

    def test_money_handed_over_at_renewal_is_marked_paid(self):
        inv = self._inv(date.today() - relativedelta(months=25))
        old = inv.maturity_date
        a = Payout.objects.create(investor=inv, payout_type='interest', due_date=old, amount_due=Decimal('7500'))
        Payout.objects.create(investor=inv, payout_type='maturity', due_date=old, amount_due=Decimal('100000'))
        Investor.objects.filter(pk=inv.pk).update(approval_status='pending', pending_revision_type='renew', pending_revision={
            'amount_invested': '80000', 'investment_date': str(old), 'interest_payout': 'quarterly', 'interest_choice': 'pay',
            'pay_now': {'paid_date': str(old), 'notes': 'UTR 99'}})
        self.assertEqual(self.api.post(f'/api/club1000/investors/{inv.pk}/action/', {'action': 'approve'}, format='json').status_code, 200)
        a.refresh_from_db()
        back = Payout.objects.get(investor=inv, payout_type='maturity', due_date=old)
        self.assertEqual((a.status, a.paid_date, a.notes), ('paid', old, 'Settled at renewal R1 · UTR 99'))
        self.assertEqual((back.status, back.amount_due), ('paid', Decimal('20000.00')))
        self.assertFalse(Payout.objects.filter(investor=inv, status='pending', due_date__lte=old).exists())
