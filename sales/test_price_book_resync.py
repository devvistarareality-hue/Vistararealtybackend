"""Editing a unit's area must carry through to its price book.

A price book is computed from the unit's `size`, so editing the size afterwards left
the two disagreeing with nothing to say so. Four Pratishtha 2 shops drifted that way:
D-SHOP18 read 425 sq.ft on the unit map and priced at 415 in the booking form — a
₹1.2 lakh difference on a ₹51 lakh shop, quoted from the stale figure.
"""
from django.core.cache import cache
from rest_framework.test import APITestCase

from companies.models import Company
from accounts.models import User
from sales.models import Plot, Project
from sales.pricing import pratishtha2

from sales.tests import auth


def _book(number, size, terrace='', facing='', floor=0):
    area = pratishtha2.area_of(size)
    return pratishtha2.price_book_for(number, flat_area=area,
                                      terrace_area=pratishtha2.area_of(terrace) or 0,
                                      sq_feet=area, facing=facing, floor=floor)


class PriceBookResyncTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='PBR', name='Price Co')
        cls.admin = User.objects.create(email='pbr@x.com', company=cls.co, role='Admin',
                                        designation='Admin', user_code='P1', name='Admin')
        cls.project = Project.objects.create(company=cls.co, name='Pratishtha 2',
                                             formula_set='pratishtha')

    def setUp(self):
        cache.clear()
        auth(self.client, self.admin)

    def _plot(self, number='D-SHOP18', size='415 sqft', book=None):
        return Plot.objects.create(project=self.project, number=number, size=size,
                                   price_book=book if book is not None else _book(number, size))

    def test_changing_the_size_recomputes_the_price_book(self):
        plot = self._plot(size='415 sqft')
        self.assertEqual(plot.price_book['sq_feet'], 415.0)
        r = self.client.patch(f'/api/sales/plots/{plot.id}/', {'size': '425 sqft'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        plot.refresh_from_db()
        self.assertEqual(plot.price_book['sq_feet'], 425.0)
        # And every figure derived from it moves with it, not just the area.
        self.assertEqual(plot.price_book, _book('D-SHOP18', '425 sqft'))

    def test_a_book_that_is_not_the_generators_own_is_left_alone(self):
        # Hand-written or differently-generated books must not be replaced by a guess.
        hand = {'sq_feet': 415.0, 'amount': 1, 'note': 'agreed with the client'}
        plot = self._plot(book=hand)
        r = self.client.patch(f'/api/sales/plots/{plot.id}/', {'size': '425 sqft'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        plot.refresh_from_db()
        self.assertEqual(plot.price_book, hand)

    def test_an_edit_that_touches_no_area_leaves_the_book_untouched(self):
        plot = self._plot()
        before = dict(plot.price_book)
        r = self.client.patch(f'/api/sales/plots/{plot.id}/', {'status': 'hold'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        plot.refresh_from_db()
        self.assertEqual(plot.price_book, before)

    def test_a_unit_with_no_book_does_not_gain_one(self):
        # Filling books is the loader's job, and it reports what it skips; silently
        # inventing one here would hide a unit that needs looking at.
        plot = self._plot(book={})
        r = self.client.patch(f'/api/sales/plots/{plot.id}/', {'size': '425 sqft'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        plot.refresh_from_db()
        self.assertEqual(plot.price_book, {})
