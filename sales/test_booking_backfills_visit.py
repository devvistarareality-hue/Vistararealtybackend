"""Approving a booking puts it in the pipeline: a lead, and a completed site visit
dated on the booking date.

Two flows reach approval. A closure recorded from a lead already has the lead but
may never have had a visit logged; a unit booked directly has a lead created at
submission. Either way the sale happened, so the funnel should show the visit that
produced it rather than a closure that appears from nowhere.
"""
from datetime import date, timedelta

from django.core.cache import cache
from rest_framework.test import APITestCase

from companies.models import Company
from accounts.models import User
from sales.models import Lead, Project, Plot, Booking, SiteVisit, Closure
from sales.tests import auth


class BookingBackfillsSiteVisitTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.co = Company.objects.create(code='BFL', name='Backfill Co')
        self.stm = User.objects.create(email='stm@x.com', company=self.co, role='Sales',
                                       user_code='S1', designation='STM')
        self.tc = User.objects.create(email='tc@x.com', company=self.co, role='Sales',
                                      user_code='T1', designation='Telecaller')
        self.admin = User.objects.create(email='ad@x.com', company=self.co, role='Admin',
                                         user_code='A1', is_staff=True)
        self.proj = Project.objects.create(company=self.co, name='Tundav', is_active=True)
        self.bdate = date(2026, 8, 10)

    def tearDown(self):
        cache.clear()

    def _booking(self, **kw):
        kw.setdefault('client_name', 'Ramesh')
        kw.setdefault('phone', '9876500001')
        return Booking.objects.create(
            company=self.co, project=self.proj, stm=self.stm, status='pending',
            booking_date=self.bdate, area='7', **kw)

    def _approve(self, b):
        auth(self.client, self.admin)
        res = self.client.post('/api/sales/bookings/%d/action/' % b.pk,
                               {'action': 'approve'}, format='json')
        self.assertEqual(res.status_code, 200, res.data)
        b.refresh_from_db()
        return b

    # ── booked directly, no lead ──────────────────────────────────────────────
    def test_direct_booking_gains_a_lead_and_a_visit(self):
        b = self._booking()
        self.assertIsNone(b.lead_id)
        b = self._approve(b)

        self.assertIsNotNone(b.lead_id, 'approval should have attached a lead')
        sv = SiteVisit.objects.get(lead_id=b.lead_id)
        self.assertEqual(sv.status, 'completed')
        self.assertEqual(sv.visited_at.date(), self.bdate, 'visit must carry the booking date')
        self.assertEqual(sv.stm_id, self.stm.id, 'visit must be credited to the booking STM')
        self.assertEqual(sv.project_id, self.proj.id)

    def test_the_created_lead_carries_the_client_and_the_stm(self):
        b = self._approve(self._booking(client_name='Meena', phone='9876500002'))
        lead = Lead.objects.get(pk=b.lead_id)
        self.assertEqual(lead.name, 'Meena')
        self.assertEqual(lead.phone, '9876500002')
        self.assertEqual(lead.stm_id, self.stm.id)
        self.assertEqual(lead.company_id, self.co.id)

    # ── closure recorded from an existing lead ────────────────────────────────
    def test_existing_lead_with_no_visit_gains_one(self):
        lead = Lead.objects.create(company=self.co, name='Anil', phone='9876500003',
                                   status='new', stm=self.stm, telecaller=self.tc)
        b = self._approve(self._booking(lead=lead))
        sv = SiteVisit.objects.get(lead_id=lead.id)
        self.assertEqual(sv.visited_at.date(), self.bdate)
        self.assertEqual(sv.referred_by_telecaller_id, self.tc.id,
                         "the lead's telecaller should keep the credit")

    def test_a_visit_on_another_day_does_not_cover_this_booking(self):
        """A repeat buyer visited once per unit. An older visit belongs to the other
        sale, so this booking still gets its own — and the old one is untouched."""
        lead = Lead.objects.create(company=self.co, name='Sita', phone='9876500004',
                                   status='new', stm=self.stm)
        from django.utils import timezone
        original = SiteVisit.objects.create(
            lead=lead, project=self.proj, stm=self.stm, status='completed',
            visited_at=timezone.now() - timedelta(days=30), outcome='warm',
            remarks='the real visit')
        self._approve(self._booking(lead=lead))

        self.assertEqual(SiteVisit.objects.filter(lead=lead).count(), 2)
        original.refresh_from_db()
        self.assertEqual(original.remarks, 'the real visit')
        self.assertEqual(original.outcome, 'warm')
        fresh = SiteVisit.objects.filter(lead=lead).exclude(pk=original.pk).get()
        self.assertEqual(fresh.visited_at.date(), self.bdate)

    def test_a_visit_already_on_the_booking_date_is_not_duplicated(self):
        """Record Closure straight from today's visit: nothing to add."""
        lead = Lead.objects.create(company=self.co, name='Sunil', phone='9876500009',
                                   status='new', stm=self.stm)
        from django.utils import timezone
        from datetime import datetime, time as dt_time
        at = timezone.make_aware(datetime.combine(self.bdate, dt_time(9, 30)),
                                 timezone.get_current_timezone())
        original = SiteVisit.objects.create(
            lead=lead, project=self.proj, stm=self.stm, status='completed',
            visited_at=at, outcome='warm', remarks='the real visit')
        self._approve(self._booking(lead=lead))

        self.assertEqual(SiteVisit.objects.filter(lead=lead).count(), 1)
        original.refresh_from_db()
        self.assertEqual(original.remarks, 'the real visit')

    def test_two_bookings_on_different_days_get_a_visit_each(self):
        lead = Lead.objects.create(company=self.co, name='Repeat', phone='9876500010',
                                   status='new', stm=self.stm)
        self._approve(self._booking(lead=lead))
        second = self._booking(lead=lead)
        second.booking_date = self.bdate + timedelta(days=14)
        second.save(update_fields=['booking_date'])
        self._approve(second)

        dates = sorted(v.visited_at.date() for v in SiteVisit.objects.filter(lead=lead))
        self.assertEqual(dates, [self.bdate, self.bdate + timedelta(days=14)])

    def test_a_scheduled_visit_does_not_count_as_done(self):
        """A visit that was booked but never marked done leaves the sale unexplained,
        so a completed one is still recorded."""
        lead = Lead.objects.create(company=self.co, name='Kiran', phone='9876500005',
                                   status='new', stm=self.stm)
        from django.utils import timezone
        SiteVisit.objects.create(lead=lead, project=self.proj, stm=self.stm,
                                 status='scheduled', scheduled_at=timezone.now())
        self._approve(self._booking(lead=lead))
        self.assertEqual(SiteVisit.objects.filter(lead=lead, status='completed').count(), 1)

    # ── guards ────────────────────────────────────────────────────────────────
    def test_rejection_creates_nothing(self):
        b = self._booking()
        auth(self.client, self.admin)
        self.client.post('/api/sales/bookings/%d/action/' % b.pk, {'action': 'reject'}, format='json')
        self.assertEqual(SiteVisit.objects.count(), 0)
        self.assertEqual(Lead.objects.count(), 0)

    def test_approving_twice_does_not_double_up(self):
        b = self._approve(self._booking())
        self._approve(b)
        self.assertEqual(SiteVisit.objects.filter(lead_id=b.lead_id).count(), 1)
        self.assertEqual(Closure.objects.filter(company=self.co).count(), 1)

    def test_a_booking_with_no_date_is_skipped_not_crashed(self):
        b = Booking.objects.create(company=self.co, project=self.proj, stm=self.stm,
                                   status='pending', client_name='NoDate', phone='9876500006',
                                   area='9')
        b = self._approve(b)
        self.assertEqual(b.status, 'sold', 'approval must still succeed')
        self.assertEqual(SiteVisit.objects.count(), 0)

    def test_the_visit_never_lands_in_another_company(self):
        b = self._approve(self._booking())
        sv = SiteVisit.objects.get(lead_id=b.lead_id)
        self.assertEqual(sv.lead.company_id, self.co.id)

    # ── matching an existing client by number ────────────────────────────────
    def test_a_booking_reuses_the_lead_that_already_has_that_number(self):
        """The same client is one person. Booking without picking their lead must
        attach to it, not create a second record of them."""
        existing = Lead.objects.create(company=self.co, name='Ramesh K', phone='9876500001',
                                       status='contacted', telecaller=self.tc)
        b = self._approve(self._booking(client_name='Ramesh', phone='9876500001'))

        self.assertEqual(b.lead_id, existing.id, 'should have matched the existing lead')
        self.assertEqual(Lead.objects.filter(company=self.co).count(), 1, 'no duplicate lead')
        existing.refresh_from_db()
        self.assertEqual(existing.name, 'Ramesh K', 'existing history must not be overwritten')
        self.assertEqual(existing.stm_id, self.stm.id, 'an unowned lead picks up the booking STM')
        sv = SiteVisit.objects.get(lead_id=existing.id)
        self.assertEqual(sv.referred_by_telecaller_id, self.tc.id)

    def test_matching_ignores_country_code_and_spacing(self):
        existing = Lead.objects.create(company=self.co, name='Meena', phone='+91 98765 00002',
                                       status='new')
        b = self._approve(self._booking(client_name='Meena', phone='9876500002'))
        self.assertEqual(b.lead_id, existing.id)
        self.assertEqual(Lead.objects.filter(company=self.co).count(), 1)

    def test_a_number_from_another_company_is_never_matched(self):
        other = Company.objects.create(code='OTH', name='Other Co')
        Lead.objects.create(company=other, name='Theirs', phone='9876500001', status='new')
        b = self._approve(self._booking(client_name='Ramesh', phone='9876500001'))
        self.assertEqual(Lead.objects.filter(company=other).count(), 1)
        self.assertEqual(Lead.objects.get(pk=b.lead_id).company_id, self.co.id)

    def test_an_owned_lead_keeps_its_stm(self):
        owner = User.objects.create(email='own@x.com', company=self.co, role='Sales',
                                    user_code='S2', designation='STM')
        existing = Lead.objects.create(company=self.co, name='Anil', phone='9876500003',
                                       status='warm', stm=owner)
        self._approve(self._booking(client_name='Anil', phone='9876500003'))
        existing.refresh_from_db()
        self.assertEqual(existing.stm_id, owner.id, 'must not steal an assigned lead')
