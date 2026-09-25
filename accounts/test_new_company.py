"""Setting up a brand-new company, end to end.

This is the walkthrough an admin actually does: create the company, add the
designations for each module, create people under them, then use Designation
Master → Permissions and the dashboards. Everything is checked through the API,
the way the website and the app call it.

The rule the whole file exists to protect: a company that never opens the
permissions screen behaves exactly as the ERP did before permissions existed.
"""
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.capabilities import (DASHBOARD_ROLES, can_see_screen, dashboard_for,
                                   data_scope, screens_for, user_can)
from accounts.models import Designation, User
from companies.models import Company


class NewCompanyWalkthrough(TestCase):
    maxDiff = None

    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='NEWC', name='New Realty')
        cls.admin = User.objects.create_user('admin@newc.com', company=cls.co, user_code='N-ADMIN',
                                             password='x', name='Company Admin', role='Admin')

    def setUp(self):
        self._head_user = None
        self.api = APIClient()
        self.api.force_authenticate(self.admin)

    # ── 1. the admin builds the company ──────────────────────────────────────
    def _designation(self, name, module):
        r = self.api.post('/api/auth/designations/', {'name': name, 'module': module}, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()

    def _head(self):
        """Every employee needs a manager — visibility runs on the reporting tree,
        so the API refuses one without. Each test gets one head."""
        if getattr(self, '_head_user', None) is None:
            self._head_user = self._user('Sales Head', 'SH', 'Manager', 'Sales Head', ['Sales', 'AR'])
        return self._head_user

    def _user(self, name, code, role, designation, modules, manager=None):
        if role in ('Employee', 'Intern') and manager is None:
            manager = self._head()
        r = self.api.post('/api/auth/users/', {
            'name': name, 'email': f'{code.lower()}@newc.com', 'phone': '9000000000',
            'password': 'Test@1234', 'role': role, 'designation': designation,
            'modules': modules, 'user_code_prefix': code,
            **({'reporting_manager_id': manager.id} if manager else {}),
        }, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        return User.objects.get(pk=r.json()['id'])

    def test_01_designations_and_people_can_be_created(self):
        for name, module in [('Telecaller', 'Sales'), ('STM', 'Sales'), ('AR Officer', 'AR')]:
            row = self._designation(name, module)
            self.assertEqual(row['module'], module)
            # Nothing is configured yet, and the editor says so.
            self.assertFalse(row['capabilities_set'])
            self.assertFalse(row['screens_set'])
        head = self._head()
        tele = self._user('Tele One', 'TC', 'Employee', 'Telecaller', ['Sales'], manager=head)
        self.assertEqual(tele.reporting_manager_id, head.id)

    # ── 2. nothing configured = the old behaviour ────────────────────────────
    def test_02_an_unconfigured_company_behaves_as_before(self):
        self._designation('Telecaller', 'Sales')
        self._designation('AR Officer', 'AR')
        head = self._head()
        tele = self._user('Tele One', 'TC', 'Employee', 'Telecaller', ['Sales'], manager=head)
        ar = self._user('AR One', 'ARU', 'Employee', 'AR Officer', ['AR'])

        # Pipelines follow the designation text, as they always did.
        self.assertTrue(user_can(tele, 'sales.pipeline.telecalling'))
        self.assertFalse(user_can(tele, 'sales.lead.assign'))
        self.assertTrue(user_can(head, 'sales.lead.assign'))
        # AR actions are on for anyone with the AR module.
        for key in ('ar.receipt.record', 'ar.receipt.edit', 'ar.import.run',
                    'ar.followup.manage', 'ar.legal_date.set'):
            self.assertTrue(user_can(ar, key), key)
        # No menu is forced on anyone, and no dashboard is pinned.
        self.assertIsNone(screens_for(tele))
        self.assertTrue(can_see_screen(tele, 'sales.screen.approvals'))
        self.assertEqual(dashboard_for(tele), '')
        self.assertEqual(data_scope(tele), '')

    # ── 3. the editor opens on that designation's own module ─────────────────
    def test_03_the_editor_is_scoped_to_the_module(self):
        sales = self._designation('CMO', 'Sales')
        ar = self._designation('AR Officer', 'AR')
        rows = {d['name']: d for d in self.api.get('/api/auth/designations/').json()}
        # A Sales title is pre-ticked with Sales only — never with AR.
        self.assertTrue(all(c.startswith('sales.') for c in rows['CMO']['effective_capabilities']),
                        rows['CMO']['effective_capabilities'])
        self.assertTrue(all(s.startswith(('sales.', 'cp.')) for s in rows['CMO']['effective_screens']),
                        rows['CMO']['effective_screens'])
        # And an AR title with AR only.
        self.assertTrue(all(c.startswith('ar.') for c in rows['AR Officer']['effective_capabilities']),
                        rows['AR Officer']['effective_capabilities'])
        self.assertTrue(all(s.startswith('ar.') for s in rows['AR Officer']['effective_screens']),
                        rows['AR Officer']['effective_screens'])

    # ── 4. taking an action away ─────────────────────────────────────────────
    def test_04_unticking_an_action_refuses_it(self):
        d = self._designation('AR Officer', 'AR')
        ar = self._user('AR One', 'ARU', 'Employee', 'AR Officer', ['AR'])
        self.assertTrue(user_can(ar, 'ar.receipt.record'))
        keep = [c for c in d['effective_capabilities'] if c != 'ar.receipt.record']
        r = self.api.patch(f"/api/auth/designations/{d['id']}/", {'capabilities': keep}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        fresh = User.objects.get(pk=ar.pk)
        self.assertFalse(user_can(fresh, 'ar.receipt.record'))
        self.assertTrue(user_can(fresh, 'ar.followup.manage'))   # untouched

    # ── 5. a Sales designation still says nothing about AR ───────────────────
    def test_05_configuring_sales_does_not_touch_ar(self):
        d = self._designation('CMO', 'Sales')
        person = self._user('Boss', 'BS', 'Manager', 'CMO', ['Sales', 'AR'])
        self.api.patch(f"/api/auth/designations/{d['id']}/",
                       {'capabilities': ['sales.lead.assign']}, format='json')
        fresh = User.objects.get(pk=person.pk)
        self.assertTrue(user_can(fresh, 'sales.lead.assign'))
        self.assertFalse(user_can(fresh, 'sales.pipeline.telecalling'))
        # They have the AR module, so AR is untouched by a Sales designation.
        self.assertTrue(user_can(fresh, 'ar.receipt.record'))

    # ── 6. the menu ──────────────────────────────────────────────────────────
    def test_06_the_menu_is_what_the_company_ticks(self):
        d = self._designation('Telecaller', 'Sales')
        tele = self._user('Tele One', 'TC', 'Employee', 'Telecaller', ['Sales'])
        r = self.api.patch(f"/api/auth/designations/{d['id']}/",
                           {'screens': ['sales.screen.dashboard', 'sales.screen.leads']}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        fresh = User.objects.get(pk=tele.pk)
        self.assertTrue(can_see_screen(fresh, 'sales.screen.leads'))
        self.assertFalse(can_see_screen(fresh, 'sales.screen.approvals'))
        # Clearing it means "no menu", not "not configured".
        self.api.patch(f"/api/auth/designations/{d['id']}/", {'screens': []}, format='json')
        fresh = User.objects.get(pk=tele.pk)
        self.assertEqual(screens_for(fresh), set())
        self.assertFalse(can_see_screen(fresh, 'sales.screen.dashboard'))

    # ── 7. dashboards, per module, per role ──────────────────────────────────
    def test_07_every_module_offers_a_dashboard_for_every_role(self):
        cat = self.api.get('/api/auth/designations/capabilities/').json()
        self.assertEqual(cat['dashboard_roles'], DASHBOARD_ROLES)
        by_module = {}
        for row in cat['dashboards']:
            if row['module']:
                by_module.setdefault(row['module'], set()).add(row['role'])
        for module in ('Sales', 'Channel Partner', 'AR', 'Accounts & Finance', 'Club 1000',
                       'HR', 'Task Allocation', 'Purchase', 'Land'):
            self.assertIn(module, by_module, module)
        # AR's one dashboard is offered to every role.
        self.assertEqual(by_module['AR'], set(DASHBOARD_ROLES))

    def test_08_a_dashboard_can_be_pinned_to_a_designation(self):
        d = self._designation('AR Officer', 'AR')
        ar = self._user('AR One', 'ARU', 'Employee', 'AR Officer', ['AR'])
        r = self.api.patch(f"/api/auth/designations/{d['id']}/",
                           {'dashboard': 'ar_manager'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(dashboard_for(User.objects.get(pk=ar.pk)), 'ar_manager')
        self.assertEqual(self.api.patch(f"/api/auth/designations/{d['id']}/",
                                        {'dashboard': 'nope'}, format='json').status_code, 400)

    def test_09_copy_to_role_gives_a_dashboard_to_a_whole_role(self):
        head = self._head()
        r = self.api.post('/api/auth/role-dashboards/',
                          {'module': 'Sales', 'view': 'manager',
                           'roles': ['Manager', 'General Manager']}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        # /me tells the website and the app what this person's role opens.
        self.api.force_authenticate(User.objects.get(pk=head.pk))
        me = self.api.get('/api/auth/me/').json()
        self.assertEqual(me['role_dashboards'], {'Sales': 'manager'})
        self.assertEqual(me['dashboard'], '')          # nothing pinned on the designation

    def test_10_a_pinned_designation_beats_the_role(self):
        d = self._designation('Sales Head', 'Sales')
        head = self._head()
        self.api.post('/api/auth/role-dashboards/',
                      {'module': 'Sales', 'view': 'manager', 'roles': ['Manager']}, format='json')
        self.api.patch(f"/api/auth/designations/{d['id']}/", {'dashboard': 'director'}, format='json')
        self.api.force_authenticate(User.objects.get(pk=head.pk))
        me = self.api.get('/api/auth/me/').json()
        self.assertEqual(me['dashboard'], 'director')
        self.assertEqual(me['role_dashboards'], {'Sales': 'manager'})

    # ── 8. the module is always the gate ─────────────────────────────────────
    def test_11_a_capability_without_the_module_is_nothing(self):
        from club1000.views import club_can
        from receivables.permissions import ar_can
        d = self._designation('Everything', 'AR')
        self.api.patch(f"/api/auth/designations/{d['id']}/",
                       {'capabilities': ['ar.receipt.record', 'ar.receipt.edit', 'ar.import.run',
                                         'ar.followup.manage', 'ar.legal_date.set']}, format='json')
        nobody = self._user('No Modules', 'NM', 'Employee', 'Everything', [])
        self.assertTrue(user_can(nobody, 'ar.receipt.record'))     # the tick is there
        self.assertFalse(ar_can(nobody, 'ar.receipt.record'))      # the door is not
        self.assertFalse(club_can(nobody, 'club.investor.manage'))
        self.api.force_authenticate(nobody)
        self.assertEqual(self.api.get('/api/ar/accounts/').status_code, 403)

    # ── 9. whose records they see ────────────────────────────────────────────
    def test_12_the_data_scope_follows_the_reporting_tree(self):
        from sales.models import Lead, LeadSource, Project
        proj = Project.objects.create(company=self.co, name='Tundav')
        src = LeadSource.objects.create(company=self.co, name='Meta')
        head = self._head()
        d = self._designation('Telecaller', 'Sales')
        a = self._user('Tele A', 'TA', 'Employee', 'Telecaller', ['Sales'], manager=head)
        b = self._user('Tele B', 'TB', 'Employee', 'Telecaller', ['Sales'], manager=head)
        for owner, name in ((a, 'Mine'), (b, 'Theirs')):
            Lead.objects.create(company=self.co, project=proj, source=src,
                                name=name, phone='9000000001', telecaller=owner)

        def total(user):
            api = APIClient()
            api.force_authenticate(User.objects.get(pk=user.pk))
            return api.get('/api/sales/stats/').json()['total_leads']

        self.assertEqual(total(a), 1)          # their own
        self.assertEqual(total(head), 2)       # the tree below them
        # Pinning the manager dashboard changes the view, never the figures.
        self.api.force_authenticate(self.admin)
        self.api.patch(f"/api/auth/designations/{d['id']}/", {'dashboard': 'manager'}, format='json')
        self.assertEqual(dashboard_for(User.objects.get(pk=a.pk)), 'manager')
        self.assertEqual(total(a), 1)
        # A company-wide scope is the one way to widen it, and it is explicit.
        self.api.patch(f"/api/auth/designations/{d['id']}/", {'data_scope': 'company'}, format='json')
        self.assertEqual(total(a), 2)

    # ── 10. one company never sees another ───────────────────────────────────
    def test_13_everything_is_per_company(self):
        d = self._designation('Telecaller', 'Sales')
        self.api.patch(f"/api/auth/designations/{d['id']}/",
                       {'capabilities': [], 'screens': [], 'dashboard': 'director'}, format='json')
        self.api.post('/api/auth/role-dashboards/',
                      {'module': 'Sales', 'view': 'manager', 'roles': ['Manager']}, format='json')

        other = Company.objects.create(code='OTHC', name='Other Realty')
        other_admin = User.objects.create_user('a@othc.com', company=other, user_code='O-ADMIN',
                                               password='x', name='Other Admin', role='Admin')
        other_tele = User.objects.create_user('t@othc.com', company=other, user_code='O-TC',
                                              password='x', name='Other Tele', role='Employee',
                                              designation='Telecaller', modules=['Sales'])
        Designation.objects.create(company=other, name='Telecaller', module='Sales')
        api2 = APIClient()
        api2.force_authenticate(other_admin)
        self.assertEqual([d['name'] for d in api2.get('/api/auth/designations/').json()], ['Telecaller'])
        self.assertEqual(api2.get('/api/auth/role-dashboards/?module=Sales').json(), [])
        # The other company's Telecaller is untouched by ours.
        self.assertTrue(user_can(other_tele, 'sales.pipeline.telecalling'))
        self.assertIsNone(screens_for(other_tele))
        self.assertEqual(dashboard_for(other_tele), '')

    # ── 11. only admins may change any of it ─────────────────────────────────
    def test_14_an_employee_cannot_change_permissions(self):
        d = self._designation('Telecaller', 'Sales')
        tele = self._user('Tele One', 'TC', 'Employee', 'Telecaller', ['Sales'])
        self.api.force_authenticate(tele)
        self.assertEqual(self.api.patch(f"/api/auth/designations/{d['id']}/",
                                        {'capabilities': []}, format='json').status_code, 403)
        self.assertEqual(self.api.post('/api/auth/role-dashboards/',
                                       {'module': 'Sales', 'view': 'manager', 'roles': ['Manager']},
                                       format='json').status_code, 403)

    # ── 12. every change is in the log ───────────────────────────────────────
    def test_15_permission_changes_are_recorded(self):
        from activity.models import ActivityLog
        d = self._designation('Telecaller', 'Sales')
        self.api.patch(f"/api/auth/designations/{d['id']}/",
                       {'capabilities': ['sales.lead.assign']}, format='json')
        kinds = {(a.action, a.target_type) for a in ActivityLog.objects.all()}
        self.assertTrue(any(t == 'designation' for _, t in kinds), kinds)
