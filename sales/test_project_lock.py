"""A locked project, or a locked block inside one, is invisible to the sales floor.

The point of the lock is that a project still being set up — units not numbered,
prices not signed off, plans not drawn — cannot be picked, booked against, or
even seen by anyone but an administrator. Which makes hiding it only half the
job: the id survives in an open tab, a bookmark, or a draft saved before the
lock went on, so the write paths have to refuse it too.
"""
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from companies.models import Company
from sales.models import Plot, Project


class ProjectLocking(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='LCK', name='Locked Realty', is_active=True)
        cls.admin = User.objects.create_user(
            'admin@lck.com', company=cls.co, user_code='LCK001', password='adminpass1',
            name='Company Admin', role='Admin', modules=['Sales'])
        cls.rep = User.objects.create_user(
            'rep@lck.com', company=cls.co, user_code='LCK002', password='reppass1',
            name='Sales Rep', role='Employee', modules=['Sales'],
            reporting_manager=cls.admin)

        cls.open_proj = Project.objects.create(company=cls.co, name='Open Project')
        cls.locked_proj = Project.objects.create(company=cls.co, name='Secret Launch',
                                                 is_locked=True)
        # One project selling blocks A and B while C is still being built out.
        cls.partial = Project.objects.create(
            company=cls.co, name='Phased Towers', floor_wise=True,
            locked_blocks=['C'],
            floor_plans=[{'block': b, 'floor': 0} for b in ('A', 'B', 'C')])
        for b in ('A', 'B', 'C'):
            for n in (1, 2):
                Plot.objects.create(project=cls.partial, number=f'{b}-{n}')

    def setUp(self):
        self.api = APIClient()
        cache.clear()

    def _as(self, user, password):
        self.api.force_authenticate(user=user)

    def _project_names(self):
        r = self.api.get('/api/sales/projects/')
        self.assertEqual(r.status_code, 200, r.data)
        return {p['name'] for p in r.data}

    # ── the list ─────────────────────────────────────────────────────────────
    def test_a_rep_does_not_see_a_locked_project(self):
        self._as(self.rep, 'reppass1')
        names = self._project_names()
        self.assertIn('Open Project', names)
        self.assertNotIn('Secret Launch', names)

    def test_an_admin_still_sees_it_so_it_can_be_unlocked(self):
        """A lock nobody can see is a project nobody can release."""
        self._as(self.admin, 'adminpass1')
        self.assertIn('Secret Launch', self._project_names())

    def test_the_lock_flag_reaches_the_client(self):
        self._as(self.admin, 'adminpass1')
        r = self.api.get('/api/sales/projects/')
        row = next(p for p in r.data if p['name'] == 'Secret Launch')
        self.assertTrue(row['is_locked'])
        row = next(p for p in r.data if p['name'] == 'Phased Towers')
        self.assertEqual(row['locked_blocks'], ['C'])

    # ── reaching it directly ─────────────────────────────────────────────────
    def test_a_rep_cannot_open_a_locked_project_by_id(self):
        """Hiding it from the list is not enough — the id is guessable, and it
        survives in tabs and bookmarks."""
        self._as(self.rep, 'reppass1')
        r = self.api.get(f'/api/sales/projects/{self.locked_proj.id}/')
        self.assertEqual(r.status_code, 404)

    def test_it_is_404_rather_than_403(self):
        """403 would confirm the project exists, which is what the lock is hiding."""
        self._as(self.rep, 'reppass1')
        r = self.api.get(f'/api/sales/projects/{self.locked_proj.id}/')
        self.assertNotIn('locked', str(r.data).lower())

    def test_an_admin_can_open_it_by_id(self):
        self._as(self.admin, 'adminpass1')
        r = self.api.get(f'/api/sales/projects/{self.locked_proj.id}/')
        self.assertEqual(r.status_code, 200)

    # ── blocks ───────────────────────────────────────────────────────────────
    def test_units_in_a_locked_block_are_off_the_map_for_a_rep(self):
        self._as(self.rep, 'reppass1')
        r = self.api.get(f'/api/sales/plots/?project={self.partial.id}')
        self.assertEqual(r.status_code, 200, r.data)
        numbers = {p['number'] for p in r.data}
        self.assertEqual(numbers, {'A-1', 'A-2', 'B-1', 'B-2'})

    def test_an_admin_does_not_see_locked_units_on_the_unit_map_either(self):
        """Reported from production: blocks A and B were locked and an admin could
        still pick their units and book them. Seeing is not selling — an admin
        sees a locked block in the project editor so they can release it, but the
        unit map is where units get picked for a booking, and a locked block is
        not for sale to anyone."""
        self._as(self.admin, 'adminpass1')
        r = self.api.get(f'/api/sales/plots/?project={self.partial.id}')
        self.assertEqual({p['number'] for p in r.data}, {'A-1', 'A-2', 'B-1', 'B-2'})

    def test_an_admin_can_ask_for_locked_units_back_to_build_the_block_out(self):
        """Manage Plots passes include_locked=1 — a block still being set up has
        to be editable while it is held back, which is the point of locking it."""
        self._as(self.admin, 'adminpass1')
        r = self.api.get(f'/api/sales/plots/?project={self.partial.id}&include_locked=1')
        self.assertEqual(len(r.data), 6)

    def test_a_rep_cannot_ask_for_locked_units_back(self):
        """Otherwise the lock is one query parameter away from nothing."""
        self._as(self.rep, 'reppass1')
        r = self.api.get(f'/api/sales/plots/?project={self.partial.id}&include_locked=1')
        self.assertEqual({p['number'] for p in r.data}, {'A-1', 'A-2', 'B-1', 'B-2'})

    def test_an_open_block_in_the_same_project_is_unaffected(self):
        """Locking C must not take A and B down with it."""
        self._as(self.rep, 'reppass1')
        r = self.api.get(f'/api/sales/plots/?project={self.partial.id}')
        self.assertTrue(any(p['number'] == 'A-1' for p in r.data))

    def test_the_project_itself_is_still_listed_when_only_a_block_is_locked(self):
        self._as(self.rep, 'reppass1')
        self.assertIn('Phased Towers', self._project_names())

    # ── how a unit maps to a block ───────────────────────────────────────────
    def test_block_is_read_off_the_unit_number_prefix(self):
        self.assertEqual(Project.block_of('A-101'), 'A')
        self.assertEqual(Project.block_of('A-12-A'), 'A')   # suffixed unit
        self.assertEqual(Project.block_of('12'), '')        # plotted scheme, no block
        self.assertEqual(Project.block_of(''), '')
        self.assertEqual(Project.block_of(None), '')

    def test_a_project_with_no_locked_blocks_locks_nothing(self):
        """`blocks_unit_locked` must not treat an empty list as "lock everything"."""
        self.assertFalse(self.open_proj.blocks_unit_locked('A-1'))
        self.assertFalse(self.open_proj.blocks_unit_locked('12'))

    def test_locked_blocks_ignores_blank_entries(self):
        p = Project(locked_blocks=['', '  ', 'C'])
        self.assertEqual(p.locked_block_set(), {'C'})
        self.assertFalse(p.blocks_unit_locked('12'), 'a no-block unit must not match')


class LockedThingsCannotBeBooked(TestCase):
    """The half that was missing: hiding a lock is not enforcing it."""

    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='BKL', name='Booking Lock', is_active=True)
        cls.admin = User.objects.create_user(
            'admin@bkl.com', company=cls.co, user_code='BKL001', password='adminpass1',
            name='Admin', role='Admin', modules=['Sales'])
        cls.locked_proj = Project.objects.create(company=cls.co, name='Held Back',
                                                 is_locked=True)
        cls.partial = Project.objects.create(company=cls.co, name='Part Locked',
                                             locked_blocks=['B'])
        cls.open_unit = Plot.objects.create(project=cls.partial, number='A-1')
        cls.locked_unit = Plot.objects.create(project=cls.partial, number='B-1')

    def setUp(self):
        self.api = APIClient()
        cache.clear()
        self.api.force_authenticate(user=self.admin)

    def _book(self, payload):
        return self.api.post('/api/sales/bookings/', payload, format='json')

    def test_an_admin_cannot_book_a_locked_project(self):
        r = self._book({'project': self.locked_proj.id, 'client_name': 'X', 'phone': '+919800000001'})
        self.assertEqual(r.status_code, 403)
        self.assertIn('locked', str(r.data).lower())

    def test_an_admin_cannot_book_a_unit_in_a_locked_block(self):
        r = self._book({'project': self.partial.id, 'plot': self.locked_unit.id,
                        'client_name': 'X', 'phone': '+919800000002'})
        self.assertEqual(r.status_code, 403)
        self.assertIn('B-1', str(r.data))

    def test_the_refusal_names_the_units_so_it_is_actionable(self):
        r = self._book({'project': self.partial.id,
                        'plot_ids': [self.open_unit.id, self.locked_unit.id],
                        'client_name': 'X', 'phone': '+919800000003'})
        self.assertEqual(r.status_code, 403)
        self.assertIn('B-1', str(r.data))
        self.assertNotIn('A-1', str(r.data), 'the open unit is not the problem')

    def test_an_open_block_in_the_same_project_is_still_bookable(self):
        """The guard must refuse the locked block, not the whole project."""
        r = self._book({'project': self.partial.id, 'plot': self.open_unit.id,
                        'client_name': 'X', 'phone': '+919800000004'})
        self.assertNotEqual(r.status_code, 403, r.data)
