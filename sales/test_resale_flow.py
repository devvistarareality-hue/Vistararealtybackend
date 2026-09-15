"""A unit sold, put back on the market, and sold again.

The earlier sale is not a mistake to be blocked — it happened, and it stays on file.
The guard that stops a unit being sold twice had closed this flow outright: booking a
resale unit answered "this plot already has a sold booking for Mr. Vipul Gandhi".
"""
from django.core.cache import cache
from rest_framework.test import APITestCase

from companies.models import Company
from accounts.models import User
from sales.models import Booking, Plot, Project

from sales.tests import auth


class ResaleFlowTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='RSL', name='Resale Co')
        cls.admin = User.objects.create(email='rsl_a@x.com', company=cls.co, role='Admin',
                                        designation='Admin', user_code='RS0', name='Admin')
        cls.rep = User.objects.create(email='rsl_r@x.com', company=cls.co, role='Employee',
                                      designation='STM', user_code='RS1', name='Parth Kapadia',
                                      reporting_manager=cls.admin)
        cls.project = Project.objects.create(company=cls.co, name='Resale Tower',
                                             booking_approvers=[cls.admin.id])

    def setUp(self):
        cache.clear()
        auth(self.client, self.rep)
        self.plot = Plot.objects.create(project=self.project, number='44', status='resale')
        self.first = Booking.objects.create(
            company=self.co, project=self.project, plot=self.plot, plot_ids=[self.plot.id],
            stm=self.admin, status='sold', approval_status='APPROVED',
            client_name='Mr. Vipul Gandhi', phone='9000000180')

    def _book(self, client='New Buyer'):
        return self.client.post('/api/sales/bookings/', {
            'project': self.project.id, 'plot': self.plot.id, 'plot_ids': [self.plot.id],
            'client_name': client, 'phone': '9000000181',
            'booking_date': '2026-09-15', 'final_amount': '2800000',
        }, format='json')

    def test_a_resale_unit_can_be_booked_again(self):
        r = self._book()
        self.assertEqual(r.status_code, 201, r.data)

    def test_the_new_booking_is_marked_a_resale_and_names_what_it_replaces(self):
        b = Booking.objects.get(id=self._book().data['id'])
        self.assertTrue(b.is_resale)
        self.assertEqual(b.resale_of_id, self.first.id)
        auth(self.client, self.admin)
        row = self.client.get(f'/api/sales/bookings/{b.id}/').data
        self.assertEqual(row['resale_of_client'], 'Mr. Vipul Gandhi')
        self.assertEqual(row['stm_name'], 'Parth Kapadia', 'who made the resale')

    def test_a_resale_can_be_approved(self):
        # The approval guard must not read the earlier sale as a clash either.
        b = Booking.objects.get(id=self._book().data['id'])
        auth(self.client, self.admin)
        r = self.client.post(f'/api/sales/bookings/{b.id}/action/', {'action': 'approve'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        b.refresh_from_db()
        self.assertEqual(b.status, 'sold')

    def test_a_unit_that_is_NOT_on_resale_is_still_blocked(self):
        # The whole point of the guard: only an explicit resale reopens a sold unit.
        self.plot.status = 'sold'
        self.plot.save(update_fields=['status'])
        self.assertEqual(self._book('Somebody Else').status_code, 409)

    def test_an_ordinary_booking_is_not_marked_a_resale(self):
        free = Plot.objects.create(project=self.project, number='45', status='available')
        r = self.client.post('/api/sales/bookings/', {
            'project': self.project.id, 'plot': free.id, 'plot_ids': [free.id],
            'client_name': 'Plain Buyer', 'phone': '9000000182',
            'booking_date': '2026-09-15', 'final_amount': '2800000',
        }, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertFalse(Booking.objects.get(id=r.data['id']).is_resale)
