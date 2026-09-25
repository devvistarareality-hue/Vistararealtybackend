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
                         'Task Allocation', 'Purchase', 'Land'])
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


class EveryModuleScopeTests(TestCase):
    """The record scope, module by module. Each module's own list has to answer
    to the same setting — or, where the module's records are the company's book
    by design, to say so plainly here."""

    def setUp(self):
        from datetime import date
        from club1000.models import Investor, Scheme
        from sales.models import Booking, Closure, Lead, LeadSource, Plot, Project, SiteVisit
        cache.clear()
        self.co = Company.objects.create(code='MOD', name='Module Co')
        self.proj = Project.objects.create(company=self.co, name='Tundav')
        src = LeadSource.objects.create(company=self.co, name='Channel Partner')
        self.head = User.objects.create_user('h@mod.com', company=self.co, user_code='M-H',
                                             password='x', name='Head', role='Manager',
                                             modules=['Sales', 'AR', 'Club 1000'],
                                             manager_modules=['Club 1000'],
                                             designation='Desk Head')
        self.mate = User.objects.create_user('m@mod.com', company=self.co, user_code='M-M',
                                             password='x', name='Mate', role='Employee',
                                             modules=['Sales', 'AR', 'Club 1000'],
                                             designation='Desk Head', reporting_manager=self.head)
        self.desig = Designation.objects.create(company=self.co, name='Desk Head', module='Sales')
        self.club_desig = Designation.objects.create(company=self.co, name='Club Head',
                                                     module='Club 1000')
        # Sales / Channel Partner: a lead, a visit and a closure each.
        for owner in (self.head, self.mate):
            lead = Lead.objects.create(company=self.co, project=self.proj, source=src,
                                       name=f'{owner.name} lead', phone='9000000005', stm=owner)
            SiteVisit.objects.create(lead=lead, stm=owner, status='completed',
                                     visited_at=timezone.now())
            Closure.objects.create(company=self.co, lead=lead, project=self.proj, stm=owner,
                                   closure_date=date.today(), status='approved')
        # Club 1000: an investor each.
        scheme = Scheme.objects.create(company=self.co, name='Plan A', tenure_months=12,
                                       min_ticket_size=100000)
        for owner in (self.head, self.mate):
            Investor.objects.create(company=self.co, scheme=scheme, added_by=owner,
                                    name=f'{owner.name} investor', phone='9000000006',
                                    amount_invested=100000, investment_date=date.today(),
                                    maturity_date=date.today(), status='active')

    def _api(self):
        api = APIClient()
        api.force_authenticate(User.objects.get(pk=self.head.pk))
        return api

    def _set(self, desig, scope):
        desig.data_scope = scope
        desig.save(update_fields=['data_scope'])
        bump_permissions_version(self.co.id)

    def test_sales_and_cp_narrow_with_the_scope(self):
        api = self._api()
        d = api.get('/api/sales/stats/').json()
        self.assertEqual((d['total_leads'], d['sv_done'], d['closures']), (2, 2, 2))
        self._set(self.desig, 'own')
        d = self._api().get('/api/sales/stats/').json()
        self.assertEqual((d['total_leads'], d['sv_done'], d['closures']), (1, 1, 1))
        # The Channel Partner cut of the same dashboard narrows too.
        d = self._api().get('/api/sales/stats/?cp_only=true').json()
        self.assertEqual((d['total_leads'], d['sv_done'], d['closures']), (1, 1, 1))

    def test_club_1000_narrows_with_the_scope(self):
        # The old rule: a Club manager sees the desk.
        self.assertEqual(len(self._api().get('/api/club1000/investors/').json()), 2)
        self.club_desig.data_scope = 'own'
        self.club_desig.save(update_fields=['data_scope'])
        # …the scope is read from the person's OWN designation, which is Sales here,
        # so setting it on the Club designation changes nothing for them.
        self.assertEqual(len(self._api().get('/api/club1000/investors/').json()), 2)
        self._set(self.desig, 'own')
        self.assertEqual(len(self._api().get('/api/club1000/investors/').json()), 1)
        self._set(self.desig, 'team')
        self.assertEqual(len(self._api().get('/api/club1000/investors/').json()), 2)

    def test_ar_is_the_company_book_whatever_the_scope(self):
        """AR accounts are not owned by anyone — the receivables book is the
        company's. The scope leaves it alone, by design."""
        before = self._api().get('/api/ar/accounts/').json()['results']
        self._set(self.desig, 'own')
        after = self._api().get('/api/ar/accounts/').json()['results']
        self.assertEqual(len(before), len(after))


class ChannelPartnerIsItsOwnModuleTests(TestCase):
    """Channel Partner used to be a corner of Sales. It is a module now, like AR
    and Club 1000: granted in User Management, with its own designations, menu
    and dashboards."""

    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='CPM', name='CP Module Co')
        cls.admin = User.objects.create_user('a@cpm.com', company=cls.co, user_code='CPM-A',
                                             password='x', name='Admin', role='Admin')
        cls.desig = Designation.objects.create(company=cls.co, name='CP Executive',
                                               module='Channel Partner')
        cls.person = User.objects.create_user('p@cpm.com', company=cls.co, user_code='CPM-P',
                                              password='x', name='Partner Exec', role='Employee',
                                              designation='CP Executive',
                                              modules=['Channel Partner'])

    def test_the_module_is_what_lets_them_in(self):
        from sales.views import can_access_cp_module, has_cp_access, has_sales_access
        self.assertTrue(has_cp_access(self.person))
        self.assertTrue(can_access_cp_module(self.person))
        # Channel Partner works the same lead tables, so it reaches those endpoints…
        self.assertTrue(has_sales_access(self.person))
        # …and someone with neither module is still out.
        outsider = User.objects.create_user('o@cpm.com', company=self.co, user_code='CPM-O',
                                            password='x', name='Outsider', role='Employee',
                                            designation='Nothing', modules=[])
        self.assertFalse(has_cp_access(outsider))

    def test_its_designation_decides_channel_partner_only(self):
        from accounts.capabilities import legacy_capabilities, preset_screens
        caps = legacy_capabilities('CP Executive', 'Channel Partner')
        self.assertIn('sales.pipeline.cp', caps)
        menu = preset_screens('CP Executive', 'Channel Partner')
        self.assertTrue(menu and all(k.startswith('cp.screen.') for k in menu), menu)

    def test_the_editor_lists_it_as_a_module_of_its_own(self):
        api = APIClient()
        api.force_authenticate(self.admin)
        cat = api.get('/api/auth/designations/capabilities/').json()
        caps = {c['module'] for c in cat['capabilities']}
        screens = {c['module'] for c in cat['screens']}
        dashboards = {d['module'] for d in cat['dashboards'] if d['module']}
        self.assertIn('Channel Partner', caps)
        self.assertIn('Channel Partner', screens)
        self.assertIn('Channel Partner', dashboards)
        # The Sales menu no longer carries a Channel Partner item.
        self.assertNotIn('sales.screen.cp', [c['key'] for c in cat['screens']])

    def test_a_sales_designation_says_nothing_about_it(self):
        from accounts.capabilities import legacy_capabilities, preset_screens
        caps = legacy_capabilities('CMO', 'Sales')
        self.assertTrue(all(c.startswith('sales.') for c in caps), caps)
        self.assertNotIn('sales.pipeline.cp', caps)
        menu = preset_screens('CMO', 'Sales')
        self.assertTrue(all(k.startswith('sales.') for k in menu), menu)


class CpPeopleMoveAcrossTests(TestCase):
    """The CP people move from Sales to Channel Partner rather than holding both
    — the transfer an admin would otherwise do by hand in User Management."""

    def test_a_cp_person_ends_up_in_the_cp_module_only(self):
        co = Company.objects.create(code='XFER', name='Transfer Co')
        person = User.objects.create_user('x@xfer.com', company=co, user_code='X-1', password='x',
                                          name='CP Person', role='Manager',
                                          designation='CP Cluster Head',
                                          modules=['Sales'], manager_modules=['Sales'])
        # What the migration does, applied here the same way.
        for field in ('modules', 'manager_modules'):
            mods = [m for m in (getattr(person, field) or []) if m != 'Sales']
            if 'Channel Partner' not in mods:
                mods.append('Channel Partner')
            setattr(person, field, mods)
        person.save(update_fields=['modules', 'manager_modules'])
        fresh = User.objects.get(pk=person.pk)
        self.assertEqual(fresh.modules, ['Channel Partner'])
        self.assertEqual(fresh.manager_modules, ['Channel Partner'])
        from sales.views import has_cp_access, has_sales_access
        self.assertTrue(has_cp_access(fresh))
        # They still reach the shared lead endpoints, because CP works them.
        self.assertTrue(has_sales_access(fresh))


class TheEditorShowsOneModuleTests(TestCase):
    """A designation decides its own module and nothing else — the editor must
    not offer a Sales title the Channel Partner switches, or the other way."""

    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='ONEM', name='One Module Co')
        cls.admin = User.objects.create_user('a@onem.com', company=cls.co, user_code='OM-A',
                                             password='x', name='Admin', role='Admin')

    def test_presets_carry_their_module(self):
        api = APIClient()
        api.force_authenticate(self.admin)
        presets = {p['key']: p['module'] for p in
                   api.get('/api/auth/designations/capabilities/').json()['presets']}
        self.assertEqual(presets['telecaller'], 'Sales')
        self.assertEqual(presets['stm'], 'Sales')
        self.assertEqual(presets['sales_desk'], 'Sales')
        self.assertEqual(presets['cp_executive'], 'Channel Partner')
        self.assertEqual(presets['cp_manager'], 'Channel Partner')

    def test_a_sales_title_is_pre_ticked_with_sales_alone(self):
        from accounts.capabilities import legacy_capabilities, modules_of, preset_screens
        self.assertEqual(tuple(modules_of('Sales')), ('Sales',))
        self.assertEqual(tuple(modules_of('Channel Partner')), ('Channel Partner',))
        caps = legacy_capabilities('Cluster Head', 'Sales')
        self.assertNotIn('sales.pipeline.cp', caps)
        self.assertNotIn('sales.pipeline.cp_manager', caps)
        menu = preset_screens('Cluster Head', 'Sales')
        self.assertTrue(all(k.startswith('sales.') for k in menu), menu)

    def test_a_cp_title_is_pre_ticked_with_channel_partner_alone(self):
        from accounts.capabilities import legacy_capabilities, preset_screens
        caps = legacy_capabilities('CP EXECUTIVE', 'Channel Partner')
        self.assertTrue(all(c.startswith('sales.pipeline.cp') for c in caps), caps)
        menu = preset_screens('CP EXECUTIVE', 'Channel Partner')
        self.assertTrue(all(k.startswith('cp.') for k in menu), menu)


class BookingSourceFilterTests(TestCase):
    """Each module answers for its own book. The Sales module asks for
    source=sales, so partner-sourced bookings stay in Channel Partner and a
    booking is never counted in both."""

    def setUp(self):
        from datetime import date
        from sales.models import Booking, Lead, LeadSource, Project
        cache.clear()
        self.co = Company.objects.create(code='SRCF', name='Source Co')
        proj = Project.objects.create(company=self.co, name='Tundav')
        meta = LeadSource.objects.create(company=self.co, name='Meta')
        cp = LeadSource.objects.create(company=self.co, name='Channel Partner')
        self.admin = User.objects.create_user('a@srcf.com', company=self.co, user_code='SF-A',
                                              password='x', name='Admin', role='Admin',
                                              modules=['Sales'])
        for src, client in ((meta, 'Sales client'), (cp, 'Partner client')):
            lead = Lead.objects.create(company=self.co, project=proj, source=src,
                                       name=client, phone='9000000010', stm=self.admin)
            Booking.objects.create(company=self.co, project=proj, lead=lead, stm=self.admin,
                                   client_name=client, status='sold', booking_date=date.today())

    def _names(self, query):
        api = APIClient()
        api.force_authenticate(User.objects.get(pk=self.admin.pk))
        rows = api.get(f'/api/sales/bookings/?mine=1&status=sold{query}').json()
        rows = rows if isinstance(rows, list) else rows.get('results', [])
        return sorted(r['client_name'] for r in rows)

    def test_both_sides_unless_asked(self):
        self.assertEqual(self._names(''), ['Partner client', 'Sales client'])

    def test_sales_only(self):
        self.assertEqual(self._names('&source=sales'), ['Sales client'])

    def test_channel_partner_only(self):
        self.assertEqual(self._names('&source=cp'), ['Partner client'])
