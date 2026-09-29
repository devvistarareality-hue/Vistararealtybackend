"""A client the STM has already booked is not someone to ring.

An STM who books a walk-in straight from the booking form has no lead yet, so
one is created for them. It used to be created at status 'new' with an empty
stm_status — which is exactly the shape of "assigned to me, not yet called", so
the client landed in that STM's To Call queue and sat there until somebody got
round to approving the booking. Reported from production: a queue full of
people who had already bought.

The To Call / Called split is keyed off stm_status for an STM
(LeadListView: work=pending -> stm_status='', work=called -> exclude that), so
these tests assert on stm_status rather than on the tab.
"""
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from companies.models import Company
from sales.models import Booking, Lead, LeadSource, Plot, Project


class DirectBookingClosesItsLead(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='DBK', name='Direct Book Co', is_active=True)
        cls.admin = User.objects.create_user(
            'admin@dbk.com', company=cls.co, user_code='DBK001', password='p',
            name='Admin', role='Admin', modules=['Sales'])
        cls.stm = User.objects.create_user(
            'stm@dbk.com', company=cls.co, user_code='DBK002', password='p',
            name='STM', role='Employee', designation='STM', modules=['Sales'],
            reporting_manager=cls.admin)
        cls.project = Project.objects.create(company=cls.co, name='Kalrav 2')
        LeadSource.objects.create(company=cls.co, name='Walk-In')
        cls.plot = Plot.objects.create(project=cls.project, number='K-1')

    def setUp(self):
        self.api = APIClient()
        cache.clear()
        self.api.force_authenticate(user=self.stm)

    def _payload(self, name, **over):
        body = {
            'client_name': name, 'phone': '+919800000001', 'source': 'Walk-In',
            'project': self.project.id, 'plot': self.plot.id,
            'final_amount': '1000000', 'land_rate': '3500', 'area': '100',
        }
        body.update(over)
        return body

    def _lead_for(self, booking_id):
        return Lead.objects.get(id=Booking.objects.get(id=booking_id).lead_id)

    # ── a submitted booking ──────────────────────────────────────────────────
    def test_a_submitted_booking_closes_the_lead_it_created(self):
        r = self.api.post('/api/sales/bookings/', self._payload('Walk-in Buyer'), format='json')
        self.assertIn(r.status_code, (200, 201), r.data)
        lead = self._lead_for(r.data['id'])
        self.assertEqual(lead.stm_status, 'closed')
        self.assertEqual(lead.status, 'closed')

    def test_so_it_is_out_of_the_stms_to_call_queue(self):
        """The reported symptom, asserted through the endpoint the tab uses."""
        r = self.api.post('/api/sales/bookings/', self._payload('Sold Already'), format='json')
        lead_id = Booking.objects.get(id=r.data['id']).lead_id

        pending = self.api.get('/api/sales/leads/?work=pending')
        self.assertNotIn(lead_id, [l['id'] for l in pending.data['results']])

        called = self.api.get('/api/sales/leads/?work=called')
        self.assertIn(lead_id, [l['id'] for l in called.data['results']])

    def test_it_does_not_wait_for_approval(self):
        """The booking is still pending — the lead is closed regardless. That gap
        was the bug: approval could be days later, or never come."""
        r = self.api.post('/api/sales/bookings/', self._payload('Awaiting Nod'), format='json')
        self.assertEqual(Booking.objects.get(id=r.data['id']).status, 'pending')
        self.assertEqual(self._lead_for(r.data['id']).stm_status, 'closed')

    def test_the_lead_stays_with_the_stm_who_booked_it(self):
        r = self.api.post('/api/sales/bookings/', self._payload('Mine'), format='json')
        self.assertEqual(self._lead_for(r.data['id']).stm_id, self.stm.id)

    def test_an_existing_lead_is_closed_too_not_just_a_new_one(self):
        """Booking a lead that came through the pipeline closes it the same way."""
        lead = Lead.objects.create(company=self.co, name='From Pipeline',
                                   phone='+919800000077', project=self.project,
                                   stm=self.stm, status='sv_done', stm_status='sv_done')
        r = self.api.post('/api/sales/bookings/',
                          self._payload('From Pipeline', lead=lead.id), format='json')
        self.assertIn(r.status_code, (200, 201), r.data)
        lead.refresh_from_db()
        self.assertEqual(lead.stm_status, 'closed')

    # ── a draft ──────────────────────────────────────────────────────────────
    def test_a_draft_marks_the_lead_hot_not_closed(self):
        """A draft is not a booking, so 'closed' would be a lie — but nobody needs
        to ring a client the STM is writing up right now."""
        r = self.api.post('/api/sales/bookings/draft/',
                          self._payload('Half Typed'), format='json')
        self.assertIn(r.status_code, (200, 201), r.data)
        lead = Lead.objects.get(id=Booking.objects.get(id=r.data['id']).lead_id)
        self.assertEqual(lead.stm_status, 'hot')
        self.assertEqual(lead.status, 'hot')

    def test_a_draft_lead_is_also_out_of_the_to_call_queue(self):
        r = self.api.post('/api/sales/bookings/draft/',
                          self._payload('Half Typed Two'), format='json')
        lead_id = Booking.objects.get(id=r.data['id']).lead_id
        pending = self.api.get('/api/sales/leads/?work=pending')
        self.assertNotIn(lead_id, [l['id'] for l in pending.data['results']])

    def test_submitting_that_draft_then_closes_it(self):
        d = self.api.post('/api/sales/bookings/draft/',
                          self._payload('Draft Then Real'), format='json')
        lead_id = Booking.objects.get(id=d.data['id']).lead_id
        self.assertEqual(Lead.objects.get(id=lead_id).stm_status, 'hot')

        r = self.api.post('/api/sales/bookings/',
                          self._payload('Draft Then Real', lead=lead_id), format='json')
        self.assertIn(r.status_code, (200, 201), r.data)
        self.assertEqual(Lead.objects.get(id=lead_id).stm_status, 'closed')


class ARejectedSubmissionLeavesNoOrphan(TestCase):
    """The lead used to be written before the booking was validated.

    So a submission the server refused returned 400 with the lead already in the
    database — a client sitting in the STM's To Call queue with no booking, no
    site visit and no closure behind them. That is the exact shape of the leads
    reported from production. A revision had the same problem from the other
    end: it takes the prior booking's lead, so the one it had just minted was
    thrown away.
    """

    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='ORP', name='Orphan Co', is_active=True)
        cls.admin = User.objects.create_user(
            'admin@orp.com', company=cls.co, user_code='ORP001', password='p',
            name='Admin', role='Admin', modules=['Sales'])
        cls.stm = User.objects.create_user(
            'stm@orp.com', company=cls.co, user_code='ORP002', password='p',
            name='STM', role='Employee', designation='STM', modules=['Sales'],
            reporting_manager=cls.admin)
        cls.project = Project.objects.create(company=cls.co, name='Orphan Project')
        cls.plot = Plot.objects.create(project=cls.project, number='O-1')

    def setUp(self):
        self.api = APIClient()
        cache.clear()
        self.api.force_authenticate(user=self.stm)

    def test_a_refused_booking_writes_no_lead_at_all(self):
        before = Lead.objects.count()
        r = self.api.post('/api/sales/bookings/', {
            'client_name': 'Never Booked', 'phone': '+919800000055',
            'project': self.project.id,
            # No unit, on a project that has one mapped — refused by the server.
        }, format='json')
        self.assertEqual(r.status_code, 400, r.data)
        self.assertEqual(Lead.objects.count(), before,
                         'a refused booking left a lead behind')

    def test_a_lead_is_born_closed_not_closed_afterwards(self):
        """Belt and braces: even if the closing update below it were removed, the
        row never exists in the 'new'/'' state the To Call queue selects on."""
        r = self.api.post('/api/sales/bookings/', {
            'client_name': 'Born Closed', 'phone': '+919800000056',
            'project': self.project.id, 'plot': self.plot.id,
            'final_amount': '1000000', 'land_rate': '3500', 'area': '100',
        }, format='json')
        self.assertIn(r.status_code, (200, 201), r.data)
        lead = Lead.objects.get(id=Booking.objects.get(id=r.data['id']).lead_id)
        self.assertEqual((lead.status, lead.stm_status), ('closed', 'closed'))

    def test_a_revision_does_not_mint_a_second_lead(self):
        first = self.api.post('/api/sales/bookings/', {
            'client_name': 'Revised Client', 'phone': '+919800000057',
            'project': self.project.id, 'plot': self.plot.id,
            'final_amount': '1000000', 'land_rate': '3500', 'area': '100',
        }, format='json')
        self.assertIn(first.status_code, (200, 201), first.data)
        b = Booking.objects.get(id=first.data['id'])
        before = Lead.objects.count()

        rev = self.api.post('/api/sales/bookings/', {
            'client_name': 'Revised Client', 'phone': '+919800000057',
            'project': self.project.id, 'plot': self.plot.id,
            'final_amount': '1100000', 'land_rate': '3600', 'area': '100',
            'revision_of': b.id,
        }, format='json')
        self.assertIn(rev.status_code, (200, 201), rev.data)
        self.assertEqual(Lead.objects.count(), before,
                         'the revision minted a lead it then threw away')
        self.assertEqual(Booking.objects.get(id=rev.data['id']).lead_id, b.lead_id)


class ABookingReusesTheLeadAlreadyOnFile(TestCase):
    """Seven of one STM's clients had two leads each.

    Entered around 09:15 one morning as ordinary leads, booked the same
    afternoon through the booking form — which minted a second lead rather than
    matching the first. The booking closed its own copy; the original sat in the
    To Call queue at 'new' forever, which is what showed up as "these are already
    booked, why are they in my calling list".
    """

    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='DUP', name='Dup Co', is_active=True)
        cls.admin = User.objects.create_user(
            'admin@dup.com', company=cls.co, user_code='DUP001', password='p',
            name='Admin', role='Admin', modules=['Sales'])
        cls.stm = User.objects.create_user(
            'stm@dup.com', company=cls.co, user_code='DUP002', password='p',
            name='STM', role='Employee', designation='STM', modules=['Sales'],
            reporting_manager=cls.admin)
        cls.project = Project.objects.create(company=cls.co, name='Tower One')
        cls.other = Project.objects.create(company=cls.co, name='Tower Two')
        cls.plot = Plot.objects.create(project=cls.project, number='T-1')
        cls.plot2 = Plot.objects.create(project=cls.other, number='U-1')

    def setUp(self):
        self.api = APIClient()
        cache.clear()
        self.api.force_authenticate(user=self.stm)

    def _book(self, phone, project, plot, name='Viral Soni'):
        return self.api.post('/api/sales/bookings/', {
            'client_name': name, 'phone': phone, 'project': project.id, 'plot': plot.id,
            'final_amount': '1000000', 'land_rate': '3500', 'area': '100',
        }, format='json')

    def test_it_reuses_the_existing_lead_instead_of_making_a_second(self):
        first = Lead.objects.create(company=self.co, name='Viral Soni',
                                    phone='+919812345600', project=self.project,
                                    stm=self.stm, status='new')
        before = Lead.objects.count()

        r = self._book('+919812345600', self.project, self.plot)
        self.assertIn(r.status_code, (200, 201), r.data)
        self.assertEqual(Lead.objects.count(), before, 'a duplicate lead was created')
        self.assertEqual(Booking.objects.get(id=r.data['id']).lead_id, first.id)

    def test_and_that_lead_is_closed_so_it_leaves_the_calling_queue(self):
        first = Lead.objects.create(company=self.co, name='Viral Soni',
                                    phone='+919812345601', project=self.project,
                                    stm=self.stm, status='new')
        self._book('+919812345601', self.project, self.plot)
        first.refresh_from_db()
        self.assertEqual((first.status, first.stm_status), ('closed', 'closed'))

    def test_the_match_ignores_the_country_code(self):
        """The stored number and the typed one rarely agree on +91."""
        first = Lead.objects.create(company=self.co, name='Viral Soni',
                                    phone='9812345602', project=self.project,
                                    stm=self.stm, status='new')
        before = Lead.objects.count()
        r = self._book('+91 98123 45602', self.project, self.plot)
        self.assertIn(r.status_code, (200, 201), r.data)
        self.assertEqual(Lead.objects.count(), before)
        self.assertEqual(Booking.objects.get(id=r.data['id']).lead_id, first.id)

    def test_a_different_project_is_a_separate_inquiry_and_gets_its_own_lead(self):
        """Same person, different project, is a genuinely different deal — the
        same rule the Add Lead form applies."""
        Lead.objects.create(company=self.co, name='Viral Soni',
                            phone='+919812345603', project=self.project,
                            stm=self.stm, status='new')
        before = Lead.objects.count()
        r = self._book('+919812345603', self.other, self.plot2)
        self.assertIn(r.status_code, (200, 201), r.data)
        self.assertEqual(Lead.objects.count(), before + 1)

    def test_a_repeat_buyer_on_the_same_project_reuses_their_closed_lead(self):
        """A second unit is still the same client — minting a duplicate for them
        is the thing being fixed."""
        first = Lead.objects.create(company=self.co, name='Viral Soni',
                                    phone='+919812345604', project=self.project,
                                    stm=self.stm, status='closed', stm_status='closed')
        before = Lead.objects.count()
        plot_b = Plot.objects.create(project=self.project, number='T-2')
        r = self._book('+919812345604', self.project, plot_b)
        self.assertIn(r.status_code, (200, 201), r.data)
        self.assertEqual(Lead.objects.count(), before)
        self.assertEqual(Booking.objects.get(id=r.data['id']).lead_id, first.id)

    def test_a_draft_reuses_it_too_and_lifts_it_out_of_to_call(self):
        first = Lead.objects.create(company=self.co, name='Viral Soni',
                                    phone='+919812345605', project=self.project,
                                    stm=self.stm, status='new')
        before = Lead.objects.count()
        r = self.api.post('/api/sales/bookings/draft/', {
            'client_name': 'Viral Soni', 'phone': '+919812345605',
            'project': self.project.id, 'plot': self.plot.id,
        }, format='json')
        self.assertIn(r.status_code, (200, 201), r.data)
        self.assertEqual(Lead.objects.count(), before, 'the draft created a duplicate')
        first.refresh_from_db()
        self.assertEqual(first.stm_status, 'hot')

    def test_a_brand_new_client_still_gets_a_lead(self):
        before = Lead.objects.count()
        r = self._book('+919812345699', self.project, self.plot, name='Nobody Yet')
        self.assertIn(r.status_code, (200, 201), r.data)
        self.assertEqual(Lead.objects.count(), before + 1)
