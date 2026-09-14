"""Nobody below leadership may be left without a reporting manager.

Visibility runs on the reporting tree, so such a user is invisible to every manager
in the company. It happened: an STM with 78 bookings sat with no manager, and his
work reached no manager's list at all. It surfaced only where a rule deliberately
reaches past the hierarchy — the Channel Partner pool — which is why the same
figure read 67 in the CP module and 65 in Sales.
"""
from rest_framework.test import APITestCase

from companies.models import Company
from accounts.models import User

from sales.tests import auth


class ReportingManagerRequiredTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='RMR', name='Tree Co')
        cls.admin = User.objects.create(email='rmr_admin@x.com', company=cls.co, role='Admin',
                                        designation='Admin', user_code='R0', name='Admin')
        cls.head = User.objects.create(email='rmr_head@x.com', company=cls.co, role='Manager',
                                       designation='REGIONAL HEAD', user_code='R1', name='Head')

    def setUp(self):
        auth(self.client, self.admin)

    def _create(self, **over):
        payload = {'name': 'New STM', 'email': 'rmr_new@x.com', 'password': 'secret123',
                   'role': 'Employee', 'designation': 'STM', 'modules': ['Sales']}
        payload.update(over)
        return self.client.post('/api/auth/users/', payload, format='json')

    def test_an_stm_cannot_be_created_without_a_manager(self):
        r = self._create()
        self.assertEqual(r.status_code, 400)
        self.assertIn('reporting_manager_id', r.data)

    def test_an_stm_with_a_manager_is_fine(self):
        r = self._create(reporting_manager_id=self.head.id)
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(User.objects.get(email_key__isnull=False, user_code=r.data['user_code'])
                         .reporting_manager_id, self.head.id)

    def test_leadership_may_sit_at_the_top(self):
        # A Director reports to nobody by design — the rule must not force a manager
        # onto the top of the tree.
        r = self._create(role='Director', designation='DIRECTOR', email='rmr_dir@x.com')
        self.assertEqual(r.status_code, 201, r.data)

    def test_the_kiosk_account_is_exempt(self):
        # The unattended self-booking terminal belongs to no one.
        r = self._create(role='Kiosk', designation='Kiosk', email='rmr_kiosk@x.com')
        self.assertEqual(r.status_code, 201, r.data)

    def test_an_existing_manager_cannot_be_cleared(self):
        stm = User.objects.create(email='rmr_stm@x.com', company=self.co, role='Employee',
                                  designation='STM', user_code='R2', name='Stm',
                                  reporting_manager=self.head)
        r = self.client.patch(f'/api/auth/users/{stm.id}/',
                              {'reporting_manager_id': None}, format='json')
        self.assertEqual(r.status_code, 400)
        stm.refresh_from_db()
        self.assertEqual(stm.reporting_manager_id, self.head.id)

    def test_an_unrelated_edit_to_a_valid_user_still_works(self):
        stm = User.objects.create(email='rmr_ok@x.com', company=self.co, role='Employee',
                                  designation='STM', user_code='R3', name='Stm Ok',
                                  reporting_manager=self.head)
        r = self.client.patch(f'/api/auth/users/{stm.id}/', {'phone': '9000000099'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)

    def test_a_user_left_without_a_manager_can_still_be_deactivated(self):
        # Closing an account must not require fixing the tree first, and an inactive
        # user is outside every visibility rule anyway.
        orphan = User.objects.create(email='rmr_orphan@x.com', company=self.co, role='Employee',
                                     designation='STM', user_code='R4', name='Orphan')
        r = self.client.patch(f'/api/auth/users/{orphan.id}/', {'is_active': False}, format='json')
        self.assertEqual(r.status_code, 200, r.data)

    def test_promoting_someone_to_manager_lets_them_lose_their_manager(self):
        stm = User.objects.create(email='rmr_promo@x.com', company=self.co, role='Employee',
                                  designation='STM', user_code='R5', name='Promo',
                                  reporting_manager=self.head)
        r = self.client.patch(f'/api/auth/users/{stm.id}/',
                              {'role': 'Director', 'reporting_manager_id': None}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
