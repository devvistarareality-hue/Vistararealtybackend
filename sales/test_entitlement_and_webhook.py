"""LOI/EOI entitlement, and Meta webhook signature verification."""
import hashlib
import hmac
import json

from django.test import TestCase
from rest_framework.test import APIClient, APIRequestFactory, force_authenticate

from accounts.models import User
from companies.models import Company
from sales.models import Booking, MetaWebhookConfig, Project
from sales.views import MetaWebhookView


def auth(client, user):
    client.force_authenticate(user=user)


class LOIEntitlement(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.on = Company.objects.create(code='ON', name='Entitled', loi_enabled=True)
        self.off = Company.objects.create(code='OFF', name='Not Entitled')  # default False
        self.u_on = User.objects.create(email='a@on.com', company=self.on, role='Admin', user_code='O1')
        self.u_off = User.objects.create(email='a@off.com', company=self.off, role='Admin', user_code='F1')

    def _booking(self, co, user):
        return Booking.objects.create(company=co, stm=user, status='sold',
                                      client_name='C', area='A1',
                                      loi_document='proj/plot/loi.pdf')

    def test_entitled_company_can_open_its_loi(self):
        b = self._booking(self.on, self.u_on)
        auth(self.client, self.u_on)
        self.assertEqual(self.client.get(f'/api/sales/bookings/{b.id}/loi-url/').status_code, 200)

    def test_unentitled_company_cannot_open_an_loi(self):
        b = self._booking(self.off, self.u_off)
        auth(self.client, self.u_off)
        res = self.client.get(f'/api/sales/bookings/{b.id}/loi-url/')
        self.assertEqual(res.status_code, 403)
        self.assertIn('not enabled', res.json()['detail'])

    def test_default_for_a_new_company_is_off(self):
        self.assertFalse(Company.objects.create(code='NEW', name='New Co').loi_enabled)

    def test_unentitled_company_cannot_attach_an_loi_on_booking_create(self):
        import base64
        proj = Project.objects.create(company=self.off, name='P')
        auth(self.client, self.u_off)
        res = self.client.post('/api/sales/bookings/', {
            'project': proj.id, 'client_name': 'X', 'phone': '9000000000',
            'loi_file': {'name': 'a.pdf', 'type': 'application/pdf',
                         'data': base64.b64encode(b'%PDF-1.4 fake').decode()},
        }, format='json')
        self.assertEqual(res.status_code, 403)
        self.assertIn('not enabled', res.json()['detail'])


class WebhookSignature(TestCase):
    SECRET = 'test-app-secret'
    PAGE = '111222333'

    def setUp(self):
        co = Company.objects.create(code='WH', name='Webhook Co')
        self.cfg = MetaWebhookConfig.objects.create(
            company=co, verify_token='vt', page_access_token='pat', app_secret=self.SECRET)

    def _post(self, body, signature=None):
        raw = json.dumps(body).encode()
        headers = {}
        if signature is not None:
            headers['HTTP_X_HUB_SIGNATURE_256'] = signature
        req = APIRequestFactory().post('/api/sales/webhooks/meta/', raw,
                                       content_type='application/json', **headers)
        return MetaWebhookView.as_view()(req)

    def _sign(self, body):
        raw = json.dumps(body).encode()
        return 'sha256=' + hmac.new(self.SECRET.encode(), raw, hashlib.sha256).hexdigest()

    def test_correct_signature_is_accepted(self):
        body = {'object': 'page', 'entry': [{'id': self.PAGE, 'changes': []}]}
        self.assertTrue(MetaWebhookView._signature_ok(
            self._req_with(body, self._sign(body)), self.SECRET))

    def test_wrong_signature_is_rejected(self):
        body = {'object': 'page', 'entry': [{'id': self.PAGE, 'changes': []}]}
        self.assertFalse(MetaWebhookView._signature_ok(
            self._req_with(body, 'sha256=' + '0' * 64), self.SECRET))

    def test_missing_signature_header_is_rejected(self):
        body = {'object': 'page', 'entry': []}
        self.assertFalse(MetaWebhookView._signature_ok(self._req_with(body, None), self.SECRET))

    def test_tampered_body_is_rejected(self):
        body = {'object': 'page', 'entry': [{'id': self.PAGE}]}
        sig = self._sign(body)
        tampered = {'object': 'page', 'entry': [{'id': '999'}]}
        self.assertFalse(MetaWebhookView._signature_ok(self._req_with(tampered, sig), self.SECRET))

    def test_config_without_a_secret_still_accepts(self):
        """Mid-setup tenants must not silently lose real leads."""
        body = {'object': 'page', 'entry': []}
        self.assertTrue(MetaWebhookView._signature_ok(self._req_with(body, None), ''))

    def _req_with(self, body, signature):
        raw = json.dumps(body).encode()
        kw = {'HTTP_X_HUB_SIGNATURE_256': signature} if signature else {}
        return APIRequestFactory().post('/api/sales/webhooks/meta/', raw,
                                        content_type='application/json', **kw)
