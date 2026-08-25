"""Transferring a lead from one STM to another, held until an approver signs it off.

The whole point is that a rep cannot move work off their own name — or onto someone
else's — unilaterally. So the tests are mostly about who may do what, and about the
lead NOT moving until the moment it is approved.
"""
from django.core.cache import cache
from rest_framework.test import APITestCase

from companies.models import Company
from accounts.models import User
from sales.models import Lead, Project, LeadTransfer
from sales.tests import auth


class LeadTransferTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.co = Company.objects.create(code='LTR', name='Transfer Co')
        self.a = User.objects.create(email='a@x.com', company=self.co, role='Sales',
                                     user_code='A1', designation='STM')
        self.b = User.objects.create(email='b@x.com', company=self.co, role='Sales',
                                     user_code='B1', designation='STM')
        self.approver = User.objects.create(email='ap@x.com', company=self.co, role='Manager',
                                            user_code='M1', designation='Manager')
        self.other_mgr = User.objects.create(email='om@x.com', company=self.co, role='Manager',
                                             user_code='M2', designation='Manager')
        self.admin = User.objects.create(email='ad@x.com', company=self.co, role='Admin',
                                         user_code='AD', is_staff=True)
        self.proj = Project.objects.create(company=self.co, name='Tundav', is_active=True,
                                           booking_approvers=[self.approver.id])
        self.lead = Lead.objects.create(company=self.co, name='Ramesh', phone='9800000001',
                                        status='new', project=self.proj, stm=self.a)

    def tearDown(self):
        cache.clear()

    def _request(self, user=None, lead=None, to=None):
        auth(self.client, user or self.a)
        return self.client.post('/api/sales/lead-transfers/',
                                {'lead': (lead or self.lead).id, 'to_stm': (to or self.b).id,
                                 'reason': 'moving territory'}, format='json')

    def _act(self, t, action, user=None):
        auth(self.client, user or self.approver)
        return self.client.post('/api/sales/lead-transfers/%d/action/' % t.id,
                                {'action': action}, format='json')

    # ── raising a request ────────────────────────────────────────────────────
    def test_the_lead_does_not_move_when_the_request_is_raised(self):
        res = self._request()
        self.assertEqual(res.status_code, 201, res.data)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.stm_id, self.a.id, 'lead must stay put until approved')
        self.assertEqual(LeadTransfer.objects.get().status, 'pending')

    def test_a_rep_cannot_give_away_a_lead_that_is_not_theirs(self):
        res = self._request(user=self.b)          # b does not hold this lead
        self.assertEqual(res.status_code, 403)
        self.assertFalse(LeadTransfer.objects.exists())

    def test_cannot_transfer_to_the_current_owner(self):
        res = self._request(to=self.a)
        self.assertEqual(res.status_code, 400)

    def test_cannot_transfer_to_someone_in_another_company(self):
        other_co = Company.objects.create(code='OTH', name='Other')
        outsider = User.objects.create(email='out@x.com', company=other_co, role='Sales',
                                       user_code='O1', designation='STM')
        res = self._request(to=outsider)
        self.assertEqual(res.status_code, 404)

    def test_only_one_open_request_per_lead(self):
        self.assertEqual(self._request().status_code, 201)
        second = self._request()
        self.assertEqual(second.status_code, 409)
        self.assertEqual(LeadTransfer.objects.count(), 1)

    # ── approving ────────────────────────────────────────────────────────────
    def test_approval_moves_the_lead(self):
        self._request()
        t = LeadTransfer.objects.get()
        res = self._act(t, 'approve')
        self.assertEqual(res.status_code, 200, res.data)
        self.lead.refresh_from_db(); t.refresh_from_db()
        self.assertEqual(self.lead.stm_id, self.b.id)
        self.assertEqual(t.status, 'approved')
        self.assertEqual(t.decided_by_id, self.approver.id)
        self.assertIsNotNone(t.decided_at)
        self.assertIsNotNone(self.lead.stm_assigned_at)

    def test_a_manager_not_named_on_the_project_cannot_approve(self):
        """Being a manager is not authority — the project names its approvers."""
        self._request()
        t = LeadTransfer.objects.get()
        res = self._act(t, 'approve', user=self.other_mgr)
        self.assertEqual(res.status_code, 403)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.stm_id, self.a.id, 'lead must not have moved')

    def test_neither_rep_can_approve_their_own_transfer(self):
        self._request()
        t = LeadTransfer.objects.get()
        self.assertEqual(self._act(t, 'approve', user=self.a).status_code, 403)
        self.assertEqual(self._act(t, 'approve', user=self.b).status_code, 403)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.stm_id, self.a.id)

    def test_an_admin_can_approve_any_project(self):
        self._request()
        t = LeadTransfer.objects.get()
        self.assertEqual(self._act(t, 'approve', user=self.admin).status_code, 200)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.stm_id, self.b.id)

    def test_rejection_leaves_the_lead_where_it_was(self):
        self._request()
        t = LeadTransfer.objects.get()
        self.assertEqual(self._act(t, 'reject').status_code, 200)
        self.lead.refresh_from_db(); t.refresh_from_db()
        self.assertEqual(self.lead.stm_id, self.a.id)
        self.assertEqual(t.status, 'rejected')

    def test_a_decided_request_cannot_be_decided_again(self):
        self._request()
        t = LeadTransfer.objects.get()
        self._act(t, 'approve')
        self.assertEqual(self._act(t, 'approve').status_code, 409)
        self.assertEqual(self._act(t, 'reject').status_code, 409)

    def test_the_requester_can_withdraw_but_nobody_else_can(self):
        self._request()
        t = LeadTransfer.objects.get()
        self.assertEqual(self._act(t, 'cancel', user=self.b).status_code, 403)
        self.assertEqual(self._act(t, 'cancel', user=self.a).status_code, 200)
        t.refresh_from_db()
        self.assertEqual(t.status, 'cancelled')
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.stm_id, self.a.id)

    def test_a_project_with_no_named_approver_is_admin_only(self):
        bare = Project.objects.create(company=self.co, name='Unset', is_active=True,
                                      booking_approvers=[])
        lead = Lead.objects.create(company=self.co, name='X', phone='9800000002',
                                   status='new', project=bare, stm=self.a)
        self._request(lead=lead)
        t = LeadTransfer.objects.get(lead=lead)
        self.assertEqual(self._act(t, 'approve', user=self.approver).status_code, 403)
        self.assertEqual(self._act(t, 'approve', user=self.admin).status_code, 200)

    # ── visibility ───────────────────────────────────────────────────────────
    def test_the_queue_shows_an_approver_their_projects_and_a_rep_their_own(self):
        self._request()
        auth(self.client, self.approver)
        self.assertEqual(len(self.client.get('/api/sales/lead-transfers/').data), 1)
        auth(self.client, self.a)
        self.assertEqual(len(self.client.get('/api/sales/lead-transfers/').data), 1)
        auth(self.client, self.other_mgr)
        self.assertEqual(len(self.client.get('/api/sales/lead-transfers/').data), 0,
                         'a manager with no claim on the project should see nothing')

    def test_another_company_never_sees_the_request(self):
        self._request()
        other_co = Company.objects.create(code='OTH', name='Other')
        outsider = User.objects.create(email='out2@x.com', company=other_co, role='Admin',
                                       user_code='O2', is_staff=True)
        auth(self.client, outsider)
        self.assertEqual(len(self.client.get('/api/sales/lead-transfers/').data), 0)
