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
        # Their Sales designation decides Sales…
        sales = [c for c in d['capabilities'] if c.startswith('sales.')]
        self.assertEqual(sales, ['sales.pipeline.telecalling'])
        # …and says nothing about AR or Club 1000, where they keep what anyone
        # with that module always had (module access is still the gate).
        self.assertIn('ar.receipt.record', d['capabilities'])
        self.assertIn('club.investor.manage', d['capabilities'])

    def test_a_sales_designation_is_not_pre_ticked_with_ar(self):
        """The editor opens a Sales designation on Sales, not on every module's
        actions — ticking AR on a Sales title was never what the old code did."""
        from accounts.capabilities import legacy_capabilities, preset_screens
        caps = legacy_capabilities('CMO', 'Sales')
        self.assertTrue(all(c.startswith('sales.') for c in caps), caps)
        screens = preset_screens('CMO', 'Sales')
        self.assertTrue(all(s.startswith(('sales.', 'cp.')) for s in screens), screens)
        # An AR designation is pre-ticked with AR, and only AR.
        ar = legacy_capabilities('AR Officer', 'AR')
        self.assertTrue(all(c.startswith('ar.') for c in ar), ar)


class ModuleActionTests(TestCase):
    """AR and Club 1000 actions can be taken away from a designation."""

    def setUp(self):
        from rest_framework.test import APIClient
        from datetime import date
        from sales.models import Project
        from receivables.models import ARAccount
        from receivables.test_api import make_booking
        self.co = Company.objects.create(code='MOD', name='Mod Co')
        self.proj = Project.objects.create(company=self.co, name='Kalrav 2')
        self.user = User.objects.create_user('ar@x.com', company=self.co, user_code='M1', password='x',
                                             name='AR Person', role='Employee', modules=['AR'],
                                             designation='AR Officer')
        Designation.objects.create(company=self.co, name='AR Officer', module='AR',
                                   capabilities=sorted(legacy_capabilities('AR Officer')), capabilities_set=True)
        make_booking(self.co, self.proj)
        self.api = APIClient()
        self.api.force_authenticate(self.user)
        self.acct_id = self.api.get('/api/ar/accounts/').json()['results'][0]['id']
        self.today = date.today().isoformat()

    def _record(self):
        return self.api.post(f'/api/ar/accounts/{self.acct_id}/receipts/',
                             {'paid_on': self.today, 'amount': 1000, 'mode': 'bank'}, format='json')

    def test_recording_receipts_works_by_default(self):
        self.assertEqual(self._record().status_code, 201)

    def test_a_company_can_take_receipt_entry_away(self):
        d = Designation.objects.get(company=self.co, name='AR Officer')
        d.capabilities = [c for c in d.capabilities if c != 'ar.receipt.record']
        d.save(update_fields=['capabilities'])
        r = self._record()
        self.assertEqual(r.status_code, 403)
        # They can still read the register — only the action was removed.
        self.assertEqual(self.api.get('/api/ar/accounts/').status_code, 200)


class DataScopeTests(TestCase):
    """The scope on a designation decides whose leads a person sees."""

    def setUp(self):
        from sales.models import Lead
        from sales.views import scope_leads_to_role
        self.scope_leads_to_role = scope_leads_to_role
        self.co = Company.objects.create(code='SCO', name='Scope Co')
        self.boss = User.objects.create_user('b@x.com', company=self.co, user_code='S0', password='x',
                                             name='Boss', role='Employee', designation='Team Lead')
        self.rep = User.objects.create_user('r@x.com', company=self.co, user_code='S1', password='x',
                                            name='Rep', role='Employee', designation='Team Lead',
                                            reporting_manager=self.boss)
        self.mine = Lead.objects.create(company=self.co, name='Mine', phone='9000000401', stm=self.boss)
        self.theirs = Lead.objects.create(company=self.co, name='Theirs', phone='9000000402', stm=self.rep)
        self.other = Lead.objects.create(company=self.co, name='Other', phone='9000000403')
        self.desig = Designation.objects.create(company=self.co, name='Team Lead', module='Sales',
                                                capabilities=sorted(legacy_capabilities('Team Lead')),
                                                capabilities_set=True)

    def _names(self, user):
        from sales.models import Lead
        return sorted(self.scope_leads_to_role(Lead.objects.filter(company=self.co), user).values_list('name', flat=True))

    def test_own_only(self):
        self.desig.data_scope = 'own'
        self.desig.save(update_fields=['data_scope'])
        self.assertEqual(self._names(self.boss), ['Mine'])

    def test_team(self):
        self.desig.data_scope = 'team'
        self.desig.save(update_fields=['data_scope'])
        self.assertEqual(self._names(self.boss), ['Mine', 'Theirs'])

    def test_company(self):
        self.desig.data_scope = 'company'
        self.desig.save(update_fields=['data_scope'])
        self.assertEqual(self._names(self.boss), ['Mine', 'Other', 'Theirs'])


class ScreenAndDashboardTests(TestCase):
    """The menu and the dashboard are settings, and default to today's behaviour."""

    def setUp(self):
        from rest_framework.test import APIClient
        self.co = Company.objects.create(code='SCR', name='Scr Co')
        self.admin = User.objects.create_user('sadm@x.com', company=self.co, user_code='S1', password='x',
                                              name='Admin', role='Admin')
        self.tc = User.objects.create_user('stc@x.com', company=self.co, user_code='S2', password='x',
                                           name='Tele', role='Employee', designation='Telecaller')
        self.desig = Designation.objects.create(company=self.co, name='Telecaller', module='Sales')
        self.api = APIClient()

    def test_unset_keeps_the_old_menu_and_dashboard(self):
        from accounts.capabilities import can_see_screen, dashboard_for, screens_for
        self.assertIsNone(screens_for(self.tc))
        self.assertTrue(can_see_screen(self.tc, 'sales.screen.approvals'))   # role rules still decide
        self.assertEqual(dashboard_for(self.tc), '')
        self.api.force_authenticate(self.tc)
        me = self.api.get('/api/auth/me/').json()
        self.assertIsNone(me['screens'])
        self.assertEqual(me['dashboard'], '')

    def test_an_admin_sets_the_menu_and_the_dashboard(self):
        self.api.force_authenticate(self.admin)
        r = self.api.patch(f'/api/auth/designations/{self.desig.id}/', {
            'screens': ['sales.screen.dashboard', 'sales.screen.leads', 'sales.screen.followups'],
            'dashboard': 'telecaller'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        from accounts.capabilities import can_see_screen, dashboard_for
        fresh = User.objects.get(pk=self.tc.pk)
        self.assertTrue(can_see_screen(fresh, 'sales.screen.leads'))
        self.assertFalse(can_see_screen(fresh, 'sales.screen.approvals'))
        self.assertEqual(dashboard_for(fresh), 'telecaller')
        self.api.force_authenticate(fresh)
        me = self.api.get('/api/auth/me/').json()
        self.assertEqual(me['screens'], ['sales.screen.dashboard', 'sales.screen.followups', 'sales.screen.leads'])
        self.assertEqual(me['dashboard'], 'telecaller')

    def test_unticking_every_screen_hides_the_lot(self):
        """An admin who clears the menu means it: an empty list is a configured
        empty menu, not "nothing configured" (which would show everything)."""
        self.api.force_authenticate(self.admin)
        r = self.api.patch(f'/api/auth/designations/{self.desig.id}/',
                           {'screens': []}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(r.json()['screens_set'])
        from accounts.capabilities import can_see_screen, screens_for
        fresh = User.objects.get(pk=self.tc.pk)
        self.assertEqual(screens_for(fresh), set())
        self.assertFalse(can_see_screen(fresh, 'sales.screen.leads'))
        self.assertFalse(can_see_screen(fresh, 'cp.screen.dashboard'))
        self.api.force_authenticate(fresh)
        self.assertEqual(self.api.get('/api/auth/me/').json()['screens'], [])

    def test_the_channel_partner_menu_has_its_own_keys(self):
        """A CP Cluster Head's whole sidebar is the Channel Partner module, so
        each of its items is switchable on its own."""
        from accounts.capabilities import SCREEN_KEYS, preset_screens
        for key in ('cp.screen.dashboard', 'cp.screen.leads', 'cp.screen.sitevisits',
                    'cp.screen.followups', 'cp.screen.booking',
                    'cp.screen.myteam', 'cp.screen.approvals'):
            self.assertIn(key, SCREEN_KEYS)
        # A CP title's preset ticks them, so setting one up changes nothing at first.
        head = preset_screens('CP Cluster Head')
        self.assertIn('cp.screen.leads', head)
        self.assertIn('cp.screen.myteam', head)
        exec_ = preset_screens('CP Executive')
        self.assertIn('cp.screen.booking', exec_)
        self.assertNotIn('cp.screen.myteam', exec_)   # no reports to show

    def test_every_module_offers_a_dashboard_per_role(self):
        """Each module's Dashboard has a role filter; the editor lists the same
        views so a designation can be pinned to one."""
        self.api.force_authenticate(self.admin)
        cat = self.api.get('/api/auth/designations/capabilities/').json()
        # The roles are the ones User Management creates people with.
        self.assertEqual(cat['dashboard_roles'],
                         ['Director', 'General Manager', 'Manager', 'Employee', 'Intern'])
        by_module = {}
        for d in cat['dashboards']:
            if d['module']:
                by_module.setdefault(d['module'], set()).add(d['role'])
        self.assertEqual(by_module['Sales'], {'Employee', 'Manager', 'General Manager', 'Director'})
        self.assertEqual(by_module['Channel Partner'], {'Employee', 'Manager', 'General Manager', 'Director'})
        self.assertIn('Club 1000', by_module)
        # The default row has no role, so a role filter never hides it.
        self.assertEqual([d for d in cat['dashboards'] if d['value'] == ''][0]['role'], '')

    def test_a_dashboard_can_be_pinned_by_role(self):
        self.api.force_authenticate(self.admin)
        r = self.api.patch(f'/api/auth/designations/{self.desig.id}/',
                           {'dashboard': 'cp_manager'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        from accounts.capabilities import dashboard_for
        self.assertEqual(dashboard_for(User.objects.get(pk=self.tc.pk)), 'cp_manager')

    def test_bad_values_are_refused(self):
        self.api.force_authenticate(self.admin)
        self.assertEqual(self.api.patch(f'/api/auth/designations/{self.desig.id}/',
                                        {'screens': ['sales.screen.nope']}, format='json').status_code, 400)
        self.assertEqual(self.api.patch(f'/api/auth/designations/{self.desig.id}/',
                                        {'dashboard': 'wizard'}, format='json').status_code, 400)

    def test_the_editor_is_pre_ticked_with_the_usual_menu(self):
        self.api.force_authenticate(self.admin)
        rows = self.api.get('/api/auth/designations/').json()
        row = [d for d in rows if d['name'] == 'Telecaller'][0]
        self.assertIn('sales.screen.leads', row['effective_screens'])
        self.assertNotIn('sales.screen.approvals', row['effective_screens'])
        cat = self.api.get('/api/auth/designations/capabilities/').json()
        self.assertTrue(any(s['key'] == 'ar.screen.collections' for s in cat['screens']))
        self.assertTrue(any(d['value'] == 'director' for d in cat['dashboards']))
