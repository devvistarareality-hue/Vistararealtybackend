"""Regression tests locking in multi-tenant isolation for the Sales/CRM module.

Every authenticated user must only ever see/modify data belonging to their own
company. The single exception is a *platform admin* (VRL-company Admin or any
Django staff/superuser), who can see across all companies.

If any of these tests fail, tenant isolation has regressed — do not ship.
"""
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
        from sales.models import UserAvailability
        from django.utils import timezone

        A = Company.objects.create(code='AAA', name='Alpha')
        B = Company.objects.create(code='BBB', name='Beta')
        mgr_a = User.objects.create(email='m@a.com', company=A, role='Manager', user_code='MA')
        tc_a  = User.objects.create(email='t@a.com', company=A, role='Telecaller',
                                    user_code='TA', designation='TELECALLER')
        # Unassigned 'new' leads in BOTH companies
        Lead.objects.create(company=A, name='A1', phone='+919000000001', status='new')
        Lead.objects.create(company=B, name='B1', phone='+919000000002', status='new')
        # tc_a signs in today
        UserAvailability.objects.create(user=tc_a, date=timezone.localdate(), is_available=True)

        auth(self.client, mgr_a)
        res = self.client.post('/api/sales/distribute/', {'type': 'telecaller'}, format='json')
        self.assertEqual(res.status_code, 200)

        # Only company A's lead got assigned; B's stays unassigned + 'new'
        a_lead = Lead.objects.get(name='A1')
        b_lead = Lead.objects.get(name='B1')
        self.assertEqual(a_lead.telecaller_id, tc_a.id)
        self.assertIsNone(b_lead.telecaller_id)
        self.assertEqual(b_lead.status, 'new')
