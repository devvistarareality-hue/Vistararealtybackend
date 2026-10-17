"""A signed LOI that fails to upload must not be linked as if it had."""
from unittest import mock

from django.test import TestCase

from sales import views


class LoiUploadFailureTests(TestCase):
    def test_missing_object_is_not_linked(self):
        with mock.patch('sales.supabase_storage.create_signed_url', return_value=None):
            from sales.supabase_storage import object_exists
            self.assertFalse(object_exists('Project/Plot 1 - X/R0_LOI_Plot1_X.pdf'))

    def test_present_object_is_found(self):
        with mock.patch('sales.supabase_storage.create_signed_url', return_value='https://x/sign'):
            from sales.supabase_storage import object_exists
            self.assertTrue(object_exists('Project/Plot 1 - X/R0_LOI_Plot1_X.pdf'))

    def test_tests_do_not_use_the_real_bucket(self):
        from django.conf import settings
        self.assertFalse(str(getattr(settings, 'DEFAULT_FILE_STORAGE', '')).endswith('SupabaseStorage'))
        self.assertTrue(hasattr(views, 'BookingLOIUrlView'))


class LoiAllOrNothingTests(TestCase):
    """A booking and its signed LOI are saved together, or not at all."""

    def setUp(self):
        from datetime import date, timedelta
        from rest_framework.test import APIClient
        from accounts.models import User
        from companies.models import Company
        from sales.models import Project
        self.co = Company.objects.create(code='LAN', name='LOI Co', is_active=True, loi_enabled=True)
        self.u = User.objects.create_user('s@lan.com', company=self.co, user_code='LAN1', password='p',
                                          name='Admin', role='Admin', modules=['Sales'])
        self.project = Project.objects.create(company=self.co, name='P')
        self.api = APIClient(); self.api.force_authenticate(self.u)
        self.body = {'project': self.project.id, 'client_name': 'Buyer', 'phone': '9000000777',
                     'booking_date': str(date.today()), 'final_amount': 100000, 'total_extra': 0,
                     'installments': [{'no': 1, 'date': str(date.today() + timedelta(days=30)), 'amt': 100000}]}

    def _count(self):
        from sales.models import Booking
        return Booking.objects.filter(company=self.co).count()

    def test_no_loi_is_refused(self):
        r = self.api.post('/api/sales/bookings/', self.body, format='json')
        self.assertEqual(r.status_code, 400, r.content)
        self.assertEqual(self._count(), 0)

    def test_failed_upload_saves_nothing(self):
        body = dict(self.body, loi_file={'name': 'l.pdf', 'type': 'application/pdf', 'data': 'JVBERi0xLjQK'})
        with mock.patch('django.db.models.fields.files.FieldFile.save', side_effect=Exception('storage down')), \
             mock.patch('sales.supabase_storage.object_exists', return_value=False):
            r = self.api.post('/api/sales/bookings/', body, format='json')
        self.assertEqual(r.status_code, 502, r.content)
        self.assertIn('not submitted', r.json()['detail'])
        self.assertEqual(self._count(), 0)

    def test_with_loi_it_is_saved(self):
        body = dict(self.body, loi_file={'name': 'l.pdf', 'type': 'application/pdf', 'data': 'JVBERi0xLjQK'})
        r = self.api.post('/api/sales/bookings/', body, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(self._count(), 1)

    def test_upload_that_did_not_land_saves_nothing(self):
        body = dict(self.body, loi_file={'name': 'l.pdf', 'type': 'application/pdf', 'data': 'JVBERi0xLjQK'})
        with mock.patch('sales.views._loi_stored', return_value=False):
            r = self.api.post('/api/sales/bookings/', body, format='json')
        self.assertEqual(r.status_code, 502, r.content)
        self.assertEqual(self._count(), 0)


class StorageSettingTests(TestCase):
    """Django 5.1+ ignores DEFAULT_FILE_STORAGE — uploads went to the server's disk."""
    def test_uses_STORAGES_not_the_removed_setting(self):
        import vistararealtybackend.settings as conf
        self.assertFalse(hasattr(conf, 'DEFAULT_FILE_STORAGE'))
        self.assertFalse(hasattr(conf, 'STATICFILES_STORAGE'))
        self.assertIn('default', conf.STORAGES)
        self.assertIn('staticfiles', conf.STORAGES)


class LoiReuploadTests(LoiAllOrNothingTests):
    """A booking whose signed LOI is missing from storage can have it put back."""
    def test_missing_then_uploaded_again(self):
        from sales.models import Booking
        body = dict(self.body, loi_file={'name': 'l.pdf', 'type': 'application/pdf', 'data': 'JVBERi0xLjQK'})
        bid = self.api.post('/api/sales/bookings/', body, format='json').json()['id']
        b = Booking.objects.get(pk=bid)
        b.loi_document.storage.delete(b.loi_document.name)          # lost, as on the server's disk
        r = self.api.get(f'/api/sales/bookings/{bid}/loi-url/')
        self.assertEqual((r.status_code, r.json().get('code')), (404, 'loi_missing'))
        r = self.api.post(f'/api/sales/bookings/{bid}/loi-url/', {'loi_file': {'name': 'l.pdf', 'data': 'JVBERi0xLjQK'}}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self.api.get(f'/api/sales/bookings/{bid}/loi-url/').status_code, 200)

    def test_outsider_cannot_upload(self):
        from rest_framework.test import APIClient
        from accounts.models import User
        body = dict(self.body, loi_file={'name': 'l.pdf', 'type': 'application/pdf', 'data': 'JVBERi0xLjQK'})
        bid = self.api.post('/api/sales/bookings/', body, format='json').json()['id']
        other = User.objects.create_user('o@lan.com', company=self.co, user_code='LAN2', password='p', name='O', role='Employee', modules=['Sales'])
        api = APIClient(); api.force_authenticate(other)
        r = api.post(f'/api/sales/bookings/{bid}/loi-url/', {'loi_file': {'name': 'l.pdf', 'data': 'JVBERi0xLjQK'}}, format='json')
        self.assertIn(r.status_code, (403, 404))
