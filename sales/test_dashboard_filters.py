"""The Channel Partner dashboard narrows to a date range, project, partner or person.

Every tile, the funnel and the recent-leads table narrow together. A dashboard
whose headline and the list beneath it answer different questions is worse than
one with no filters at all.
"""
from datetime import date, timedelta

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from companies.models import Company
from sales.models import ChannelPartner, Closure, Lead, LeadSource, Project, SiteVisit


class CpDashboardFilters(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='DSH', name='Dash Co', is_active=True)
        cls.admin = User.objects.create_user(
            'admin@dsh.com', company=cls.co, user_code='DSH001', password='p',
            name='Admin', role='Admin', modules=['Sales', 'Channel Partner'])
        cls.exec_a = User.objects.create_user(
            'a@dsh.com', company=cls.co, user_code='DSH002', password='p',
            name='Exec A', role='Employee', modules=['Channel Partner'],
            reporting_manager=cls.admin)
        cls.exec_b = User.objects.create_user(
            'b@dsh.com', company=cls.co, user_code='DSH003', password='p',
            name='Exec B', role='Employee', modules=['Channel Partner'],
            reporting_manager=cls.admin)

        cls.p1 = Project.objects.create(company=cls.co, name='Tower One')
        cls.p2 = Project.objects.create(company=cls.co, name='Tower Two')
        cls.cp1 = ChannelPartner.objects.create(company=cls.co, name='Partner One',
                                                contact_no='+919800001111')
        cls.cp2 = ChannelPartner.objects.create(company=cls.co, name='Partner Two',
                                                contact_no='+919800002222')
        cls.src = LeadSource.objects.create(company=cls.co, name='Channel Partner')

        def lead(name, project, partner, owner, when=None):
            l = Lead.objects.create(company=cls.co, name=name, phone='+91980000' + str(abs(hash(name)) % 10000).zfill(4),
                                    project=project, source=cls.src, channel_partner=partner,
                                    stm=owner, status='new')
            if when:
                Lead.objects.filter(pk=l.pk).update(created_at=when)
            return l

        today = date.today()
        cls.l1 = lead('One', cls.p1, cls.cp1, cls.exec_a)
        cls.l2 = lead('Two', cls.p2, cls.cp1, cls.exec_a)
        cls.l3 = lead('Three', cls.p1, cls.cp2, cls.exec_b)
        # Comfortably outside any "this month" style window used below.
        cls.old = lead('Old', cls.p1, cls.cp1, cls.exec_a,
                       when=f'{today.year - 1}-01-15T10:00:00Z')

    def setUp(self):
        self.api = APIClient()
        cache.clear()
        self.api.force_authenticate(user=self.admin)

    def _total(self, qs=''):
        r = self.api.get(f'/api/sales/stats/?cp_only=true{qs}')
        self.assertEqual(r.status_code, 200, r.data)
        return r.data['total_leads']

    def test_unfiltered_counts_every_partner_lead(self):
        self.assertEqual(self._total(), 4)

    def test_filtering_by_project(self):
        self.assertEqual(self._total(f'&project_id={self.p1.id}'), 3)
        self.assertEqual(self._total(f'&project_id={self.p2.id}'), 1)

    def test_filtering_by_channel_partner(self):
        self.assertEqual(self._total(f'&channel_partner_id={self.cp1.id}'), 3)
        self.assertEqual(self._total(f'&channel_partner_id={self.cp2.id}'), 1)

    def test_filtering_by_person(self):
        self.assertEqual(self._total(f'&person_id={self.exec_a.id}'), 3)
        self.assertEqual(self._total(f'&person_id={self.exec_b.id}'), 1)

    def test_filtering_by_date_range(self):
        today = date.today().isoformat()
        week_ago = (date.today() - timedelta(days=7)).isoformat()
        self.assertEqual(self._total(f'&date_from={week_ago}&date_to={today}'), 3,
                         'the year-old lead should be out of a one-week window')

    def test_filters_combine(self):
        self.assertEqual(
            self._total(f'&project_id={self.p1.id}&channel_partner_id={self.cp1.id}'), 2)
        self.assertEqual(
            self._total(f'&project_id={self.p2.id}&channel_partner_id={self.cp2.id}'), 0)

    def test_the_recent_leads_list_narrows_with_the_tiles(self):
        """The headline and the list beneath it must answer the same question."""
        r = self.api.get(f'/api/sales/stats/?cp_only=true&project_id={self.p2.id}')
        names = {l['name'] for l in r.data['recent_leads']}
        self.assertEqual(names, {'Two'})

    def test_a_filtered_view_is_not_served_the_unfiltered_cache(self):
        """The cache key has to know about the filters, or the first unfiltered
        load poisons every filtered one for 20 seconds."""
        self.assertEqual(self._total(), 4)
        self.assertEqual(self._total(f'&project_id={self.p2.id}'), 1)
        self.assertEqual(self._total(), 4)

    def test_a_nonsense_filter_value_is_ignored_not_crashed_on(self):
        r = self.api.get('/api/sales/stats/?cp_only=true&project_id=abc&person_id=%20')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['total_leads'], 4)


class TheCpPersonPickerListsOnlyCpPeople(TestCase):
    """The partner desk's person filter must not become an STM list.

    crm_role='cp' falls back to EVERY Sales user when a company has no CP
    designations, and 'cp_module' also pulls in admins plus any STM who happens
    to hold a CP lead after a hand-off. On VRL that second one listed 13 people,
    11 of them STMs. 'cp_only' asks the narrow question instead.
    """

    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='CPO', name='CP Only Co', is_active=True)
        cls.admin = User.objects.create_user(
            'admin@cpo.com', company=cls.co, user_code='CPO001', password='p',
            name='Admin', role='Admin', modules=['Sales', 'Channel Partner'])
        cls.cp_exec = User.objects.create_user(
            'cpe@cpo.com', company=cls.co, user_code='CPO002', password='p',
            name='CP Exec', role='Employee', designation='CP Executive',
            modules=['Channel Partner'], reporting_manager=cls.admin)
        cls.cp_head = User.objects.create_user(
            'cph@cpo.com', company=cls.co, user_code='CPO003', password='p',
            name='CP Cluster Head', role='Manager', designation='CP Cluster Head',
            modules=['Channel Partner'], reporting_manager=cls.admin)
        cls.stm = User.objects.create_user(
            'stm@cpo.com', company=cls.co, user_code='CPO004', password='p',
            name='Plain STM', role='Employee', designation='STM',
            modules=['Sales'], reporting_manager=cls.admin)

    def setUp(self):
        self.api = APIClient()
        cache.clear()
        self.api.force_authenticate(user=self.admin)

    def _names(self, role):
        r = self.api.get(f'/api/sales/users/telecallers/?crm_role={role}')
        self.assertEqual(r.status_code, 200, r.data)
        rows = r.data['results'] if isinstance(r.data, dict) else r.data
        return {u['name'] for u in rows}

    def test_it_lists_cp_executives_and_cp_managers(self):
        names = self._names('cp_only')
        self.assertIn('CP Exec', names)
        self.assertIn('CP Cluster Head', names)

    def test_it_does_not_list_an_stm(self):
        self.assertNotIn('Plain STM', self._names('cp_only'))

    def test_it_does_not_list_an_admin(self):
        """cp_module does; that is the difference."""
        self.assertNotIn('Admin', self._names('cp_only'))

    def test_an_stm_holding_a_cp_lead_is_still_not_listed(self):
        """The hand-off case cp_module deliberately includes — and this one does
        not, because holding one partner lead does not make somebody the partner
        desk."""
        src = LeadSource.objects.create(company=self.co, name='Channel Partner')
        cp = ChannelPartner.objects.create(company=self.co, name='A Partner',
                                           contact_no='+919800009999')
        Lead.objects.create(company=self.co, name='Handed Off', phone='+919800008888',
                            source=src, channel_partner=cp, stm=self.stm)
        self.assertNotIn('Plain STM', self._names('cp_only'))
        self.assertIn('Plain STM', self._names('cp_module'),
                      'cp_module should still include them — that is its job')
