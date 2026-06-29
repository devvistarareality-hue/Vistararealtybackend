"""Regression tests locking in multi-tenant isolation for the Sales/CRM module.

Every authenticated user must only ever see/modify data belonging to their own
company. The single exception is a *platform admin* (VRL-company Admin or any
Django staff/superuser), who can see across all companies.

If any of these tests fail, tenant isolation has regressed — do not ship.
"""
from unittest import mock

from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from companies.models import Company
from accounts.models import User
from sales.models import Lead, Project, Plot, LeadSource


def auth(client, user):
    token = RefreshToken.for_user(user).access_token
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')


class TenantIsolationTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.A = Company.objects.create(code='AAA', name='Alpha')
        cls.B = Company.objects.create(code='BBB', name='Beta')
        cls.VRL = Company.objects.create(code='VRL', name='Vistara HQ')

        # Managers can create/delete; telecaller is a plain member.
        cls.mgr_a = User.objects.create(email='mgr_a@x.com', company=cls.A, role='Manager', user_code='MA')
        cls.tc_a  = User.objects.create(email='tc_a@x.com',  company=cls.A, role='Telecaller', user_code='TA')
        cls.mgr_b = User.objects.create(email='mgr_b@x.com', company=cls.B, role='Manager', user_code='MB')
        cls.staff = User.objects.create(email='staff@x.com', company=cls.A, role='Admin', is_staff=True, user_code='ST')
        cls.vrl_admin = User.objects.create(email='vrl@x.com', company=cls.VRL, role='Admin', user_code='VA')

        # A plain "Manager" role only sees all-company data when they're top-of-tree
        # (reports to nobody AND has active reports) — see _sees_all_company. Make tc_a
        # report to mgr_a so mgr_a is a real company-wide manager in these tests.
        cls.tc_a.reporting_manager = cls.mgr_a
        cls.tc_a.save(update_fields=['reporting_manager'])

        # Projects + leads per company
        cls.proj_a = Project.objects.create(company=cls.A, name='Tower A')
        cls.proj_b = Project.objects.create(company=cls.B, name='Tower B')
        cls.plot_a = Plot.objects.create(project=cls.proj_a, number='1')
        cls.plot_b = Plot.objects.create(project=cls.proj_b, number='1')
        cls.lead_a = Lead.objects.create(company=cls.A, name='Lead A', phone='+919000000001', telecaller=cls.tc_a)
        cls.lead_b = Lead.objects.create(company=cls.B, name='Lead B', phone='+919000000002')

    # ── Leads ────────────────────────────────────────────────────────────
    def test_lead_list_is_company_scoped(self):
        auth(self.client, self.mgr_a)
        res = self.client.get('/api/sales/leads/')
        self.assertEqual(res.status_code, 200)
        names = [l['name'] for l in res.json()['results']]
        self.assertEqual(names, ['Lead A'])

    def test_cannot_read_other_company_lead(self):
        auth(self.client, self.mgr_a)
        self.assertEqual(self.client.get(f'/api/sales/leads/{self.lead_b.id}/').status_code, 404)

    def test_cannot_patch_other_company_lead(self):
        auth(self.client, self.mgr_a)
        res = self.client.patch(f'/api/sales/leads/{self.lead_b.id}/', {'name': 'hacked'}, format='json')
        self.assertEqual(res.status_code, 404)
        self.lead_b.refresh_from_db()
        self.assertEqual(self.lead_b.name, 'Lead B')

    def test_cannot_delete_other_company_lead(self):
        auth(self.client, self.mgr_a)
        self.assertEqual(self.client.delete(f'/api/sales/leads/{self.lead_b.id}/').status_code, 404)
        self.assertTrue(Lead.objects.filter(id=self.lead_b.id).exists())

    def test_created_lead_is_stamped_with_company(self):
        auth(self.client, self.mgr_a)
        res = self.client.post('/api/sales/leads/', {'name': 'New', 'phone': '+919111111111'}, format='json')
        self.assertEqual(res.status_code, 201)
        self.assertEqual(Lead.objects.get(id=res.json()['id']).company_id, self.A.id)

    def test_bulk_delete_only_affects_own_company(self):
        auth(self.client, self.mgr_a)
        res = self.client.delete(
            '/api/sales/leads/bulk-delete/',
            {'ids': [self.lead_a.id, self.lead_b.id]}, format='json',
        )
        self.assertEqual(res.status_code, 200)
        self.assertFalse(Lead.objects.filter(id=self.lead_a.id).exists())
        self.assertTrue(Lead.objects.filter(id=self.lead_b.id).exists())  # B untouched

    # ── Projects & plots ─────────────────────────────────────────────────
    def test_project_list_is_company_scoped(self):
        auth(self.client, self.mgr_a)
        res = self.client.get('/api/sales/projects/')
        self.assertEqual([p['name'] for p in res.json()], ['Tower A'])

    def test_created_project_is_stamped_with_company(self):
        auth(self.client, self.mgr_a)
        res = self.client.post('/api/sales/projects/', {'name': 'Tower C'}, format='json')
        self.assertEqual(res.status_code, 201)
        self.assertEqual(Project.objects.get(id=res.json()['id']).company_id, self.A.id)

    def test_cannot_list_plots_of_other_company_project(self):
        auth(self.client, self.mgr_a)
        self.assertEqual(self.client.get(f'/api/sales/plots/?project={self.proj_b.id}').status_code, 404)
        self.assertEqual(self.client.get(f'/api/sales/plots/?project={self.proj_a.id}').status_code, 200)

    def test_cannot_patch_other_company_plot(self):
        auth(self.client, self.mgr_a)
        res = self.client.patch(f'/api/sales/plots/{self.plot_b.id}/', {'status': 'sold'}, format='json')
        self.assertEqual(res.status_code, 404)

    # ── Stats / aggregates ───────────────────────────────────────────────
    def test_stats_count_only_own_company(self):
        auth(self.client, self.mgr_a)
        data = self.client.get('/api/sales/stats/').json()
        self.assertEqual(data['total_leads'], 1)
        self.assertEqual([l['name'] for l in data['recent_leads']], ['Lead A'])

    # ── Cross-tenant write attempts via foreign keys ─────────────────────
    def test_cannot_attach_followup_to_other_company_lead(self):
        auth(self.client, self.mgr_a)
        res = self.client.post('/api/sales/follow-ups/', {
            'lead': self.lead_b.id, 'assigned_to': self.tc_a.id,
            'role_context': 'telecaller', 'scheduled_at': '2030-01-01T10:00:00Z',
        }, format='json')
        self.assertEqual(res.status_code, 400)

    def test_bulk_import_stamps_company(self):
        auth(self.client, self.mgr_a)
        res = self.client.post('/api/sales/leads/import/', {
            'leads': [{'name': 'Imp1', 'phone': '+919222222222'}],
        }, format='json')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(Lead.objects.get(name='Imp1').company_id, self.A.id)

    # ── Platform admins see everything ───────────────────────────────────
    def test_staff_superadmin_sees_all_companies(self):
        auth(self.client, self.staff)
        names = {l['name'] for l in self.client.get('/api/sales/leads/').json()['results']}
        self.assertEqual(names, {'Lead A', 'Lead B'})
        self.assertEqual(self.client.get(f'/api/sales/leads/{self.lead_b.id}/').status_code, 200)

    def test_vrl_admin_sees_all_companies(self):
        auth(self.client, self.vrl_admin)
        names = {l['name'] for l in self.client.get('/api/sales/leads/').json()['results']}
        self.assertEqual(names, {'Lead A', 'Lead B'})


class DistributionIsolationTests(APITestCase):
    def test_distribution_only_assigns_own_company_leads(self):
        from sales.models import UserAvailability, UserProjectAssignment
        from django.utils import timezone

        A = Company.objects.create(code='AAA', name='Alpha')
        B = Company.objects.create(code='BBB', name='Beta')
        mgr_a = User.objects.create(email='m@a.com', company=A, role='Manager', user_code='MA')
        tc_a  = User.objects.create(email='t@a.com', company=A, role='Telecaller',
                                    user_code='TA', designation='TELECALLER')
        # Distribution is STRICT on project: the lead needs a project and the member an
        # assignment to it, else it's skipped (can't be routed to anyone).
        projA = Project.objects.create(company=A, name='PA')
        UserProjectAssignment.objects.create(user=tc_a, project=projA)
        # Unassigned 'new' leads in BOTH companies
        Lead.objects.create(company=A, name='A1', phone='+919000000001', status='new', project=projA)
        Lead.objects.create(company=B, name='B1', phone='+919000000002', status='new')
        # tc_a signs in today (checked_in_at is required — distribution only counts
        # availability within the 12h window).
        UserAvailability.objects.create(user=tc_a, date=timezone.localdate(),
                                        is_available=True, checked_in_at=timezone.now())

        auth(self.client, mgr_a)
        res = self.client.post('/api/sales/distribute/', {'type': 'telecaller'}, format='json')
        self.assertEqual(res.status_code, 200)

        # Only company A's lead got assigned; B's stays unassigned + 'new'
        a_lead = Lead.objects.get(name='A1')
        b_lead = Lead.objects.get(name='B1')
        self.assertEqual(a_lead.telecaller_id, tc_a.id)
        self.assertIsNone(b_lead.telecaller_id)
        self.assertEqual(b_lead.status, 'new')


class DistributionRoutingTests(APITestCase):
    """Locks in _run_distribution project-scoping + weighted fairness — the path the
    O(L×M)→O(L+M) pre-bucketing optimization rewrote. No DistributionSettings, so the
    window is 'open'."""

    @staticmethod
    def _avail(user):
        from sales.models import UserAvailability
        from django.utils import timezone
        UserAvailability.objects.create(user=user, date=timezone.localdate(),
                                        is_available=True, checked_in_at=timezone.now())

    def test_leads_route_only_to_members_assigned_that_project(self):
        from sales.models import UserProjectAssignment
        from sales.views import _run_distribution
        co = Company.objects.create(code='AAA', name='Alpha')
        p1 = Project.objects.create(company=co, name='P1')
        p2 = Project.objects.create(company=co, name='P2')
        t1 = User.objects.create(email='t1@a.com', company=co, role='Telecaller', user_code='T1', designation='TELECALLER')
        t2 = User.objects.create(email='t2@a.com', company=co, role='Telecaller', user_code='T2', designation='TELECALLER')
        UserProjectAssignment.objects.create(user=t1, project=p1)
        UserProjectAssignment.objects.create(user=t2, project=p2)
        self._avail(t1); self._avail(t2)
        l1 = Lead.objects.create(company=co, name='L1', phone='+910000000001', project=p1, status='new')
        l2 = Lead.objects.create(company=co, name='L2', phone='+910000000002', project=p1, status='new')
        l3 = Lead.objects.create(company=co, name='L3', phone='+910000000003', project=p2, status='new')
        l0 = Lead.objects.create(company=co, name='L0', phone='+910000000004', project=None, status='new')

        _run_distribution(co, 'telecaller')
        for l in (l1, l2, l3, l0):
            l.refresh_from_db()
        self.assertEqual(l1.telecaller_id, t1.id)   # P1 → t1
        self.assertEqual(l2.telecaller_id, t1.id)
        self.assertEqual(l3.telecaller_id, t2.id)   # P2 → t2
        self.assertIsNone(l0.telecaller_id)         # no project → skipped, never mis-routed

    def test_weighted_fairness_within_a_project(self):
        from sales.models import UserProjectAssignment, UserDistributionWeight
        from sales.views import _run_distribution
        co = Company.objects.create(code='BBB', name='Beta')
        p = Project.objects.create(company=co, name='P')
        a = User.objects.create(email='a@b.com', company=co, role='Telecaller', user_code='A', designation='TELECALLER')
        b = User.objects.create(email='b@b.com', company=co, role='Telecaller', user_code='B', designation='TELECALLER')
        UserProjectAssignment.objects.create(user=a, project=p)
        UserProjectAssignment.objects.create(user=b, project=p)
        UserDistributionWeight.objects.create(user=a, weight=2)
        UserDistributionWeight.objects.create(user=b, weight=1)
        self._avail(a); self._avail(b)
        for i in range(6):
            Lead.objects.create(company=co, name=f'L{i}', phone=f'+9120000000{i:02d}', project=p, status='new')

        _run_distribution(co, 'telecaller')
        na = Lead.objects.filter(telecaller=a).count()
        nb = Lead.objects.filter(telecaller=b).count()
        self.assertEqual(na + nb, 6)
        self.assertEqual((na, nb), (4, 2))  # weight 2:1 over 6 leads


class DistributionWindowTests(APITestCase):
    """Dry-run of Meta-lead auto-assignment around the sign-in window.

    Auto-distribution fires on an EVENT (a new lead, or a user marking available =
    "login") and only assigns while the window is OPEN (signin ≤ now < signout).
    Availability persists for the day, so leads queued while the window is closed
    flush to whoever is available on the first in-window event. Fairness uses a
    weighted round-robin keyed on today's per-user assignment count, so a late
    joiner catches up.
    """

    def _setup(self, code='AAA'):
        co = Company.objects.create(code=code, name=code)
        p = Project.objects.create(company=co, name='P')
        return co, p

    def _tc(self, co, p, code):
        from sales.models import UserProjectAssignment
        u = User.objects.create(email=f'{code}@x.com', company=co, role='Telecaller',
                                user_code=code, designation='TELECALLER')
        UserProjectAssignment.objects.create(user=u, project=p)
        return u

    def _login(self, u):                       # marking available == logging in
        from sales.models import UserAvailability
        from django.utils import timezone
        UserAvailability.objects.update_or_create(
            user=u, date=timezone.localdate(),
            defaults={'is_available': True, 'checked_in_at': timezone.now()})

    def _lead(self, co, p, n):
        return Lead.objects.create(company=co, name=f'L{n}', phone=f'+91{n:010d}',
                                   project=p, status='new')

    # 1) BEFORE sign-in: even if both log in and leads arrive, nothing is assigned.
    def test_before_signin_holds_everything(self):
        from sales.views import _run_distribution
        co, p = self._setup()
        t1, t2 = self._tc(co, p, 'T1'), self._tc(co, p, 'T2')
        self._login(t1); self._login(t2)
        leads = [self._lead(co, p, i) for i in range(3)]
        with mock.patch('sales.views._window_state', return_value='before_signin'):
            res = _run_distribution(co, 'telecaller')
        self.assertEqual(res['distributed'], 0)
        for l in leads:
            l.refresh_from_db()
            self.assertIsNone(l.telecaller_id)

    # 2) OPEN, both logged in → leads split fairly (2/2 of 4).
    def test_open_both_available_split_fairly(self):
        from sales.views import _run_distribution
        co, p = self._setup()
        t1, t2 = self._tc(co, p, 'T1'), self._tc(co, p, 'T2')
        self._login(t1); self._login(t2)
        for i in range(4):
            self._lead(co, p, i)
        _run_distribution(co, 'telecaller')    # no DistributionSettings → window OPEN
        self.assertEqual(Lead.objects.filter(telecaller=t1).count(), 2)
        self.assertEqual(Lead.objects.filter(telecaller=t2).count(), 2)

    # 3) STAGGERED: T1 logs in first and takes the queue; T2 joins later and the
    #    fairness counter routes the next leads to T2 to catch up.
    def test_staggered_login_catches_up(self):
        from sales.views import _run_distribution
        co, p = self._setup()
        t1, t2 = self._tc(co, p, 'T1'), self._tc(co, p, 'T2')
        self._login(t1)
        for i in range(2):
            self._lead(co, p, i)
        _run_distribution(co, 'telecaller')                 # only T1 available
        self.assertEqual(Lead.objects.filter(telecaller=t1).count(), 2)
        self.assertEqual(Lead.objects.filter(telecaller=t2).count(), 0)
        self._login(t2)
        for i in range(2, 4):
            self._lead(co, p, i)
        _run_distribution(co, 'telecaller')                 # both available now
        self.assertEqual(Lead.objects.filter(telecaller=t1).count(), 2)   # unchanged
        self.assertEqual(Lead.objects.filter(telecaller=t2).count(), 2)   # caught up

    # 4) AFTER sign-out: nothing is assigned (auto path).
    def test_after_signout_holds_everything(self):
        from sales.views import _run_distribution
        co, p = self._setup()
        t1 = self._tc(co, p, 'T1'); self._login(t1)
        l = self._lead(co, p, 0)
        with mock.patch('sales.views._window_state', return_value='after_signout'):
            res = _run_distribution(co, 'telecaller')
        self.assertEqual(res['distributed'], 0)
        l.refresh_from_db()
        self.assertIsNone(l.telecaller_id)

    # 5) STM path behaves identically (warm_transferred bucket → STM split).
    def test_stm_open_split_fairly(self):
        from sales.views import _run_distribution
        from sales.models import UserProjectAssignment, UserAvailability
        from django.utils import timezone
        co, p = self._setup('BBB')

        def stm(code):
            u = User.objects.create(email=f'{code}@b.com', company=co, role='Sales',
                                    user_code=code, designation='STM')
            UserProjectAssignment.objects.create(user=u, project=p)
            UserAvailability.objects.create(user=u, date=timezone.localdate(),
                                            is_available=True, checked_in_at=timezone.now())
            return u

        s1, s2 = stm('S1'), stm('S2')
        for i in range(4):
            Lead.objects.create(company=co, name=f'W{i}', phone=f'+9133{i:08d}',
                                project=p, status='warm_transferred')
        _run_distribution(co, 'stm')
        self.assertEqual(Lead.objects.filter(stm=s1).count(), 2)
        self.assertEqual(Lead.objects.filter(stm=s2).count(), 2)


class AvailabilityExpiryTests(APITestCase):
    """mark-available auto-expires at the role's configured sign-out time
    (was a fixed 12h TTL)."""

    def _avail(self, co, signout_time, code):
        from sales.models import DistributionSettings, UserAvailability
        from django.utils import timezone
        DistributionSettings.objects.update_or_create(
            company=co, defaults={'tc_signout_time': signout_time})
        u = User.objects.create(email=f'{code}@x.com', company=co, role='Telecaller',
                                user_code=code, designation='TELECALLER')
        a = UserAvailability.objects.create(user=u, date=timezone.localdate(),
                                            is_available=True, checked_in_at=timezone.now())
        return u, a

    def test_active_before_signout(self):
        from sales.views import _availability_active
        from datetime import time
        co = Company.objects.create(code='AAA', name='A')
        u, a = self._avail(co, time(23, 59), 'EARLY')
        self.assertTrue(_availability_active(a, u))

    def test_expired_after_signout(self):
        from sales.views import _availability_active
        from datetime import time
        co = Company.objects.create(code='BBB', name='B')
        u, a = self._avail(co, time(0, 0), 'LATE')   # sign-out 00:00 → past all day
        self.assertFalse(_availability_active(a, u))


class WebhookVerifyTests(APITestCase):
    """Meta webhook GET handshake: returns the challenge only for a known token."""

    def test_challenge_returned_on_token_match(self):
        from sales.models import MetaWebhookConfig
        co = Company.objects.create(code='AAA', name='A')
        MetaWebhookConfig.objects.create(company=co, verify_token='secret123')
        res = self.client.get('/api/sales/webhooks/meta/', {
            'hub.mode': 'subscribe', 'hub.verify_token': 'secret123', 'hub.challenge': 'PING'})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.content.decode(), 'PING')

    def test_bad_token_rejected(self):
        from sales.models import MetaWebhookConfig
        co = Company.objects.create(code='BBB', name='B')
        MetaWebhookConfig.objects.create(company=co, verify_token='secret123')
        res = self.client.get('/api/sales/webhooks/meta/', {
            'hub.mode': 'subscribe', 'hub.verify_token': 'nope', 'hub.challenge': 'PING'})
        self.assertEqual(res.status_code, 403)


class WarmTransferOnCreateTests(APITestCase):
    """A telecaller adding a lead with TC status = warm warm-transfers it to the STM
    pipeline on create (not just on edit)."""

    def test_warm_lead_transfers_and_assigns_stm(self):
        from sales.models import UserProjectAssignment, UserAvailability
        from django.utils import timezone
        co = Company.objects.create(code='AAA', name='A')
        p = Project.objects.create(company=co, name='P')
        tc  = User.objects.create(email='tc@a.com', company=co, role='Telecaller', user_code='TC', designation='TELECALLER')
        stm = User.objects.create(email='stm@a.com', company=co, role='Sales',     user_code='SM', designation='STM')
        UserProjectAssignment.objects.create(user=tc, project=p)
        UserProjectAssignment.objects.create(user=stm, project=p)
        UserAvailability.objects.create(user=stm, date=timezone.localdate(),
                                        is_available=True, checked_in_at=timezone.now())

        auth(self.client, tc)
        res = self.client.post('/api/sales/leads/', {
            'name': 'Warm Lead', 'phone': '+919000000009', 'project': p.id, 'telecaller_status': 'warm',
        }, format='json')
        self.assertEqual(res.status_code, 201)
        lead = Lead.objects.get(id=res.json()['id'])
        self.assertEqual(lead.telecaller_id, tc.id)          # self-sourced by the telecaller
        self.assertEqual(lead.status, 'warm_transferred')    # warm → transferred
        self.assertEqual(lead.stm_id, stm.id)                # auto-assigned to the available STM


class LOIAccessTests(APITestCase):
    """LOI signed-URL endpoint: only the booking's STM or an admin/manager may open it."""

    def test_only_owner_stm_or_admin_can_fetch_url(self):
        from sales.models import Booking
        coA = Company.objects.create(code='AAA', name='A')
        coB = Company.objects.create(code='BBB', name='B')
        stm       = User.objects.create(email='s@a.com',  company=coA, role='Sales', user_code='S1', designation='STM')
        other_stm = User.objects.create(email='s2@a.com', company=coA, role='Sales', user_code='S2', designation='STM')
        admin     = User.objects.create(email='ad@a.com', company=coA, role='Admin', user_code='AD')
        outsider  = User.objects.create(email='o@b.com',  company=coB, role='Admin', user_code='OB')
        b = Booking.objects.create(company=coA, stm=stm, status='sold',
                                   client_name='C', area='A1', loi_document='proj/plot/loi.pdf')
        url = f'/api/sales/bookings/{b.id}/loi-url/'

        auth(self.client, stm);       self.assertEqual(self.client.get(url).status_code, 200)   # owner
        auth(self.client, admin);     self.assertEqual(self.client.get(url).status_code, 200)   # admin
        auth(self.client, other_stm); self.assertEqual(self.client.get(url).status_code, 403)   # same co, not owner
        auth(self.client, outsider);  self.assertEqual(self.client.get(url).status_code, 404)   # other company


class BookingApprovalTests(APITestCase):
    """Booking approve → closure + plot sold; reject → plot freed."""

    def test_approve_creates_closure_and_sells_plot(self):
        from datetime import date
        from sales.models import Booking, Closure
        co = Company.objects.create(code='AAA', name='A')
        p  = Project.objects.create(company=co, name='P')
        pl = Plot.objects.create(project=p, number='7', status='hold')
        stm   = User.objects.create(email='s@a.com',  company=co, role='Sales', user_code='S1', designation='STM')
        admin = User.objects.create(email='ad@a.com', company=co, role='Admin', user_code='AD')
        lead = Lead.objects.create(company=co, name='L', phone='+919000000010', project=p, stm=stm)
        b = Booking.objects.create(company=co, project=p, plot=pl, lead=lead, stm=stm, status='pending',
                                   client_name='C', final_amount=1000000, plot_basic=900000, booking_date=date.today())

        auth(self.client, admin)
        res = self.client.post(f'/api/sales/bookings/{b.id}/action/', {'action': 'approve'}, format='json')
        self.assertEqual(res.status_code, 200)
        b.refresh_from_db(); pl.refresh_from_db(); lead.refresh_from_db()
        self.assertEqual(b.status, 'sold')
        self.assertEqual(pl.status, 'sold')
        self.assertEqual(lead.stm_status, 'closed')
        self.assertTrue(Closure.objects.filter(lead=lead, stm=stm, status='booked').exists())

    def test_reject_frees_plot(self):
        from sales.models import Booking
        co = Company.objects.create(code='BBB', name='B')
        p  = Project.objects.create(company=co, name='P')
        pl = Plot.objects.create(project=p, number='8', status='hold')
        stm   = User.objects.create(email='s@b.com',  company=co, role='Sales', user_code='S1', designation='STM')
        admin = User.objects.create(email='ad@b.com', company=co, role='Admin', user_code='AD')
        b = Booking.objects.create(company=co, project=p, plot=pl, stm=stm, status='pending', client_name='C')

        auth(self.client, admin)
        res = self.client.post(f'/api/sales/bookings/{b.id}/action/', {'action': 'reject'}, format='json')
        self.assertEqual(res.status_code, 200)
        b.refresh_from_db(); pl.refresh_from_db()
        self.assertEqual(b.status, 'rejected')
        self.assertEqual(pl.status, 'available')


class MultiPlotBookingTests(APITestCase):
    """A booking can span multiple plots: all are reserved on create, all sold on
    approve, and plot_numbers is the comma display."""

    def test_multi_plot_reserve_and_sell(self):
        from datetime import date
        from sales.models import Booking, Closure
        co = Company.objects.create(code='MUL', name='M')
        p  = Project.objects.create(company=co, name='P')
        pl1 = Plot.objects.create(project=p, number='10', status='available')
        pl2 = Plot.objects.create(project=p, number='11', status='available')
        stm   = User.objects.create(email='s@m.com', company=co, role='Sales', user_code='S1', designation='STM')
        admin = User.objects.create(email='a@m.com', company=co, role='Admin', user_code='AD')
        lead = Lead.objects.create(company=co, name='L', phone='+919000000050', project=p, stm=stm)

        auth(self.client, stm)
        res = self.client.post('/api/sales/bookings/', {
            'project': p.id, 'plot': pl1.id, 'plot_ids': [pl1.id, pl2.id], 'lead': lead.id,
            'client_name': 'L', 'area': '2400', 'final_amount': 5000000, 'plot_basic': 4500000,
            'booking_date': str(date.today()),
        }, format='json')
        self.assertEqual(res.status_code, 201)
        b = Booking.objects.get(id=res.json()['id'])
        self.assertEqual(b.plot_ids, [pl1.id, pl2.id])
        self.assertEqual(b.plot_numbers, '10, 11')
        pl1.refresh_from_db(); pl2.refresh_from_db()
        self.assertEqual(pl1.status, 'hold')
        self.assertEqual(pl2.status, 'hold')          # both reserved

        auth(self.client, admin)
        self.client.post(f'/api/sales/bookings/{b.id}/action/', {'action': 'approve'}, format='json')
        pl1.refresh_from_db(); pl2.refresh_from_db()
        self.assertEqual(pl1.status, 'sold')
        self.assertEqual(pl2.status, 'sold')          # both sold
        self.assertTrue(Closure.objects.filter(lead=lead, unit_no='10, 11').exists())


class DataResetTests(APITestCase):
    """Admin-only trial-data reset must wipe the caller's company data, reset its
    plots, and NEVER touch another company's data."""

    def _seed(self, code):
        from datetime import date
        from sales.models import Booking, Closure, SiteVisit
        co = Company.objects.create(code=code, name=code)
        proj = Project.objects.create(company=co, name=code + ' P')
        plot = Plot.objects.create(project=proj, number='1', status='sold')
        admin = User.objects.create(email=f'a{code}@x.com', company=co, role='Admin', user_code='A' + code)
        lead = Lead.objects.create(company=co, name='L', phone='+9190000' + code.zfill(5)[:5], project=proj, stm=admin)
        SiteVisit.objects.create(lead=lead, stm=admin)
        Booking.objects.create(company=co, plot=plot, lead=lead, stm=admin, client_name='L', booking_date=date.today())
        Closure.objects.create(lead=lead, project=proj, stm=admin, status='booked', closure_date=date.today(), unit_no='1')
        return co, admin, proj, plot, lead

    def test_reset_scoped_to_company(self):
        from sales.models import Booking, Closure, SiteVisit
        a_co, a_admin, a_proj, a_plot, a_lead = self._seed('11')
        b_co, b_admin, b_proj, b_plot, b_lead = self._seed('22')

        auth(self.client, a_admin)
        # missing confirm -> 400
        self.assertEqual(self.client.post('/api/sales/admin/reset-trial-data/', {}, format='json').status_code, 400)
        # do it
        res = self.client.post('/api/sales/admin/reset-trial-data/', {'confirm': 'DELETE'}, format='json')
        self.assertEqual(res.status_code, 200)

        # company A wiped + plot reset
        self.assertFalse(Lead.objects.filter(company=a_co).exists())
        self.assertFalse(Booking.objects.filter(company=a_co).exists())
        self.assertFalse(Closure.objects.filter(lead__company=a_co).exists())
        self.assertFalse(SiteVisit.objects.filter(lead__company=a_co).exists())
        a_plot.refresh_from_db(); self.assertEqual(a_plot.status, 'available')

        # company B fully intact
        self.assertTrue(Lead.objects.filter(company=b_co).exists())
        self.assertTrue(Booking.objects.filter(company=b_co).exists())
        b_plot.refresh_from_db(); self.assertEqual(b_plot.status, 'sold')

    def test_non_admin_forbidden(self):
        a_co, a_admin, *_ = self._seed('33')
        stm = User.objects.create(email='stm@x.com', company=a_co, role='Sales', user_code='S33', designation='STM')
        auth(self.client, stm)
        self.assertEqual(self.client.post('/api/sales/admin/reset-trial-data/', {'confirm': 'DELETE'}, format='json').status_code, 403)
        self.assertTrue(Lead.objects.filter(company=a_co).exists())


class FormMappingBackfillTests(APITestCase):
    """Saving a form→project mapping retroactively maps existing unmapped leads
    that carry that form_id, and leaves other forms' leads alone."""

    def test_backfill_by_stored_form_id(self):
        co = Company.objects.create(code='FB', name='FB')
        proj = Project.objects.create(company=co, name='P1')
        admin = User.objects.create(email='a@fb.com', company=co, role='Admin', user_code='AFB')
        src = LeadSource.objects.create(company=co, name='meta')
        l1 = Lead.objects.create(company=co, name='A', phone='+919000000001', source=src, meta_form_id='F123')
        l2 = Lead.objects.create(company=co, name='B', phone='+919000000002', source=src, meta_form_id='F123')
        l3 = Lead.objects.create(company=co, name='C', phone='+919000000003', source=src, meta_form_id='OTHER')

        auth(self.client, admin)
        res = self.client.post('/api/sales/webhooks/meta/mappings/',
                               {'form_id': 'F123', 'form_name': 'F', 'project_id': proj.id}, format='json')
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.json()['backfilled'], 2)
        l1.refresh_from_db(); l2.refresh_from_db(); l3.refresh_from_db()
        self.assertEqual(l1.project_id, proj.id)
        self.assertEqual(l2.project_id, proj.id)
        self.assertIsNone(l3.project_id)   # different form untouched


class PhoneDedupTests(APITestCase):
    """Duplicate detection must catch +91-prefixed numbers (last-10 match)."""

    def test_plus91_duplicate_flagged(self):
        co = Company.objects.create(code='DUP', name='Dup')
        admin = User.objects.create(email='a@dup.com', company=co, role='Admin', user_code='AD')
        first = Lead.objects.create(company=co, name='First', phone='+919510188522')

        auth(self.client, admin)
        res = self.client.post('/api/sales/leads/',
                               {'name': 'Second', 'phone': '+919510188522'}, format='json')
        self.assertEqual(res.status_code, 201)
        new = Lead.objects.get(id=res.json()['id'])
        self.assertTrue(new.is_duplicate)
        self.assertEqual(new.duplicate_of_id, first.id)
        first.refresh_from_db()
        self.assertEqual(first.duplicate_count, 1)
