"""Signing in late costs you the backlog, not your place in the rotation.

Distribution ranks by how many leads someone already holds today, so a person
arriving at 2pm on zero outranked colleagues sitting on 50 and swept the next 50
leads by themselves — the rotation "filling their bucket" to make the day's totals
match. Being there on time is supposed to be worth something, so a late arrival now
joins level: everyone gets one each from then on.
"""
from datetime import date, timedelta

from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APITestCase

from companies.models import Company
from accounts.models import User
from sales.models import (DistributionSettings, Lead, LeadSource, Project,
                          UserAvailability, UserProjectAssignment)
from sales.views import _distribution_credit_for, _run_distribution

from sales.tests import auth


class LateSignInTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='LTE', name='Late Co')
        DistributionSettings.objects.create(
            company=cls.co, tc_signin_time='10:10', tc_signout_time='23:59',
            stm_signin_time='10:20', stm_signout_time='23:59')
        cls.project = Project.objects.create(company=cls.co, name='Kalrav')
        cls.source = LeadSource.objects.create(company=cls.co, name='Meta')
        cls.a, cls.b, cls.c = [
            User.objects.create(email=f'lt_{n}@x.com', company=cls.co, role='STM',
                                designation='STM', user_code=f'L{n}')
            for n in ('a', 'b', 'c')]
        for u in (cls.a, cls.b, cls.c):
            UserProjectAssignment.objects.create(user=u, project=cls.project)

    def setUp(self):
        cache.clear()

    def _available(self, user, credit=0):
        return UserAvailability.objects.update_or_create(
            user=user, date=str(date.today()),
            defaults={'is_available': True, 'checked_in_at': timezone.now(),
                      'distribution_credit': credit})[0]

    def _leads(self, n):
        for i in range(n):
            Lead.objects.create(company=self.co, project=self.project, source=self.source,
                                status='new', name=f'L{i}', phone=f'90000{i:05d}')

    def _counts(self):
        return {u.id: Lead.objects.filter(stm=u).count() for u in (self.a, self.b, self.c)}

    def test_two_on_time_split_the_backlog(self):
        self._available(self.a); self._available(self.b)
        self._leads(100)
        _run_distribution(self.co, 'stm', gate='signout')
        c = self._counts()
        self.assertEqual(c[self.a.id], 50)
        self.assertEqual(c[self.b.id], 50)
        self.assertEqual(c[self.c.id], 0)

    def test_a_latecomer_does_not_get_the_backlog_back_filled(self):
        """The scenario as described: after 100 leads are split 50/50, a third person
        signs in and three leads arrive — they go one each, not three to the newcomer."""
        self._available(self.a); self._available(self.b)
        self._leads(100)
        _run_distribution(self.co, 'stm', gate='signout')

        # arrives late, so is credited with the level of the room (50)
        self._available(self.c, credit=50)
        self._leads(3)
        _run_distribution(self.co, 'stm', gate='signout')

        # one each: the two who were here take their 51st, the newcomer their 1st.
        c = self._counts()
        self.assertEqual(c[self.a.id], 51)
        self.assertEqual(c[self.b.id], 51)
        self.assertEqual(c[self.c.id], 1)

    def test_without_the_credit_the_latecomer_would_take_the_lot(self):
        """What the old behaviour did, kept as the contrast the credit exists to stop."""
        self._available(self.a); self._available(self.b)
        self._leads(100)
        _run_distribution(self.co, 'stm', gate='signout')

        self._available(self.c, credit=0)      # no handicap
        self._leads(3)
        _run_distribution(self.co, 'stm', gate='signout')

        self.assertEqual(self._counts()[self.c.id], 3)

    def test_credit_is_the_lowest_of_the_room_not_the_highest(self):
        """Late should cost the backlog, not more than it — a latecomer must not land
        behind the least-loaded person already working."""
        self._available(self.a); self._available(self.b)
        self._leads(100)
        _run_distribution(self.co, 'stm', gate='signout')
        # skew the room: give A one extra
        Lead.objects.filter(stm=self.b).first().delete()
        credit = _distribution_credit_for(self.c, self.co, 'stm')
        self.assertEqual(credit, min(Lead.objects.filter(stm=self.a).count(),
                                     Lead.objects.filter(stm=self.b).count()))

    def test_on_time_earns_no_credit(self):
        early = timezone.now().replace(hour=0, minute=1)
        self.assertEqual(_distribution_credit_for(self.c, self.co, 'stm', when=early), 0)

    def test_the_first_person_in_is_never_penalised(self):
        """Late with an empty room means there is no backlog to have missed."""
        late = timezone.now().replace(hour=23, minute=0)
        self.assertEqual(_distribution_credit_for(self.a, self.co, 'stm', when=late), 0)

    def test_peers_on_other_projects_do_not_set_the_level(self):
        other = Project.objects.create(company=self.co, name='Elsewhere')
        loner = User.objects.create(email='lt_x@x.com', company=self.co, role='STM',
                                    designation='STM', user_code='LX')
        UserProjectAssignment.objects.create(user=loner, project=other)
        self._available(loner)
        for i in range(30):
            Lead.objects.create(company=self.co, project=other, source=self.source,
                                status='new', name=f'O{i}', phone=f'91000{i:05d}',
                                stm=loner, stm_assigned_at=timezone.now())
        late = timezone.now().replace(hour=23, minute=0)
        # nobody on the newcomer's own project is signed in, so there is no level to join
        self.assertEqual(_distribution_credit_for(self.c, self.co, 'stm', when=late), 0)


class SignInAwardsCreditTests(APITestCase):
    """The credit must be set by actually signing in, not only by the helper — both
    the admin toggle and a rep's own switch go through _mark_available."""

    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='LT2', name='Late Co 2')
        DistributionSettings.objects.create(
            company=cls.co, tc_signin_time='10:10', tc_signout_time='23:59',
            stm_signin_time='00:01', stm_signout_time='23:59')   # everyone is late
        cls.project = Project.objects.create(company=cls.co, name='Kalrav')
        cls.source = LeadSource.objects.create(company=cls.co, name='Meta')
        cls.admin = User.objects.create(email='lt2_admin@x.com', company=cls.co,
                                        role='Admin', is_staff=True, user_code='T0')
        cls.early = User.objects.create(email='lt2_a@x.com', company=cls.co, role='STM',
                                        designation='STM', user_code='T1')
        cls.late = User.objects.create(email='lt2_b@x.com', company=cls.co, role='STM',
                                       designation='STM', user_code='T2')
        for u in (cls.early, cls.late):
            UserProjectAssignment.objects.create(user=u, project=cls.project)

    def setUp(self):
        cache.clear()

    def test_signing_in_after_the_deadline_records_the_room_level(self):
        UserAvailability.objects.create(
            user=self.early, date=str(date.today()), is_available=True,
            checked_in_at=timezone.now(), distribution_credit=0)
        for i in range(7):
            Lead.objects.create(company=self.co, project=self.project, source=self.source,
                                status='new', name=f'X{i}', phone=f'93000{i:05d}',
                                stm=self.early, stm_assigned_at=timezone.now())

        auth(self.client, self.admin)
        r = self.client.post('/api/sales/availability/',
                             {'user_id': self.late.id, 'is_available': True}, format='json')
        self.assertEqual(r.status_code, 200)
        row = UserAvailability.objects.get(user=self.late, date=str(date.today()))
        self.assertEqual(row.distribution_credit, 7)

    def test_signing_out_clears_the_credit(self):
        auth(self.client, self.admin)
        self.client.post('/api/sales/availability/',
                         {'user_id': self.late.id, 'is_available': True}, format='json')
        self.client.post('/api/sales/availability/',
                         {'user_id': self.late.id, 'is_available': False}, format='json')
        row = UserAvailability.objects.get(user=self.late, date=str(date.today()))
        self.assertFalse(row.is_available)
        self.assertEqual(row.distribution_credit, 0)


class LateSignInIsReportedTests(APITestCase):
    """The availability widgets tell the person they signed in late, so a count that
    trails the room's reads as expected rather than as distribution being broken."""

    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='LT3', name='Late Co 3')
        DistributionSettings.objects.create(
            company=cls.co, tc_signin_time='10:10', tc_signout_time='23:59',
            stm_signin_time='00:01', stm_signout_time='23:59')   # everyone is late
        cls.project = Project.objects.create(company=cls.co, name='Kalrav')
        cls.source = LeadSource.objects.create(company=cls.co, name='Meta')
        cls.early = User.objects.create(email='lt3_a@x.com', company=cls.co, role='STM',
                                        designation='STM', user_code='U1')
        cls.late = User.objects.create(email='lt3_b@x.com', company=cls.co, role='STM',
                                       designation='STM', user_code='U2')
        for u in (cls.early, cls.late):
            UserProjectAssignment.objects.create(user=u, project=cls.project)

    def setUp(self):
        cache.clear()

    def test_a_late_sign_in_is_reported_back(self):
        UserAvailability.objects.create(
            user=self.early, date=str(date.today()), is_available=True,
            checked_in_at=timezone.now(), distribution_credit=0)
        for i in range(4):
            Lead.objects.create(company=self.co, project=self.project, source=self.source,
                                status='new', name=f'Y{i}', phone=f'94000{i:05d}',
                                stm=self.early, stm_assigned_at=timezone.now())
        auth(self.client, self.late)
        r = self.client.post('/api/sales/availability/me/', {'is_available': True}, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data['signed_in_late'])
        self.assertEqual(r.data['missed_leads'], 4)
        self.assertEqual(r.data['signin_time'], '00:01')

    def test_it_still_says_so_when_asked_later(self):
        """Reads the recorded handicap, not the clock — so it describes this sign-in."""
        auth(self.client, self.late)
        self.client.post('/api/sales/availability/me/', {'is_available': True}, format='json')
        r = self.client.get('/api/sales/availability/me/')
        self.assertEqual(r.data['signed_in_late'], r.data['missed_leads'] > 0)

    def test_the_first_person_in_is_not_told_they_are_late(self):
        """Late with an empty room costs nothing, so there is nothing to report."""
        auth(self.client, self.late)
        r = self.client.post('/api/sales/availability/me/', {'is_available': True}, format='json')
        self.assertFalse(r.data['signed_in_late'])
        self.assertEqual(r.data['missed_leads'], 0)

    def test_nothing_is_claimed_when_signed_out(self):
        auth(self.client, self.late)
        self.client.post('/api/sales/availability/me/', {'is_available': True}, format='json')
        self.client.post('/api/sales/availability/me/', {'is_available': False}, format='json')
        r = self.client.get('/api/sales/availability/me/')
        self.assertFalse(r.data['signed_in_late'])
