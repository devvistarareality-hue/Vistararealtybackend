"""Who can see a draft booking in Approvals.

Drafts are half-finished commercial terms, so they were private to their author —
which left an approver looking at a plot map full of drafted units and a Drafts tab
saying "no bookings here". They are now visible to the author, to a real admin, and
to whoever approves that project's bookings: the same people who can already cancel
a drafted unit from the map.

Which approver list governs follows the booking's own routing, as approve and reject
do — a CP-sourced draft answers to the CP list, not the regular one.
"""
from django.core.cache import cache
from rest_framework.test import APITestCase

from companies.models import Company
from accounts.models import User
from sales.models import Booking, Project

from sales.tests import auth


class DraftVisibilityTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='DRV', name='Draft Co')
        cls.admin = User.objects.create(email='drv_admin@x.com', company=cls.co,
                                        role='Admin', is_staff=True, user_code='V0')
        cls.author = User.objects.create(email='drv_stm@x.com', company=cls.co,
                                         role='STM', designation='STM', user_code='V1')
        cls.approver = User.objects.create(email='drv_mgr@x.com', company=cls.co,
                                           role='Manager', user_code='V2')
        cls.cp_approver = User.objects.create(email='drv_cp@x.com', company=cls.co,
                                              role='Manager', user_code='V3')
        cls.bystander = User.objects.create(email='drv_other@x.com', company=cls.co,
                                            role='Manager', user_code='V4')
        cls.project = Project.objects.create(
            company=cls.co, name='Tower', booking_approvers=[cls.approver.id],
            cp_booking_approvers=[cls.cp_approver.id])
        cls.draft = Booking.objects.create(
            company=cls.co, project=cls.project, stm=cls.author, status='draft',
            client_name='Sales Draft', phone='9000000001')
        cls.cp_draft = Booking.objects.create(
            company=cls.co, project=cls.project, stm=cls.author, status='draft',
            source='Channel Partner', client_name='CP Draft', phone='9000000002')

    def setUp(self):
        cache.clear()

    def _drafts_for(self, user):
        auth(self.client, user)
        r = self.client.get('/api/sales/bookings/?status=draft')
        self.assertEqual(r.status_code, 200)
        return sorted(b['client_name'] for b in r.data)

    def test_the_author_sees_their_own(self):
        self.assertEqual(self._drafts_for(self.author), ['CP Draft', 'Sales Draft'])

    def test_an_admin_sees_every_draft(self):
        self.assertEqual(self._drafts_for(self.admin), ['CP Draft', 'Sales Draft'])

    def test_the_projects_approver_sees_its_sales_draft(self):
        self.assertIn('Sales Draft', self._drafts_for(self.approver))

    def test_a_sales_approver_does_not_see_the_cp_draft(self):
        """The two approver lists exist to separate CP from regular — this must not
        quietly reunite them."""
        self.assertNotIn('CP Draft', self._drafts_for(self.approver))

    def test_the_cp_approver_sees_the_cp_draft_only(self):
        self.assertEqual(self._drafts_for(self.cp_approver), ['CP Draft'])

    def test_a_manager_who_approves_nothing_here_sees_none(self):
        """Being a manager is not itself authority, same as approve/reject/cancel."""
        self.assertEqual(self._drafts_for(self.bystander), [])

    def test_drafts_still_stay_out_of_the_all_tab_for_outsiders(self):
        auth(self.client, self.bystander)
        r = self.client.get('/api/sales/bookings/')
        self.assertNotIn('Sales Draft', [b['client_name'] for b in r.data])
        self.assertNotIn('CP Draft', [b['client_name'] for b in r.data])


class CpUserSeesOwnDraftTests(APITestCase):
    """A CP-designated user's list is filtered to CP-sourced bookings. A draft is
    uncategorised until a lead or Source says otherwise, so that filter was hiding a
    CP rep's own draft from their own list — resuming it loaded nothing and the form
    sat on "Loading unit pricing…" with no project, no unit and no pricing.
    """

    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='CPD', name='CP Draft Co')
        cls.cp = User.objects.create(email='cpd_cp@x.com', company=cls.co, role='Manager',
                                     designation='CP CLUSTER HEAD', user_code='C1')
        cls.other = User.objects.create(email='cpd_other@x.com', company=cls.co, role='STM',
                                        designation='STM', user_code='C2')
        cls.project = Project.objects.create(company=cls.co, name='Kalrav')
        # exactly the shape of the real one: saved before any Source was chosen
        cls.own = Booking.objects.create(
            company=cls.co, project=cls.project, stm=cls.cp, status='draft',
            source='walk-in', client_name='Own Draft', phone='9000000010')
        cls.own_cp = Booking.objects.create(
            company=cls.co, project=cls.project, stm=cls.cp, status='draft',
            source='Channel Partner', client_name='Own CP Draft', phone='9000000011')
        cls.someone_elses = Booking.objects.create(
            company=cls.co, project=cls.project, stm=cls.other, status='draft',
            source='walk-in', client_name='Other Draft', phone='9000000012')

    def setUp(self):
        cache.clear()
        auth(self.client, self.cp)

    def _names(self, url='/api/sales/bookings/?status=draft'):
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        return sorted(b['client_name'] for b in r.data)

    def test_a_cp_user_sees_their_own_non_cp_draft(self):
        """The regression: resume needs to find it, whatever its Source says."""
        self.assertIn('Own Draft', self._names())

    def test_an_approver_still_finds_their_own_draft_with_mine(self):
        """Being an approver narrows the list to the projects you approve, which hid
        the approver's own draft from themselves — the form then loaded nothing and
        sat on "Loading unit pricing…". The resume fetch asks for `mine=1`, which
        skips that narrowing, so this is the query it actually makes."""
        self.project.cp_booking_approvers = [self.cp.id]
        self.project.save(update_fields=['cp_booking_approvers'])
        other_project = Project.objects.create(company=self.co, name='Not Mine')
        mine_elsewhere = Booking.objects.create(
            company=self.co, project=other_project, stm=self.cp, status='draft',
            source='walk-in', client_name='Draft Elsewhere', phone='9000000020')

        # without mine=1 the approver scoping drops it
        self.assertNotIn('Draft Elsewhere', self._names('/api/sales/bookings/?status=draft'))
        # with mine=1 — what the booking form sends — it comes back
        self.assertIn('Draft Elsewhere',
                      self._names('/api/sales/bookings/?status=draft&mine=1'))
        self.assertTrue(Booking.objects.filter(pk=mine_elsewhere.pk).exists())

    def test_and_still_sees_their_cp_draft(self):
        self.assertIn('Own CP Draft', self._names())

    def test_the_exemption_does_not_leak_anyone_elses(self):
        self.assertNotIn('Other Draft', self._names())

    def test_own_submitted_non_cp_bookings_are_visible_too(self):
        """Source records where the client came from, not who did the paperwork — a
        CP rep booking a walk-in still needs it in their own list."""
        Booking.objects.create(company=self.co, project=self.project, stm=self.cp,
                               status='pending', source='walk-in',
                               client_name='Own Submitted', phone='9000000013')
        self.assertIn('Own Submitted', self._names('/api/sales/bookings/?mine=1'))

    def test_someone_elses_non_cp_booking_is_still_filtered_out(self):
        """The exemption is stm=self only — the CP pool is otherwise unchanged."""
        Booking.objects.create(company=self.co, project=self.project, stm=self.other,
                               status='pending', source='walk-in',
                               client_name='Other Submitted', phone='9000000014')
        self.assertNotIn('Other Submitted', self._names('/api/sales/bookings/'))


class BookingDetailAccessTests(APITestCase):
    """Opening one booking by id, which is how resuming a draft loads it.

    Listing and searching made resuming hostage to the list's scoping — approver
    narrowing and the CP pool filter each silently returned nothing, leaving the form
    blank on "Loading unit pricing…". Fetching the record by id answers a plain
    question with a plain answer.
    """

    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='BDT', name='Detail Co')
        cls.other_co = Company.objects.create(code='BDX', name='Other Co')
        cls.admin = User.objects.create(email='bdt_admin@x.com', company=cls.co,
                                        role='Admin', is_staff=True, user_code='B0')
        cls.author = User.objects.create(email='bdt_stm@x.com', company=cls.co,
                                         role='STM', designation='STM', user_code='B1')
        cls.approver = User.objects.create(email='bdt_mgr@x.com', company=cls.co,
                                           role='Manager', user_code='B2')
        cls.stranger = User.objects.create(email='bdt_x@x.com', company=cls.co,
                                           role='STM', designation='STM', user_code='B3')
        cls.outsider = User.objects.create(email='bdt_out@x.com', company=cls.other_co,
                                           role='Admin', is_staff=True, user_code='B4')
        cls.project = Project.objects.create(company=cls.co, name='Tower',
                                             booking_approvers=[cls.approver.id])
        cls.draft = Booking.objects.create(
            company=cls.co, project=cls.project, stm=cls.author, status='draft',
            source='walk-in', client_name='Draft Client', phone='9000000030')

    def setUp(self):
        cache.clear()

    def _get(self, user):
        auth(self.client, user)
        return self.client.get('/api/sales/bookings/%d/' % self.draft.id)

    def test_the_author_can_open_it(self):
        self.assertEqual(self._get(self.author).status_code, 200)

    def test_an_admin_can_open_anyones(self):
        r = self._get(self.admin)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['client_name'], 'Draft Client')

    def test_the_projects_approver_can_open_it(self):
        self.assertEqual(self._get(self.approver).status_code, 200)

    def test_an_unrelated_colleague_cannot(self):
        self.assertEqual(self._get(self.stranger).status_code, 404)

    def test_another_company_cannot(self):
        """Tenant isolation holds even for their admin."""
        self.assertEqual(self._get(self.outsider).status_code, 404)

    def test_refusal_is_404_not_403(self):
        """A booking you may not see should not be confirmed to exist."""
        r = self._get(self.stranger)
        self.assertEqual(r.status_code, 404)
        self.assertNotIn('client_name', r.data)
