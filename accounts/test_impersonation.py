"""Viewing the app as another user — what it must and must not allow.

This is the replacement for the shared master password that used to sit in
LoginView, so the tests are largely about the ways that password was bad: it
worked for anyone who knew the string, it named nobody, and it could be used
from a session that had proved nothing. Each test below pins one of those shut.
"""
import json

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import OtpCode, User
from activity.models import ActivityLog
from companies.models import Company
from sales.models import Lead, LeadSource, Project


class Impersonation(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.vrl = Company.objects.create(code='VRL', name='Vistara', is_active=True)
        cls.acme = Company.objects.create(code='ACME', name='Acme', is_active=True)

        cls.super_admin = User.objects.create_user(
            'boss@vrl.com', company=cls.vrl, user_code='VRL001', password='bosspass1',
            name='Platform Boss', role='Admin', modules=['Sales', 'AR', 'HR'])
        cls.staff = User.objects.create_user(
            'django@vrl.com', company=cls.vrl, user_code='VRL002', password='staffpass1',
            name='Django Staff', role='Admin', modules=['Sales'], is_staff=True)

        cls.acme_admin = User.objects.create_user(
            'admin@acme.com', company=cls.acme, user_code='ACME001', password='adminpass1',
            name='Acme Admin', role='Admin', modules=['Sales'])
        cls.rep = User.objects.create_user(
            'rep@acme.com', company=cls.acme, user_code='ACME002', password='reppass1',
            name='Acme Rep', role='Employee', modules=['Sales'],
            reporting_manager=cls.acme_admin)
        cls.dormant = User.objects.create_user(
            'gone@acme.com', company=cls.acme, user_code='ACME003', password='gonepass1',
            name='Left Company', role='Employee', modules=['Sales'],
            reporting_manager=cls.acme_admin, is_active=False)

        cls.project = Project.objects.create(company=cls.acme, name='Acme Heights')
        cls.source = LeadSource.objects.create(company=cls.acme, name='walkin')

    def setUp(self):
        self.api = APIClient()
        # Every test here signs in for real, and the login throttle counts those
        # across tests — without this the run fails on 429 rather than on anything
        # under test.
        cache.clear()

    def _as(self, user, password):
        """Sign in the way a person does — password, then the emailed OTP."""
        r = self.api.post('/api/auth/login/', {
            'company_code': user.company.code, 'user_code': user.user_code,
            'password': password, 'platform': 'web'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        if not r.data.get('otp_required'):
            return r.data['tokens']['access']
        code = OtpCode.objects.filter(user=user, is_used=False).latest('created_at').code
        r = self.api.post('/api/auth/otp/verify/', {
            'otp_token': r.data['otp_token'], 'code': code, 'platform': 'web'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        return r.data['tokens']['access']

    def _impersonate(self, actor, password, target_id):
        token = self._as(actor, password)
        self.api.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
        return self.api.post('/api/auth/impersonate/',
                             {'user_id': target_id, 'platform': 'web'}, format='json')

    # ── who may do it ────────────────────────────────────────────────────────
    def test_platform_admin_can_view_as_a_user_in_another_company(self):
        r = self._impersonate(self.super_admin, 'bosspass1', self.rep.pk)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data['user']['id'], self.rep.pk)
        self.assertEqual(r.data['impersonated_by']['id'], self.super_admin.pk)
        self.assertIn('access', r.data['tokens'])

    def test_a_company_admin_cannot(self):
        """Being Admin of your own company is not platform admin — that is the
        whole point of the distinction, and impersonation crosses companies."""
        r = self._impersonate(self.acme_admin, 'adminpass1', self.rep.pk)
        self.assertEqual(r.status_code, 403)

    def test_an_employee_cannot(self):
        r = self._impersonate(self.rep, 'reppass1', self.acme_admin.pk)
        self.assertEqual(r.status_code, 403)

    def test_an_anonymous_caller_cannot(self):
        r = self.api.post('/api/auth/impersonate/', {'user_id': self.rep.pk}, format='json')
        self.assertEqual(r.status_code, 401)

    # ── what it refuses ──────────────────────────────────────────────────────
    def test_cannot_step_into_a_django_staff_account(self):
        """is_staff reaches the Django admin, which none of this app's permission
        checks cover. Impersonation stays inside the app."""
        r = self._impersonate(self.super_admin, 'bosspass1', self.staff.pk)
        self.assertEqual(r.status_code, 403)

    def test_cannot_step_into_a_deactivated_account(self):
        r = self._impersonate(self.super_admin, 'bosspass1', self.dormant.pk)
        self.assertEqual(r.status_code, 404)

    def test_cannot_chain_from_one_impersonation_into_another(self):
        """Otherwise the `impersonator` claim stops naming the person responsible:
        admin → rep → someone else would come back reading as rep's doing."""
        first = self._impersonate(self.super_admin, 'bosspass1', self.rep.pk)
        self.api.credentials(HTTP_AUTHORIZATION=f'Bearer {first.data["tokens"]["access"]}')
        r = self.api.post('/api/auth/impersonate/',
                          {'user_id': self.acme_admin.pk}, format='json')
        self.assertEqual(r.status_code, 403)

    def test_missing_user_id_is_a_400_not_a_500(self):
        token = self._as(self.super_admin, 'bosspass1')
        self.api.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
        for body in ({}, {'user_id': ''}, {'user_id': 'abc'}):
            with self.subTest(body=body):
                r = self.api.post('/api/auth/impersonate/', body, format='json')
                self.assertEqual(r.status_code, 400)

    # ── the target's own account is untouched ────────────────────────────────
    def test_the_target_is_not_signed_out_and_their_password_still_works(self):
        before_web = self.rep.session_token_web
        before_app = self.rep.session_token_app
        self._impersonate(self.super_admin, 'bosspass1', self.rep.pk)
        self.rep.refresh_from_db()
        self.assertEqual(self.rep.session_token_web, before_web,
                         'impersonation rotated the web session token, kicking the real user out')
        self.assertEqual(self.rep.session_token_app, before_app)
        self.assertTrue(self.rep.check_password('reppass1'),
                        "the target's password was changed")

    def test_the_real_user_logging_in_ends_the_impersonated_session(self):
        """Their login rotates the session token the impersonated token carries,
        and SessionJWTAuthentication rejects it from then on — so the person
        being watched can always end it themselves."""
        r = self._impersonate(self.super_admin, 'bosspass1', self.rep.pk)
        impersonated = r.data['tokens']['access']
        self._as(self.rep, 'reppass1')                      # rotates session_token_web
        self.api.credentials(HTTP_AUTHORIZATION=f'Bearer {impersonated}')
        self.assertEqual(self.api.get('/api/auth/me/').status_code, 401)

    # ── it is the target's session, with the target's limits ─────────────────
    def test_the_session_really_is_the_target_user(self):
        r = self._impersonate(self.super_admin, 'bosspass1', self.rep.pk)
        self.api.credentials(HTTP_AUTHORIZATION=f'Bearer {r.data["tokens"]["access"]}')
        me = self.api.get('/api/auth/me/')
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.data['id'], self.rep.pk)
        self.assertEqual(me.data['company_code'], 'ACME')

    def test_me_keeps_reporting_the_impersonation_after_a_reload(self):
        """The banner has to survive a refresh, or the admin forgets they are
        not themselves and takes an action believing it is their own."""
        r = self._impersonate(self.super_admin, 'bosspass1', self.rep.pk)
        self.api.credentials(HTTP_AUTHORIZATION=f'Bearer {r.data["tokens"]["access"]}')
        me = self.api.get('/api/auth/me/')
        self.assertEqual(me.data['impersonated_by']['id'], self.super_admin.pk)

    def test_an_ordinary_session_reports_no_impersonation(self):
        token = self._as(self.rep, 'reppass1')
        self.api.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
        self.assertIsNone(self.api.get('/api/auth/me/').data['impersonated_by'])

    def test_the_impersonated_session_does_not_inherit_platform_admin(self):
        """A rep's token is a rep's token. If it still opened every company the
        impersonation would be an escalation route, not a viewing tool."""
        r = self._impersonate(self.super_admin, 'bosspass1', self.rep.pk)
        self.api.credentials(HTTP_AUTHORIZATION=f'Bearer {r.data["tokens"]["access"]}')
        # The backup endpoint is company-admin-or-above; a rep must be refused.
        self.assertEqual(self.api.get('/api/sales/backups/excel/').status_code, 403)

    # ── it leaves a trail ────────────────────────────────────────────────────
    def test_starting_an_impersonation_is_logged_against_the_admin(self):
        self._impersonate(self.super_admin, 'bosspass1', self.rep.pk)
        row = ActivityLog.objects.filter(action='impersonate').first()
        self.assertIsNotNone(row, 'no activity log row for the impersonation')
        self.assertEqual(row.actor_id, self.super_admin.pk)
        self.assertEqual(row.target_id, str(self.rep.pk))
        self.assertIn('Platform Boss', row.summary)
        self.assertIn('Acme Rep', row.summary)

    def test_work_done_while_impersonating_names_the_admin(self):
        """Without this, a lead created by the admin-as-rep is indistinguishable
        in the log from one the rep created."""
        r = self._impersonate(self.super_admin, 'bosspass1', self.rep.pk)
        self.api.credentials(HTTP_AUTHORIZATION=f'Bearer {r.data["tokens"]["access"]}')
        created = self.api.post('/api/sales/leads/', {
            'name': 'Walk-in Client', 'phone': '+919800000111',
            'project': self.project.pk, 'source': self.source.pk, 'status': 'new',
        }, format='json')
        self.assertIn(created.status_code, (200, 201), created.data)
        # `name` is encrypted, so the row can only be found by id.
        lead = Lead.objects.filter(pk=created.data['id']).first()
        self.assertIsNotNone(lead)
        self.assertEqual(lead.company_id, self.acme.pk)
        self.assertEqual(lead.name, 'Walk-in Client')

        row = ActivityLog.objects.exclude(action='impersonate').first()
        self.assertIsNotNone(row, 'the lead creation was not logged at all')
        self.assertIn('signed in as this user', row.summary)
        self.assertIn('Platform Boss', row.summary)
        self.assertEqual(json.loads(row.details)['impersonated_by']['id'],
                         self.super_admin.pk)
