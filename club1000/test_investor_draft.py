"""Saving an unfinished investor, the way a booking is saved as a draft.

The Add Investor form is long — scheme, dates, amount, payout terms, reference,
KYC scan, LOI security — and losing all of it because one field was missing is
the thing a draft exists to prevent. So a draft skips the completeness checks.

That is exactly why it cannot be approved: approval is what generates the payout
schedule and the referral reward from figures a draft may not have yet. It has
to be completed and submitted first. And because a half-filled record is nobody
else's business, a draft is visible only to whoever saved it.
"""
from datetime import date

from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from club1000.models import Investor, Scheme
from companies.models import Company


class InvestorDrafts(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='CLB', name='Club Co', is_active=True)
        cls.admin = User.objects.create_user(
            'admin@clb.com', company=cls.co, user_code='CLB001', password='p',
            name='Club Admin', role='Admin', modules=['Club 1000'])
        cls.other = User.objects.create_user(
            'other@clb.com', company=cls.co, user_code='CLB002', password='p',
            name='Other Admin', role='Admin', modules=['Club 1000'],
            reporting_manager=cls.admin)
        cls.scheme = Scheme.objects.create(
            company=cls.co, name='RISE', tenure_months=12, min_ticket_size=250000,
            interest_payout_options=['maturity'])

    def setUp(self):
        self.api = APIClient()
        self.api.force_authenticate(user=self.admin)

    def _ids(self, resp):
        """The investors endpoint answers with a plain list, not a page."""
        body = resp.data
        rows = body['results'] if isinstance(body, dict) else body
        return [i['id'] for i in rows]

    def _draft(self, **over):
        body = {'scheme': self.scheme.id}
        body.update(over)
        return self.api.post('/api/club1000/investors/draft/', body, format='json')

    # ── saving ───────────────────────────────────────────────────────────────
    def test_a_draft_saves_with_almost_nothing_filled_in(self):
        r = self._draft()
        self.assertIn(r.status_code, (200, 201), r.data)
        inv = Investor.objects.get(id=r.data['id'])
        self.assertEqual(inv.approval_status, 'draft')
        self.assertEqual(inv.added_by_id, self.admin.id)

    def test_it_derives_the_maturity_date_so_the_form_can_show_it(self):
        r = self._draft(investment_date='2026-09-30')
        inv = Investor.objects.get(id=r.data['id'])
        self.assertEqual(inv.investment_date, date(2026, 9, 30))
        self.assertEqual(inv.maturity_date, date(2027, 9, 30))

    def test_an_amount_below_the_minimum_is_allowed_in_a_draft(self):
        """The submit path enforces the ticket size; a draft is mid-typing."""
        r = self._draft(amount_invested='1000')
        self.assertIn(r.status_code, (200, 201), r.data)

    def test_it_still_needs_a_scheme_because_the_dates_come_from_it(self):
        r = self.api.post('/api/club1000/investors/draft/', {}, format='json')
        self.assertEqual(r.status_code, 400)

    def test_saving_again_updates_the_same_draft_rather_than_adding_one(self):
        first = self._draft(name='Half Typed')
        before = Investor.objects.count()
        again = self._draft(id=first.data['id'], name='Half Typed More', phone='+919800000501')
        self.assertEqual(again.status_code, 200, again.data)
        self.assertEqual(Investor.objects.count(), before)
        inv = Investor.objects.get(id=first.data['id'])
        self.assertEqual(inv.name, 'Half Typed More')

    # ── whose draft it is ────────────────────────────────────────────────────
    def test_only_its_author_sees_it(self):
        mine = self._draft(name='Mine Alone')
        self.api.force_authenticate(user=self.other)
        listed = self.api.get('/api/club1000/investors/?approval_status=draft')
        self.assertEqual(listed.status_code, 200, listed.data)
        self.assertNotIn(mine.data['id'], self._ids(listed))

    def test_its_author_sees_it_under_the_draft_filter(self):
        mine = self._draft(name='Mine Alone Two')
        listed = self.api.get('/api/club1000/investors/?approval_status=draft')
        self.assertIn(mine.data['id'], self._ids(listed))

    def test_it_is_not_counted_among_live_investors(self):
        """Nothing has been agreed yet — counting one would overstate the book."""
        mine = self._draft(name='Not An Investor Yet')
        listed = self.api.get('/api/club1000/investors/')
        self.assertNotIn(mine.data['id'], self._ids(listed))

    def test_another_persons_draft_cannot_be_updated(self):
        mine = self._draft(name='Mine')
        self.api.force_authenticate(user=self.other)
        r = self.api.post('/api/club1000/investors/draft/',
                          {'id': mine.data['id'], 'scheme': self.scheme.id, 'name': 'Hijacked'},
                          format='json')
        # Falls through to creating their own draft; mine is untouched.
        self.assertEqual(Investor.objects.get(id=mine.data['id']).name, 'Mine')

    # ── it cannot be approved ────────────────────────────────────────────────
    def test_a_draft_cannot_be_approved(self):
        mine = self._draft(name='Incomplete')
        r = self.api.post(f'/api/club1000/investors/{mine.data["id"]}/action/',
                          {'action': 'approve'}, format='json')
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn('draft', str(r.data).lower())
        self.assertEqual(Investor.objects.get(id=mine.data['id']).approval_status, 'draft')

    # ── submitting it ────────────────────────────────────────────────────────
    def test_submitting_promotes_the_same_row_rather_than_duplicating(self):
        mine = self._draft(name='Will Submit')
        before = Investor.objects.count()
        r = self.api.post('/api/club1000/investors/', {
            'draft_id': mine.data['id'], 'scheme': self.scheme.id,
            'name': 'Will Submit', 'phone': '+919800000502',
            'amount_invested': '300000', 'investment_date': '2026-09-30',
        }, format='json')
        self.assertIn(r.status_code, (200, 201), r.data)
        self.assertEqual(Investor.objects.count(), before, 'submitting left the draft behind')
        inv = Investor.objects.get(id=mine.data['id'])
        self.assertEqual(inv.approval_status, 'pending')
        self.assertEqual(str(inv.amount_invested), '300000.00')

    def test_submitting_enforces_the_minimum_the_draft_let_through(self):
        mine = self._draft(amount_invested='1000')
        r = self.api.post('/api/club1000/investors/', {
            'draft_id': mine.data['id'], 'scheme': self.scheme.id,
            'name': 'Too Small', 'phone': '+919800000503', 'amount_invested': '1000',
        }, format='json')
        self.assertEqual(r.status_code, 400, r.data)
        self.assertEqual(Investor.objects.get(id=mine.data['id']).approval_status, 'draft')
