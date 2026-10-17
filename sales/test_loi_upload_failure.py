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
