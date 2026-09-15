"""The Channel Partner module's My Team — /api/sales/my-team/?cp=1.

Scoped by CP designation rather than by `module`: there is no "Channel Partner"
module for anyone to be assigned to, CP staff sit in Sales and are marked by a CP
designation. That is the same test that decides who gets into the module at all,
so the chart and the module's membership cannot drift apart.
"""
from django.core.cache import cache
from rest_framework.test import APITestCase

from companies.models import Company
from accounts.models import User

from sales.tests import auth


class CpOrgChartTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='CPO', name='CP Org Co')
        cls.other_co = Company.objects.create(code='CPX', name='Other Co')
        cls.admin = User.objects.create(email='cporg_admin@x.com', company=cls.co,
                                        role='Admin', designation='Admin', user_code='C0', name='Admin Person')
        # The CP head reports to a Sales director, as in the real org. Top-of-tree
        # users are treated as company-wide by _sees_all_company, so a head with no
        # manager would take the admin path and prove nothing about scoping.
        cls.director = User.objects.create(email='cporg_dir@x.com', company=cls.co,
                                           role='Director', designation='DIRECTOR',
                                           user_code='C5', name='Director')
        cls.head = User.objects.create(email='cporg_head@x.com', company=cls.co,
                                       role='Manager', designation='CP CLUSTER HEAD',
                                       user_code='C1', name='Cluster Head',
                                       reporting_manager=cls.director)
        cls.exec_ = User.objects.create(email='cporg_exec@x.com', company=cls.co,
                                        role='Employee', designation='CP EXECUTIVE',
                                        user_code='C2', name='CP Exec', reporting_manager=cls.head)
        # Sales, not CP — must not appear on the CP chart.
        cls.stm = User.objects.create(email='cporg_stm@x.com', company=cls.co,
                                      role='Employee', designation='STM', user_code='C3', name='Sales STM',
                                      reporting_manager=cls.head)
        # A CP Cluster Head at another company — the chart is per-company.
        cls.foreign = User.objects.create(email='cporg_foreign@x.com', company=cls.other_co,
                                          role='Manager', designation='CP CLUSTER HEAD',
                                          user_code='C4', name='Foreign Head')

    def setUp(self):
        cache.clear()

    def _codes(self, user, url='/api/sales/my-team/?cp=1'):
        auth(self.client, user)
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        return {m['user_code'] for m in r.data}

    def test_admin_sees_the_cp_side_and_only_the_cp_side(self):
        codes = self._codes(self.admin)
        self.assertEqual(codes, {self.head.user_code, self.exec_.user_code})

    def test_another_companys_cp_staff_never_appear(self):
        self.assertNotIn(self.foreign.user_code, self._codes(self.admin))

    def test_a_cp_manager_still_sees_only_their_own_reports(self):
        # cp=1 is an admin scope. A CP manager asking for it gets their subtree, the
        # same as anywhere else — the flag must not widen anyone's visibility.
        self.assertEqual(self._codes(self.head), {self.exec_.user_code, self.stm.user_code})

    def test_the_sales_chart_is_unaffected(self):
        # module= still scopes by assigned module, and nobody here is assigned one,
        # so the CP branch must not have taken over the ordinary path.
        self.assertEqual(self._codes(self.admin, '/api/sales/my-team/?module=Sales'), set())
