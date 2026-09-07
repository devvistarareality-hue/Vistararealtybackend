"""A Pratishtha unit cannot be booked until its price book is loaded.

Pratishtha prices each unit from that unit's price book. With an empty book the
web/app form falls through to the generic rate branch — whose fieldFlags() has no
'pratishtha' case (it returns the Kalrav field set) and whose computeFormulas has
no pratishtha branch (it returns saleDeed 0). The result was a booking saved at a
zero total under a "PRATISHTHA pricing" header, with nothing surfacing the problem.
"""
from django.core.cache import cache
from rest_framework.test import APITestCase

from companies.models import Company
from accounts.models import User
from sales.models import Project, Plot

from sales.tests import auth


class PratishthaPriceBookGuardTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='PRT', name='Prat Co')
        cls.admin = User.objects.create(
            email='prt_admin@x.com', company=cls.co, role='Admin',
            is_staff=True, user_code='PA')
        cls.prat = Project.objects.create(
            company=cls.co, name='Pratishtha 2', formula_set='pratishtha')
        cls.kal = Project.objects.create(
            company=cls.co, name='Kalrav 9', formula_set='kalrav')
        cls.no_book = Plot.objects.create(project=cls.prat, number='E-104', price_book={})
        cls.with_book = Plot.objects.create(
            project=cls.prat, number='E-105',
            price_book={'kind': 'flat', 'unit': 'E-105', 'flat_price': 5000000, 'token': 100000})
        cls.kal_plot = Plot.objects.create(project=cls.kal, number='K-1', price_book={})

    def setUp(self):
        cache.clear()
        auth(self.client, self.admin)

    def _book(self, project, plot):
        return self.client.post('/api/sales/bookings/', {
            'project': project.id, 'plot': plot.id,
            'client_name': 'Test Client', 'phone': '9800000001',
        }, format='json')

    def test_a_unit_with_no_price_book_is_refused(self):
        r = self._book(self.prat, self.no_book)
        self.assertEqual(r.status_code, 400)
        self.assertIn('E-104', r.data['detail'])
        self.assertIn('Price book', r.data['detail'])

    def test_a_unit_that_has_a_price_book_is_not_blocked_by_this_rule(self):
        r = self._book(self.prat, self.with_book)
        self.assertNotIn('Price book not loaded', str(r.data))

    def test_other_formula_sets_are_untouched(self):
        """Kalrav prices from rate fields and has no price book by design."""
        r = self._book(self.kal, self.kal_plot)
        self.assertNotIn('Price book not loaded', str(r.data))

    def test_an_eoi_is_exempt(self):
        r = self.client.post('/api/sales/bookings/', {
            'project': self.prat.id, 'eoi': True,
            'client_name': 'Test Client', 'phone': '9800000002',
        }, format='json')
        self.assertNotIn('Price book not loaded', str(r.data))
