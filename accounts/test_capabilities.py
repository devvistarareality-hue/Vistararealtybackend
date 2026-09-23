"""Capabilities must reproduce the old designation-text rules exactly.

Powers used to come from matching text in the designation. These tests pin that
behaviour so nobody's access changes, and then check a company can override it.
"""
from django.test import TestCase

from accounts.capabilities import capabilities_for, data_scope, legacy_capabilities, user_can
from accounts.models import Designation, User
from companies.models import Company
from sales.views import can_assign_leads, is_cp, is_cp_manager, is_stm, is_telecaller

# The old rules, written out once more, independently of the implementation.
OLD_TELECALLER = lambda d: 'telecaller' in d or 'tele caller' in d
OLD_STM = lambda d: 'stm' in d or 'sales team' in d or 'sales executive' in d
OLD_CP = lambda d: 'cp executive' in d or 'channel partner' in d
OLD_CP_MANAGER = lambda d, role: role == 'Manager' and d.startswith('cp')
OLD_ASSIGN = lambda d: not (OLD_TELECALLER(d) or OLD_STM(d) or OLD_CP(d))

TITLES = [
    'Telecaller', 'Sr. Telecaller', 'Tele Caller', 'STM', 'Sr. STM', 'Sales Team Member',
    'Sales Executive', 'CP Executive', 'Channel Partner', 'CP Cluster Head', 'CP Head',
    'Manager', 'General Manager', 'Director', 'Accountant', 'AR Officer', 'HR Executive', '',
]


class LegacyParityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='CAP', name='Cap Co')

    def _user(self, title, role='Employee'):
        return User(company=self.co, designation=title, role=role, email='x@x.com')

    def test_every_title_behaves_as_before(self):
        for title in TITLES:
            d = title.lower()
            for role in ('Employee', 'Manager'):
                u = self._user(title, role)
                self.assertEqual(is_telecaller(u), OLD_TELECALLER(d), f'{title} / {role} telecaller')
                self.assertEqual(is_stm(u), OLD_STM(d), f'{title} / {role} stm')
                self.assertEqual(is_cp(u), OLD_CP(d), f'{title} / {role} cp')
                self.assertEqual(is_cp_manager(u), OLD_CP_MANAGER(d, role), f'{title} / {role} cp manager')
                self.assertEqual(can_assign_leads(u), OLD_ASSIGN(d), f'{title} / {role} assign')

    def test_a_seeded_designation_matches_the_old_rules(self):
        for title in TITLES:
            if not title:
                continue
            Designation.objects.filter(company=self.co).delete()
            Designation.objects.create(company=self.co, name=title, module='Sales',
                                       capabilities=sorted(legacy_capabilities(title)), capabilities_set=True)
            u = self._user(title)
            self.assertEqual(capabilities_for(u), legacy_capabilities(title), title)

    def test_titles_nobody_configured_still_work(self):
        # No Designation row at all: the old text rules still decide.
        u = self._user('Senior STM — West')
        self.assertTrue(is_stm(u))
        self.assertFalse(can_assign_leads(u))


class CompanyOverrideTests(TestCase):
    """The point of all this: two companies, same title, different powers."""

    def setUp(self):
        self.a = Company.objects.create(code='AAA', name='A')
        self.b = Company.objects.create(code='BBB', name='B')
        Designation.objects.create(company=self.a, name='Telecaller', module='Sales',
                                   capabilities=['sales.pipeline.telecalling'], capabilities_set=True)
        Designation.objects.create(company=self.b, name='Telecaller', module='Sales',
                                   capabilities=['sales.pipeline.telecalling', 'sales.lead.assign'],
                                   capabilities_set=True, data_scope='team')
        self.ua = User(company=self.a, designation='Telecaller', role='Employee', email='a@x.com')
        self.ub = User(company=self.b, designation='Telecaller', role='Employee', email='b@x.com')

    def test_same_title_different_companies(self):
        self.assertFalse(can_assign_leads(self.ua))
        self.assertTrue(can_assign_leads(self.ub))
        self.assertTrue(is_telecaller(self.ua) and is_telecaller(self.ub))

    def test_scope_is_per_designation(self):
        self.assertEqual(data_scope(self.ua), '')
        self.assertEqual(data_scope(self.ub), 'team')

    def test_one_person_can_be_an_exception(self):
        u = User(company=self.a, designation='Telecaller', role='Employee', email='c@x.com',
                 extra_capabilities=['sales.lead.assign'])
        self.assertTrue(can_assign_leads(u))
        u2 = User(company=self.b, designation='Telecaller', role='Employee', email='d@x.com',
                  denied_capabilities=['sales.lead.assign'])
        self.assertFalse(can_assign_leads(u2))

    def test_unknown_keys_are_ignored(self):
        u = User(company=self.a, designation='Telecaller', role='Employee', email='e@x.com',
                 extra_capabilities=['sales.made.up'])
        self.assertFalse(user_can(u, 'sales.made.up'))


class CapabilityApiTests(TestCase):
    """Admins edit a designation's permissions; nobody else can."""

    def setUp(self):
        from rest_framework.test import APIClient
        self.co = Company.objects.create(code='API', name='Api Co')
        self.admin = User.objects.create_user('adm@x.com', company=self.co, user_code='A1', password='x',
                                              name='Admin', role='Admin')
        self.emp = User.objects.create_user('emp@x.com', company=self.co, user_code='E1', password='x',
                                            name='Emp', role='Employee', designation='Telecaller')
        self.desig = Designation.objects.create(company=self.co, name='Telecaller', module='Sales',
                                                capabilities=['sales.pipeline.telecalling'], capabilities_set=True)
        self.api = APIClient()

    def test_catalogue_lists_the_vocabulary(self):
        self.api.force_authenticate(self.admin)
        d = self.api.get('/api/auth/designations/capabilities/').json()
        keys = [c['key'] for c in d['capabilities']]
        self.assertIn('sales.lead.assign', keys)
        self.assertTrue(all('label' in c and 'module' in c for c in d['capabilities']))
        self.assertTrue(any(p['key'] == 'telecaller' for p in d['presets']))

    def test_admin_can_grant_and_it_takes_effect(self):
        self.api.force_authenticate(self.admin)
        self.assertFalse(can_assign_leads(User.objects.get(pk=self.emp.pk)))
        r = self.api.patch(f'/api/auth/designations/{self.desig.id}/',
                           {'capabilities': ['sales.pipeline.telecalling', 'sales.lead.assign'],
                            'data_scope': 'team'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        fresh = User.objects.get(pk=self.emp.pk)
        self.assertTrue(can_assign_leads(fresh))
        self.assertEqual(data_scope(fresh), 'team')

    def test_unknown_key_is_refused(self):
        self.api.force_authenticate(self.admin)
        r = self.api.patch(f'/api/auth/designations/{self.desig.id}/', {'capabilities': ['sales.made.up']}, format='json')
        self.assertEqual(r.status_code, 400)

    def test_a_non_admin_cannot_change_permissions(self):
        self.api.force_authenticate(self.emp)
        r = self.api.patch(f'/api/auth/designations/{self.desig.id}/', {'capabilities': []}, format='json')
        self.assertEqual(r.status_code, 403)

    def test_me_reports_what_the_person_may_do(self):
        self.api.force_authenticate(self.emp)
        d = self.api.get('/api/auth/me/').json()
        self.assertEqual(d['capabilities'], ['sales.pipeline.telecalling'])
