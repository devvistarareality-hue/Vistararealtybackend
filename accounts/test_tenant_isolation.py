"""One company must not be able to reach another's data, or to become someone
who can.

Each test here is a hole that was actually open, found in a September 2026
audit. They are regression guards, not hypotheticals:

  * a shared master password logged into any account, bypassing password and OTP
  * any employee could PATCH themselves to role=Admin, or reset a colleague's
    password — and inside the VRL company that promotion is platform admin,
    which opens every other company
  * bookings accepted another company's lead, project and units, and flipped
    those units to 'hold'
  * media delete took any path at all
  * a Meta form mapping could be taken over by form_id
  * lead/follow-up/site-visit updates accepted foreign keys from any company
  * a reporting manager could be set to someone in another company, which leaks
    through every view that walks the reporting tree
"""
from django.conf import settings
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from companies.models import Company
from sales.models import Lead, LeadSource, MetaFormMapping, Plot, Project


def _seed(code, name):
    company = Company.objects.create(code=code, name=name, is_active=True)
    admin = User.objects.create_user(f'admin@{code}.com', company=company, user_code=f'{code}001',
                                     password='realpass1', name=f'{name} Admin', role='Admin',
                                     modules=['Sales', 'AR'])
    employee = User.objects.create_user(f'emp@{code}.com', company=company, user_code=f'{code}002',
                                        password='realpass2', name=f'{name} Rep', role='Employee',
                                        modules=['Sales'])
    # Below leadership a manager is required, or an unrelated PATCH trips that
    # rule instead of the thing under test.
    employee.reporting_manager = admin
    employee.save(update_fields=['reporting_manager'])
    project = Project.objects.create(company=company, name=f'{name} Project')
    source = LeadSource.objects.create(company=company, name='walkin')
    lead = Lead.objects.create(company=company, name=f'{name} Client', phone=f'+9198{code}00001',
                               project=project, source=source, status='new')
    plot = Plot.objects.create(project=project, number=f'{code}-1', status='available')
    return dict(company=company, admin=admin, employee=employee,
                project=project, lead=lead, plot=plot)


class TenantIsolation(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.a = _seed('AAA', 'Alpha')
        cls.b = _seed('BBB', 'Beta')

    def setUp(self):
        self.api = APIClient()

    # ── login ────────────────────────────────────────────────────────────────
    def test_there_is_no_master_password(self):
        self.assertFalse(hasattr(settings, 'MASTER_LOGIN_PASSWORD'),
                         'the shared master-password setting is back')
        r = self.api.post('/api/auth/login/', {
            'company_code': 'BBB', 'user_code': 'BBB001',
            'password': '67186718', 'platform': 'web'}, format='json')
        self.assertEqual(r.status_code, 401)

    def test_a_real_password_still_logs_in(self):
        r = self.api.post('/api/auth/login/', {
            'company_code': 'BBB', 'user_code': 'BBB001',
            'password': 'realpass1', 'platform': 'web'}, format='json')
        self.assertEqual(r.status_code, 200)

    # ── user management ──────────────────────────────────────────────────────
    def test_an_employee_cannot_promote_themselves(self):
        self.api.force_authenticate(self.a['employee'])
        r = self.api.patch(f'/api/auth/users/{self.a["employee"].id}/',
                           {'role': 'Admin'}, format='json')
        self.assertEqual(r.status_code, 403)
        self.a['employee'].refresh_from_db()
        self.assertEqual(self.a['employee'].role, 'Employee')

    def test_an_employee_cannot_reset_a_colleagues_password_or_delete_them(self):
        self.api.force_authenticate(self.a['employee'])
        target = self.a['admin'].id
        self.assertEqual(self.api.patch(f'/api/auth/users/{target}/',
                                        {'password': 'hijacked1'}, format='json').status_code, 403)
        self.assertEqual(self.api.delete(f'/api/auth/users/{target}/').status_code, 403)
        self.assertEqual(self.api.post('/api/auth/users/', {'name': 'x'},
                                       format='json').status_code, 403)

    def test_an_admin_still_manages_their_own_company_but_not_another(self):
        self.api.force_authenticate(self.a['admin'])
        self.assertEqual(self.api.patch(f'/api/auth/users/{self.a["employee"].id}/',
                                        {'designation': 'Senior Rep'}, format='json').status_code, 200)
        self.assertEqual(self.api.patch(f'/api/auth/users/{self.b["employee"].id}/',
                                        {'designation': 'Nope'}, format='json').status_code, 404)

    def test_a_reporting_manager_must_be_in_the_same_company(self):
        self.api.force_authenticate(self.a['admin'])
        r = self.api.patch(f'/api/auth/users/{self.a["employee"].id}/',
                           {'reporting_manager_id': self.b['admin'].id}, format='json')
        self.assertEqual(r.status_code, 400)
        r = self.api.patch(f'/api/auth/users/{self.a["employee"].id}/',
                           {'reporting_manager_id': self.a['admin'].id}, format='json')
        self.assertEqual(r.status_code, 200)

    # ── sales writes ─────────────────────────────────────────────────────────
    def _booking(self, **extra):
        payload = {'client_name': 'X', 'project': self.a['project'].id,
                   'booking_type': 'LOI', **extra}
        return self.api.post('/api/sales/bookings/', payload, format='json')

    def test_a_booking_cannot_name_another_companys_records(self):
        self.api.force_authenticate(self.a['admin'])
        self.assertEqual(self._booking(lead=self.b['lead'].id).status_code, 403)
        self.assertEqual(self._booking(project=self.b['project'].id).status_code, 403)
        self.assertEqual(self._booking(plot_ids=[self.b['plot'].id]).status_code, 403)
        self.b['plot'].refresh_from_db()
        self.assertEqual(self.b['plot'].status, 'available',
                         "another company's unit was put on hold")

    def test_a_booking_draft_cannot_hold_another_companys_units(self):
        self.api.force_authenticate(self.a['admin'])
        r = self.api.post('/api/sales/bookings/draft/',
                          {'client_name': 'X', 'project': self.a['project'].id,
                           'plot_ids': [self.b['plot'].id]}, format='json')
        self.assertEqual(r.status_code, 403)
        self.b['plot'].refresh_from_db()
        self.assertEqual(self.b['plot'].status, 'available')

    def test_a_lead_cannot_be_repointed_at_another_company(self):
        self.api.force_authenticate(self.a['admin'])
        url = f'/api/sales/leads/{self.a["lead"].id}/'
        self.assertEqual(self.api.patch(url, {'project': self.b['project'].id},
                                        format='json').status_code, 403)
        self.assertEqual(self.api.patch(url, {'stm': self.b['employee'].id},
                                        format='json').status_code, 403)
        self.assertEqual(self.api.patch(url, {'stm': self.a['employee'].id},
                                        format='json').status_code, 200)

    def test_media_delete_is_scoped_to_the_callers_own_files(self):
        self.api.force_authenticate(self.a['employee'])
        r = self.api.post('/api/sales/media/delete/',
                          {'path': f'c{self.b["company"].id}/erp/media/x.png'}, format='json')
        self.assertEqual(r.status_code, 403)
        # Files uploaded before the per-company prefix have nothing to check, so
        # only an admin may remove one.
        r = self.api.post('/api/sales/media/delete/',
                          {'path': 'erp/media/legacy.png'}, format='json')
        self.assertEqual(r.status_code, 403)

    def test_a_meta_form_mapping_cannot_be_hijacked(self):
        MetaFormMapping.objects.create(company=self.b['company'], form_id='FORM-9',
                                       project=self.b['project'])
        self.api.force_authenticate(self.a['admin'])
        r = self.api.post('/api/sales/webhooks/meta/mappings/',
                          {'form_id': 'FORM-9', 'project_id': self.a['project'].id}, format='json')
        self.assertEqual(r.status_code, 403)
        self.assertEqual(MetaFormMapping.objects.get(form_id='FORM-9').project_id,
                         self.b['project'].id)
