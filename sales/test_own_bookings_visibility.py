"""Being an approver must not hide your own bookings from you.

Approver scoping answers "what do I review". That is a different question from
"what have I sold", and collapsing the two hid a CP Cluster Head's own bookings
from their own module: of Kunal's 107, only 56 survived, because the rest sat in
projects he does not approve or were not Channel-Partner-sourced.

The `mine=1` exemption already in the view was the same intent, applied only when
the caller happened to ask for it — every other screen got the narrowed list.
"""
from django.core.cache import cache
from rest_framework.test import APITestCase

from companies.models import Company
from accounts.models import User
from sales.models import Booking, Project

from sales.tests import auth


class OwnBookingsSurviveApproverScopingTests(APITestCase):
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

    def _names(self, url='/api/sales/bookings/?status=sold&cp_only=true'):
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

    def test_the_cp_pool_i_approve_is_still_listed(self):
        self.assertIn('Others CP Here', self._names())

    def test_someone_elses_work_outside_my_remit_is_not(self):
        """The exemption is stm=self — it must not widen into a company-wide list."""
        self.assertNotIn('Others Elsewhere', self._names())

    def test_mine_still_returns_only_mine(self):
        self.assertEqual(
            self._names('/api/sales/bookings/?status=sold&mine=1'),
            ['Own CP Here', 'Own Elsewhere', 'Own Walk-in Here'])

    def test_a_reportees_booking_is_listed_whatever_its_source(self):
        """A CP manager sees what the people reporting to them have sold, not only
        what came through a channel partner."""
        self.assertIn('Reportee Walk-in', self._names())

    def test_a_reportees_work_is_not_returned_by_mine(self):
        """`mine` means mine — the team rule widens the module, not that query."""
        self.assertNotIn('Reportee Walk-in',
                         self._names('/api/sales/bookings/?status=sold&mine=1'))
