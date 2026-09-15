"""What "My Bookings" shows, in each module.

Two screens ask two different questions, and conflating them is what made this
drift. My Bookings is "what I and my people have sold" — own work plus the
reporting tree, and in the CP module the partner-sourced pool as well. Approvals
is "what am I named to decide", and is covered by test_can_approve_flag.

A CP Cluster Head's own bookings had been vanishing from his own module: of
Kunal's 107, only 56 survived, the rest sitting in projects he does not approve or
simply not being Channel-Partner-sourced.
"""
from django.core.cache import cache
from rest_framework.test import APITestCase

from companies.models import Company
from accounts.models import User
from sales.models import Booking, Lead, LeadSource, Project

from sales.tests import auth


class MyBookingsScopeTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='OWN', name='Own Co')
        cls.cp = User.objects.create(email='own_cp@x.com', company=cls.co, role='Manager',
                                     designation='CP CLUSTER HEAD', user_code='O1')
        cls.other = User.objects.create(email='own_other@x.com', company=cls.co, role='STM',
                                        designation='STM', user_code='O2')
        # Reports to the CP manager, so their work is his to see as well.
        cls.reportee = User.objects.create(email='own_rep@x.com', company=cls.co,
                                           role='STM', designation='CP EXECUTIVE',
                                           user_code='O3', reporting_manager=cls.cp)
        # He approves this one...
        cls.approved_project = Project.objects.create(
            company=cls.co, name='Mine To Approve', cp_booking_approvers=[cls.cp.id])
        # ...but not this one, where he has nonetheless sold units himself.
        cls.elsewhere = Project.objects.create(company=cls.co, name='Not Mine')

        mk = lambda **kw: Booking.objects.create(company=cls.co, status='sold', **kw)
        cls.own_cp_here = mk(project=cls.approved_project, stm=cls.cp,
                             source='Channel Partner', client_name='Own CP Here',
                             phone='9000000040')
        cls.own_walkin_here = mk(project=cls.approved_project, stm=cls.cp,
                                 source='walk-in', client_name='Own Walk-in Here',
                                 phone='9000000041')
        cls.own_elsewhere = mk(project=cls.elsewhere, stm=cls.cp, source='walk-in',
                               client_name='Own Elsewhere', phone='9000000042')
        cls.others_cp_here = mk(project=cls.approved_project, stm=cls.other,
                                source='Channel Partner', client_name='Others CP Here',
                                phone='9000000043')
        cls.others_elsewhere = mk(project=cls.elsewhere, stm=cls.other, source='walk-in',
                                  client_name='Others Elsewhere', phone='9000000044')
        cls.reportee_walkin = mk(project=cls.elsewhere, stm=cls.reportee, source='walk-in',
                                 client_name='Reportee Walk-in', phone='9000000045')

    def setUp(self):
        cache.clear()
        auth(self.client, self.cp)

    def _names(self, url='/api/sales/bookings/?status=sold&mine=1&cp_only=true'):
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        return sorted(b['client_name'] for b in r.data)

    def test_every_booking_of_mine_is_listed(self):
        """Whatever its project and whatever its Source."""
        names = self._names()
        for n in ('Own CP Here', 'Own Walk-in Here', 'Own Elsewhere'):
            self.assertIn(n, names)

    def test_a_walk_in_i_sold_in_a_project_i_do_not_approve_still_shows(self):
        """The exact shape of the ones that went missing."""
        self.assertIn('Own Elsewhere', self._names())

    def test_the_cp_pool_is_listed_in_the_cp_module(self):
        """The module tracks partner business, so the pool belongs here too."""
        self.assertIn('Others CP Here', self._names())

    def test_the_cp_pool_is_not_listed_in_sales(self):
        """Same screen in Sales is own work and the team's, nothing more."""
        self.assertNotIn('Others CP Here',
                         self._names('/api/sales/bookings/?status=sold&mine=1'))

    def test_someone_elses_work_outside_my_remit_is_not(self):
        """The exemption is stm=self — it must not widen into a company-wide list."""
        self.assertNotIn('Others Elsewhere', self._names())

    def test_sales_my_bookings_is_own_work_and_the_team(self):
        self.assertEqual(
            self._names('/api/sales/bookings/?status=sold&mine=1'),
            ['Own CP Here', 'Own Elsewhere', 'Own Walk-in Here', 'Reportee Walk-in'])

    def test_a_reportees_booking_is_listed_whatever_its_source(self):
        """A CP manager sees what the people reporting to them have sold, not only
        what came through a channel partner."""
        self.assertIn('Reportee Walk-in', self._names())

    def test_someone_outside_the_tree_is_never_returned(self):
        """The rule is own work plus the reporting tree — not the whole company."""
        self.assertNotIn('Others Elsewhere',
                         self._names('/api/sales/bookings/?status=sold&mine=1'))


class NoDuplicateRowsTests(APITestCase):
    """A booking must appear once, however many of the visibility rules it satisfies.

    The rules are ORed and reach through `lead` into the CP directory and the source
    table, which is exactly the shape that starts returning a row per matching join.
    """

    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='DUP', name='Dup Co')
        cls.cp = User.objects.create(email='dup_cp@x.com', company=cls.co, role='Manager',
                                     designation='CP CLUSTER HEAD', user_code='D1')
        cls.reportee = User.objects.create(email='dup_rep@x.com', company=cls.co, role='STM',
                                           designation='CP EXECUTIVE', user_code='D2',
                                           reporting_manager=cls.cp)
        cls.project = Project.objects.create(company=cls.co, name='Tower',
                                             cp_booking_approvers=[cls.cp.id])
        # Satisfies every arm at once: his own, CP-sourced, in a project he approves.
        Booking.objects.create(company=cls.co, project=cls.project, stm=cls.cp,
                               status='sold', source='Channel Partner',
                               client_name='Every Rule', phone='9000000060')
        # Satisfies two: a reportee's, and CP-sourced.
        Booking.objects.create(company=cls.co, project=cls.project, stm=cls.reportee,
                               status='sold', source='Channel Partner',
                               client_name='Two Rules', phone='9000000061')

    def setUp(self):
        cache.clear()
        auth(self.client, self.cp)

    def _ids(self, url):
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        return [b['id'] for b in r.data]

    def test_my_bookings_lists_each_booking_once(self):
        ids = self._ids('/api/sales/bookings/?status=sold&mine=1&cp_only=true')
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), 2)

    def test_approvals_lists_each_booking_once(self):
        ids = self._ids('/api/sales/bookings/?status=sold&cp_only=true')
        self.assertEqual(len(ids), len(set(ids)))

    def test_sales_my_bookings_lists_each_booking_once(self):
        ids = self._ids('/api/sales/bookings/?status=sold&mine=1')
        self.assertEqual(len(ids), len(set(ids)))


class CpSourcedFlagTests(APITestCase):
    """`is_cp_sourced` on each booking — what the CP module's "Source: CP" filter reads.

    It has to come from the server. The rule is "the lead is CP-attributed OR the
    booking's own Source says so", and the lead half never reaches the client: a UI
    guessing from the fields it does have — Source plus the free-text cp_name —
    called 107 of a real cluster head's 133 bookings CP-sourced where the server
    counts 68, because a Reference deal can still name a partner in cp_name.
    """

    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='CPF', name='Flag Co')
        cls.user = User.objects.create(email='flag@x.com', company=cls.co, role='Manager',
                                       designation='CP CLUSTER HEAD', user_code='F1')
        cls.project = Project.objects.create(company=cls.co, name='Flag Tower')
        cp_source = LeadSource.objects.create(company=cls.co, name='Channel Partner')
        ref_source = LeadSource.objects.create(company=cls.co, name='Reference')

        # 1. Partner-sourced by the booking's own free-text Source.
        cls.by_source = Booking.objects.create(
            company=cls.co, project=cls.project, stm=cls.user, status='sold',
            source='Channel Partner', client_name='By Source', phone='9000000070')
        # 2. Partner-sourced through the lead, with the booking's Source saying
        #    something else entirely — the case a client-side guess cannot see.
        lead = Lead.objects.create(company=cls.co, name='Via Lead', phone='9000000071',
                                   source=cp_source, stm=cls.user)
        cls.by_lead = Booking.objects.create(
            company=cls.co, project=cls.project, stm=cls.user, status='sold', lead=lead,
            source='Reference', client_name='Via Lead', phone='9000000071')
        # 3. Not partner-sourced, but names a partner in the free-text cp_name. This
        #    is the row a client-side guess gets wrong.
        ref_lead = Lead.objects.create(company=cls.co, name='Plain Ref', phone='9000000072',
                                       source=ref_source, stm=cls.user)
        cls.plain = Booking.objects.create(
            company=cls.co, project=cls.project, stm=cls.user, status='sold', lead=ref_lead,
            source='Reference', cp_name='SOME PARTNER NAME',
            client_name='Plain Ref', phone='9000000072')

    def setUp(self):
        cache.clear()
        auth(self.client, self.user)

    def _flags(self):
        r = self.client.get('/api/sales/bookings/?status=sold&mine=1&cp_only=true')
        self.assertEqual(r.status_code, 200)
        return {b['id']: b['is_cp_sourced'] for b in r.data}

    def test_booking_source_marks_it_cp(self):
        self.assertIs(self._flags()[self.by_source.id], True)

    def test_lead_source_marks_it_cp_even_when_the_booking_says_otherwise(self):
        self.assertIs(self._flags()[self.by_lead.id], True)

    def test_a_named_partner_alone_does_not_make_it_cp(self):
        # cp_name is free text on a Reference deal — the money did not come through
        # the partner, and the filter must not claim it did.
        self.assertIs(self._flags()[self.plain.id], False)

    def test_the_flag_survives_without_the_list_annotation(self):
        # The detail endpoint doesn't annotate, so the serializer computes it. The
        # two paths have to agree or the filter and the opened booking contradict.
        listed = self._flags()
        for b in (self.by_source, self.by_lead, self.plain):
            r = self.client.get(f'/api/sales/bookings/{b.id}/')
            self.assertEqual(r.status_code, 200)
            self.assertIs(r.data['is_cp_sourced'], listed[b.id])
