"""Regression tests for leave-approval authorization.

Only the applicant's reporting manager, a company Admin, or a platform admin
may approve/reject a leave request — never a peer, and never the applicant
themselves. If these fail, the approval authorization has regressed.
"""
from datetime import date

from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from companies.models import Company
from accounts.models import User
from attendance.models import LeaveApplication


def auth(client, user):
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}')


class LeaveApprovalAuthTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.A = Company.objects.create(code='AAA', name='Alpha')
        cls.B = Company.objects.create(code='BBB', name='Beta')
        cls.VRL = Company.objects.create(code='VRL', name='HQ')

        cls.mgr   = User.objects.create(email='mgr@a.com',   company=cls.A, role='Manager', user_code='MGR')
        cls.emp   = User.objects.create(email='emp@a.com',   company=cls.A, role='Employee', user_code='EMP', reporting_manager=cls.mgr)
        cls.peer  = User.objects.create(email='peer@a.com',  company=cls.A, role='Employee', user_code='PEER')
        cls.admin = User.objects.create(email='admin@a.com', company=cls.A, role='Admin',    user_code='ADM')
        cls.mgr_b = User.objects.create(email='mgr@b.com',   company=cls.B, role='Manager',  user_code='MGRB')
        cls.staff = User.objects.create(email='staff@x.com', company=cls.VRL, role='Admin', is_staff=True, user_code='ST')

    def _new_application(self):
        return LeaveApplication.objects.create(
            user=self.emp, work_type='leave', leave_type='paid_leave',
            day_type='full_day', from_date=date(2030, 1, 1),
        )

    def _approve(self, actor):
        app = self._new_application()
        auth(self.client, actor)
        return self.client.patch(f'/api/attendance/leave-action/{app.id}/', {'status': 'approved'}, format='json'), app

    def test_reporting_manager_can_approve(self):
        res, app = self._approve(self.mgr)
        self.assertEqual(res.status_code, 200)
        app.refresh_from_db()
        self.assertEqual(app.status, 'approved')

    def test_peer_cannot_approve(self):
        res, app = self._approve(self.peer)
        self.assertEqual(res.status_code, 403)
        app.refresh_from_db()
        self.assertEqual(app.status, 'pending')

    def test_applicant_cannot_approve_own(self):
        res, app = self._approve(self.emp)
        self.assertEqual(res.status_code, 403)

    def test_company_admin_can_approve(self):
        res, _ = self._approve(self.admin)
        self.assertEqual(res.status_code, 200)

    def test_platform_admin_can_approve_cross_company(self):
        res, _ = self._approve(self.staff)
        self.assertEqual(res.status_code, 200)

    def test_other_company_manager_cannot_approve(self):
        res, app = self._approve(self.mgr_b)
        self.assertEqual(res.status_code, 403)

    # ── Team requests listing ────────────────────────────────────────────
    def test_team_leaves_scopes_to_direct_reports(self):
        self._new_application()
        auth(self.client, self.mgr)
        data = self.client.get('/api/attendance/team-leaves/').json()
        ids = [item['id'] for section in data for item in section['data']]
        self.assertEqual(len(ids), 1)  # sees the report's request

    def test_team_leaves_empty_for_non_manager(self):
        self._new_application()
        auth(self.client, self.peer)
        data = self.client.get('/api/attendance/team-leaves/').json()
        self.assertEqual(data, [])

    def test_team_leaves_admin_sees_whole_company(self):
        self._new_application()
        auth(self.client, self.admin)
        data = self.client.get('/api/attendance/team-leaves/').json()
        ids = [item['id'] for section in data for item in section['data']]
        self.assertEqual(len(ids), 1)
