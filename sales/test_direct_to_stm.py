"""A project with no telecaller assigned routes new leads straight to an STM.

The rule is derived from the assignments, not a setting: assign a telecaller and
the project rejoins the telecaller flow by itself.
"""
from datetime import date

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from accounts.models import User
from companies.models import Company
from sales.models import (Lead, LeadSource, Project, UserAvailability,
                          UserProjectAssignment)
from sales.views import _run_distribution


class SkipTelecaller(TestCase):
    def setUp(self):
        cache.clear()
        self.co = Company.objects.create(code='SK', name='Skip Co')
        self.src = LeadSource.objects.create(company=self.co, name='meta')
        # one project with a telecalling team, one without
        self.normal = Project.objects.create(company=self.co, name='Normal')
        self.direct = Project.objects.create(company=self.co, name='Direct')
        # the telecaller covers only 'normal'; 'direct' has nobody from telecalling
        self.tc = self._member('tc@x.com', 'TC1', 'TELECALLER', [self.normal])
        self.stm = self._member('stm@x.com', 'ST1', 'STM', [self.normal, self.direct])

    def _member(self, email, code, desig, projects):
        u = User.objects.create(name=code, email=email, phone='90000' + code,
                                user_code=code, role='Employee', company=self.co,
                                designation=desig)
        for p in projects:
            UserProjectAssignment.objects.create(user=u, project=p)
        UserAvailability.objects.create(user=u, date=date.today(), is_available=True,
                                        checked_in_at=timezone.now())
        return u

    def _lead(self, project, name='L'):
        return Lead.objects.create(company=self.co, name=name, phone='9' + str(abs(hash(name)) % 10**9),
                                   status='new', project=project, source=self.src)

    def test_direct_project_lead_goes_to_the_stm_not_the_telecaller(self):
        lead = self._lead(self.direct, 'direct-lead')
        _run_distribution(self.co, 'telecaller')
        lead.refresh_from_db()
        self.assertIsNone(lead.telecaller_id, 'a telecaller-less project must not get a telecaller')
        self.assertEqual(lead.stm_id, self.stm.id)
        self.assertEqual(lead.status, 'assigned')

    def test_normal_project_still_goes_to_the_telecaller_first(self):
        lead = self._lead(self.normal, 'normal-lead')
        _run_distribution(self.co, 'telecaller')
        lead.refresh_from_db()
        self.assertEqual(lead.telecaller_id, self.tc.id)
        self.assertIsNone(lead.stm_id)
        self.assertEqual(lead.status, 'assigned')

    def test_both_kinds_are_routed_in_one_run(self):
        a, b = self._lead(self.direct, 'd2'), self._lead(self.normal, 'n2')
        _run_distribution(self.co, 'telecaller')
        a.refresh_from_db(); b.refresh_from_db()
        self.assertEqual((a.stm_id, a.telecaller_id), (self.stm.id, None))
        self.assertEqual((b.telecaller_id, b.stm_id), (self.tc.id, None))

    def test_direct_lead_still_reaches_an_stm_when_no_telecaller_is_available(self):
        """The whole point: these leads never needed a telecaller."""
        UserAvailability.objects.filter(user=self.tc).delete()
        lead = self._lead(self.direct, 'no-tc')
        _run_distribution(self.co, 'telecaller')
        lead.refresh_from_db()
        self.assertEqual(lead.stm_id, self.stm.id)

    def test_nothing_happens_twice(self):
        lead = self._lead(self.direct, 'once')
        _run_distribution(self.co, 'telecaller')
        lead.refresh_from_db()
        first = lead.stm_id
        _run_distribution(self.co, 'telecaller')
        lead.refresh_from_db()
        self.assertEqual(lead.stm_id, first)

    def test_warm_transferred_leads_are_unaffected(self):
        lead = self._lead(self.normal, 'warm')
        lead.status = 'warm_transferred'; lead.telecaller = self.tc; lead.save()
        _run_distribution(self.co, 'stm')
        lead.refresh_from_db()
        self.assertEqual(lead.stm_id, self.stm.id)
        self.assertEqual(lead.status, 'warm_transferred', 'existing status must not be rewritten')

    def test_assigning_a_telecaller_returns_the_project_to_the_telecaller_flow(self):
        UserProjectAssignment.objects.create(user=self.tc, project=self.direct)
        lead = self._lead(self.direct, 'now-covered')
        _run_distribution(self.co, 'telecaller')
        lead.refresh_from_db()
        self.assertEqual(lead.telecaller_id, self.tc.id, 'a covered project must use the telecaller')
        self.assertIsNone(lead.stm_id)

    def test_removing_the_last_telecaller_sends_the_project_direct_again(self):
        UserProjectAssignment.objects.filter(user=self.tc, project=self.normal).delete()
        lead = self._lead(self.normal, 'uncovered')
        _run_distribution(self.co, 'telecaller')
        lead.refresh_from_db()
        self.assertEqual(lead.stm_id, self.stm.id)
        self.assertIsNone(lead.telecaller_id)

    def test_an_inactive_telecaller_does_not_count_as_cover(self):
        UserProjectAssignment.objects.create(user=self.tc, project=self.direct)
        self.tc.is_active = False
        self.tc.save(update_fields=['is_active'])
        lead = self._lead(self.direct, 'inactive-tc')
        _run_distribution(self.co, 'telecaller')
        lead.refresh_from_db()
        self.assertEqual(lead.stm_id, self.stm.id)

    def test_a_lead_with_no_project_is_still_left_alone(self):
        lead = self._lead(None, 'no-project')
        _run_distribution(self.co, 'telecaller')
        lead.refresh_from_db()
        self.assertIsNone(lead.stm_id)
        self.assertIsNone(lead.telecaller_id)
        self.assertEqual(lead.status, 'new')
