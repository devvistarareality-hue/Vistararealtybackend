"""A revision can be saved before the signed LOI comes back.

Revising an investor means downloading the revised LOI, getting it signed, and
uploading it — days apart. Until now the form could not be saved in between, so
the figures were either held in an open tab or typed again from scratch.

The draft is deliberately NOT kept in pending_revision. That field means "a
revision is waiting for an approver", and an unsubmitted draft sitting there
would put the investor in front of approvers with nothing signed to approve.
These tests pin that separation, since getting it wrong would quietly send
half-finished work into the approval queue.

Also here: the revision date, which records when the revised terms were agreed.
It is not the investment date — a revision re-agrees terms, it does not re-date
the money — so maturity and the payout schedule must not move.
"""
from datetime import date
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from club1000.models import Investor, Payout, Scheme
from companies.models import Company


class RevisionDraftTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='RVD', name='Revise Co', is_active=True)
        cls.admin = User.objects.create_user(
            'admin@rvd.com', company=cls.co, user_code='RVD001', password='p',
            name='Club Admin', role='Admin', modules=['Club 1000'])
        cls.scheme = Scheme.objects.create(
            company=cls.co, name='RISE', tenure_months=12, min_ticket_size=100000,
            interest_payout_options=['quarterly'])

    def setUp(self):
        invested = date(2026, 7, 1)
        self.inv = Investor.objects.create(
            company=self.co, scheme=self.scheme, added_by=self.admin,
            name='An Investor', phone='+919800000011',
            amount_invested=Decimal('1000000'), total_return_pct=Decimal('15'),
            investment_date=invested,
            maturity_date=invested + relativedelta(months=12),
            interest_payout='quarterly', status='active', approval_status='approved')
        self.api = APIClient()
        self.api.force_authenticate(user=self.admin)

    def _save_draft(self, **fields):
        return self.api.post(f'/api/club1000/investors/{self.inv.pk}/revise/draft/',
                             fields, format='json')

    def _fresh(self):
        return Investor.objects.get(pk=self.inv.pk)

    # ------------------------------------------------------------- saving

    def test_a_draft_saves_without_a_signed_loi(self):
        """The whole point — the signed LOI is exactly what a draft is waiting
        for, so requiring it would defeat the feature."""
        r = self._save_draft(amount_invested='1500000', revision_date='2026-10-16')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(self._fresh().revision_draft['amount_invested'], '1500000')

    def test_the_draft_comes_back_on_the_investor(self):
        # The modal prefills from this, so it has to be readable.
        self._save_draft(amount_invested='1500000', notes='agreed on call')
        r = self.api.get('/api/club1000/investors/?approval_status=approved')
        row = next(i for i in r.data if i['id'] == self.inv.pk)
        self.assertEqual(row['revision_draft']['notes'], 'agreed on call')

    def test_saving_again_replaces_the_draft(self):
        self._save_draft(amount_invested='1500000')
        self._save_draft(amount_invested='1200000')
        self.assertEqual(self._fresh().revision_draft, {'amount_invested': '1200000'})

    def test_an_empty_draft_is_a_discard(self):
        self._save_draft(amount_invested='1500000')
        self._save_draft()
        self.assertIsNone(self._fresh().revision_draft)

    def test_a_draft_can_be_deleted(self):
        self._save_draft(amount_invested='1500000')
        r = self.api.delete(f'/api/club1000/investors/{self.inv.pk}/revise/draft/')
        self.assertEqual(r.status_code, 204)
        self.assertIsNone(self._fresh().revision_draft)

    def test_the_payout_schedule_is_carried_in_the_draft(self):
        self._save_draft(amount_invested='1500000', payout_schedule=[
            {'due_date': '2026-10-10', 'amount_due': '1000', 'payout_type': 'interest'}])
        self.assertEqual(len(self._fresh().revision_draft['payout_schedule']), 1)

    # ------------------------------------- what a draft must NOT do

    def test_a_draft_does_not_send_the_investor_for_approval(self):
        """The property that matters most. A draft is unfinished work, not a
        request for someone's signature."""
        self._save_draft(amount_invested='1500000')
        fresh = self._fresh()
        self.assertEqual(fresh.approval_status, 'approved')
        self.assertIsNone(fresh.pending_revision)
        self.assertEqual(fresh.revision_no, 0)

    def test_a_draft_does_not_appear_in_the_approvals_queue(self):
        self._save_draft(amount_invested='1500000')
        r = self.api.get('/api/club1000/investors/?approval_status=pending')
        self.assertNotIn(self.inv.pk, [i['id'] for i in r.data])

    def test_a_draft_changes_none_of_the_live_terms(self):
        self._save_draft(amount_invested='9999999', total_return_pct='99', name='Not Me')
        fresh = self._fresh()
        self.assertEqual(fresh.amount_invested, Decimal('1000000'))
        self.assertEqual(fresh.total_return_pct, Decimal('15'))
        self.assertEqual(fresh.name, 'An Investor')

    def test_an_investor_awaiting_approval_cannot_be_drafted_against(self):
        Investor.objects.filter(pk=self.inv.pk).update(approval_status='pending')
        self.assertEqual(self._save_draft(amount_invested='1').status_code, 400)

    def test_another_company_cannot_touch_it(self):
        other = Company.objects.create(code='RVE', name='Elsewhere', is_active=True)
        outsider = User.objects.create_user(
            'a@rve.com', company=other, user_code='RVE001', password='p',
            name='Outsider', role='Admin', modules=['Club 1000'])
        api = APIClient()
        api.force_authenticate(user=outsider)
        r = api.post(f'/api/club1000/investors/{self.inv.pk}/revise/draft/',
                     {'amount_invested': '1'}, format='json')
        self.assertEqual(r.status_code, 404)
        self.assertIsNone(self._fresh().revision_draft)

    # ----------------------------------------------- submitting clears it

    def test_submitting_a_revision_clears_the_draft(self):
        """Otherwise the next revision would open prefilled with figures that
        have already been submitted."""
        self._save_draft(amount_invested='1500000')
        r = self.api.post(f'/api/club1000/investors/{self.inv.pk}/revise/', {
            'amount_invested': '1500000', 'revision_date': '2026-10-16',
            'loi_file': {'name': 'loi.pdf', 'type': 'application/pdf', 'data': 'eA=='},
        }, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        fresh = self._fresh()
        self.assertIsNone(fresh.revision_draft)
        self.assertEqual(fresh.approval_status, 'pending')
        self.assertEqual(fresh.pending_revision['revision_date'], '2026-10-16')

    # ---------------------------------------------------- the revision date

    def test_the_revision_date_is_applied_on_approval(self):
        self.api.post(f'/api/club1000/investors/{self.inv.pk}/revise/', {
            'amount_invested': '1500000', 'revision_date': '2026-10-16',
            'loi_file': {'name': 'loi.pdf', 'type': 'application/pdf', 'data': 'eA=='},
        }, format='json')
        r = self.api.post(f'/api/club1000/investors/{self.inv.pk}/action/',
                          {'action': 'approve'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(self._fresh().revision_date, date(2026, 10, 16))

    def test_the_revision_date_does_not_move_the_investment_or_maturity(self):
        """A revision re-agrees the terms; it does not re-date the money. If
        this ever changed, every payout date would shift with it."""
        before = Payout.objects.filter(investor=self.inv).count()
        self.api.post(f'/api/club1000/investors/{self.inv.pk}/revise/', {
            'amount_invested': '1500000', 'revision_date': '2027-01-01',
            'loi_file': {'name': 'loi.pdf', 'type': 'application/pdf', 'data': 'eA=='},
        }, format='json')
        self.api.post(f'/api/club1000/investors/{self.inv.pk}/action/',
                      {'action': 'approve'}, format='json')
        fresh = self._fresh()
        self.assertEqual(fresh.investment_date, date(2026, 7, 1))
        self.assertEqual(fresh.maturity_date, date(2027, 7, 1))
        self.assertEqual(fresh.revision_date, date(2027, 1, 1))
        self.assertGreaterEqual(Payout.objects.filter(investor=fresh).count(), before)

    def test_a_revision_with_no_date_still_works(self):
        # The field is new; nothing that omits it should break.
        self.api.post(f'/api/club1000/investors/{self.inv.pk}/revise/', {
            'amount_invested': '1500000',
            'loi_file': {'name': 'loi.pdf', 'type': 'application/pdf', 'data': 'eA=='},
        }, format='json')
        r = self.api.post(f'/api/club1000/investors/{self.inv.pk}/action/',
                          {'action': 'approve'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIsNone(self._fresh().revision_date)

    def test_an_unparseable_date_is_ignored_rather_than_breaking_approval(self):
        self.api.post(f'/api/club1000/investors/{self.inv.pk}/revise/', {
            'amount_invested': '1500000', 'revision_date': 'not-a-date',
            'loi_file': {'name': 'loi.pdf', 'type': 'application/pdf', 'data': 'eA=='},
        }, format='json')
        r = self.api.post(f'/api/club1000/investors/{self.inv.pk}/action/',
                          {'action': 'approve'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIsNone(self._fresh().revision_date)
