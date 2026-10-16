"""Revise LOI is the Add Investor form, prefilled: everything on it can change —
the scheme and the investment date too. Maturity follows them, the still-pending
payouts are rebuilt on approval, and a rejection leaves the investor as it was."""
from datetime import date
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from club1000.models import Investor, Payout, Scheme
from club1000.services import generate_payout_schedule
from companies.models import Company

LOI = {'name': 'loi.pdf', 'type': 'application/pdf', 'data': 'JVBERi0='}


class ReviseFullFormTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='RFF', name='Revise Co', is_active=True)
        cls.admin = User.objects.create_user(
            'admin@rff.com', company=cls.co, user_code='RFF001', password='p',
            name='Club Admin', role='Admin', modules=['Club 1000'])
        cls.rise = Scheme.objects.create(company=cls.co, name='RISE', tenure_months=12,
                                         min_ticket_size=100000, interest_payout_options=['quarterly'])
        cls.growth = Scheme.objects.create(company=cls.co, name='GROWTH 35', tenure_months=24,
                                           min_ticket_size=500000, interest_payout_options=['maturity'])
        other = Company.objects.create(code='OTH', name='Other', is_active=True)
        cls.foreign = Scheme.objects.create(company=other, name='X', tenure_months=12, min_ticket_size=1)

    def setUp(self):
        invested = date(2026, 7, 1)
        self.inv = Investor.objects.create(
            company=self.co, scheme=self.rise, added_by=self.admin,
            name='An Investor', phone='+919800000011',
            amount_invested=Decimal('1000000'), total_return_pct=Decimal('15'),
            investment_date=invested, maturity_date=invested + relativedelta(months=12),
            interest_payout='quarterly', status='active', approval_status='approved')
        generate_payout_schedule(self.inv)
        self.api = APIClient()
        self.api.force_authenticate(user=self.admin)

    def _revise(self, **fields):
        return self.api.post(f'/api/club1000/investors/{self.inv.pk}/revise/',
                             {'loi_file': LOI, **fields}, format='json')

    def _act(self, action):
        return self.api.post(f'/api/club1000/investors/{self.inv.pk}/action/',
                             {'action': action, 'reason': 'no'}, format='json')

    def _fresh(self):
        return Investor.objects.get(pk=self.inv.pk)

    def test_scheme_and_date_move_maturity_once_approved(self):
        r = self._revise(scheme=self.growth.id, investment_date='2026-08-15', amount_invested='600000',
                         interest_payout='maturity', total_return_pct='35', email='a@b.com', pan='ABCDE1234F')
        self.assertEqual(r.status_code, 201, r.data)
        inv = self._fresh()
        self.assertEqual(inv.scheme_id, self.rise.id)          # nothing moves until approval
        self.assertEqual(inv.investment_date, date(2026, 7, 1))
        self.assertEqual(self._act('approve').status_code, 200)
        inv = self._fresh()
        self.assertEqual(inv.scheme_id, self.growth.id)
        self.assertEqual(inv.investment_date, date(2026, 8, 15))
        self.assertEqual(inv.maturity_date, date(2028, 8, 15))  # 24-month GROWTH tenure
        self.assertEqual((inv.email, inv.pan), ('a@b.com', 'ABCDE1234F'))
        pending = Payout.objects.filter(investor=inv, status='pending')
        self.assertEqual(max(p.due_date for p in pending), date(2028, 8, 15))

    def test_a_rejected_revision_leaves_everything(self):
        self._revise(scheme=self.growth.id, investment_date='2026-08-15', amount_invested='600000')
        self.assertEqual(self._act('reject').status_code, 200)
        inv = self._fresh()
        self.assertEqual((inv.scheme_id, inv.investment_date, inv.maturity_date),
                         (self.rise.id, date(2026, 7, 1), date(2027, 7, 1)))

    def test_the_new_schemes_minimum_applies(self):
        r = self._revise(scheme=self.growth.id, amount_invested='200000')
        self.assertEqual(r.status_code, 400)
        self.assertIn('GROWTH 35', r.data['detail'])

    def test_another_companys_scheme_is_refused(self):
        self.assertEqual(self._revise(scheme=self.foreign.id).status_code, 400)

    def test_a_draft_keeps_the_new_fields(self):
        r = self.api.post(f'/api/club1000/investors/{self.inv.pk}/revise/draft/',
                          {'scheme': self.growth.id, 'investment_date': '2026-08-15', 'email': 'a@b.com'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        d = self._fresh().revision_draft
        self.assertEqual((d['scheme'], d['investment_date'], d['email']), (self.growth.id, '2026-08-15', 'a@b.com'))
