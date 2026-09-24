"""Designation Master, swept across every module the launcher offers.

The other permission tests each take one question deep — a scope, a preset, a
module's own tiles. This one is broad and shallow on purpose: for EVERY module,
it walks the four things an admin can set on a designation (the menu, the
actions, the dashboard, the record scope) and checks each one takes effect and
stops where it should.

It is the test that would have caught a module quietly left out of the editor,
or a new module whose keys nothing enforces.
"""
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.capabilities import (ALL_MODULES, CAPABILITIES, DASHBOARDS, DATA_SCOPES,
                                   SCREENS, can_see_screen, dashboard_for, data_scope,
                                   user_can)
from accounts.models import Designation, User
from companies.models import Company


def screens_of(module):
    return [k for k, _, m in SCREENS if m == module]


def caps_of(module):
    return [c[0] for c in CAPABILITIES if c[2] == module]


def dashboards_of(module):
    return [d[0] for d in DASHBOARDS if d[2] == module]


class EveryModuleIsInTheVocabulary(TestCase):
    """Every module the launcher offers can be configured at all."""

    def test_every_module_has_a_menu(self):
        missing = [m for m in ALL_MODULES if not screens_of(m)]
        self.assertEqual(missing, [], 'these modules have no screens to tick')

    def test_every_module_has_a_dashboard_to_pin(self):
        missing = [m for m in ALL_MODULES if not dashboards_of(m)]
        self.assertEqual(missing, [], 'these modules have no dashboard to pin')

    def test_the_editor_is_offered_every_module(self):
        """The catalogue the Permissions editor reads has to carry each module,
        or the module picker cannot offer it."""
        co = Company.objects.create(code='VOC', name='Voc Co')
        admin = User.objects.create_user('a@voc.com', company=co, user_code='V-A',
                                         password='x', name='Admin', role='Admin')
        api = APIClient()
        api.force_authenticate(admin)
        d = api.get('/api/auth/designations/capabilities/').json()
        offered = {s['module'] for s in d['screens']}
        self.assertEqual(sorted(set(ALL_MODULES) - offered), [],
                         'the editor cannot show these modules')


class EveryModuleObeysItsDesignation(TestCase):
    """Set each of the four things, in each module, and check it lands."""

    def setUp(self):
        cache.clear()
        self.co = Company.objects.create(code='SWP', name='Sweep Co')
        self.admin = User.objects.create_user('a@swp.com', company=self.co, user_code='S-A',
                                              password='x', name='Admin', role='Admin')
        self.api = APIClient()
        self.api.force_authenticate(self.admin)

    def _person(self, module, n):
        """Someone holding a designation in `module`, granted every module — the
        shape that makes cross-module leakage visible."""
        title = f'Desk {n}'
        desig = Designation.objects.create(company=self.co, name=title, module=module)
        user = User.objects.create_user(f'u{n}@swp.com', company=self.co, user_code=f'S-{n}',
                                        password='x', name=f'User {n}', role='Employee',
                                        designation=title, modules=list(ALL_MODULES))
        return desig, user

    def _fresh(self, user):
        cache.clear()
        return User.objects.get(pk=user.pk)

    def _patch(self, desig, body):
        r = self.api.patch(f'/api/auth/designations/{desig.id}/', body, format='json')
        self.assertEqual(r.status_code, 200, f'{desig.module}: {r.content}')
        return r.json()

    def test_the_menu_is_settable_and_bounded_in_every_module(self):
        for n, module in enumerate(ALL_MODULES):
            with self.subTest(module=module):
                desig, user = self._person(module, n)
                keys = screens_of(module)
                others = [k for k, _, m in SCREENS if m != module]

                # Nothing configured: the whole menu shows, everywhere.
                for k in keys[:3] + others[:3]:
                    self.assertTrue(can_see_screen(self._fresh(user), k),
                                    f'{module}: {k} hidden before anything was set')

                # One tab ticked, and this module named: that tab and no other of
                # its own — while every other module keeps its default menu.
                self._patch(desig, {'screens': [keys[0]], 'screens_modules': [module]})
                fresh = self._fresh(user)
                self.assertTrue(can_see_screen(fresh, keys[0]))
                for k in keys[1:]:
                    self.assertFalse(can_see_screen(fresh, k), f'{module}: {k} should be hidden')
                for k in others[:5]:
                    self.assertTrue(can_see_screen(fresh, k),
                                    f'{module}: hid {k}, which belongs to another module')

                # Cleared: an empty menu is an answer, not "not set up".
                self._patch(desig, {'screens': [], 'screens_modules': [module]})
                fresh = self._fresh(user)
                for k in keys:
                    self.assertFalse(can_see_screen(fresh, k), f'{module}: {k} survived a clear')

    def test_a_second_module_can_be_taken_charge_of_in_every_module(self):
        """The CFO case, from each module in turn: name another module and its
        menu answers to this designation too."""
        partner = {m: next(x for x in ALL_MODULES if x != m) for m in ALL_MODULES}
        for n, module in enumerate(ALL_MODULES):
            with self.subTest(module=module):
                other = partner[module]
                desig, user = self._person(module, 100 + n)
                mine, theirs = screens_of(module), screens_of(other)
                self._patch(desig, {'screens': [mine[0], theirs[0]],
                                    'screens_modules': [module, other]})
                fresh = self._fresh(user)
                self.assertTrue(can_see_screen(fresh, mine[0]))
                self.assertTrue(can_see_screen(fresh, theirs[0]))
                for k in theirs[1:]:
                    self.assertFalse(can_see_screen(fresh, k),
                                     f'{module}+{other}: {k} should be hidden')
                # A third module, never named, is untouched.
                third = next((x for x in ALL_MODULES if x not in (module, other)), None)
                if third:
                    self.assertTrue(can_see_screen(fresh, screens_of(third)[0]))

    def test_actions_are_settable_and_bounded_in_every_module(self):
        for n, module in enumerate(m for m in ALL_MODULES if caps_of(m)):
            with self.subTest(module=module):
                desig, user = self._person(module, 200 + n)
                keys = caps_of(module)
                self._patch(desig, {'capabilities': keys})
                fresh = self._fresh(user)
                for k in keys:
                    self.assertTrue(user_can(fresh, k), f'{module}: {k} not granted')
                self._patch(desig, {'capabilities': []})
                fresh = self._fresh(user)
                for k in keys:
                    self.assertFalse(user_can(fresh, k), f'{module}: {k} survived a clear')

    def test_every_dashboard_of_every_module_can_be_pinned(self):
        for n, module in enumerate(ALL_MODULES):
            with self.subTest(module=module):
                desig, user = self._person(module, 300 + n)
                for view in dashboards_of(module):
                    self._patch(desig, {'dashboard': view})
                    self.assertEqual(dashboard_for(self._fresh(user)), view,
                                     f'{module}: {view} did not stick')

    def test_every_record_scope_can_be_set_in_every_module(self):
        for n, module in enumerate(ALL_MODULES):
            with self.subTest(module=module):
                desig, user = self._person(module, 400 + n)
                for scope, _label in DATA_SCOPES:
                    self._patch(desig, {'data_scope': scope})
                    self.assertEqual(data_scope(self._fresh(user)), scope,
                                     f'{module}: scope {scope!r} did not stick')

    def test_a_designation_is_refused_a_key_that_does_not_exist(self):
        """The vocabulary is fixed in code, so a typo cannot invent a permission."""
        desig, _ = self._person('Sales', 500)
        for body in ({'screens': ['sales.screen.nope']},
                     {'capabilities': ['sales.invented.power']},
                     {'dashboard': 'not-a-dashboard'},
                     {'data_scope': 'whatever'},
                     {'screens': ['sales.screen.leads'], 'screens_modules': ['Atlantis']}):
            r = self.api.patch(f'/api/auth/designations/{desig.id}/', body, format='json')
            self.assertEqual(r.status_code, 400, f'{body} was accepted')

    def test_only_an_administrator_may_change_permissions(self):
        desig, user = self._person('Sales', 600)
        api = APIClient()
        api.force_authenticate(User.objects.get(pk=user.pk))
        r = api.patch(f'/api/auth/designations/{desig.id}/',
                      {'screens': []}, format='json')
        self.assertEqual(r.status_code, 403)


class HoldingTheModuleIsStillTheGate(TestCase):
    """Two people, one designation, different module grants — the shape of "one
    accountant sees AR and the other does not". The designation says what the job
    may do; User Management says which modules this person holds, and the server
    refuses the rest whatever the designation says.
    """

    def setUp(self):
        cache.clear()
        self.co = Company.objects.create(code='GAT', name='Gate Co')
        self.admin = User.objects.create_user('a@gat.com', company=self.co, user_code='G-A',
                                              password='x', name='Admin', role='Admin')
        Designation.objects.create(company=self.co, name='Accountant',
                                   module='Accounts & Finance')
        self.with_ar = User.objects.create_user('w@gat.com', company=self.co, user_code='G-1',
                                                password='x', name='Asha', role='Employee',
                                                designation='Accountant',
                                                modules=['Accounts & Finance', 'AR'])
        self.without = User.objects.create_user('n@gat.com', company=self.co, user_code='G-2',
                                                password='x', name='Bharat', role='Employee',
                                                designation='Accountant',
                                                modules=['Accounts & Finance'])
        api = APIClient()
        api.force_authenticate(self.admin)
        d = Designation.objects.get(company=self.co, name='Accountant')
        api.patch(f'/api/auth/designations/{d.id}/', {
            'screens': screens_of('Accounts & Finance') + screens_of('AR'),
            'screens_modules': ['Accounts & Finance', 'AR'],
            'capabilities': ['ar.receipt.record']}, format='json')

    def _ar(self, user):
        api = APIClient()
        api.force_authenticate(User.objects.get(pk=user.pk))
        return api.get('/api/ar/accounts/').status_code

    def test_the_one_granted_ar_is_let_in(self):
        self.assertEqual(self._ar(self.with_ar), 200)
        self.assertTrue(user_can(User.objects.get(pk=self.with_ar.pk), 'ar.receipt.record'))

    def test_the_one_not_granted_ar_is_refused(self):
        """Refused by the server, not merely hidden — the designation grants the
        capability, and it still is not enough without the module."""
        self.assertEqual(self._ar(self.without), 403)
        from receivables.permissions import ar_can
        fresh = User.objects.get(pk=self.without.pk)
        self.assertTrue(user_can(fresh, 'ar.receipt.record'), 'the job may do it')
        self.assertFalse(ar_can(fresh, 'ar.receipt.record'), 'this person may not')
