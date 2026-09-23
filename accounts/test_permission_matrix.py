"""The whole permission chain, module by module.

Two gates decide what a person can do, and they are not the same question:

    1. Module access  — User Management: may they enter AR at all?
    2. Designation    — Designation Master → Permissions: inside AR, what may
                        they do, which screens, whose records?

This file checks both ends for every module that has a vocabulary: a ticked
capability does nothing without the module, and the module alone is not enough
once a company has unticked the action.
"""
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.capabilities import (CAPABILITY_KEYS, SCREEN_KEYS, can_see_screen,
                                   legacy_capabilities, user_can)
from accounts.models import Designation, User
from companies.models import Company


def _designation(co, name, module, caps=None, screens=None):
    return Designation.objects.create(
        company=co, name=name, module=module,
        capabilities=sorted(caps if caps is not None else legacy_capabilities(name)),
        capabilities_set=True,
        screens=sorted(screens or []), screens_set=screens is not None)


class ModuleGateTests(TestCase):
    """A capability is refinement inside a module, never a way into one."""

    def setUp(self):
        self.co = Company.objects.create(code='GATE', name='Gate Co')
        # Everything ticked — but no modules at all.
        _designation(self.co, 'Everything', 'Sales', caps=list(CAPABILITY_KEYS))
        self.outsider = User.objects.create_user(
            'out@x.com', company=self.co, user_code='G1', password='x', name='Outsider',
            role='Employee', designation='Everything', modules=[])

    def test_every_capability_ticked_but_no_module(self):
        from receivables.permissions import ar_can, has_ar_access
        from club1000.views import club_can
        self.assertTrue(user_can(self.outsider, 'ar.receipt.record'))     # the tick is there
        self.assertFalse(has_ar_access(self.outsider))                    # the door is not
        self.assertFalse(ar_can(self.outsider, 'ar.receipt.record'))
        self.assertFalse(club_can(self.outsider, 'club.payout.mark_paid'))

    def test_the_api_refuses_them(self):
        api = APIClient()
        api.force_authenticate(self.outsider)
        self.assertEqual(api.get('/api/ar/accounts/').status_code, 403)


class ArMatrixTests(TestCase):
    """AR: the module lets them in, the designation decides what they may do."""

    def setUp(self):
        from datetime import date
        from sales.models import Project
        from receivables.test_api import make_booking
        self.co = Company.objects.create(code='ARM', name='AR Co')
        proj = Project.objects.create(company=self.co, name='Kalrav 9')
        make_booking(self.co, proj)
        self.desig = _designation(self.co, 'AR Officer', 'AR')
        self.user = User.objects.create_user('arm@x.com', company=self.co, user_code='A1', password='x',
                                             name='AR Person', role='Employee', modules=['AR'],
                                             designation='AR Officer')
        self.api = APIClient()
        self.api.force_authenticate(self.user)
        self.acct = self.api.get('/api/ar/accounts/').json()['results'][0]['id']
        self.today = date.today().isoformat()

    def _record(self):
        return self.api.post(f'/api/ar/accounts/{self.acct}/receipts/',
                             {'paid_on': self.today, 'amount': 500, 'mode': 'bank'}, format='json')

    def test_each_ar_action_can_be_taken_away_on_its_own(self):
        self.assertEqual(self._record().status_code, 201)
        self.desig.capabilities = [c for c in self.desig.capabilities if c != 'ar.receipt.record']
        self.desig.save(update_fields=['capabilities'])
        # A real request loads the person afresh; the test client would otherwise
        # keep handing the view the instance whose capabilities it already cached.
        self.api.force_authenticate(User.objects.get(pk=self.user.pk))
        self.assertEqual(self._record().status_code, 403)
        # Untouched actions still work: follow-ups were never unticked.
        self.assertTrue(user_can(User.objects.get(pk=self.user.pk), 'ar.followup.manage'))

    def test_the_ar_menu_follows_the_ticks(self):
        self.desig.screens = ['ar.screen.dashboard', 'ar.screen.register']
        self.desig.screens_set = True
        self.desig.save(update_fields=['screens', 'screens_set'])
        fresh = User.objects.get(pk=self.user.pk)
        self.assertTrue(can_see_screen(fresh, 'ar.screen.register'))
        self.assertFalse(can_see_screen(fresh, 'ar.screen.collections'))
        self.assertFalse(can_see_screen(fresh, 'ar.screen.import'))


class ClubMatrixTests(TestCase):
    """Club 1000: the manager gate still applies, capabilities refine it."""

    def setUp(self):
        self.co = Company.objects.create(code='CLB', name='Club Co')
        self.desig = _designation(self.co, 'Club Manager', 'Club 1000')
        self.user = User.objects.create_user('clb@x.com', company=self.co, user_code='C1', password='x',
                                             name='Club Person', role='Manager',
                                             designation='Club Manager',
                                             modules=['Club 1000'], manager_modules=['Club 1000'])

    def test_marking_a_payout_paid_can_be_taken_away(self):
        from club1000.views import club_can
        self.assertTrue(club_can(self.user, 'club.payout.mark_paid'))
        self.desig.capabilities = [c for c in self.desig.capabilities if c != 'club.payout.mark_paid']
        self.desig.save(update_fields=['capabilities'])
        fresh = User.objects.get(pk=self.user.pk)
        self.assertFalse(club_can(fresh, 'club.payout.mark_paid'))
        self.assertTrue(club_can(fresh, 'club.investor.manage'))   # the other one is untouched

    def test_the_club_menu_has_a_key_for_every_item(self):
        for key in ('club.screen.dashboard', 'club.screen.leads', 'club.screen.followups',
                    'club.screen.investors', 'club.screen.schemes', 'club.screen.payouts',
                    'club.screen.rewards', 'club.screen.approvals', 'club.screen.myteam'):
            self.assertIn(key, SCREEN_KEYS)


class SalesMatrixTests(TestCase):
    """Sales and Channel Partner: pipeline, lead assignment and the menu."""

    def setUp(self):
        self.co = Company.objects.create(code='SLM', name='Sales Co')
        self.tc_desig = _designation(self.co, 'Telecaller', 'Sales')
        self.tc = User.objects.create_user('tc@x.com', company=self.co, user_code='S1', password='x',
                                           name='Tele', role='Employee', modules=['Sales'],
                                           designation='Telecaller')
        self.head_desig = _designation(self.co, 'CP Cluster Head', 'Sales')
        self.head = User.objects.create_user('cph@x.com', company=self.co, user_code='S2', password='x',
                                             name='Head', role='Manager', modules=['Sales'],
                                             designation='CP Cluster Head')

    def test_pipelines_follow_the_ticks(self):
        from sales.views import can_assign_leads, is_cp_manager, is_telecaller
        self.assertTrue(is_telecaller(self.tc))
        self.assertFalse(can_assign_leads(self.tc))          # a telecaller never could
        self.assertTrue(is_cp_manager(self.head))
        self.tc_desig.capabilities = ['sales.lead.assign']
        self.tc_desig.save(update_fields=['capabilities'])
        fresh = User.objects.get(pk=self.tc.pk)
        self.assertFalse(is_telecaller(fresh))
        self.assertTrue(can_assign_leads(fresh))

    def test_the_channel_partner_menu_can_be_cut_down(self):
        self.head_desig.screens = ['cp.screen.leads', 'cp.screen.booking']
        self.head_desig.screens_set = True
        self.head_desig.save(update_fields=['screens', 'screens_set'])
        fresh = User.objects.get(pk=self.head.pk)
        self.assertTrue(can_see_screen(fresh, 'cp.screen.leads'))
        for hidden in ('cp.screen.dashboard', 'cp.screen.approvals', 'cp.screen.myteam',
                       'sales.screen.leads'):
            self.assertFalse(can_see_screen(fresh, hidden), hidden)


class VocabularyTests(TestCase):
    """Every menu item the web and the app draw has a key here, and the editor
    can reach all of them."""

    def test_every_module_with_a_menu_is_represented(self):
        modules = {m for _, _, m in __import__('accounts.capabilities', fromlist=['SCREENS']).SCREENS}
        for name in ('Sales', 'Channel Partner', 'Accounts & Finance', 'AR', 'Club 1000'):
            self.assertIn(name, modules)

    def test_keys_are_unique(self):
        self.assertEqual(len(SCREEN_KEYS), len(set(SCREEN_KEYS)))
        self.assertEqual(len(CAPABILITY_KEYS), len(set(CAPABILITY_KEYS)))


class DashboardFollowsTheTreeTests(TestCase):
    """Pinning a dashboard changes which view opens, never what the numbers
    count: the figures stay scoped by role and the reporting tree."""

    def setUp(self):
        from django.core.cache import cache
        from sales.models import Lead, LeadSource, Project
        # Each test rolls back, so user ids repeat while the local cache does not
        # reset — clear it or one test's figures answer another's request.
        cache.clear()
        self.co = Company.objects.create(code='TREE', name='Tree Co')
        self.proj = Project.objects.create(company=self.co, name='Tundav')
        self.src = LeadSource.objects.create(company=self.co, name='Meta')
        self.desig = _designation(self.co, 'Telecaller', 'Sales')
        self.boss = User.objects.create_user('boss@x.com', company=self.co, user_code='T0', password='x',
                                             name='Boss', role='Manager', modules=['Sales'],
                                             designation='Sales Head')
        self.tc = User.objects.create_user('tc@x.com', company=self.co, user_code='T1', password='x',
                                           name='Tele', role='Employee', modules=['Sales'],
                                           designation='Telecaller', reporting_manager=self.boss)
        self.other = User.objects.create_user('other@x.com', company=self.co, user_code='T2', password='x',
                                              name='Other', role='Employee', modules=['Sales'],
                                              designation='Telecaller', reporting_manager=self.boss)
        for owner, name in ((self.tc, 'Mine'), (self.other, 'Theirs'), (self.other, 'Theirs too')):
            Lead.objects.create(company=self.co, project=self.proj, source=self.src,
                                name=name, phone='9000000000', telecaller=owner)

    def _my_total(self, user):
        api = APIClient()
        api.force_authenticate(User.objects.get(pk=user.pk))
        return api.get('/api/sales/stats/').json()['total_leads']

    def test_pinning_the_manager_dashboard_shows_no_one_else_s_numbers(self):
        self.assertEqual(self._my_total(self.tc), 1)          # their own lead
        self.desig.dashboard = 'manager'
        self.desig.save(update_fields=['dashboard'])
        from accounts.capabilities import dashboard_for
        self.assertEqual(dashboard_for(User.objects.get(pk=self.tc.pk)), 'manager')
        # The Manager view opens, but the figures are still theirs alone.
        self.assertEqual(self._my_total(self.tc), 1)

    def test_a_manager_sees_the_tree_below_them(self):
        self.assertEqual(self._my_total(self.boss), 3)


class RoleDashboardTests(TestCase):
    """The Copy button on a module's Dashboard: give this dashboard to another
    role. It decides which view opens, never what the figures count."""

    def setUp(self):
        self.co = Company.objects.create(code='RDSH', name='Dash Co')
        self.admin = User.objects.create_user('a@x.com', company=self.co, user_code='D1', password='x',
                                              name='Admin', role='Admin')
        self.emp = User.objects.create_user('e@x.com', company=self.co, user_code='D2', password='x',
                                            name='Emp', role='Employee', modules=['Sales'])
        self.api = APIClient()

    def test_an_admin_copies_a_dashboard_to_other_roles(self):
        self.api.force_authenticate(self.admin)
        r = self.api.post('/api/auth/role-dashboards/',
                          {'module': 'Sales', 'view': 'manager',
                           'roles': ['Manager', 'General Manager']}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        rows = self.api.get('/api/auth/role-dashboards/?module=Sales').json()
        self.assertEqual({x['role']: x['view'] for x in rows},
                         {'Manager': 'manager', 'General Manager': 'manager'})

    def test_copying_again_replaces_it(self):
        self.api.force_authenticate(self.admin)
        for view in ('manager', 'director'):
            self.api.post('/api/auth/role-dashboards/',
                          {'module': 'Sales', 'view': view, 'roles': ['Manager']}, format='json')
        rows = self.api.get('/api/auth/role-dashboards/?module=Sales').json()
        self.assertEqual(rows, [{'module': 'Sales', 'role': 'Manager', 'view': 'director'}])

    def test_only_an_admin_may_set_it(self):
        self.api.force_authenticate(self.emp)
        r = self.api.post('/api/auth/role-dashboards/',
                          {'module': 'Sales', 'view': 'manager', 'roles': ['Manager']}, format='json')
        self.assertEqual(r.status_code, 403)

    def test_unknown_dashboard_or_role_is_refused(self):
        self.api.force_authenticate(self.admin)
        self.assertEqual(self.api.post('/api/auth/role-dashboards/',
                                       {'module': 'Sales', 'view': 'nope', 'roles': ['Manager']},
                                       format='json').status_code, 400)
        self.assertEqual(self.api.post('/api/auth/role-dashboards/',
                                       {'module': 'Sales', 'view': 'manager', 'roles': ['Wizard']},
                                       format='json').status_code, 400)

    def test_another_company_never_sees_it(self):
        self.api.force_authenticate(self.admin)
        self.api.post('/api/auth/role-dashboards/',
                      {'module': 'Sales', 'view': 'manager', 'roles': ['Manager']}, format='json')
        other = Company.objects.create(code='OTH2', name='Other')
        outsider = User.objects.create_user('o@x.com', company=other, user_code='O9', password='x',
                                            name='Other Admin', role='Admin')
        api2 = APIClient()
        api2.force_authenticate(outsider)
        self.assertEqual(api2.get('/api/auth/role-dashboards/?module=Sales').json(), [])


class CachedFiguresFollowPermissionsTests(TestCase):
    """Changing a designation has to change the numbers at once — not when a
    cache happens to expire."""

    def setUp(self):
        from django.core.cache import cache
        from sales.models import Lead, LeadSource, Project
        cache.clear()
        self.co = Company.objects.create(code='CACH', name='Cache Co')
        proj = Project.objects.create(company=self.co, name='Tundav')
        src = LeadSource.objects.create(company=self.co, name='Meta')
        self.desig = _designation(self.co, 'Telecaller', 'Sales')
        self.boss = User.objects.create_user('b@x.com', company=self.co, user_code='C0', password='x',
                                             name='Boss', role='Manager', modules=['Sales'],
                                             designation='Sales Head')
        self.tc = User.objects.create_user('t@x.com', company=self.co, user_code='C1', password='x',
                                           name='Tele', role='Employee', modules=['Sales'],
                                           designation='Telecaller', reporting_manager=self.boss)
        other = User.objects.create_user('o@x.com', company=self.co, user_code='C2', password='x',
                                         name='Other', role='Employee', modules=['Sales'],
                                         designation='Telecaller', reporting_manager=self.boss)
        for owner in (self.tc, other):
            Lead.objects.create(company=self.co, project=proj, source=src,
                                name=owner.name, phone='9000000002', telecaller=owner)

    def _total(self):
        api = APIClient()
        api.force_authenticate(User.objects.get(pk=self.tc.pk))
        return api.get('/api/sales/stats/').json()['total_leads']

    def test_widening_the_scope_shows_at_once(self):
        self.assertEqual(self._total(), 1)          # warms the cache
        self.desig.data_scope = 'company'
        self.desig.save(update_fields=['data_scope'])
        from accounts.capabilities import bump_permissions_version
        bump_permissions_version(self.co.id)        # what the API does on save
        self.assertEqual(self._total(), 2)

    def test_the_api_bumps_it_for_us(self):
        api = APIClient()
        admin = User.objects.create_user('ad@x.com', company=self.co, user_code='C3', password='x',
                                         name='Admin', role='Admin')
        api.force_authenticate(admin)
        self.assertEqual(self._total(), 1)
        r = api.patch(f'/api/auth/designations/{self.desig.id}/',
                      {'data_scope': 'company'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self._total(), 2)


class CpDesignationDashboardTests(TestCase):
    """A CP designation pinned to a Sales dashboard — "give the CP Cluster Head
    the Manager dashboard" — must actually open one."""

    def setUp(self):
        self.co = Company.objects.create(code='CPDS', name='CP Dash Co')
        self.desig = _designation(self.co, 'CP CLUSTER HEAD', 'Sales')
        self.head = User.objects.create_user('cph@x.com', company=self.co, user_code='P1',
                                             password='x', name='CP Head', role='Manager',
                                             modules=['Sales'], designation='CP CLUSTER HEAD')

    def test_the_pin_is_reported_to_both_clients(self):
        self.desig.dashboard = 'manager'
        self.desig.save(update_fields=['dashboard'])
        from accounts.capabilities import dashboard_for
        self.assertEqual(dashboard_for(User.objects.get(pk=self.head.pk)), 'manager')
        api = APIClient()
        api.force_authenticate(User.objects.get(pk=self.head.pk))
        self.assertEqual(api.get('/api/auth/me/').json()['dashboard'], 'manager')

    def test_saving_the_cp_module_keeps_its_tabs(self):
        """An older screen ticked "Channel Partner" without its tabs, which left
        a CP person with an empty sidebar. Saving that list now brings the
        module's own tabs with it."""
        api = APIClient()
        admin = User.objects.create_user('ad@cpds.com', company=self.co, user_code='P9',
                                         password='x', name='Admin', role='Admin')
        api.force_authenticate(admin)
        r = api.patch(f'/api/auth/designations/{self.desig.id}/', {'screens': [
            'sales.screen.dashboard', 'sales.screen.cp', 'sales.screen.leads',
            'sales.screen.myteam']}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        from accounts.capabilities import can_see_screen
        fresh = User.objects.get(pk=self.head.pk)
        for key in ('cp.screen.dashboard', 'cp.screen.leads', 'cp.screen.booking'):
            self.assertTrue(can_see_screen(fresh, key), key)
        # Unticking Channel Partner itself still hides the module outright.
        api.patch(f'/api/auth/designations/{self.desig.id}/',
                  {'screens': ['sales.screen.dashboard']}, format='json')
        fresh = User.objects.get(pk=self.head.pk)
        self.assertFalse(can_see_screen(fresh, 'cp.screen.dashboard'))

    def test_the_cp_menu_is_pre_ticked_with_cp_screens(self):
        """The editor must offer the Channel Partner tabs for a CP title —
        without them a save would wipe that person's whole menu."""
        from accounts.capabilities import preset_screens
        menu = preset_screens('CP CLUSTER HEAD', 'Sales')
        for key in ('cp.screen.dashboard', 'cp.screen.leads', 'cp.screen.booking',
                    'cp.screen.approvals', 'cp.screen.myteam'):
            self.assertIn(key, menu)


class EveryTileFollowsTheSameScopeTests(TestCase):
    """The dashboard's tiles have to count the same population. With "own records
    only" the leads shrank while Site Visits and Closures still showed the whole
    desk — so the conversion rate divided one scope by another."""

    def setUp(self):
        from datetime import date
        from django.core.cache import cache
        from sales.models import Closure, Lead, LeadSource, Project, SiteVisit
        cache.clear()
        self.co = Company.objects.create(code='TILE', name='Tile Co')
        proj = Project.objects.create(company=self.co, name='Tundav')
        # A CP title sees the partner pool, so the fixtures have to be partner leads.
        src = LeadSource.objects.create(company=self.co, name='Channel Partner')
        self.desig = _designation(self.co, 'CP CLUSTER HEAD', 'Sales')
        self.head = User.objects.create_user('h@x.com', company=self.co, user_code='T1', password='x',
                                             name='Head', role='Manager', modules=['Sales'],
                                             designation='CP CLUSTER HEAD')
        self.mate = User.objects.create_user('m@x.com', company=self.co, user_code='T2', password='x',
                                             name='Mate', role='Employee', modules=['Sales'],
                                             designation='CP CLUSTER HEAD', reporting_manager=self.head)
        for owner in (self.head, self.mate):
            lead = Lead.objects.create(company=self.co, project=proj, source=src,
                                       name=f'{owner.name} lead', phone='9000000003', stm=owner)
            SiteVisit.objects.create(lead=lead, stm=owner, status='completed',
                                     visited_at=timezone.now())
            Closure.objects.create(company=self.co, lead=lead, project=proj, stm=owner,
                                   closure_date=date.today(), status='approved')

    def _tiles(self):
        api = APIClient()
        api.force_authenticate(User.objects.get(pk=self.head.pk))
        d = api.get('/api/sales/stats/').json()
        return d['total_leads'], d['sv_done'], d['closures']

    def test_own_records_only_narrows_every_tile(self):
        self.assertEqual(self._tiles(), (2, 2, 2))      # the desk, as before
        self.desig.data_scope = 'own'
        self.desig.save(update_fields=['data_scope'])
        from accounts.capabilities import bump_permissions_version
        bump_permissions_version(self.co.id)
        self.assertEqual(self._tiles(), (1, 1, 1))      # theirs alone, all three

    def test_team_scope_is_the_whole_tree_below_them(self):
        self.desig.data_scope = 'team'
        self.desig.save(update_fields=['data_scope'])
        from accounts.capabilities import bump_permissions_version
        bump_permissions_version(self.co.id)
        self.assertEqual(self._tiles(), (2, 2, 2))
