"""Ten wrong passwords lock the account for a while; five wrong codes kill an OTP."""
from unittest import mock

from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import OtpCode, User
from accounts.views import LOGIN_MAX_FAILS, OTP_MAX_ATTEMPTS
from companies.models import Company


@mock.patch('notifications.send_email_otp', lambda *a, **k: None)
class LoginLockoutTests(APITestCase):
    def setUp(self):
        cache.clear()   # the per-IP throttle shares the cache
        self.co = Company.objects.create(code='AAA', name='Alpha')
        self.u = User.objects.create(email='u@x.com', company=self.co, role='STM', user_code='U1')
        self.u.set_password('right-pass-123')
        self.u.save()

    def login(self, pw):
        cache.clear()
        return self.client.post('/api/auth/login/', {'company_code': 'AAA', 'user_code': 'U1',
                                                     'password': pw, 'platform': 'web'}, format='json')

    def test_locks_after_too_many_wrong_passwords(self):
        for _ in range(LOGIN_MAX_FAILS):
            self.assertEqual(self.login('wrong').status_code, 401)
        r = self.login('right-pass-123')        # even the right password is refused now
        self.assertEqual(r.status_code, 429)
        User.objects.filter(pk=self.u.pk).update(login_locked_until=timezone.now() - timezone.timedelta(seconds=1))
        self.assertEqual(self.login('right-pass-123').status_code, 200)

    def test_success_resets_the_count(self):
        for _ in range(LOGIN_MAX_FAILS - 1):
            self.login('wrong')
        self.assertEqual(self.login('right-pass-123').status_code, 200)
        self.assertEqual(User.objects.get(pk=self.u.pk).failed_logins, 0)

    def test_otp_dies_after_too_many_wrong_codes(self):
        token = self.login('right-pass-123').data['otp_token']
        real = OtpCode.objects.get(token=token).code
        wrong = '000001' if real != '000001' else '000002'
        for _ in range(OTP_MAX_ATTEMPTS):
            cache.clear()
            r = self.client.post('/api/auth/otp/verify/', {'otp_token': token, 'code': wrong}, format='json')
            self.assertEqual(r.status_code, 401)
        cache.clear()
        r = self.client.post('/api/auth/otp/verify/', {'otp_token': token, 'code': real}, format='json')
        self.assertEqual(r.status_code, 401)    # the OTP is gone — sign in again
