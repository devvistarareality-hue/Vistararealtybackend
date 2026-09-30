"""A platform admin sees any employee's Club 1000 data.

They own the account and answer for what is in it, so nothing in this module
narrows them: not a designation configured to "own records only", and not the
draft rule, which otherwise shows a half-finished investor to its author alone.

Everyone else still gets the ordinary scoping — this is an exemption for the
account owner, not a hole.
"""
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import Designation, User
from club1000.models import Investor, Scheme
from companies.models import Company


class SuperAdminSeesEverything(TestCase):

    @classmethod
    def setUpTestData(cls):
        # is_platform_admin keys off the VRL company code plus role Admin.
        cls.vrl = Company.objects.create(code='VRL', name='Vistara', is_active=True)
        # More than one module on purpose: an Admin restricted to a single module
        # is a MODULE admin, and is_platform_admin deliberately says no to those.
        cls.super_admin = User.objects.create_user(
            'boss@vrl.com', company=cls.vrl, user_code='VRL001', password='p',
            name='Platform Boss', role='Admin', modules=['Club 1000', 'Sales'])
        # Deliberately restricted by designation — it must not narrow them.
        Designation.objects.create(company=cls.vrl, name='Locked Down', module='Club 1000',
                                   data_scope='own')
        cls.super_admin.designation = 'Locked Down'
        cls.super_admin.save(update_fields=['designation'])

        cls.employee = User.objects.create_user(
            'emp@vrl.com', company=cls.vrl, user_code='VRL002', password='p',
            name='Club Employee', role='Employee', modules=['Club 1000'],
            reporting_manager=cls.super_admin)
        cls.other_emp = User.objects.create_user(
            'emp2@vrl.com', company=cls.vrl, user_code='VRL003', password='p',
            name='Other Employee', role='Employee', modules=['Club 1000'],
            reporting_manager=cls.super_admin)

        cls.scheme = Scheme.objects.create(
            company=cls.vrl, name='RISE', tenure_months=12, min_ticket_size=100000,
            interest_payout_options=['maturity'])

        def inv(author, name, approval):
            return Investor.objects.create(
                company=cls.vrl, scheme=cls.scheme, added_by=author, name=name,
                phone='+9198000007' + str(abs(hash(name)) % 100).zfill(2),
                amount_invested=500000, investment_date='2026-01-01',
                maturity_date='2027-01-01', approval_status=approval)

        cls.emp_approved = inv(cls.employee, 'Employee Approved', 'approved')
        cls.emp_draft = inv(cls.employee, 'Employee Draft', 'draft')
        cls.other_draft = inv(cls.other_emp, 'Other Draft', 'draft')

    def setUp(self):
        self.api = APIClient()

    def _ids(self, user, qs=''):
        self.api.force_authenticate(user=user)
        r = self.api.get(f'/api/club1000/investors/{qs}')
        self.assertEqual(r.status_code, 200, r.data)
        body = r.data
        rows = body['results'] if isinstance(body, dict) else body
        return [i['id'] for i in rows]

    # ── the super admin ──────────────────────────────────────────────────────
    def test_they_see_another_employees_investor(self):
        self.assertIn(self.emp_approved.id, self._ids(self.super_admin))

    def test_a_restrictive_designation_does_not_narrow_them(self):
        """Their designation is scoped to own records; they added none of these."""
        self.assertIn(self.emp_approved.id, self._ids(self.super_admin))

    def test_they_see_other_peoples_drafts_too(self):
        got = self._ids(self.super_admin, '?approval_status=draft')
        self.assertIn(self.emp_draft.id, got)
        self.assertIn(self.other_draft.id, got)

    # ── everyone else is unchanged ───────────────────────────────────────────
    def test_an_employee_still_does_not_see_another_persons_draft(self):
        got = self._ids(self.employee, '?approval_status=draft')
        self.assertIn(self.emp_draft.id, got, 'their own draft should be there')
        self.assertNotIn(self.other_draft.id, got, "another person's draft leaked")

    def test_an_employee_does_not_see_another_persons_investor(self):
        self.assertNotIn(self.emp_approved.id, self._ids(self.other_emp))
