"""The History button on a follow-up you were assigned must actually open.

A follow-up can be assigned to someone who is neither the lead's telecaller nor
its STM — a colleague covering the call. Follow-Ups shows them that row, with
the lead's name and number on it and a History button beside it. That button
fetches the lead, and lead reads are scoped to telecaller/STM, so for the
person actually making the call it answered 404: "Could not load the history".

The fix widens reading only. Being told to call someone is a reason to see what
happened on that lead before you dial; it is not a reason to be able to edit or
delete it, and these tests pin that line — a widened GET next to a PATCH and a
DELETE that must both still refuse.
"""
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from companies.models import Company
from sales.models import FollowUp, Lead, Project, SiteVisit


class FollowUpHistoryAccessTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='FHA', name='History Co', is_active=True)
        cls.other_co = Company.objects.create(code='FHB', name='Elsewhere', is_active=True)

        def emp(email, code, name, co=None):
            return User.objects.create_user(
                email, company=co or cls.co, user_code=code, password='p',
                name=name, role='Employee', designation='STM', modules=['Sales'])

        cls.owner = emp('owner@fha.com', 'FHA001', 'Lead Owner')
        cls.caller = emp('caller@fha.com', 'FHA002', 'Covering Caller')
        cls.stranger = emp('stranger@fha.com', 'FHA003', 'Unrelated Person')
        cls.visitor = emp('visitor@fha.com', 'FHA004', 'Site Visit STM')
        cls.outsider = emp('out@fhb.com', 'FHB001', 'Other Company', co=cls.other_co)

        cls.project = Project.objects.create(company=cls.co, name='Pratishtha')
        cls.lead = Lead.objects.create(
            company=cls.co, name='Yogesh Raykwar', phone='+919265011432',
            project=cls.project, telecaller=cls.owner, stm=cls.owner)

        # Assigned to someone who does not own the lead — the whole case.
        FollowUp.objects.create(
            lead=cls.lead, assigned_to=cls.caller, scheduled_at=timezone.now(),
            role_context='stm', created_by=cls.owner)
        SiteVisit.objects.create(
            lead=cls.lead, project=cls.project, stm=cls.visitor,
            scheduled_at=timezone.now())

    def _get(self, user):
        api = APIClient()
        api.force_authenticate(user=user)
        return api.get(f'/api/sales/leads/{self.lead.pk}/')

    # ------------------------------------------------------------------ reading

    def test_the_assigned_caller_can_open_the_history(self):
        r = self._get(self.caller)
        self.assertEqual(r.status_code, 200, 'the History button is dead for the person making the call')
        self.assertEqual(r.data['name'], 'Yogesh Raykwar')
        self.assertIn('history', r.data)

    def test_the_stm_on_a_site_visit_can_open_it_too(self):
        # Same situation, other activity: shown the visit, so shown its history.
        self.assertEqual(self._get(self.visitor).status_code, 200)

    def test_the_owner_still_can(self):
        self.assertEqual(self._get(self.owner).status_code, 200)

    # ------------------------------------------------- what must still be refused

    def test_someone_with_no_connection_to_the_lead_still_cannot(self):
        """The widening is 'you were assigned something on this lead', not
        'anyone in the company'."""
        self.assertEqual(self._get(self.stranger).status_code, 404)

    def test_another_company_still_cannot(self):
        self.assertEqual(self._get(self.outsider).status_code, 404)

    def test_the_assigned_caller_cannot_edit_the_lead(self):
        """The line this fix must not cross. Being told to call someone lets
        you read what happened; it does not hand you the lead."""
        api = APIClient()
        api.force_authenticate(user=self.caller)
        r = api.patch(f'/api/sales/leads/{self.lead.pk}/', {'status': 'lost'}, format='json')
        self.assertEqual(r.status_code, 404)
        self.lead.refresh_from_db()
        self.assertNotEqual(self.lead.status, 'lost')

    def test_the_assigned_caller_cannot_delete_the_lead(self):
        api = APIClient()
        api.force_authenticate(user=self.caller)
        r = api.delete(f'/api/sales/leads/{self.lead.pk}/')
        self.assertIn(r.status_code, (403, 404))
        self.assertTrue(Lead.objects.filter(pk=self.lead.pk).exists())

    def test_a_finished_follow_up_still_counts(self):
        """Access follows having been assigned the work, not whether it is
        still open — the history of a call you made last week is the thing you
        are most likely to go back and read."""
        FollowUp.objects.filter(lead=self.lead, assigned_to=self.caller).update(
            status='completed', completed_at=timezone.now())
        self.assertEqual(self._get(self.caller).status_code, 200)

    def test_the_lead_is_returned_once_despite_two_assignments(self):
        # Two joins in one filter can duplicate rows; .first() on a detail view
        # would hide it, so assert the shape the caller actually gets.
        FollowUp.objects.create(
            lead=self.lead, assigned_to=self.caller, scheduled_at=timezone.now(),
            role_context='stm', created_by=self.owner)
        r = self._get(self.caller)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['id'], self.lead.pk)
