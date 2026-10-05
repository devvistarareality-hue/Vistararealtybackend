"""Correcting a completed site visit: who may, what is checked, what is recorded."""
from datetime import timedelta

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from companies.models import Company
from sales.models import Lead, LeadStatusHistory, Project, SiteVisit


class VisitEditTests(TestCase):
    def setUp(self):
        cache.clear()
        self.co = Company.objects.create(code='VED', name='Ved Co')
        self.p = Project.objects.create(company=self.co, name='Kalrav')
        mk = lambda code, role, boss=None: User.objects.create(
            name=code, email=f'{code}@x.com', phone='9' + code + '00000', user_code=code, role=role,
            designation='STM' if role == 'Employee' else role, company=self.co, modules=['Sales'],
            reporting_manager=boss)
        self.admin = mk('VA', 'Admin')
        self.boss = mk('VB', 'Manager')
        self.stm = mk('VS', 'Employee', self.boss)
        self.other = mk('VO', 'Employee')
        lead = Lead.objects.create(company=self.co, project=self.p, name='Client', phone='9812345678', stm=self.stm)
        when = timezone.now() - timedelta(days=3)
        self.sv = SiteVisit.objects.create(lead=lead, project=self.p, stm=self.stm, status='completed',
                                           outcome='warm', remarks='came with family',
                                           scheduled_at=when, visited_at=when)

    def _edit(self, user, **data):
        c = APIClient(); c.force_authenticate(user)
        return c.post(f'/api/sales/site-visits/{self.sv.id}/edit/', data, format='json')

    def test_stm_fixes_the_date_and_it_is_recorded(self):
        new = (timezone.localdate() - timedelta(days=10)).isoformat()
        res = self._edit(self.stm, visited_at=new, reason='Typed the wrong day')
        self.assertEqual(res.status_code, 200, res.data)
        self.sv.refresh_from_db()
        self.assertEqual(timezone.localtime(self.sv.visited_at).date().isoformat(), new)
        self.assertEqual(timezone.localtime(self.sv.scheduled_at).date().isoformat(), new)
        h = LeadStatusHistory.objects.filter(lead=self.sv.lead, field_changed='site_visit').latest('id')
        self.assertIn('Visit date', h.new_value)
        self.assertEqual(h.remarks, 'Typed the wrong day')

    def test_manager_and_admin_may_others_may_not(self):
        self.assertEqual(self._edit(self.boss, outcome='hot', reason='x').status_code, 200)
        self.assertEqual(self._edit(self.admin, outcome='cold', reason='x').status_code, 200)
        self.assertEqual(self._edit(self.other, outcome='warm', reason='x').status_code, 403)

    def test_future_date_and_missing_reason_are_refused(self):
        tomorrow = (timezone.localdate() + timedelta(days=1)).isoformat()
        self.assertEqual(self._edit(self.stm, visited_at=tomorrow, reason='x').status_code, 400)
        self.assertEqual(self._edit(self.stm, outcome='hot').status_code, 400)

    def test_list_says_who_can_edit(self):
        c = APIClient()
        for user, expected in ((self.stm, True), (self.boss, True), (self.admin, True)):
            c.force_authenticate(user)
            row = next(r for r in c.get('/api/sales/site-visits/').json() if r['id'] == self.sv.id)
            self.assertEqual(row['can_edit'], expected, user.name)
