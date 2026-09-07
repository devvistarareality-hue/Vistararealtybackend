"""The dashboard's "Unassigned" tile must count leads nobody owns.

`status='new'` is a pipeline stage, not an ownership check: a lead handed to a
telecaller keeps status='new' until it moves warm to an STM, and a lead an STM
self-sources is created with status='new' too. Counting status alone reported
those assigned leads as unassigned — a ~10x overcount on a real dataset.
"""
from django.core.cache import cache
from rest_framework.test import APITestCase

from companies.models import Company
from accounts.models import User
from sales.models import Lead, Project

from sales.tests import auth


class UnassignedCountTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='UNA', name='Unassigned Co')
        cls.admin = User.objects.create(
            email='una_admin@x.com', company=cls.co, role='Admin',
            is_staff=True, user_code='UA')
        cls.tc = User.objects.create(
            email='una_tc@x.com', company=cls.co, role='Telecaller',
            designation='TELECALLER', user_code='UT')
        cls.stm = User.objects.create(
            email='una_stm@x.com', company=cls.co, role='STM',
            designation='STM', user_code='US')
        cls.project = Project.objects.create(company=cls.co, name='Pratishtha')

        mk = lambda **kw: Lead.objects.create(
            company=cls.co, project=cls.project, **kw)
        # 2 genuinely unassigned
        mk(name='free one',  phone='9000000001', status='new')
        mk(name='free two',  phone='9000000002', status='new')
        # assigned, but still sitting at status='new' — the leads that used to be
        # miscounted
        mk(name='has tc',    phone='9000000003', status='new', telecaller=cls.tc)
        mk(name='has stm',   phone='9000000004', status='new', stm=cls.stm)
        mk(name='has both',  phone='9000000005', status='new',
           telecaller=cls.tc, stm=cls.stm)
        # not at 'new' at all
        mk(name='moved on',  phone='9000000006', status='warm_transferred')

    def setUp(self):
        # /stats caches per user id for 20s, and ids restart every test class —
        # without this a neighbouring class is served this one's payload.
        cache.clear()
        auth(self.client, self.admin)

    def tearDown(self):
        cache.clear()

    def test_tile_counts_only_leads_nobody_owns(self):
        r = self.client.get('/api/sales/stats/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['unassigned_leads'], 2)

    def test_new_leads_still_reports_the_pipeline_stage(self):
        """The old field stays put — other callers still read it."""
        r = self.client.get('/api/sales/stats/')
        self.assertEqual(r.data['new_leads'], 5)

    def test_drill_through_returns_the_same_rows_the_tile_counted(self):
        r = self.client.get('/api/sales/leads/?unassigned=true')
        self.assertEqual(r.status_code, 200)
        names = sorted(l['name'] for l in r.data['results'])
        self.assertEqual(names, ['free one', 'free two'])
