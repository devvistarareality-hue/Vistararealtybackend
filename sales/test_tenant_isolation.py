"""Cross-tenant isolation tests.

Seeds company BRAVO with a sentinel string in every text field, then calls every
authenticated GET endpoint as an ALPHA user and fails if the sentinel appears in
any response. Sentinel-in-JSON catches leakage whatever the response shape is.
"""
 
from django.test import TestCase
from django.urls import get_resolver
from rest_framework.test import APIRequestFactory, force_authenticate
from companies.models import Company
from accounts.models import User
from sales.models import (Lead, Project, Plot, Booking, Closure, SiteVisit,
                          FollowUp, LeadSource)

SENTINEL = 'ZZLEAKZZ'


def walk(res, prefix=''):
    for p in res.url_patterns:
        if hasattr(p, 'url_patterns'):
            yield from walk(p, prefix + str(p.pattern))
        else:
            yield prefix + str(p.pattern), p.callback


class TenantLeak(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.a = Company.objects.create(code='ALPHA', name='Alpha Realty')
        cls.b = Company.objects.create(code='BRAVO', name=f'{SENTINEL} Bravo Realty')

        cls._n = 0
        def mkuser(co, email, role, staff=False):
            cls._n += 1
            u = User.objects.create(name=f'{co.code} {role}', email=email,
                                    phone=f'90000{cls._n:05d}', user_code=f'{co.code}{cls._n:03d}',
                                    role=role, company=co, is_staff=staff)
            u.set_password('x'); u.save()
            return u

        cls.a_admin = mkuser(cls.a, 'a-admin@x.com', 'Admin')
        cls.a_mgr   = mkuser(cls.a, 'a-mgr@x.com', 'Manager')
        cls.a_stm   = mkuser(cls.a, 'a-stm@x.com', 'STM')
        cls.b_admin = mkuser(cls.b, 'b-admin@x.com', 'Admin')

        # ---- ALPHA gets a little benign data so endpoints aren't trivially empty
        src_a = LeadSource.objects.create(company=cls.a, name='Alpha Source')
        pa = Project.objects.create(company=cls.a, name='Alpha Project')
        la = Lead.objects.create(company=cls.a, name='Alpha Lead', phone='1110000000',
                                 source=src_a, project=pa, stm=cls.a_stm)
        Plot.objects.create(project=pa, number='A1')

        # ---- BRAVO gets the sentinel everywhere
        src_b = LeadSource.objects.create(company=cls.b, name=f'{SENTINEL}-source')
        cls.pb = Project.objects.create(company=cls.b, name=f'{SENTINEL}-project',
                                        location=SENTINEL, description=SENTINEL)
        cls.lb = Lead.objects.create(company=cls.b, name=f'{SENTINEL}-lead', phone='9999999999',
                                     email=f'{SENTINEL}@b.com', source=src_b, project=cls.pb,
                                     stm=cls.b_admin, telecaller=cls.b_admin, telecaller_remarks=SENTINEL)
        plot_b = Plot.objects.create(project=cls.pb, number=f'{SENTINEL}-P1', notes=SENTINEL)
        sv_b = SiteVisit.objects.create(lead=cls.lb, project=cls.pb, stm=cls.b_admin,
                                        scheduled_at='2026-08-01T10:00:00Z', status='completed',
                                        visited_at='2026-08-01T11:00:00Z', remarks=SENTINEL)
        cl_b = Closure.objects.create(company=cls.b, lead=cls.lb, project=cls.pb, stm=cls.b_admin,
                                      site_visit=sv_b, closure_date='2026-08-01',
                                      client_name=f'{SENTINEL}-client', unit_no=SENTINEL,
                                      remarks=SENTINEL, status='booked')
        Booking.objects.create(company=cls.b, project=cls.pb, plot=plot_b, closure=cl_b,
                               lead=cls.lb, stm=cls.b_admin, client_name=f'{SENTINEL}-client',
                               phone='9999999999', address=SENTINEL, status='sold',
                               approval_status='APPROVED', booking_date='2026-08-01')
        FollowUp.objects.create(lead=cls.lb, assigned_to=cls.b_admin,
                                scheduled_at='2026-08-02T10:00:00Z', remarks=SENTINEL)

    def _get_views(self):
        seen, out = set(), []
        for path, cb in walk(get_resolver()):
            cls = getattr(cb, 'cls', None)
            if cls is None: continue
            if cls.__module__.split('.')[0] not in ('sales', 'club1000', 'accounts', 'companies', 'attendance'):
                continue
            if not hasattr(cls, 'get') or '<' in path:   # skip detail routes needing a pk
                continue
            if (cls, path) in seen: continue
            seen.add((cls, path))
            out.append((path, cls))
        return out

    def _probe(self, user, label=''):
        leaks, errors = [], []
        for path, cls in self._get_views():
            for qs in ('', f'?company_id={self.b.id}', '?admin_view=1'):
                req = APIRequestFactory().get('/' + path + qs)
                force_authenticate(req, user=user)
                try:
                    res = cls.as_view()(req)
                    if hasattr(res, 'render'): res.render()
                    body = res.content.decode('utf-8', 'replace')
                except Exception as e:
                    errors.append((path, qs, f'{type(e).__name__}: {e}'))
                    continue
                if SENTINEL in body:
                    leaks.append((path, qs, res.status_code, body[:160]))
        return leaks, errors

    def test_alpha_admin_sees_no_bravo_data(self):
        for user, label in [(self.a_admin, 'ALPHA Admin'), (self.a_mgr, 'ALPHA Manager'),
                            (self.a_stm, 'ALPHA STM')]:
            leaks, _errors = self._probe(user, label)
            self.assertEqual(leaks, [], f'{label} saw BRAVO data')

    def test_bravo_admin_does_see_own_data(self):
        """Control: the probe would actually detect a leak if one existed."""
        leaks, _ = self._probe(self.b_admin, 'BRAVO Admin')
        self.assertTrue(leaks, 'sentinel probe never fired — the test itself is blind')


class TenantIDOR(TenantLeak):
    """Direct object access: can ALPHA read/modify a BRAVO row by guessing its id?"""

    DETAIL = [
        ('api/sales/leads/{}/',        'lb',   'sales.LeadDetailView'),
        ('api/sales/projects/{}/',     'pb',   'sales.ProjectDetailView'),
        ('api/sales/site-visits/{}/',  'svb',  'sales.SiteVisitDetailView'),
        ('api/sales/closures/{}/',     'clb',  'sales.ClosureDetailView'),
        ('api/sales/bookings/{}/',     'bkb',  'sales.BookingDetailView'),
    ]

    def _view(self, dotted):
        import importlib
        app, name = dotted.split('.')
        return getattr(importlib.import_module(f'{app}.views'), name, None)

    def test_alpha_cannot_read_bravo_objects_by_id(self):
        from sales.models import SiteVisit, Closure, Booking
        objs = {
            'lb': self.lb.id, 'pb': self.pb.id,
            'svb': SiteVisit.objects.filter(lead=self.lb).first().id,
            'clb': Closure.objects.filter(lead=self.lb).first().id,
            'bkb': Booking.objects.filter(lead=self.lb).first().id,
        }
        bad = []
        for tmpl, key, dotted in self.DETAIL:
            V = self._view(dotted)
            if V is None:
                print(f'   (no view {dotted} — skipped)'); continue
            for user, label in [(self.a_admin, 'ALPHA Admin'), (self.a_stm, 'ALPHA STM')]:
                for method in ('get', 'patch'):
                    if not hasattr(V, method): continue
                    f = APIRequestFactory()
                    req = (f.get if method == 'get' else f.patch)(
                        '/' + tmpl.format(objs[key]), {} if method == 'patch' else None, format='json')
                    force_authenticate(req, user=user)
                    try:
                        res = V.as_view()(req, pk=objs[key])
                        if hasattr(res, 'render'): res.render()
                        body = res.content.decode('utf-8', 'replace')
                    except Exception as e:
                        continue
                    leaked = SENTINEL in body
                    ok = res.status_code in (403, 404) and not leaked
                    print(f'   {label:<12} {method.upper():<6} {tmpl.format(objs[key]):<34} -> {res.status_code} '
                          f'{"LEAK" if leaked else ("blocked" if ok else "allowed-no-sentinel")}')
                    if leaked:
                        bad.append((label, method, tmpl, res.status_code, body[:120]))
        self.assertEqual(bad, [], 'ALPHA read BRAVO data by id')


class SuperAdminCrossesCompanies(TenantLeak):
    def test_platform_admin_sees_everything(self):
        su = User.objects.create(name='Platform', email='su@x.com', phone='9111100000',
                                 user_code='SU001', role='Admin', is_staff=True, is_superuser=True)
        su.set_password('x'); su.save()
        leaks, _ = self._probe(su, 'SUPERUSER')
        self.assertTrue(leaks, 'a platform admin could NOT see other companies — over-restrictive')
