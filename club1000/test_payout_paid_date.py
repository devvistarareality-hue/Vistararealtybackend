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
