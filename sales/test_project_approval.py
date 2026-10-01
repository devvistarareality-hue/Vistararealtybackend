"""A new project does not exist until someone signs it off.

Creating a project used to make it instantly real: pickable, bookable, on the
unit map. It now waits for a named approver, and until then it is invisible
exactly where a locked project is invisible — the two answer the same question,
is this project open for business.

The approvers are configured once for the company rather than per project,
because a project does not exist yet when it needs approving, so there is
nothing to scope the choice to.
"""
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from companies.models import Company
from sales.models import Project


class ProjectApproval(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='PAP', name='Approve Co', is_active=True)
        cls.admin = User.objects.create_user(
            'admin@pap.com', company=cls.co, user_code='PAP001', password='p',
            name='Admin', role='Admin', modules=['Sales'])
        cls.approver = User.objects.create_user(
            'appr@pap.com', company=cls.co, user_code='PAP002', password='p',
            name='Approver', role='Manager', modules=['Sales'],
            reporting_manager=cls.admin)
        cls.creator = User.objects.create_user(
            'mgr@pap.com', company=cls.co, user_code='PAP003', password='p',
            name='Creator', role='Manager', modules=['Sales'],
            reporting_manager=cls.admin)
        cls.rep = User.objects.create_user(
            'rep@pap.com', company=cls.co, user_code='PAP004', password='p',
            name='Rep', role='Employee', modules=['Sales'],
            reporting_manager=cls.admin)
        cls.co.project_approvers = [cls.approver.id]
        cls.co.save(update_fields=['project_approvers'])

        cls.live = Project.objects.create(company=cls.co, name='Already Selling',
                                          approval_status='approved')

    def setUp(self):
        self.api = APIClient()
        cache.clear()

    def _names(self, user):
        self.api.force_authenticate(user=user)
        r = self.api.get('/api/sales/projects/')
        self.assertEqual(r.status_code, 200, r.data)
        return {p['name'] for p in r.data}

    def _create(self, user, name):
        self.api.force_authenticate(user=user)
        return self.api.post('/api/sales/projects/', {'name': name}, format='json')

    # ── creating ─────────────────────────────────────────────────────────────
    def test_a_manager_creating_one_leaves_it_pending(self):
        r = self._create(self.creator, 'Needs A Nod')
        self.assertIn(r.status_code, (200, 201), r.data)
        self.assertEqual(Project.objects.get(name='Needs A Nod').approval_status, 'pending')

    def test_an_admin_creating_one_approves_it_in_the_same_breath(self):
        """Asking the person who may approve to approve their own creation is
        ceremony, not control."""
        r = self._create(self.admin, 'Admin Made')
        self.assertEqual(Project.objects.get(name='Admin Made').approval_status, 'approved')

    def test_the_creator_is_recorded(self):
        self._create(self.creator, 'Mine')
        self.assertEqual(Project.objects.get(name='Mine').created_by_id, self.creator.id)

    # ── invisible until approved ─────────────────────────────────────────────
    def test_the_sales_floor_does_not_see_a_pending_project(self):
        self._create(self.creator, 'Hidden Until Approved')
        self.assertNotIn('Hidden Until Approved', self._names(self.rep))

    def test_an_approver_does_see_it_so_they_can_act(self):
        self._create(self.creator, 'Waiting On Me')
        self.assertIn('Waiting On Me', self._names(self.approver))

    def test_approved_projects_are_unaffected(self):
        self.assertIn('Already Selling', self._names(self.rep))

    def test_a_pending_project_cannot_be_booked_even_by_an_approver(self):
        """Seeing it is not the same as it being open for business."""
        self._create(self.creator, 'Not Yet Sellable')
        proj = Project.objects.get(name='Not Yet Sellable')
        self.api.force_authenticate(user=self.approver)
        r = self.api.post('/api/sales/bookings/', {
            'project': proj.id, 'client_name': 'X', 'phone': '+919800000901',
        }, format='json')
        self.assertEqual(r.status_code, 403)
        self.assertIn('approved', str(r.data).lower())

    # ── approving ────────────────────────────────────────────────────────────
    def test_approving_makes_it_visible_to_everyone(self):
        self._create(self.creator, 'About To Launch')
        proj = Project.objects.get(name='About To Launch')
        self.api.force_authenticate(user=self.approver)
        r = self.api.post(f'/api/sales/projects/{proj.id}/approval/',
                          {'action': 'approve'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIn('About To Launch', self._names(self.rep))

    def test_rejecting_keeps_it_hidden_and_records_why(self):
        self._create(self.creator, 'Refused')
        proj = Project.objects.get(name='Refused')
        self.api.force_authenticate(user=self.approver)
        r = self.api.post(f'/api/sales/projects/{proj.id}/approval/',
                          {'action': 'reject', 'reason': 'RERA number missing'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        proj.refresh_from_db()
        self.assertEqual(proj.approval_status, 'rejected')
        self.assertEqual(proj.rejected_reason, 'RERA number missing')
        self.assertNotIn('Refused', self._names(self.rep))

    def test_a_rejected_project_is_kept_not_deleted(self):
        """Whoever created it should be able to read why and fix it."""
        self._create(self.creator, 'Fixable')
        proj = Project.objects.get(name='Fixable')
        self.api.force_authenticate(user=self.approver)
        self.api.post(f'/api/sales/projects/{proj.id}/approval/',
                      {'action': 'reject', 'reason': 'wrong location'}, format='json')
        self.assertTrue(Project.objects.filter(pk=proj.pk).exists())

    def test_someone_who_is_not_an_approver_cannot_approve(self):
        self._create(self.creator, 'Not Yours To Approve')
        proj = Project.objects.get(name='Not Yours To Approve')
        self.api.force_authenticate(user=self.rep)
        r = self.api.post(f'/api/sales/projects/{proj.id}/approval/',
                          {'action': 'approve'}, format='json')
        self.assertEqual(r.status_code, 403)
        self.assertEqual(Project.objects.get(pk=proj.pk).approval_status, 'pending')

    def test_it_cannot_be_approved_twice(self):
        self._create(self.creator, 'Once Only')
        proj = Project.objects.get(name='Once Only')
        self.api.force_authenticate(user=self.approver)
        self.api.post(f'/api/sales/projects/{proj.id}/approval/', {'action': 'approve'}, format='json')
        again = self.api.post(f'/api/sales/projects/{proj.id}/approval/', {'action': 'approve'}, format='json')
        self.assertEqual(again.status_code, 400)

    # ── who approves ─────────────────────────────────────────────────────────
    def test_an_admin_can_set_the_approver_list(self):
        self.api.force_authenticate(user=self.admin)
        r = self.api.patch('/api/sales/projects/approvers/',
                           {'approvers': [self.creator.id]}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.co.refresh_from_db()
        self.assertEqual(self.co.project_approvers, [self.creator.id])

    def test_a_rep_cannot_make_themselves_an_approver(self):
        self.api.force_authenticate(user=self.rep)
        r = self.api.patch('/api/sales/projects/approvers/',
                           {'approvers': [self.rep.id]}, format='json')
        self.assertEqual(r.status_code, 403)

    def _people(self):
        self.api.force_authenticate(user=self.admin)
        r = self.api.get('/api/sales/projects/approvers/')
        self.assertEqual(r.status_code, 200, r.data)
        return {p['name'] for p in r.data['people']}

    def test_the_picker_offers_directors(self):
        """The list to choose FROM, not the list already chosen — returning only
        the selected ids left the picker with nothing in it."""
        director = User.objects.create_user(
            'dir@pap.com', company=self.co, user_code='PAP005', password='p',
            name='A Director', role='Director', modules=['Sales'])
        self.assertIn('A Director', self._people())

    def test_it_offers_nobody_else(self):
        """Authorising a project to exist is a board-level call. Not a manager,
        not a rep — and not an administrator either, who can already approve
        without being appointed."""
        people = self._people()
        for name in ('Rep', 'Creator', 'Approver', 'Admin'):
            self.assertNotIn(name, people)

    def test_an_inactive_director_is_not_offered(self):
        User.objects.create_user(
            'gone@pap.com', company=self.co, user_code='PAP006', password='p',
            name='Former Director', role='Director', modules=['Sales'], is_active=False)
        self.assertNotIn('Former Director', self._people())

    def test_a_director_at_another_company_is_not_offered(self):
        other = Company.objects.create(code='OT2', name='Other Two', is_active=True)
        User.objects.create_user(
            'd@ot2.com', company=other, user_code='OT2001', password='p',
            name='Their Director', role='Director', modules=['Sales'])
        self.assertNotIn('Their Director', self._people())

    def test_an_administrator_can_still_approve_without_being_in_the_list(self):
        """The floor beneath the list: a company that has named nobody must not
        be stuck with projects it can never release."""
        self.co.project_approvers = []
        self.co.save(update_fields=['project_approvers'])
        self._create(self.creator, 'Nobody Appointed')
        proj = Project.objects.get(name='Nobody Appointed')
        self.api.force_authenticate(user=self.admin)
        r = self.api.post(f'/api/sales/projects/{proj.id}/approval/',
                          {'action': 'approve'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)

    def test_an_outsider_cannot_be_parked_in_the_list(self):
        other_co = Company.objects.create(code='OTH', name='Other', is_active=True)
        outsider = User.objects.create_user('x@oth.com', company=other_co, user_code='OTH001',
                                            password='p', name='Outsider', role='Admin')
        self.api.force_authenticate(user=self.admin)
        r = self.api.patch('/api/sales/projects/approvers/',
                           {'approvers': [outsider.id]}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data['approvers'], [])
