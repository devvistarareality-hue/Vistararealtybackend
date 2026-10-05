"""A partner-sourced lead's visit that a Sales STM did is Sales work: the Sales site
visit list shows it to everyone who sees the project, not only the STM's own chain.
A visit a CP Executive did stays partner work."""
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from accounts.models import User
from companies.models import Company
from sales.models import Lead, LeadSource, Project, SiteVisit
from sales.views import SiteVisitListView


class CpVisitSplitTests(TestCase):
    def setUp(self):
        cache.clear()
        self.co = Company.objects.create(code='CVS', name='Cvs Co')
        self.p = Project.objects.create(company=self.co, name='Pratishtha')
        cp_src = LeadSource.objects.create(company=self.co, name='Channel Partner')
        meta = LeadSource.objects.create(company=self.co, name='Meta')
        mk = lambda code, role, desig, boss=None: User.objects.create(
            name=code, email=f'{code}@x.com', phone='9' + code + '00000', user_code=code, role=role,
            designation=desig, company=self.co, modules=['Sales'], reporting_manager=boss)
        self.director = mk('D1', 'Director', 'CMO')
        self.manager = mk('M1', 'Manager', 'Cluster Head')
        self.other_manager = mk('M2', 'Manager', 'Regional Head')
        self.stm = mk('S1', 'Employee', 'STM', self.manager)
        self.cpx = mk('C1', 'Employee', 'CP Executive', self.director)

        def visit(name, source, stm):
            lead = Lead.objects.create(company=self.co, project=self.p, source=source, name=name,
                                       phone='9' + str(abs(hash(name)) % 10**9), stm=stm)
            return SiteVisit.objects.create(lead=lead, project=self.p, stm=stm, status='completed',
                                            scheduled_at='2026-10-01T10:00:00Z')
        self.sales_on_cp = visit('Partner lead, Sales STM', cp_src, self.stm)
        self.cp_on_cp = visit('Partner lead, CP Executive', cp_src, self.cpx)
        self.plain = visit('Meta lead', meta, self.stm)

    def _ids(self, user):
        req = APIRequestFactory().get('/x/')
        force_authenticate(req, user=user)
        d = SiteVisitListView.as_view()(req).data
        return {r['id'] for r in (d.get('results', d) if isinstance(d, dict) else d)}

    def test_sales_stm_visit_on_a_partner_lead_shows_for_everyone(self):
        for u in (self.director, self.manager, self.other_manager):
            self.assertIn(self.sales_on_cp.id, self._ids(u), u.name)
            self.assertIn(self.plain.id, self._ids(u), u.name)

    def test_cp_executive_visit_stays_partner_work(self):
        # Outside the CP Executive's chain it is not in the Sales list…
        self.assertNotIn(self.cp_on_cp.id, self._ids(self.manager))
        self.assertNotIn(self.cp_on_cp.id, self._ids(self.other_manager))
        # …and their own chain still sees it, as before.
        self.assertIn(self.cp_on_cp.id, self._ids(self.director))
