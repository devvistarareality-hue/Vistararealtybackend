"""A booking approved by Sales and Accounts closes its lead — whatever path approved it."""
from django.test import TestCase
from django.utils import timezone

from accounts.models import User
from companies.models import Company
from sales.models import Booking, Lead, LeadStatusHistory, Project


class LeadFollowsBookingTests(TestCase):
    def setUp(self):
        self.co = Company.objects.create(code='LSY', name='Lsy')
        self.p = Project.objects.create(company=self.co, name='Kalrav')
        self.lead = Lead.objects.create(company=self.co, project=self.p, name='Kalpesh', phone='9950000001',
                                        status='lost', stm_status='sv_done')

    def _book(self, **kw):
        with self.captureOnCommitCallbacks(execute=True):
            return Booking.objects.create(company=self.co, project=self.p, lead=self.lead, client_name='Kalpesh',
                                          booking_date=timezone.localdate(), **kw)

    def test_approved_booking_closes_the_lead(self):
        b = self._book(status='pending', accounts_status='pending')
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, 'lost')                 # not approved yet
        b.status, b.accounts_status = 'sold', 'approved'
        with self.captureOnCommitCallbacks(execute=True):
            b.save()
        self.lead.refresh_from_db()
        self.assertEqual((self.lead.status, self.lead.stm_status), ('closed', 'closed'))
        self.assertEqual(LeadStatusHistory.objects.filter(lead=self.lead, new_value='closed').count(), 2)

    def test_cancelled_or_unapproved_leave_it(self):
        self._book(status='sold', accounts_status='pending')
        self._book(status='sold', accounts_status='approved', approval_status='CANCELLED')
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, 'lost')
