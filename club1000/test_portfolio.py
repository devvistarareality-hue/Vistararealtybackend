"""Investor portfolio — one person across investments, schemes and the people they referred."""
from datetime import date, timedelta
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from club1000.models import Investor, Payout, ReferralReward, Scheme
from companies.models import Company


class PortfolioTests(TestCase):
    def setUp(self):
        self.co = Company.objects.create(code='PTF', name='Folio Co', is_active=True)
        self.admin = User.objects.create_user('a@ptf.com', company=self.co, user_code='PTF1', password='p',
                                              name='Admin', role='Admin', modules=['Club 1000'])
        self.rise = Scheme.objects.create(company=self.co, name='RISE', tenure_months=24, min_ticket_size=1)
        self.grow = Scheme.objects.create(company=self.co, name='GROW', tenure_months=12, min_ticket_size=1)
        self.api = APIClient(); self.api.force_authenticate(self.admin)

    def _inv(self, name, phone, scheme, amount, on, **kw):
        kw.setdefault('status', 'active'); kw.setdefault('approval_status', 'approved')
        return Investor.objects.create(company=self.co, scheme=scheme, added_by=self.admin, name=name, phone=phone,
                                       amount_invested=Decimal(amount), investment_date=on,
                                       maturity_date=on + relativedelta(months=24), total_return_pct=Decimal('30'),
                                       interest_payout='quarterly', **kw)

    def test_one_person_across_investments_and_referrals(self):
        a1 = self._inv('Ramesh Patel', '+91 98000 11111', self.rise, '100000', date(2021, 4, 1), status='matured', renewal_count=2)
        a2 = self._inv('RAMESH PATEL', '9800011111', self.grow, '50000', date(2024, 1, 1))
        self._inv('Suresh Patel', '9800011111', self.rise, '70000', date(2024, 1, 1), reference_phone='9800011111')  # same number = same portfolio; not a referral
        self._inv('Draft Guy', '9800011111', self.rise, '1', date(2024, 1, 1), approval_status='draft')
        f = self._inv('Friend One', '9811111111', self.rise, '200000', date(2024, 2, 1), reference_phone='98000 11111')
        ReferralReward.objects.create(investor=f, reference_phone='9800011111', amount=Decimal('2000'), status='paid', paid_amount=Decimal('2000'))
        Payout.objects.create(investor=a1, payout_type='interest', due_date=date(2022, 1, 1), amount_due=Decimal('7500'),
                              status='paid', paid_amount=Decimal('7500'))
        Payout.objects.create(investor=a2, payout_type='interest', due_date=date.today() - timedelta(days=5), amount_due=Decimal('3000'))
        Payout.objects.create(investor=a2, payout_type='maturity', due_date=date.today() + timedelta(days=300), amount_due=Decimal('50000'))

        rows = self.api.get('/api/club1000/portfolios/').data
        self.assertEqual(len(rows), 2)                       # 98000 11111 and Friend — the draft is not a holding
        r = next(x for x in rows if '9800011111' in x['phone'].replace(' ', '').replace('+91', ''))
        self.assertEqual(r['name'], 'Ramesh Patel')          # the biggest holding names the card
        self.assertEqual(r['names'], ['Ramesh Patel', 'Suresh Patel'])
        self.assertEqual((r['investments'], r['renewals'], r['times_invested']), (3, 2, 5))
        self.assertEqual({s['scheme'] for s in r['schemes']}, {'RISE', 'GROW'})
        self.assertEqual(Decimal(r['active_amount']), Decimal('120000'))
        self.assertEqual(Decimal(r['total_invested']), Decimal('220000'))
        self.assertEqual(Decimal(r['paid_out']), Decimal('7500'))
        self.assertEqual(Decimal(r['pending_payouts']), Decimal('53000'))
        self.assertEqual(Decimal(r['due_now']), Decimal('3000'))
        self.assertEqual(r['referrals'], 1)
        self.assertEqual(Decimal(r['rewards_earned']), Decimal('2000'))
        self.assertEqual(str(r['since']), '2021-04-01')

        d = self.api.get(f'/api/club1000/investors/{a1.pk}/portfolio/').data
        self.assertEqual([h['id'] for h in d['history']][-1], a1.pk)
        self.assertEqual(len(d['history']), 3)
        self.assertEqual(d['referred'][0]['name'], 'Friend One')
        self.assertEqual(d['referred'][0]['reward_status'], 'paid')
        self.assertEqual(self.api.get(f'/api/club1000/investors/{a2.pk}/portfolio/').data['id'], a1.pk)

    def test_since_is_when_the_money_first_came_in(self):
        self._inv('Old Timer', '9811100000', self.rise, '100000', date(2025, 4, 1), renewal_count=3, first_invested_on=date(2019, 4, 1))
        r = self.api.get('/api/club1000/portfolios/').data[0]
        self.assertEqual((str(r['since']), r['times_invested']), ('2019-04-01', 4))

    def test_search_and_visibility(self):
        self._inv('Ramesh Patel', '9800011111', self.rise, '100000', date(2024, 1, 1))
        self.assertEqual(len(self.api.get('/api/club1000/portfolios/?search=rames').data), 1)
        self.assertEqual(len(self.api.get('/api/club1000/portfolios/?search=00011').data), 1)
        self.assertEqual(len(self.api.get('/api/club1000/portfolios/?search=zzz').data), 0)
        other = Company.objects.create(code='OTH', name='Other', is_active=True)
        u = User.objects.create_user('b@oth.com', company=other, user_code='OTH1', password='p',
                                     name='B', role='Admin', modules=['Club 1000'])
        api = APIClient(); api.force_authenticate(u)
        self.assertEqual(api.get('/api/club1000/portfolios/').data, [])

    def test_ask_nexora_counts_referrals_and_renewals_like_the_portfolio(self):
        from sales import assistant
        self._inv('Ramesh Patel', '9800011111', self.rise, '100000', date(2021, 4, 1), renewal_count=2)
        self._inv('Friend One', '9811111111', self.rise, '200000', date(2024, 2, 1), reference_name='Ramesh Patel', reference_phone='9800011111')
        self._inv('Friend Two', '9822222222', self.grow, '50000', date(2024, 3, 1), reference_name='Ramesh Patel', reference_phone='9800011111')
        out = assistant.run_query(self.admin, None, {'entity': 'investors', 'mode': 'count',
                                                     'filters': [{'field': 'referrer_phone', 'op': 'contains', 'value': '9800011111'}]})
        portfolio = next(r for r in self.api.get('/api/club1000/portfolios/').data if r['name'] == 'Ramesh Patel')
        self.assertEqual(out['total'], portfolio['referrals'])
        out = assistant.run_query(self.admin, None, {'entity': 'investors', 'mode': 'count',
                                                     'filters': [{'field': 'renewals', 'op': 'gte', 'value': 2}]})
        self.assertEqual((out['total'], portfolio['renewals']), (1, 2))
