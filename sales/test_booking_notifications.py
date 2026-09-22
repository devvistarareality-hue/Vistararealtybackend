"""Every booking event reaches the rep, the project's Sales/CP approvers and its
Accounts approvers — each person once, and never the person who acted."""
from unittest import mock

from django.core.cache import cache
from rest_framework.test import APITestCase

from companies.models import Company
from accounts.models import User
from sales.models import Booking, Closure, Lead, Plot, Project
from sales.tests import auth
from sales.views import _notify_booking_event


class BookingNotificationTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='BNT', name='Notify Co')
        mk = lambda code, name, role='Employee', **kw: User.objects.create(
            email=f'{code}@x.com', company=cls.co, role=role, designation=role,
            user_code=code, name=name, **kw)
        cls.admin = mk('N0', 'Admin', 'Admin')
        cls.sales_mgr = mk('N1', 'Sales Mgr', 'Manager')
        cls.cp_mgr = mk('N2', 'CP Mgr', 'Manager')
        cls.acc = mk('N3', 'Accounts', 'Manager', manager_modules=['Accounts & Finance'])
        cls.acc_cp = mk('N4', 'Accounts CP', 'Manager', manager_modules=['Accounts & Finance'])
        cls.acc_other = mk('N5', 'Accounts Elsewhere', 'Manager', manager_modules=['Accounts & Finance'])
        cls.stm = mk('N6', 'Rep', reporting_manager=cls.sales_mgr)
        cls.project = Project.objects.create(
            company=cls.co, name='Kalrav',
            booking_approvers=[cls.sales_mgr.id], cp_booking_approvers=[cls.cp_mgr.id],
            accounts_booking_approvers=[cls.acc.id], accounts_cp_booking_approvers=[cls.acc_cp.id])

    def setUp(self):
        cache.clear()
        self.plot = Plot.objects.create(project=self.project, number='12', status='hold')
        self.booking = Booking.objects.create(
            company=self.co, project=self.project, plot=self.plot, plot_ids=[self.plot.id],
            stm=self.stm, status='pending', client_name='Asha', phone='9000000200',
            final_amount=2500000)

    def _sent(self, fn):
        sent = []
        with mock.patch('notifications.notify',
                        side_effect=lambda u, ntype, title, *a, **k: sent.append((u.id, ntype, title))):
            fn()
        return sent

    def _event(self, event, actor=None, booking=None):
        return self._sent(lambda: _notify_booking_event(self.co, booking or self.booking, event, actor))

    def test_sales_approval_reaches_rep_sales_and_project_accounts(self):
        sent = self._event('sales_approved', actor=self.admin)
        ids = [s[0] for s in sent]
        self.assertCountEqual(ids, [self.stm.id, self.sales_mgr.id, self.acc.id])
        self.assertIn((self.acc.id, 'accounts_booking_approval', 'Accounts approval needed'),
                      [(s[0], s[1], s[2]) for s in sent])
        self.assertNotIn(self.acc_other.id, ids, 'only this project\'s Accounts approvers')

    def test_accounts_approval_reaches_sales_side_too(self):
        ids = [s[0] for s in self._event('accounts_approved', actor=self.acc)]
        self.assertCountEqual(ids, [self.stm.id, self.sales_mgr.id])

    def test_cancellation_reaches_everyone_but_the_canceller(self):
        ids = [s[0] for s in self._event('cancelled', actor=self.sales_mgr)]
        self.assertCountEqual(ids, [self.stm.id, self.acc.id])

    def test_cp_deal_routes_to_cp_lists_and_tells_sales(self):
        self.booking.source = 'Channel Partner'
        self.booking.save(update_fields=['source'])
        with mock.patch('sales.views._is_cp_sourced_booking', return_value=True):
            ids = [s[0] for s in self._event('sales_approved', actor=self.cp_mgr)]
        self.assertCountEqual(ids, [self.stm.id, self.sales_mgr.id, self.acc_cp.id])

    def test_rep_gets_a_receipt_on_submit(self):
        sent = self._event('submitted', actor=self.stm)
        self.assertIn((self.stm.id, 'booking_submitted'), [(s[0], s[1]) for s in sent])
        self.assertIn(self.acc.id, [s[0] for s in sent])

    def test_no_accounts_approvers_falls_back_to_accounts_managers(self):
        self.project.accounts_booking_approvers = []
        self.project.save(update_fields=['accounts_booking_approvers'])
        try:
            ids = [s[0] for s in self._event('sales_rejected', actor=self.sales_mgr)]
        finally:
            self.project.accounts_booking_approvers = [self.acc.id]
            self.project.save(update_fields=['accounts_booking_approvers'])
        self.assertIn(self.acc_other.id, ids)
        self.assertIn(self.stm.id, ids)

    def test_everyone_is_told_once(self):
        # The Sales approver is also an Accounts approver: one notification, not two.
        self.project.accounts_booking_approvers = [self.acc.id, self.sales_mgr.id]
        self.project.save(update_fields=['accounts_booking_approvers'])
        try:
            ids = [s[0] for s in self._event('accounts_rejected', actor=self.acc)]
        finally:
            self.project.accounts_booking_approvers = [self.acc.id]
            self.project.save(update_fields=['accounts_booking_approvers'])
        self.assertEqual(len(ids), len(set(ids)))

    def test_accounts_approve_endpoint_notifies_sales_side(self):
        self.booking.status = 'sold'
        self.booking.approval_status = 'APPROVED'
        self.booking.accounts_status = 'pending'
        self.booking.save(update_fields=['status', 'approval_status', 'accounts_status'])
        auth(self.client, self.admin)
        sent = self._sent(lambda: self.assertEqual(self.client.post(
            f'/api/sales/bookings/{self.booking.id}/accounts-action/', {'action': 'approve'},
            format='json').status_code, 200))
        ids = [s[0] for s in sent]
        self.assertIn(self.stm.id, ids)
        self.assertIn(self.sales_mgr.id, ids)
        self.assertIn(self.acc.id, ids)
        # …and the approval is in the activity log, naming who did it and the deal.
        from activity.models import ActivityLog
        row = ActivityLog.objects.filter(target_type='booking', target_id=str(self.booking.id)).latest('id')
        self.assertEqual((row.actor_id, row.module, row.action), (self.admin.id, 'Accounts & Finance', 'approved'))
        self.assertIn('Approved (Accounts) booking — Asha', row.summary)

    def test_cancel_endpoint_notifies_accounts(self):
        lead = Lead.objects.create(company=self.co, name='Asha', phone='9000000200',
                                   stm=self.stm, status='closed', stm_status='closed')
        closure = Closure.objects.create(company=self.co, lead=lead, project=self.project, stm=self.stm,
                                         client_name='Asha', status='booked', closure_date='2026-09-01',
                                         unit_no='12', total_amount=2500000)
        self.booking.closure = closure
        self.booking.lead = lead
        self.booking.status = 'sold'
        self.booking.save(update_fields=['closure', 'lead', 'status'])
        auth(self.client, self.admin)
        sent = self._sent(lambda: self.assertEqual(self.client.post(
            f'/api/sales/closures/{closure.id}/cancel/', {}, format='json').status_code, 200))
        by_user = {s[0]: s[1] for s in sent}
        self.assertEqual(by_user.get(self.acc.id), 'accounts_booking_cancelled')
        self.assertEqual(by_user.get(self.sales_mgr.id), 'booking_cancelled')
        self.assertEqual(by_user.get(self.stm.id), 'booking_cancelled')
