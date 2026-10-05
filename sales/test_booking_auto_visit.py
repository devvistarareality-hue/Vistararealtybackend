"""On a booking's approval a completed visit is recorded on the booking date — but
only when the booking brought a new number in (for that project). A lead already on
file keeps its own visit history; nothing is added to it."""
from datetime import date, timedelta

from django.test import TestCase
from django.utils import timezone

from accounts.models import User
from companies.models import Company
from sales.models import Booking, Lead, Project, SiteVisit
from sales.views import _ensure_lead_and_site_visit_for_booking


class AutoVisitOnApproval(TestCase):
    def setUp(self):
        self.co = Company.objects.create(code='AVB', name='Avb Co')
        self.p = Project.objects.create(company=self.co, name='Kalrav')
        self.other_p = Project.objects.create(company=self.co, name='Tundav')
        self.stm = User.objects.create(name='S', email='s@avb.com', phone='9100000001', user_code='AV-S',
                                       role='Employee', designation='STM', company=self.co)

    def _booking(self, phone, lead=None):
        return Booking.objects.create(company=self.co, project=self.p, stm=self.stm, client_name='Client',
                                      phone=phone, booking_date=date(2026, 9, 20), status='sold', lead=lead)

    def _visits(self, lead_id):
        return SiteVisit.objects.filter(lead_id=lead_id, status='completed')

    def test_new_number_gets_a_lead_and_a_visit_on_the_booking_date(self):
        lead_id, sv_id = _ensure_lead_and_site_visit_for_booking(self._booking('9876500001'))
        self.assertIsNotNone(sv_id)
        sv = SiteVisit.objects.get(pk=sv_id)
        self.assertEqual(timezone.localtime(sv.visited_at).date(), date(2026, 9, 20))

    def test_lead_created_with_the_booking_still_gets_a_visit(self):
        lead = Lead.objects.create(company=self.co, project=self.p, name='Client', phone='9876500002', stm=self.stm)
        _, sv_id = _ensure_lead_and_site_visit_for_booking(self._booking('9876500002', lead))
        self.assertIsNotNone(sv_id)

    def test_lead_already_on_file_gets_no_visit(self):
        lead = Lead.objects.create(company=self.co, project=self.p, name='Client', phone='9876500003', stm=self.stm)
        Lead.objects.filter(pk=lead.pk).update(created_at=timezone.now() - timedelta(days=30))
        _, sv_id = _ensure_lead_and_site_visit_for_booking(self._booking('9876500003', lead))
        self.assertIsNone(sv_id)
        self.assertFalse(self._visits(lead.pk).exists())

    def test_same_number_same_project_is_linked_not_duplicated(self):
        lead = Lead.objects.create(company=self.co, project=self.p, name='Client', phone='9876500004', stm=self.stm)
        b = self._booking('9876500004')
        lead_id, sv_id = _ensure_lead_and_site_visit_for_booking(b)
        self.assertEqual(lead_id, lead.pk)
        self.assertIsNone(sv_id)
        self.assertEqual(Lead.objects.filter(company=self.co, project=self.p).count(), 1)

    def test_same_number_on_another_project_is_a_new_lead(self):
        old = Lead.objects.create(company=self.co, project=self.other_p, name='Client', phone='9876500005', stm=self.stm)
        lead_id, sv_id = _ensure_lead_and_site_visit_for_booking(self._booking('9876500005'))
        self.assertNotEqual(lead_id, old.pk)
        self.assertIsNotNone(sv_id)
