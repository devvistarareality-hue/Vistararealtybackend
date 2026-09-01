"""A hand-added lead belongs to whoever added it.

Previously only self-sourcing roles (telecaller/STM/CP) kept their own lead; an
admin or manager who added one without picking an assignee left it unassigned and
handed it to distribution. Distribution drops any lead whose project has no
available member of the matching designation ("skipped" in _distribute) and throws
that count away on auto-runs, so those leads sat unassigned indefinitely.
"""
from django.core.cache import cache
from rest_framework.test import APITestCase

from companies.models import Company
from accounts.models import User
from sales.models import Lead, Project, UserProjectAssignment

from sales.tests import auth


class CreatorAssignmentTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='CRA', name='Creator Co')
        cls.admin = User.objects.create(
            email='cra_admin@x.com', company=cls.co, role='Admin',
            designation='SALES CLUSTER HEAD', is_staff=True, user_code='CA')
        cls.stm = User.objects.create(
            email='cra_stm@x.com', company=cls.co, role='STM',
            designation='STM', user_code='CS')
        cls.tc = User.objects.create(
            email='cra_tc@x.com', company=cls.co, role='Telecaller',
            designation='TELECALLER', user_code='CT')
        # A project with nobody assigned — exactly the shape that stranded leads.
        cls.orphan = Project.objects.create(company=cls.co, name='Waghodiya')

    def setUp(self):
        cache.clear()

    def _add(self, **over):
        body = {'name': 'Walk In', 'phone': '9111100001', 'project': self.orphan.id}
        body.update(over)
        return self.client.post('/api/sales/leads/', body, format='json')

    def test_admin_added_lead_belongs_to_the_admin(self):
        auth(self.client, self.admin)
        r = self._add()
        self.assertEqual(r.status_code, 201)
        lead = Lead.objects.get(pk=r.data['id'])
        self.assertEqual(lead.stm_id, self.admin.id)
        self.assertIsNotNone(lead.stm_assigned_at)

    def test_it_does_not_sit_unassigned_on_a_project_with_no_team(self):
        """The regression itself: nobody is assigned to `orphan`, so distribution
        could never place this lead."""
        auth(self.client, self.admin)
        lead = Lead.objects.get(pk=self._add().data['id'])
        self.assertFalse(
            lead.telecaller_id is None and lead.stm_id is None,
            'lead was left unassigned on a project distribution cannot route')

    def test_an_explicit_pick_still_wins(self):
        auth(self.client, self.admin)
        r = self._add(stm=self.stm.id)
        lead = Lead.objects.get(pk=r.data['id'])
        self.assertEqual(lead.stm_id, self.stm.id)

    def test_a_telecaller_creator_takes_the_telecaller_slot(self):
        auth(self.client, self.tc)
        lead = Lead.objects.get(pk=self._add().data['id'])
        self.assertEqual(lead.telecaller_id, self.tc.id)
        self.assertIsNone(lead.stm_id)

    def test_an_stm_creator_still_self_sources(self):
        auth(self.client, self.stm)
        lead = Lead.objects.get(pk=self._add().data['id'])
        self.assertEqual(lead.stm_id, self.stm.id)


class BlockedPoolTests(APITestCase):
    """dist-settings must report what distribution can never place."""

    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='BLK', name='Blocked Co')
        cls.admin = User.objects.create(
            email='blk_admin@x.com', company=cls.co, role='Admin',
            is_staff=True, user_code='BA')
        cls.stm = User.objects.create(
            email='blk_stm@x.com', company=cls.co, role='STM',
            designation='STM', user_code='BS')
        cls.covered = Project.objects.create(company=cls.co, name='Covered')
        cls.orphan  = Project.objects.create(company=cls.co, name='Orphan')
        UserProjectAssignment.objects.create(user=cls.stm, project=cls.covered)
        for i, p in enumerate((cls.covered, cls.orphan, cls.orphan)):
            Lead.objects.create(company=cls.co, project=p, status='new',
                                name=f'L{i}', phone=f'922220000{i}')

    def setUp(self):
        cache.clear()
        auth(self.client, self.admin)

    def test_only_the_unroutable_project_is_reported(self):
        r = self.client.get('/api/sales/dist-settings/')
        self.assertEqual(r.status_code, 200)
        blocked = r.data['pending']['blocked']
        self.assertEqual([(b['project'], b['count']) for b in blocked],
                         [('Orphan', 2)])
        self.assertEqual(blocked[0]['needs'], 'STM')
