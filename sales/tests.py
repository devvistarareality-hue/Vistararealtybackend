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
