"""The SV Scheduled tile counts the visits its Scheduled tab lists.

It used to count leads whose status had ever moved to "SV scheduled", so a visit
since done, no-showed or cancelled still counted: an STM's tile said 19 while the
tab it opens held 1.
"""
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from companies.models import Company
from sales.models import Lead, LeadStatusHistory, SiteVisit


class SvScheduledTileTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.co = Company.objects.create(code='SVT', name='SV Co')
        self.stm = User.objects.create(email='stm@svt.com', company=self.co, role='Employee',
                                       designation='STM', modules=['Sales'], user_code='S1')
        now = timezone.now()
        for status in ('scheduled', 'completed', 'cancelled', 'no_show'):
            lead = Lead.objects.create(company=self.co, name=f'L {status}', phone=f'90000{len(status):05d}',
                                       stm=self.stm, stm_status='sv_scheduled')
            # Every one of these was "SV scheduled" at some point.
            LeadStatusHistory.objects.create(lead=lead, changed_by=self.stm, field_changed='stm_status',
                                             old_value='hot', new_value='sv_scheduled')
            SiteVisit.objects.create(lead=lead, stm=self.stm, status=status,
                                     scheduled_at=now + timedelta(days=1),
                                     visited_at=now if status == 'completed' else None)
        self.client.force_authenticate(self.stm)

    def test_tile_matches_the_scheduled_tab(self):
        tile = self.client.get('/api/sales/stats/').data['stm_sv_scheduled_count']
        listed = self.client.get('/api/sales/site-visits/?status=scheduled&counts_only=true').data
        self.assertEqual(tile, 1, 'only the visit still waiting counts')
        self.assertEqual(tile, listed.get('scheduled', 0))

    def test_undated_hot_tile_counts_who_is_hot_now(self):
        # One lead is hot now; another was hot once and has since gone cold.
        now_hot = Lead.objects.create(company=self.co, name='Hot now', phone='9100000001',
                                      stm=self.stm, stm_status='hot')
        was_hot = Lead.objects.create(company=self.co, name='Was hot', phone='9100000002',
                                      stm=self.stm, stm_status='cold')
        for lead in (now_hot, was_hot):
            LeadStatusHistory.objects.create(lead=lead, changed_by=self.stm, field_changed='stm_status',
                                             old_value='', new_value='hot')
        cache.clear()
        tile = self.client.get('/api/sales/stats/').data['stm_hot_count']
        listed = self.client.get('/api/sales/leads/?stm_status=hot&work=called').data
        n = listed.get('count') if isinstance(listed, dict) else len(listed)
        self.assertEqual(tile, 1)
        self.assertEqual(tile, n)

    def test_undated_telecaller_warm_tile_counts_who_is_warm_now(self):
        tc = User.objects.create(email='tc@svt.com', company=self.co, role='Employee',
                                 designation='Telecaller', modules=['Sales'], user_code='T1')
        now_warm = Lead.objects.create(company=self.co, name='Warm now', phone='9200000001',
                                       telecaller=tc, telecaller_status='warm')
        was_warm = Lead.objects.create(company=self.co, name='Was warm', phone='9200000002',
                                       telecaller=tc, telecaller_status='cold')
        for lead in (now_warm, was_warm):
            LeadStatusHistory.objects.create(lead=lead, changed_by=tc, field_changed='telecaller_status',
                                             old_value='', new_value='warm')
        cache.clear()
        self.client.force_authenticate(tc)
        tile = self.client.get('/api/sales/stats/').data['warm_count']
        listed = self.client.get('/api/sales/leads/?telecaller_status=warm&work=called').data
        n = listed.get('count') if isinstance(listed, dict) else len(listed)
        self.assertEqual(tile, 1)
        self.assertEqual(tile, n)

    def test_trend_charts_load_for_every_role(self):
        # The chart endpoint referenced cp_only without defining it, so it 500'd for
        # everyone and every Sales dashboard's charts sat empty.
        cp = User.objects.create(email='cp@svt.com', company=self.co, role='Employee',
                                 designation='CP Executive', modules=['Channel Partner'], user_code='C1')
        tc = User.objects.create(email='tc2@svt.com', company=self.co, role='Employee',
                                 designation='Telecaller', modules=['Sales'], user_code='T2')
        for who in (self.stm, cp, tc):
            self.client.force_authenticate(who)
            r = self.client.get('/api/sales/stats/trend/')
            self.assertEqual(r.status_code, 200, who.designation)
            self.assertIn('mql', r.data)
