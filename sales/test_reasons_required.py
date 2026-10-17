"""Rejecting or cancelling always says why."""
from django.test import TestCase


class ReasonsRequiredTests(TestCase):
    def test_reason_fields_exist(self):
        from sales.models import Booking
        from club1000.models import Investor
        names = {f.name for f in Booking._meta.get_fields()}
        self.assertTrue({'rejected_reason', 'cancelled_reason'} <= names)
        self.assertIn('rejected_reason', {f.name for f in Investor._meta.get_fields()})
