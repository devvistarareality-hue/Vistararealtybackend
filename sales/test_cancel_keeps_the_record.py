"""Cancelling a booking is a record, not an erasure.

Accounts reconciles against cancelled deals, and the signed LOI is the evidence of
what the buyer agreed to. Cancelling used to delete the PDF from storage and delete
the closure row outright, leaving nothing to show on the one day it matters — when
somebody disputes the cancellation.
"""
from django.core.cache import cache
from django.core.files.base import ContentFile
from rest_framework.test import APITestCase

from companies.models import Company
from accounts.models import User
from sales.models import Booking, Closure, Lead, Plot, Project

from sales.tests import auth


class CancelKeepsTheRecordTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='CKR', name='Keep Co')
        cls.admin = User.objects.create(email='ckr@x.com', company=cls.co, role='Admin',
                                        designation='Admin', user_code='C0', name='Admin')
        cls.stm = User.objects.create(email='ckr_stm@x.com', company=cls.co, role='Employee',
                                      designation='STM', user_code='C1', name='Stm',
                                      reporting_manager=cls.admin)

    def setUp(self):
        cache.clear()
        auth(self.client, self.admin)
        self.project = Project.objects.create(company=self.co, name='Keep Tower')
        self.plot = Plot.objects.create(project=self.project, number='504', status='sold')
        self.lead = Lead.objects.create(company=self.co, name='Bhanubhai', phone='9000000110',
                                        stm=self.stm, status='closed', stm_status='closed')
        self.closure = Closure.objects.create(
            company=self.co, lead=self.lead, project=self.project, stm=self.stm,
            client_name='Bhanubhai', status='booked', closure_date='2026-08-11',
            unit_no='504', total_amount=2800000)
        self.booking = Booking.objects.create(
            company=self.co, project=self.project, plot=self.plot, plot_ids=[self.plot.id],
            lead=self.lead, stm=self.stm, closure=self.closure, status='sold',
            approval_status='APPROVED', client_name='Bhanubhai', phone='9000000110')
        self.booking.loi_document.save('LOI_504.pdf', ContentFile(b'%PDF-1.4 signed'), save=True)

    def _cancel(self):
        return self.client.post(f'/api/sales/closures/{self.closure.id}/cancel/', {}, format='json')

    def test_the_signed_loi_survives_the_cancellation(self):
        name = self.booking.loi_document.name
        self.assertTrue(name)
        r = self._cancel()
        self.assertEqual(r.status_code, 200, r.data)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.loi_document.name, name,
                         'the PDF must stay linked — Accounts reconciles against it')

    def test_the_closure_is_marked_cancelled_not_deleted(self):
        self.assertEqual(self._cancel().status_code, 200)
        self.closure.refresh_from_db()
        self.assertEqual(self.closure.status, 'cancelled')

    def test_the_booking_keeps_every_detail_and_reads_cancelled(self):
        self.assertEqual(self._cancel().status_code, 200)
        b = Booking.objects.get(pk=self.booking.pk)
        self.assertEqual(b.status, 'rejected')
        self.assertEqual(b.approval_status, 'CANCELLED')
        self.assertEqual(b.client_name, 'Bhanubhai')
        self.assertEqual(b.phone, '9000000110')
        self.assertEqual(b.closure_id, self.closure.id, 'still findable from the closure')

    def test_the_unit_goes_back_on_the_map(self):
        self.assertEqual(self._cancel().status_code, 200)
        self.plot.refresh_from_db()
        self.assertEqual(self.plot.status, 'available')

    def test_a_cancelled_closure_is_not_counted_as_a_sale(self):
        # It is kept for Accounts, but it is not a conversion — the dashboards and the
        # team's closure counts must not pick it up.
        self.assertEqual(self._cancel().status_code, 200)
        r = self.client.get('/api/sales/my-team/')
        self.assertEqual(r.status_code, 200)
        for member in r.data:
            if member['id'] == self.stm.id:
                self.assertEqual(member['closures'], 0)


class CancelledIsItsOwnTabTests(APITestCase):
    """`?status=cancelled` and `?status=rejected` are different questions.

    Both sit at status='rejected' in the table; the difference is approval_status. One
    was refused before it counted for anything, the other was a live sale that came
    off the books and keeps its signed LOI — so the tabs ask for them separately.
    """

    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='CTB', name='Tab Co')
        cls.admin = User.objects.create(email='ctb@x.com', company=cls.co, role='Admin',
                                        designation='Admin', user_code='T0', name='Admin')
        cls.project = Project.objects.create(company=cls.co, name='Tab Tower')
        cls.cancelled = Booking.objects.create(
            company=cls.co, project=cls.project, stm=cls.admin, status='rejected',
            approval_status='CANCELLED', client_name='Came Off The Books', phone='9000000120')
        cls.refused = Booking.objects.create(
            company=cls.co, project=cls.project, stm=cls.admin, status='rejected',
            approval_status='REJECTED', client_name='Refused Up Front', phone='9000000121')
        cls.live = Booking.objects.create(
            company=cls.co, project=cls.project, stm=cls.admin, status='sold',
            approval_status='APPROVED', client_name='Standing', phone='9000000122')

    def setUp(self):
        cache.clear()
        auth(self.client, self.admin)

    def _ids(self, status):
        r = self.client.get(f'/api/sales/bookings/?mine=1&status={status}')
        self.assertEqual(r.status_code, 200)
        return [b['id'] for b in r.data]

    def test_cancelled_returns_only_cancelled(self):
        self.assertEqual(self._ids('cancelled'), [self.cancelled.id])

    def test_rejected_no_longer_carries_cancelled_with_it(self):
        self.assertEqual(self._ids('rejected'), [self.refused.id])

    def test_approved_is_untouched(self):
        self.assertEqual(self._ids('sold'), [self.live.id])


class WhoDecidedTests(APITestCase):
    """Every decision names the person who made it.

    approved_at recorded WHEN a deal went on the books from the start, but never WHO —
    so a booking named nobody accountable for approving it, and a cancellation, which
    takes a live sale off the books, named nobody at all.
    """

    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='WHO', name='Who Co')
        cls.approver = User.objects.create(email='who_a@x.com', company=cls.co, role='Admin',
                                           designation='Admin', user_code='W0', name='Rachit Puranik')
        cls.stm = User.objects.create(email='who_s@x.com', company=cls.co, role='Employee',
                                      designation='STM', user_code='W1', name='Stm',
                                      reporting_manager=cls.approver)
        cls.project = Project.objects.create(company=cls.co, name='Who Tower')

    def setUp(self):
        cache.clear()
        auth(self.client, self.approver)

    def _booking(self, **over):
        f = dict(company=self.co, project=self.project, stm=self.stm, status='pending',
                 client_name='Buyer', phone='9000000130')
        f.update(over)
        return Booking.objects.create(**f)

    def _row(self, pk):
        r = self.client.get(f'/api/sales/bookings/{pk}/')
        self.assertEqual(r.status_code, 200)
        return r.data

    def test_approving_records_who(self):
        b = self._booking()
        self.assertEqual(self.client.post(f'/api/sales/bookings/{b.id}/action/',
                                          {'action': 'approve'}, format='json').status_code, 200)
        row = self._row(b.id)
        self.assertEqual(row['approved_by_name'], 'Rachit Puranik')
        self.assertIsNone(row['rejected_by_name'])
        self.assertIsNone(row['cancelled_by_name'])

    def test_rejecting_records_who(self):
        b = self._booking()
        self.assertEqual(self.client.post(f'/api/sales/bookings/{b.id}/action/',
                                          {'action': 'reject'}, format='json').status_code, 200)
        row = self._row(b.id)
        self.assertEqual(row['rejected_by_name'], 'Rachit Puranik')
        self.assertIsNone(row['approved_by_name'])

    def test_cancelling_records_who(self):
        lead = Lead.objects.create(company=self.co, name='Buyer', phone='9000000131', stm=self.stm)
        closure = Closure.objects.create(company=self.co, lead=lead, project=self.project,
                                         stm=self.stm, client_name='Buyer', status='booked',
                                         closure_date='2026-09-01', unit_no='101')
        b = self._booking(status='sold', approval_status='APPROVED', closure=closure, lead=lead)
        self.assertEqual(self.client.post(f'/api/sales/closures/{closure.id}/cancel/',
                                          {}, format='json').status_code, 200)
        row = self._row(b.id)
        self.assertEqual(row['cancelled_by_name'], 'Rachit Puranik')
        self.assertIsNotNone(row['cancelled_at'])

    def test_a_booking_nobody_has_decided_names_nobody(self):
        row = self._row(self._booking().id)
        for k in ('approved_by_name', 'rejected_by_name', 'cancelled_by_name'):
            self.assertIsNone(row[k], k)
