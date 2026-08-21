"""Data reset needs a key the app itself does not hold."""
from unittest import mock

from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from accounts.models import User
from companies.models import Company
from sales.models import Lead
from sales.views import SalesDataResetView

KEY = 's3cret-reset-key'


class DataResetKey(TestCase):
    def setUp(self):
        self.co = Company.objects.create(code='DR', name='DR Co')
        self.admin = User.objects.create(name='A', email='a@dr.com', phone='9000000001',
                                         user_code='A1', role='Admin', company=self.co)
        self.staff = User.objects.create(name='E', email='e@dr.com', phone='9000000002',
                                         user_code='E1', role='Employee', company=self.co)
        Lead.objects.create(company=self.co, name='L', phone='9111111111')

    def _post(self, user=None, **body):
        payload = {'confirm': 'DELETE'}
        payload.update(body)
        req = APIRequestFactory().post('/x/', payload, format='json')
        force_authenticate(req, user=user or self.admin)
        return SalesDataResetView.as_view()(req)

    def test_correct_key_allows_the_reset(self):
        with mock.patch.dict('os.environ', {'DATA_RESET_KEY': KEY}):
            res = self._post(reset_key=KEY)
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(Lead.objects.count(), 0)

    def test_wrong_key_is_refused_and_deletes_nothing(self):
        with mock.patch.dict('os.environ', {'DATA_RESET_KEY': KEY}):
            res = self._post(reset_key='not-the-key')
        self.assertEqual(res.status_code, 403)
        self.assertIn('Incorrect reset key', res.data['detail'])
        self.assertEqual(Lead.objects.count(), 1, 'nothing may be deleted on a bad key')

    def test_missing_key_is_refused(self):
        with mock.patch.dict('os.environ', {'DATA_RESET_KEY': KEY}):
            res = self._post()
        self.assertEqual(res.status_code, 403)
        self.assertEqual(Lead.objects.count(), 1)

    def test_typing_DELETE_alone_is_no_longer_enough(self):
        """The whole point — the confirm box is not the last line of defence."""
        with mock.patch.dict('os.environ', {'DATA_RESET_KEY': KEY}):
            res = self._post()
        self.assertEqual(res.status_code, 403)

    def test_it_fails_closed_when_no_key_is_configured(self):
        """A missing key must never mean 'no protection'."""
        with mock.patch.dict('os.environ', {}, clear=False):
            import os
            os.environ.pop('DATA_RESET_KEY', None)
            res = self._post(reset_key='anything')
        self.assertEqual(res.status_code, 403)
        self.assertIn('no DATA_RESET_KEY', res.data['detail'])
        self.assertEqual(Lead.objects.count(), 1)

    def test_a_non_admin_is_still_refused_first(self):
        with mock.patch.dict('os.environ', {'DATA_RESET_KEY': KEY}):
            res = self._post(user=self.staff, reset_key=KEY)
        self.assertEqual(res.status_code, 403)
        self.assertIn('Admin only', res.data['detail'])
        self.assertEqual(Lead.objects.count(), 1)

    def test_the_confirm_box_is_still_required(self):
        with mock.patch.dict('os.environ', {'DATA_RESET_KEY': KEY}):
            req = APIRequestFactory().post('/x/', {'reset_key': KEY}, format='json')
            force_authenticate(req, user=self.admin)
            res = SalesDataResetView.as_view()(req)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(Lead.objects.count(), 1)
