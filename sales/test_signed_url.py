"""Signed LOI links must be signed for the real file name.

Supabase checks a signed link against the decoded object path, so encoding a comma
as %2C in the signing request made every joint-buyer LOI ("A (50) ,B (50)") fail
with InvalidSignature.
"""
import os
from unittest import mock

from django.test import SimpleTestCase

from sales.supabase_storage import create_signed_url


class SignedUrlTests(SimpleTestCase):
    @mock.patch.dict(os.environ, {'SUPABASE_URL': 'https://x.supabase.co', 'SUPABASE_SERVICE_KEY': 'k', 'SUPABASE_BUCKET': 'loi'})
    def test_commas_and_brackets_are_signed_as_written(self):
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {'signedURL': '/object/sign/loi/x?token=t'}
        with mock.patch('sales.supabase_storage.requests.post', return_value=resp) as post:
            url = create_signed_url('Kalrav 1/Plot EOI-28 - A (50) ,B (50)/R1.pdf')
        sent = post.call_args[0][0]
        self.assertIn('A%20(50)%20,B%20(50)', sent)
        self.assertNotIn('%2C', sent)
        self.assertEqual(url, 'https://x.supabase.co/storage/v1/object/sign/loi/x?token=t')
