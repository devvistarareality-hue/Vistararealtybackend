"""Every switch in Designation Master, exercised one at a time.

The editor offers three kinds of switch — an action, a menu item, a dashboard —
plus the record scope. This file walks the whole vocabulary rather than a sample,
so a new capability or screen that nothing honours is caught here instead of in
production.
"""
from datetime import date

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.capabilities import (CAPABILITIES, DASHBOARD_ROLES, DASHBOARDS, DATA_SCOPES,
                                   SCREENS, bump_permissions_version, can_see_screen,
                                   dashboard_for, data_scope, modules_of, user_can)
from accounts.models import Designation, User
from companies.models import Company


def _module_of(key):
    for k, _, module, _ in CAPABILITIES:
        if k == key:
            return module
    return ''


class EveryCapabilityTests(TestCase):
    """Each action can be given and taken away on its own, and taking one away
    leaves the others alone."""

    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='EVC', name='Every Co')
        cls.desigs = {}
        cls.people = {}
        for module in ('Sales', 'AR', 'Club 1000'):
            name = f'{module} Person'
            cls.desigs[module] = Designation.objects.create(company=cls.co, name=name, module=module)
            cls.people[module] = User.objects.create_user(
                f'{module.lower().replace(" ", "")}@x.com', company=cls.co,
                user_code=f'E-{module[:3].upper()}', password='x', name=name, role='Manager',
                designation=name, modules=['Sales', 'AR', 'Club 1000'],
                manager_modules=['Club 1000'])

    def test_every_capability_can_be_switched_on_and_off(self):
        for key, label, module, _help in CAPABILITIES:
            target = 'Sales' if module in ('Sales', 'Channel Partner') else module
            desig, person = self.desigs[target], self.people[target]
            # On.
            desig.capabilities = [key]
            desig.capabilities_set = True
            desig.save(update_fields=['capabilities', 'capabilities_set'])
            fresh = User.objects.get(pk=person.pk)
            self.assertTrue(user_can(fresh, key), f'{key} could not be switched on')
            # Off — and the module's other actions go with it, since only this one
            # was ticked.
            desig.capabilities = []
            desig.save(update_fields=['capabilities'])
            fresh = User.objects.get(pk=person.pk)
            own = {k for k, _, m, _ in CAPABILITIES if m in modules_of(target)}
            if key in own:
                self.assertFalse(user_can(fresh, key), f'{key} could not be switched off')

    def test_unticking_one_leaves_the_rest(self):
        desig, person = self.desigs['AR'], self.people['AR']
        ar_keys = [k for k, _, m, _ in CAPABILITIES if m == 'AR']
        for key in ar_keys:
            desig.capabilities = [k for k in ar_keys if k != key]
            desig.capabilities_set = True
            desig.save(update_fields=['capabilities', 'capabilities_set'])
            fresh = User.objects.get(pk=person.pk)
            self.assertFalse(user_can(fresh, key), key)
            for other in ar_keys:
                if other != key:
                    self.assertTrue(user_can(fresh, other), f'{other} lost when {key} was unticked')


class EveryScreenTests(TestCase):
    """Each menu item can be shown and hidden on its own."""

    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='EVS', name='Every Screens Co')

    def _person_for(self, module):
        name = f'{module} Title'
        desig, _ = Designation.objects.get_or_create(company=self.co, name=name, module=module)
        code = 'S-' + ''.join(c for c in module.upper() if c.isalpha())[:6]
        user = User.objects.filter(company=self.co, user_code=code).first()
        if user is None:
            user = User.objects.create_user(
                f'{code.lower()}@s.com', company=self.co, user_code=code, password='x',
                name=name, role='Manager', designation=name,
                modules=['Sales', 'AR', 'Club 1000', 'Accounts & Finance', 'HR',
                         'Execution', 'Purchase', 'Land'])
        return desig, user

    def test_every_screen_can_be_shown_and_hidden(self):
        for key, label, module in SCREENS:
            owner = 'Sales' if module == 'Channel Partner' else module
            desig, user = self._person_for(owner)
            desig.screens = [key]
            desig.screens_set = True
            desig.save(update_fields=['screens', 'screens_set'])
            fresh = User.objects.get(pk=user.pk)
            self.assertTrue(can_see_screen(fresh, key), f'{key} could not be shown')
            desig.screens = [k for k, _, _ in SCREENS if k != key]
            desig.save(update_fields=['screens'])
            fresh = User.objects.get(pk=user.pk)
            self.assertFalse(can_see_screen(fresh, key), f'{key} could not be hidden')


class EveryDashboardTests(TestCase):
    """Each dashboard the editor offers can be pinned, and is reported to the
    website and the app."""

    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='EVD', name='Every Dash Co')
        cls.admin = User.objects.create_user('a@evd.com', company=cls.co, user_code='D-A',
                                             password='x', name='Admin', role='Admin')
        cls.desig = Designation.objects.create(company=cls.co, name='Anything', module='Sales')
        cls.person = User.objects.create_user('p@evd.com', company=cls.co, user_code='D-P',
                                              password='x', name='Person', role='Manager',
                                              designation='Anything', modules=['Sales'])

    def test_every_dashboard_can_be_pinned(self):
        api = APIClient()
        api.force_authenticate(self.admin)
        for value, label, module, role in DASHBOARDS:
            r = api.patch(f'/api/auth/designations/{self.desig.id}/',
                          {'dashboard': value}, format='json')
            self.assertEqual(r.status_code, 200, f'{value}: {r.content}')
            self.assertEqual(dashboard_for(User.objects.get(pk=self.person.pk)), value)

    def test_every_role_can_be_handed_a_dashboard(self):
        api = APIClient()
        api.force_authenticate(self.admin)
        for role in DASHBOARD_ROLES:
            r = api.post('/api/auth/role-dashboards/',
                         {'module': 'Sales', 'view': 'manager', 'roles': [role]}, format='json')
            self.assertEqual(r.status_code, 200, f'{role}: {r.content}')
        rows = api.get('/api/auth/role-dashboards/?module=Sales').json()
        self.assertEqual({r['role'] for r in rows}, set(DASHBOARD_ROLES))


class EveryScopeTests(TestCase):
    """Each record scope narrows every tile on the dashboard the same way —
    leads, site visits and closures always counting the same population."""

    def setUp(self):
        from sales.models import Closure, Lead, LeadSource, Project, SiteVisit
        cache.clear()
        self.co = Company.objects.create(code='EVSC', name='Every Scope Co')
        self.p1 = Project.objects.create(company=self.co, name='One')
        self.p2 = Project.objects.create(company=self.co, name='Two')
        src = LeadSource.objects.create(company=self.co, name='Meta')
        self.desig = Designation.objects.create(company=self.co, name='Desk Head', module='Sales')
        self.head = User.objects.create_user('h@evsc.com', company=self.co, user_code='SC-H',
                                             password='x', name='Head', role='Manager',
                                             modules=['Sales'], designation='Desk Head')
        self.mate = User.objects.create_user('m@evsc.com', company=self.co, user_code='SC-M',
                                             password='x', name='Mate', role='Employee',
                                             modules=['Sales'], designation='Desk Head',
                                             reporting_manager=self.head)
        self.outsider = User.objects.create_user('o@evsc.com', company=self.co, user_code='SC-O',
                                                 password='x', name='Outsider', role='Employee',
                                                 modules=['Sales'], designation='Desk Head')
        for owner, project in ((self.head, self.p1), (self.mate, self.p1), (self.outsider, self.p2)):
            lead = Lead.objects.create(company=self.co, project=project, source=src,
                                       name=f'{owner.name} lead', phone='9000000004', stm=owner)
            SiteVisit.objects.create(lead=lead, stm=owner, status='completed',
                                     visited_at=timezone.now())
            Closure.objects.create(company=self.co, lead=lead, project=project, stm=owner,
                                   closure_date=date.today(), status='approved')

    def _tiles(self):
        api = APIClient()
        api.force_authenticate(User.objects.get(pk=self.head.pk))
        d = api.get('/api/sales/stats/').json()
        return d['total_leads'], d['sv_done'], d['closures'], d['active_projects']

    def _set(self, scope):
        self.desig.data_scope = scope
        self.desig.save(update_fields=['data_scope'])
        bump_permissions_version(self.co.id)
        self.assertEqual(data_scope(User.objects.get(pk=self.head.pk)), scope)

    def test_each_scope_counts_one_population(self):
        expected = {
            '': (3, 3, 3),          # role and the reporting tree — a manager sees the desk
            'own': (1, 1, 1),       # their own records
            'team': (2, 2, 2),      # theirs and their report's
            'company': (3, 3, 3),   # everything
            'projects': (3, 3, 3),  # no project assignment → everything, as before
        }
        for scope, _label in DATA_SCOPES:
            self._set(scope)
            leads, visits, closures, projects = self._tiles()
            self.assertEqual((leads, visits, closures), expected[scope], f'scope {scope!r}')
            # Active projects is the company's list either way — never a count of
            # the person's own records.
            self.assertEqual(projects, 2, f'scope {scope!r} changed the project count')
