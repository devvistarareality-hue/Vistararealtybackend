"""Roles senior to Manager carry Manager's authority."""
from django.test import TestCase

from accounts.models import User
from companies.models import Company
from sales.views import MANAGER_ROLES, ROLE_HIERARCHY, is_admin_or_manager, is_manager_role


class RoleHierarchy(TestCase):
    def setUp(self):
        self.co = Company.objects.create(code='RH', name='Role Co')
        self.n = 0

    def _user(self, role):
        self.n += 1
        return User.objects.create(name=role or 'none', email=f'u{self.n}@r.com',
                                   phone=f'900000{self.n:04d}', user_code=f'R{self.n}',
                                   role=role, company=self.co)

    def test_hierarchy_order_is_most_senior_first(self):
        self.assertEqual(ROLE_HIERARCHY,
                         ['Director', 'General Manager', 'Manager', 'Employee', 'Intern'])

    def test_director_and_gm_have_manager_authority(self):
        for role in ('Director', 'General Manager', 'Manager'):
            u = self._user(role)
            self.assertTrue(is_manager_role(u), f'{role} should count as a manager')
            self.assertTrue(is_admin_or_manager(u), f'{role} should pass the admin/manager gate')

    def test_junior_roles_do_not(self):
        for role in ('Employee', 'Intern', 'Kiosk', ''):
            u = self._user(role)
            self.assertFalse(is_manager_role(u), f'{role} must not count as a manager')
            self.assertFalse(is_admin_or_manager(u), f'{role} must not pass the gate')

    def test_admin_and_staff_still_pass(self):
        self.assertTrue(is_admin_or_manager(self._user('Admin')))
        staff = self._user('Employee'); staff.is_staff = True; staff.save()
        self.assertTrue(is_admin_or_manager(staff))

    def test_admin_is_not_reported_as_a_manager_role(self):
        """Admin passes the gate but isn't part of the staff hierarchy."""
        self.assertFalse(is_manager_role(self._user('Admin')))
        self.assertIn('Admin', MANAGER_ROLES)   # …yet still queryable as one

    def test_no_rank_is_senior_to_director(self):
        self.assertEqual(ROLE_HIERARCHY[0], 'Director')
