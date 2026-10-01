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

    def test_an_admin_creating_one_waits_too(self):
        """Reported from production: an admin added a project called "testing do
        not approve" and it went straight to ACTIVE, because an earlier version
        let whoever could approve skip the queue. That made the gate invisible to
        the people most likely to be adding projects. It either applies or it
        does not."""
        self._create(self.admin, 'Admin Made')
        self.assertEqual(Project.objects.get(name='Admin Made').approval_status, 'pending')

    def test_an_admins_new_project_is_not_visible_to_the_floor_either(self):
        self._create(self.admin, 'Admin Made Two')
        self.assertNotIn('Admin Made Two', self._names(self.rep))

    def test_the_creator_is_recorded(self):
        self._create(self.creator, 'Mine')
        self.assertEqual(Project.objects.get(name='Mine').created_by_id, self.creator.id)

    # ── invisible until approved ─────────────────────────────────────────────
    def test_the_sales_floor_does_not_see_a_pending_project(self):
        self._create(self.creator, 'Hidden Until Approved')
        self.assertNotIn('Hidden Until Approved', self._names(self.rep))

    def test_an_approver_sees_it_on_the_screens_that_act_on_it(self):
        """Not on the plain list, which is what the booking flow calls — only
        where they can do something about it."""
        self._create(self.creator, 'Waiting On Me')
        self.assertNotIn('Waiting On Me', self._names(self.approver))

        self.api.force_authenticate(user=self.approver)
        r = self.api.get('/api/sales/projects/?include_unapproved=1')
        self.assertIn('Waiting On Me', {p['name'] for p in r.data})

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


class AnUnapprovedProjectIsAbsentFromTheBookingFlow(TestCase):
    """Reported from production: a project called "testing do not approve" sat
    pending and still appeared in the Booking module — for the very admin who had
    not approved it.

    The first version let anyone who could approve see a pending project
    everywhere. But seeing a project is not the same as it being open for
    business, which is the rule the locked blocks already follow. Only the two
    screens that ACT on one ask for it now.
    """

    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='ABS', name='Absent Co', is_active=True)
        cls.admin = User.objects.create_user(
            'admin@abs.com', company=cls.co, user_code='ABS001', password='p',
            name='Admin', role='Admin', modules=['Sales'])
        cls.director = User.objects.create_user(
            'dir@abs.com', company=cls.co, user_code='ABS002', password='p',
            name='Director', role='Director', modules=['Sales'],
            reporting_manager=cls.admin)
        cls.co.project_approvers = [cls.director.id]
        cls.co.save(update_fields=['project_approvers'])
        cls.pending = Project.objects.create(company=cls.co, name='testing do not approve',
                                             approval_status='pending')
        cls.live = Project.objects.create(company=cls.co, name='Selling Now',
                                          approval_status='approved')

    def setUp(self):
        self.api = APIClient()
        cache.clear()

    def _names(self, user, qs=''):
        self.api.force_authenticate(user=user)
        r = self.api.get(f'/api/sales/projects/{qs}')
        self.assertEqual(r.status_code, 200, r.data)
        return {p['name'] for p in r.data}

    def test_the_booking_flow_does_not_offer_it_to_an_admin(self):
        """The plain list is what the booking screens call."""
        names = self._names(self.admin)
        self.assertNotIn('testing do not approve', names)
        self.assertIn('Selling Now', names)

    def test_nor_to_the_approver_who_has_not_approved_it(self):
        self.assertNotIn('testing do not approve', self._names(self.director))

    def test_the_management_screens_ask_for_it_explicitly(self):
        names = self._names(self.admin, '?include_unapproved=1')
        self.assertIn('testing do not approve', names)

    def test_a_rep_cannot_conjure_it_with_the_same_parameter(self):
        """Otherwise the gate is one query string away from nothing."""
        rep = User.objects.create_user(
            'rep@abs.com', company=self.co, user_code='ABS003', password='p',
            name='Rep', role='Employee', modules=['Sales'], reporting_manager=self.admin)
        self.assertNotIn('testing do not approve', self._names(rep, '?include_unapproved=1'))

    def test_an_approver_can_still_open_it_by_id_to_review_it(self):
        self.api.force_authenticate(user=self.director)
        r = self.api.get(f'/api/sales/projects/{self.pending.id}/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['approval_status'], 'pending')
