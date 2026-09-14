"""A unit may carry one live sale, and only one.

Six units in production ended up sold to two different buyers at once — five of them
on a single project. The submission-time guard was looking for bookings with status
'approved', and no such row exists: an approved booking is stored as 'sold'. The guard
therefore only ever caught a second submission while the first was still pending, and
waved through every unit that had already been sold.
"""
from django.core.cache import cache
from rest_framework.test import APITestCase

from companies.models import Company
from accounts.models import User
from sales.models import Booking, Plot, Project

from sales.tests import auth


class OneUnitOneSaleTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='OUS', name='One Sale Co')
        cls.admin = User.objects.create(email='ous_admin@x.com', company=cls.co, role='Admin',
                                        designation='Admin', user_code='U0', name='Admin')
        cls.rep = User.objects.create(email='ous_rep@x.com', company=cls.co, role='Employee',
                                      designation='STM', user_code='U1', name='Rep',
                                      reporting_manager=cls.admin)
        cls.project = Project.objects.create(company=cls.co, name='One Sale Tower')
        cls.plot = Plot.objects.create(project=cls.project, number='504', status='available',
                                       size='84 sqyrd')

    def setUp(self):
        cache.clear()
        auth(self.client, self.rep)

    def _submit(self, client_name):
        return self.client.post('/api/sales/bookings/', {
            'project': self.project.id, 'plot': self.plot.id, 'plot_ids': [self.plot.id],
            'client_name': client_name, 'phone': '9000000090', 'booking_date': '2026-09-07',
            'final_amount': '2800000',
        }, format='json')

    def test_a_sold_unit_cannot_be_booked_again(self):
        Booking.objects.create(company=self.co, project=self.project, plot=self.plot,
                               plot_ids=[self.plot.id], status='sold',
                               client_name='Bhanubhai Kalabhai Parmar', phone='9000000091')
        r = self._submit('Bhurasingh Pawar')
        self.assertEqual(r.status_code, 409, r.data)
        self.assertIn('Bhanubhai', str(r.data))

    def test_a_pending_unit_still_cannot_be_booked_again(self):
        # The case the old guard did catch — it must keep working.
        Booking.objects.create(company=self.co, project=self.project, plot=self.plot,
                               plot_ids=[self.plot.id], status='pending',
                               client_name='Priyankaben thakor', phone='9000000092')
        self.assertEqual(self._submit('Manishaben Nadiya').status_code, 409)

    def test_a_free_unit_books_normally(self):
        r = self._submit('First Buyer')
        self.assertEqual(r.status_code, 201, r.data)

    def test_a_rejected_booking_does_not_block_the_unit(self):
        Booking.objects.create(company=self.co, project=self.project, plot=self.plot,
                               plot_ids=[self.plot.id], status='rejected',
                               client_name='Withdrawn', phone='9000000093')
        self.assertEqual(self._submit('Next Buyer').status_code, 201)

    def test_approval_refuses_a_unit_that_was_sold_in_the_meantime(self):
        # Two bookings can sit pending on one unit and each be approved in turn, and a
        # unit's status can be lost to a re-import between the two — neither of which
        # the submission-time check can see.
        sold = Booking.objects.create(company=self.co, project=self.project, plot=self.plot,
                                      plot_ids=[self.plot.id], status='sold',
                                      client_name='Already Sold', phone='9000000094')
        pending = Booking.objects.create(company=self.co, project=self.project, plot=self.plot,
                                         plot_ids=[self.plot.id], status='pending',
                                         client_name='Too Late', phone='9000000095')
        auth(self.client, self.admin)
        r = self.client.post(f'/api/sales/bookings/{pending.id}/action/', {'action': 'approve'}, format='json')
        self.assertEqual(r.status_code, 409, r.data)
        self.assertIn(str(sold.id), str(r.data))
        pending.refresh_from_db()
        self.assertEqual(pending.status, 'pending')

    def test_approving_a_revision_of_the_same_deal_is_not_blocked(self):
        original = Booking.objects.create(company=self.co, project=self.project, plot=self.plot,
                                          plot_ids=[self.plot.id], status='sold', revision_no=0,
                                          client_name='Same Buyer', phone='9000000096')
        revision = Booking.objects.create(company=self.co, project=self.project, plot=self.plot,
                                          plot_ids=[self.plot.id], status='pending', revision_no=1,
                                          revision_of=original, client_name='Same Buyer',
                                          phone='9000000096')
        auth(self.client, self.admin)
        r = self.client.post(f'/api/sales/bookings/{revision.id}/action/', {'action': 'approve'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)


class FreeingAUnitRequiresCancellingItsBookingTests(APITestCase):
    """A unit cannot be freed from the plot editor while a live booking holds it.

    Cancelling is a real operation — it voids the signed LOI, deletes the closure,
    reopens the lead and notifies the chain. Editing the unit's status back to
    available does none of that, so the sale stays live and approved while the unit
    goes back on the map to be sold again. That is how six units came to have two
    live sales each, with no cancellation recorded anywhere.
    """

    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='FRU', name='Free Co')
        cls.admin = User.objects.create(email='fru@x.com', company=cls.co, role='Admin',
                                        designation='Admin', user_code='F0', name='Admin')
        cls.project = Project.objects.create(company=cls.co, name='Free Tower')
        cls.plot = Plot.objects.create(project=cls.project, number='504', status='sold',
                                       size='84 sqyrd')

    def setUp(self):
        cache.clear()
        auth(self.client, self.admin)

    def _free(self):
        return self.client.patch(f'/api/sales/plots/{self.plot.id}/',
                                 {'status': 'available'}, format='json')

    def test_a_sold_unit_cannot_be_freed_from_the_plot_editor(self):
        b = Booking.objects.create(company=self.co, project=self.project, plot=self.plot,
                                   plot_ids=[self.plot.id], status='sold',
                                   client_name='Bhanubhai Kalabhai Parmar', phone='9000000097')
        r = self._free()
        self.assertEqual(r.status_code, 409, r.data)
        self.assertIn(str(b.id), str(r.data))
        self.plot.refresh_from_db()
        self.assertEqual(self.plot.status, 'sold')

    def test_a_cancelled_booking_no_longer_holds_the_unit(self):
        # What the cancel endpoint leaves behind: rejected + CANCELLED. The unit is
        # then free, and re-booking it is exactly what should happen next.
        Booking.objects.create(company=self.co, project=self.project, plot=self.plot,
                               plot_ids=[self.plot.id], status='rejected',
                               approval_status='CANCELLED',
                               client_name='Cancelled Buyer', phone='9000000098')
        self.assertEqual(self._free().status_code, 200)

    def test_other_edits_to_a_sold_unit_still_work(self):
        # The rule is about freeing the unit, not about touching it at all.
        Booking.objects.create(company=self.co, project=self.project, plot=self.plot,
                               plot_ids=[self.plot.id], status='sold',
                               client_name='Held', phone='9000000099')
        r = self.client.patch(f'/api/sales/plots/{self.plot.id}/',
                              {'size': '90 sqyrd'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
