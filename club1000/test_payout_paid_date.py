"""Marking a payout paid records the day the money went out, not just today."""
from datetime import date, timedelta
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from club1000.models import Investor, Payout, Scheme
from companies.models import Company


class PayoutPaidDateTests(TestCase):
    def setUp(self):
        co = Company.objects.create(code='PPD', name='Pay Co', is_active=True)
        self.admin = User.objects.create_user('a@ppd.com', company=co, user_code='PPD1', password='p',
                                              name='Admin', role='Admin', modules=['Club 1000'])
        scheme = Scheme.objects.create(company=co, name='RISE', tenure_months=12, min_ticket_size=1)
        inv = Investor.objects.create(company=co, scheme=scheme, added_by=self.admin, name='I', phone='9800000001',
                                      amount_invested=Decimal('100000'), investment_date=date(2026, 1, 1),
                                      maturity_date=date(2026, 1, 1) + relativedelta(months=12),
                                      status='active', approval_status='approved')
        self.p = Payout.objects.create(investor=inv, payout_type='interest', due_date=date(2026, 4, 10),
                                       amount_due=Decimal('5000'), status='pending')
        self.api = APIClient(); self.api.force_authenticate(self.admin)

    def _pay(self, **body):
        return self.api.post(f'/api/club1000/payouts/{self.p.pk}/mark-paid/', body, format='json')

    def test_the_paid_date_and_amount_are_kept(self):
        r = self._pay(amount='4800', paid_date='2026-10-01', notes='UTR 123')
        self.assertEqual(r.status_code, 200, r.data)
        self.p.refresh_from_db()
        self.assertEqual((self.p.paid_date, self.p.paid_amount, self.p.notes), (date(2026, 10, 1), Decimal('4800'), 'UTR 123'))

    def test_no_date_means_today(self):
        self._pay()
        self.p.refresh_from_db()
        self.assertEqual(self.p.paid_date, date.today())

    def test_a_future_date_is_refused(self):
        r = self._pay(paid_date=str(date.today() + timedelta(days=2)))
        self.assertEqual(r.status_code, 400)


class ReferralRewardPaidTests(PayoutPaidDateTests):
    """The Mark Paid dialog for a referral reward: amount, date and remarks."""
    def setUp(self):
        super().setUp()
        from club1000.models import ReferralReward
        self.r = ReferralReward.objects.create(investor=self.p.investor, reference_name='SM',
                                               reference_phone='8866339595', amount=Decimal('2500'))

    def _payr(self, **body):
        return self.api.post(f'/api/club1000/referral-rewards/{self.r.pk}/mark-paid/', body, format='json')

    def test_reward_amount_date_and_remarks_are_kept(self):
        res = self._payr(amount='2000', paid_date='2026-10-05', notes='UTR 9')
        self.assertEqual(res.status_code, 200, res.data)
        self.r.refresh_from_db()
        self.assertEqual((self.r.status, self.r.paid_amount, self.r.paid_date, self.r.notes),
                         ('paid', Decimal('2000'), date(2026, 10, 5), 'UTR 9'))

    def test_reward_defaults_to_full_amount_today(self):
        self._payr()
        self.r.refresh_from_db()
        self.assertEqual((self.r.paid_amount, self.r.paid_date), (Decimal('2500'), date.today()))

    def test_reward_future_date_refused(self):
        res = self._payr(paid_date=(date.today() + timedelta(days=1)).isoformat())
        self.assertEqual(res.status_code, 400)


class UndoPaidTests(ReferralRewardPaidTests):
    """A payment marked paid by mistake goes back to pending."""
    def test_undo_payout(self):
        self._pay(amount='4800', notes='UTR 1')
        res = self.api.post(f'/api/club1000/payouts/{self.p.pk}/mark-unpaid/', {}, format='json')
        self.assertEqual(res.status_code, 200, res.data)
        self.p.refresh_from_db()
        self.assertEqual((self.p.status, self.p.paid_date, self.p.paid_amount, self.p.notes), ('pending', None, None, ''))

    def test_undo_reward(self):
        self._payr(amount='2000')
        res = self.api.post(f'/api/club1000/referral-rewards/{self.r.pk}/mark-unpaid/', {}, format='json')
        self.assertEqual(res.status_code, 200, res.data)
        self.r.refresh_from_db()
        self.assertEqual((self.r.status, self.r.paid_date, self.r.paid_amount), ('pending', None, None))

    def test_undo_on_pending_refused(self):
        res = self.api.post(f'/api/club1000/payouts/{self.p.pk}/mark-unpaid/', {}, format='json')
        self.assertEqual(res.status_code, 400)


class EditDeletePaymentTests(ReferralRewardPaidTests):
    """A recorded payment is edited or deleted like an AR receipt."""
    def test_edit_payout_payment(self):
        self._pay(amount='5000', notes='UTR 1')
        r = self.api.patch(f'/api/club1000/payouts/{self.p.pk}/payment/', {'amount': '4900', 'paid_date': '2026-10-02', 'notes': 'UTR 2'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.p.refresh_from_db()
        self.assertEqual((self.p.status, self.p.paid_amount, self.p.paid_date, self.p.notes), ('paid', Decimal('4900'), date(2026, 10, 2), 'UTR 2'))

    def test_delete_payout_payment(self):
        self._pay()
        r = self.api.delete(f'/api/club1000/payouts/{self.p.pk}/payment/')
        self.assertEqual(r.status_code, 200, r.data)
        self.p.refresh_from_db()
        self.assertEqual((self.p.status, self.p.paid_amount), ('pending', None))

    def test_edit_future_date_refused(self):
        self._pay()
        r = self.api.patch(f'/api/club1000/payouts/{self.p.pk}/payment/', {'paid_date': str(date.today() + timedelta(days=3))}, format='json')
        self.assertEqual(r.status_code, 400)

    def test_edit_reward_payment(self):
        self._payr(amount='2500')
        r = self.api.patch(f'/api/club1000/referral-rewards/{self.r.pk}/payment/', {'amount': '2400'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.r.refresh_from_db()
        self.assertEqual(self.r.paid_amount, Decimal('2400'))


class ShortPaymentKeepsBalanceTests(PayoutPaidDateTests):
    """Paying less than the instalment keeps the rest due — never dropped."""
    def _pending(self):
        return list(Payout.objects.filter(investor=self.p.investor, status='pending').order_by('id'))

    def test_short_payment_leaves_a_balance_due(self):
        r = self._pay(amount='1500', paid_date='2026-10-01')
        self.assertEqual(r.status_code, 200, r.data)
        self.p.refresh_from_db()
        self.assertEqual((self.p.status, self.p.amount_due, self.p.paid_amount), ('paid', Decimal('1500'), Decimal('1500')))
        bal = self._pending()
        self.assertEqual(len(bal), 1)
        self.assertEqual((bal[0].amount_due, bal[0].due_date, bal[0].balance_of_id, bal[0].payout_type),
                         (Decimal('3500.00'), self.p.due_date, self.p.id, 'interest'))

    def test_paying_the_balance_closes_the_instalment(self):
        self._pay(amount='1500')
        bal = self._pending()[0]
        self.api.post(f'/api/club1000/payouts/{bal.pk}/mark-paid/', {}, format='json')
        self.assertEqual(self._pending(), [])
        total = sum(p.paid_amount for p in Payout.objects.filter(investor=self.p.investor, status='paid'))
        self.assertEqual(total, Decimal('5000'))

    def test_editing_the_payment_moves_the_balance(self):
        self._pay(amount='1500')
        self.api.patch(f'/api/club1000/payouts/{self.p.pk}/payment/', {'amount': '4000'}, format='json')
        self.assertEqual([b.amount_due for b in self._pending()], [Decimal('1000.00')])
        self.api.patch(f'/api/club1000/payouts/{self.p.pk}/payment/', {'amount': '5000'}, format='json')
        self.assertEqual(self._pending(), [])
        self.p.refresh_from_db()
        self.assertEqual(self.p.amount_due, Decimal('5000'))

    def test_deleting_the_payment_makes_the_instalment_whole(self):
        self._pay(amount='1500')
        self.api.delete(f'/api/club1000/payouts/{self.p.pk}/payment/')
        pend = self._pending()
        self.assertEqual([(p.id, p.amount_due) for p in pend], [(self.p.id, Decimal('5000.00'))])

    def test_full_or_more_leaves_no_balance(self):
        self._pay(amount='5200')
        self.assertEqual(self._pending(), [])


class PayoutClosesMaturedInvestmentTests(TestCase):
    """Paying a matured investment's principal from Payouts closes it; undoing reopens."""
    def setUp(self):
        from datetime import date, timedelta
        from decimal import Decimal
        from accounts.models import User
        from club1000.models import Investor, Payout, Scheme
        from companies.models import Company
        self.co = Company.objects.create(code='PCM', name='P', is_active=True)
        self.u = User.objects.create_user('a@pcm.com', company=self.co, user_code='PCM1', password='p', name='A', role='Admin', modules=['Club 1000'])
        sc = Scheme.objects.create(company=self.co, name='RISE', tenure_months=12, min_ticket_size=1)
        self.inv = Investor.objects.create(company=self.co, scheme=sc, added_by=self.u, name='M', phone='9800000009',
                                           amount_invested=Decimal('100000'), investment_date=date.today() - timedelta(days=400),
                                           maturity_date=date.today() - timedelta(days=35), total_return_pct=Decimal('15'),
                                           status='active', approval_status='approved')
        self.m = Payout.objects.create(investor=self.inv, payout_type='maturity', due_date=self.inv.maturity_date, amount_due=Decimal('115000'))
        self.api = APIClient(); self.api.force_authenticate(self.u)

    def test_pay_then_undo(self):
        from datetime import date
        r = self.api.post(f'/api/club1000/payouts/{self.m.pk}/mark-paid/', {'amount': '115000', 'paid_date': str(date.today())}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.inv.refresh_from_db(); self.assertEqual(self.inv.status, 'redeemed')
        self.api.post(f'/api/club1000/payouts/{self.m.pk}/mark-unpaid/', {}, format='json')
        self.inv.refresh_from_db(); self.assertEqual(self.inv.status, 'active')

    def test_short_payment_keeps_it_open(self):
        from datetime import date
        self.api.post(f'/api/club1000/payouts/{self.m.pk}/mark-paid/', {'amount': '100000', 'paid_date': str(date.today())}, format='json')
        self.inv.refresh_from_db(); self.assertEqual(self.inv.status, 'active')
