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
from sales.models import Booking, Closure, Lead, Plot, Project

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


class DeletingTheMapRequiresCancellingItsBookingsTests(APITestCase):
    """"Delete All Plots" must not run over a project that has live sales.

    Booking.plot is SET_NULL, so wiping the map leaves every sale standing with no
    unit attached — and rebuilding the floor brings those units back as available, to
    be sold a second time.
    """

    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='DEL', name='Delete Co')
        cls.admin = User.objects.create(email='del@x.com', company=cls.co, role='Admin',
                                        designation='Admin', user_code='D0', name='Admin')
        cls.project = Project.objects.create(company=cls.co, name='Delete Tower')
        cls.plot = Plot.objects.create(project=cls.project, number='504', status='sold')
        Plot.objects.create(project=cls.project, number='505', status='available')

    def setUp(self):
        cache.clear()
        auth(self.client, self.admin)

    def _wipe(self):
        return self.client.delete('/api/sales/plots/bulk-delete/',
                                  {'project_id': self.project.id}, format='json')

    def test_a_project_with_a_live_sale_cannot_be_wiped(self):
        b = Booking.objects.create(company=self.co, project=self.project, plot=self.plot,
                                   plot_ids=[self.plot.id], status='sold',
                                   client_name='Bhurasingh Pawar', phone='9000000100')
        r = self._wipe()
        self.assertEqual(r.status_code, 409, r.data)
        self.assertIn(str(b.id), str(r.data))
        self.assertEqual(Plot.objects.filter(project=self.project).count(), 2)

    def test_a_project_with_no_live_sales_still_wipes(self):
        Booking.objects.create(company=self.co, project=self.project, plot=self.plot,
                               plot_ids=[self.plot.id], status='rejected',
                               approval_status='CANCELLED',
                               client_name='Cancelled', phone='9000000101')
        r = self._wipe()
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(Plot.objects.filter(project=self.project).count(), 0)


class ReleasingAUnitRespectsOtherSalesTests(APITestCase):
    """Releasing a unit must not free one that another live booking still holds.

    Pratishtha 102 read as available on the unit map while Umesh's approved sale stood
    on it. An older booking on the same unit had been cancelled, and its release put
    the unit straight back on the map — a rep then drafted on it, and the map showed
    the unit In Progress for somebody else while the real sale sat approved.
    """

    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='REL', name='Release Co')
        cls.admin = User.objects.create(email='rel@x.com', company=cls.co, role='Admin',
                                        designation='Admin', user_code='R0', name='Admin')
        cls.project = Project.objects.create(company=cls.co, name='Release Tower',
                                             booking_approvers=[cls.admin.id])

    def setUp(self):
        cache.clear()
        auth(self.client, self.admin)
        self.plot = Plot.objects.create(project=self.project, number='102', status='sold')
        self.lead = Lead.objects.create(company=self.co, name='Older', phone='9000000140')
        self.live = Booking.objects.create(
            company=self.co, project=self.project, plot=self.plot, plot_ids=[self.plot.id],
            status='sold', approval_status='APPROVED', client_name='Umesh', phone='9000000141')

    def test_cancelling_another_booking_does_not_free_the_unit(self):
        closure = Closure.objects.create(company=self.co, lead=self.lead, project=self.project,
                                         stm=self.admin, client_name='Older', status='booked',
                                         closure_date='2026-08-01', unit_no='102')
        older = Booking.objects.create(
            company=self.co, project=self.project, plot=self.plot, plot_ids=[self.plot.id],
            lead=self.lead, closure=closure, status='sold', approval_status='APPROVED',
            client_name='Devendra sinh Rajput', phone='9000000142')
        r = self.client.post(f'/api/sales/closures/{closure.id}/cancel/', {}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        older.refresh_from_db()
        self.assertEqual(older.approval_status, 'CANCELLED')
        self.plot.refresh_from_db()
        self.assertEqual(self.plot.status, 'sold', "Umesh's sale still holds this unit")

    def test_discarding_a_draft_does_not_free_a_sold_unit(self):
        draft = Booking.objects.create(
            company=self.co, project=self.project, plot=self.plot, plot_ids=[self.plot.id],
            stm=self.admin, status='draft', client_name='Umesh Tomar', phone='9000000143')
        r = self.client.post(f'/api/sales/bookings/{draft.id}/discard/')
        self.assertIn(r.status_code, (200, 204), getattr(r, 'data', None))
        self.plot.refresh_from_db()
        self.assertEqual(self.plot.status, 'sold')

    def test_a_unit_with_no_other_claim_is_still_released(self):
        # The ordinary case has to keep working: nothing else holds it, so it goes back.
        free_plot = Plot.objects.create(project=self.project, number='103', status='hold')
        draft = Booking.objects.create(
            company=self.co, project=self.project, plot=free_plot, plot_ids=[free_plot.id],
            stm=self.admin, status='draft', client_name='Someone', phone='9000000144')
        self.client.post(f'/api/sales/bookings/{draft.id}/discard/')
        free_plot.refresh_from_db()
        self.assertEqual(free_plot.status, 'available')

    def test_a_lookalike_booking_is_not_treated_as_this_ones_revision(self):
        """Two separate bookings on one unit to the same buyer are not one deal.

        The revision grouping joins rows that merely share a project, phone and unit —
        a fair guess for showing a history, far too loose for deciding a unit is free.
        Discarding the draft released unit 102 out from under Umesh's live sale
        because the two looked alike, and the map put the unit back up for sale.
        """
        same_phone = self.live.phone
        draft = Booking.objects.create(
            company=self.co, project=self.project, plot=self.plot, plot_ids=[self.plot.id],
            stm=self.admin, status='draft', client_name='Umesh Tomar', phone=same_phone)
        r = self.client.post(f'/api/sales/bookings/{draft.id}/discard/')
        self.assertIn(r.status_code, (200, 204), getattr(r, 'data', None))
        self.plot.refresh_from_db()
        self.assertEqual(self.plot.status, 'sold',
                         'the live sale holds this unit, however similar the draft looked')

    def test_a_real_revision_still_releases_its_own_unit(self):
        # The exclusion has to keep working for a recorded revision: it shares the unit
        # with the version it replaces, so rejecting it must not be blocked by that.
        revision = Booking.objects.create(
            company=self.co, project=self.project, plot=self.plot, plot_ids=[self.plot.id],
            stm=self.admin, status='draft', revision_no=1, revision_of=self.live,
            client_name='Umesh', phone=self.live.phone)
        chain = __import__('sales.views', fromlist=['_revision_chain_ids'])._revision_chain_ids(
            revision.id, self.co, strict=True)
        self.assertIn(self.live.id, chain, 'a recorded revision_of link must still count')


class TheMapReclaimsUnitsWithALiveSaleTests(APITestCase):
    """The net under every release path: a unit a live booking holds never reads as
    available on the unit map.

    Three separate paths had freed a sold unit — a cancelled sibling booking's release,
    a discarded draft, a status edit in the plot editor — and each was fixed where it
    stood. This reconciles on read, so whatever is written next cannot leave a sold
    unit back on the map to be sold a second time.
    """

    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='RCL', name='Reclaim Co')
        cls.admin = User.objects.create(email='rcl@x.com', company=cls.co, role='Admin',
                                        designation='Admin', user_code='L0', name='Admin')
        cls.project = Project.objects.create(company=cls.co, name='Reclaim Tower')

    def setUp(self):
        cache.clear()
        auth(self.client, self.admin)

    def _plot(self, number, status='available'):
        return Plot.objects.create(project=self.project, number=number, status=status)

    def _map(self):
        r = self.client.get(f'/api/sales/plots/?project={self.project.id}')
        self.assertEqual(r.status_code, 200)
        return {p['number']: p['status'] for p in r.data}

    def test_a_unit_loose_under_a_completed_sale_is_reclaimed_as_sold(self):
        plot = self._plot('102')
        Booking.objects.create(company=self.co, project=self.project, plot=plot,
                               plot_ids=[plot.id], status='sold', approval_status='APPROVED',
                               client_name='Umesh', phone='9000000150')
        self.assertEqual(self._map()['102'], 'sold')

    def test_a_unit_loose_under_a_pending_booking_is_reclaimed_as_held(self):
        # Pending means spoken for, not gone — the unit goes back to hold, not sold.
        plot = self._plot('103')
        Booking.objects.create(company=self.co, project=self.project, plot=plot,
                               plot_ids=[plot.id], status='pending',
                               client_name='Awaiting', phone='9000000151')
        self.assertEqual(self._map()['103'], 'hold')

    def test_a_resale_unit_is_left_exactly_alone(self):
        # Resale is a deliberate decision to offer a sold unit again, and it keeps its
        # old booking on purpose. Reclaiming it would undo the decision.
        plot = self._plot('104', status='resale')
        Booking.objects.create(company=self.co, project=self.project, plot=plot,
                               plot_ids=[plot.id], status='sold', approval_status='APPROVED',
                               client_name='Previous Owner', phone='9000000152')
        self.assertEqual(self._map()['104'], 'resale')

    def test_a_genuinely_free_unit_stays_available(self):
        self._plot('105')
        Booking.objects.create(company=self.co, project=self.project, plot=self._plot('106'),
                               status='rejected', approval_status='CANCELLED',
                               client_name='Cancelled', phone='9000000153')
        m = self._map()
        self.assertEqual(m['105'], 'available')
        self.assertEqual(m['106'], 'available', 'a cancelled booking holds nothing')
