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


class SourceFilterTests(TestCase):
    """The Sales / CP / All filter (?book=) on leads, visits and the dashboard:
    Sales + CP equals All for an STM, a manager and a director alike."""

    def setUp(self):
        cache.clear()
        self.co = Company.objects.create(code='BKF', name='Bkf Co')
        self.p = Project.objects.create(company=self.co, name='Pratishtha')
        cp_src = LeadSource.objects.create(company=self.co, name='Channel Partner')
        meta = LeadSource.objects.create(company=self.co, name='Meta')
        mk = lambda code, role, desig, boss=None: User.objects.create(
            name=code, email=f'{code}@x.com', phone='9' + code + '00000', user_code=code, role=role,
            designation=desig, company=self.co, modules=['Sales', 'Channel Partner'], reporting_manager=boss)
        self.director = mk('BD', 'Director', 'CMO')
        self.manager = mk('BM', 'Manager', 'Cluster Head')
        self.stm = mk('BS', 'Employee', 'STM', self.manager)
        n = 0
        for src, count in ((meta, 3), (cp_src, 2)):
            for _ in range(count):
                n += 1
                lead = Lead.objects.create(company=self.co, project=self.p, source=src, name=f'L{n}',
                                           phone=f'98{n:08d}', stm=self.stm)
                SiteVisit.objects.create(lead=lead, project=self.p, stm=self.stm, status='completed',
                                         scheduled_at='2026-10-01T10:00:00Z', visited_at='2026-10-01T10:00:00Z')

    def _counts(self, user, extra=''):
        from rest_framework.test import APIClient
        c = APIClient(); c.force_authenticate(user)
        out = {}
        for b in ('sales', 'cp', 'all'):
            leads = c.get(f'/api/sales/leads/?page=1&book={b}{extra}').json()
            svs = c.get(f'/api/sales/site-visits/?counts_only=true&book={b}{extra}').json()
            tile = c.get(f'/api/sales/stats/?book={b}{extra}').json()
            out[b] = (leads.get('count', len(leads.get('results', []))), svs.get('completed', 0), tile.get('sv_done'))
        return out

    def test_sales_plus_cp_is_all_for_every_role(self):
        for u in (self.stm, self.manager, self.director):
            got = self._counts(u)
            self.assertEqual(got['sales'], (3, 3, 3), u.name)
            self.assertEqual(got['cp'], (2, 2, 2), u.name)
            self.assertEqual(got['all'], (5, 5, 5), u.name)

    def test_cp_module_can_see_sales_and_all(self):
        got = self._counts(self.director, '&cp_only=true')
        self.assertEqual(got['sales'][:2], (3, 3))
        self.assertEqual(got['all'][:2], (5, 5))


class CpSourceNeedsPartnerTests(TestCase):
    """Add Lead with Source = Channel Partner must name the partner (from the CP
    module's directory); other sources need none."""

    def setUp(self):
        from sales.models import ChannelPartner
        cache.clear()
        self.co = Company.objects.create(code='CPN', name='Cpn Co')
        self.p = Project.objects.create(company=self.co, name='Kalrav')
        self.cp_src = LeadSource.objects.create(company=self.co, name='Channel Partner')
        self.meta = LeadSource.objects.create(company=self.co, name='Meta')
        self.partner = ChannelPartner.objects.create(company=self.co, name='Shah Realty', contact_no='9000000001')
        self.admin = User.objects.create(name='A', email='a@cpn.com', phone='9000000009', user_code='CPN-A',
                                         role='Admin', company=self.co)

    def _add(self, **extra):
        from rest_framework.test import APIClient
        c = APIClient(); c.force_authenticate(self.admin)
        body = {'name': 'Tejas', 'phone': '9726737708', 'project': self.p.id, **extra}
        return c.post('/api/sales/leads/', body, format='json')

    def test_channel_partner_source_without_partner_is_refused(self):
        res = self._add(source=self.cp_src.id)
        self.assertEqual(res.status_code, 400)
        self.assertIn('channel_partner', res.json())

    def test_with_the_partner_it_is_saved(self):
        res = self._add(source=self.cp_src.id, channel_partner=self.partner.id)
        self.assertIn(res.status_code, (200, 201), res.content)

    def test_other_sources_need_no_partner(self):
        self.assertIn(self._add(source=self.meta.id).status_code, (200, 201))
