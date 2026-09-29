"""Filtering a Not Qualified list down to why.

A lead carries one disqualify_reason whichever stage set it (telecaller or STM),
so the same query parameter narrows either status filter. It is only offered on
the clients beside a Not Qualified status, because no other status has a reason
and filtering on one would silently return nothing.
"""
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from companies.models import Company
from sales.models import Lead, Project


class DisqualifyReasonFilter(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='DQF', name='Reason Co', is_active=True)
        cls.admin = User.objects.create_user(
            'admin@dqf.com', company=cls.co, user_code='DQF001', password='p',
            name='Admin', role='Admin', modules=['Sales'])
        cls.project = Project.objects.create(company=cls.co, name='P1')

        def lead(name, phone, **kw):
            return Lead.objects.create(company=cls.co, name=name, phone=phone,
                                       project=cls.project, **kw)

        cls.budget_tc = lead('TC Budget', '+919800000101',
                             telecaller_status='not_qualified', disqualify_reason='budget')
        cls.caste_tc = lead('TC Caste', '+919800000102',
                            telecaller_status='not_qualified', disqualify_reason='caste')
        cls.budget_stm = lead('STM Budget', '+919800000103',
                              stm_status='not_qualified', disqualify_reason='budget')
        cls.other_stm = lead('STM Other', '+919800000104',
                             stm_status='not_qualified', disqualify_reason='other')
        # Qualified leads carry no reason at all.
        cls.warm = lead('Still Warm', '+919800000105', telecaller_status='warm')

    def setUp(self):
        self.api = APIClient()
        cache.clear()
        self.api.force_authenticate(user=self.admin)

    def _ids(self, qs):
        r = self.api.get(f'/api/sales/leads/?{qs}')
        self.assertEqual(r.status_code, 200, r.data)
        return {l['id'] for l in r.data['results']}

    def test_it_narrows_a_not_qualified_list_to_one_reason(self):
        got = self._ids('telecaller_status=not_qualified&disqualify_reason=budget')
        self.assertEqual(got, {self.budget_tc.id})

    def test_it_works_off_the_stm_status_filter_too(self):
        """One reason per lead whichever stage set it — no TC/STM split needed."""
        got = self._ids('stm_status=not_qualified&disqualify_reason=budget')
        self.assertEqual(got, {self.budget_stm.id})

    def test_without_a_reason_every_not_qualified_lead_is_listed(self):
        got = self._ids('telecaller_status=not_qualified')
        self.assertEqual(got, {self.budget_tc.id, self.caste_tc.id})

    def test_each_reason_selects_only_its_own(self):
        for lead, reason in ((self.caste_tc, 'caste'), (self.other_stm, 'other')):
            with self.subTest(reason=reason):
                got = self._ids(f'disqualify_reason={reason}')
                self.assertEqual(got, {lead.id})

    def test_a_qualified_lead_never_matches_a_reason(self):
        for reason in ('budget', 'caste', 'religion', 'other'):
            with self.subTest(reason=reason):
                self.assertNotIn(self.warm.id, self._ids(f'disqualify_reason={reason}'))

    def test_an_unused_reason_returns_nothing_rather_than_everything(self):
        """An unmatched filter must narrow to empty, not be quietly ignored."""
        self.assertEqual(self._ids('disqualify_reason=religion'), set())
