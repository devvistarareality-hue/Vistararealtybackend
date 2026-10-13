"""Impossible dates are refused on save (year 2202, a booking dated next month)."""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from sales.date_rules import problem


class DateRuleTests(TestCase):
    def test_rules(self):
        today = timezone.localdate()
        self.assertEqual(problem(today, 'booking date'), '')
        self.assertIn('year', problem(today.replace(year=2202), 'booking date'))
        self.assertIn('year', problem(today.replace(year=2014), 'booking date'))
        self.assertIn('future', problem(today + timedelta(days=3), 'booking date'))
        self.assertEqual(problem(today + timedelta(days=30), 'visit date', allow_future=True, max_ahead_days=366), '')
        self.assertIn('year ahead', problem(today + timedelta(days=500), 'visit date', allow_future=True,
                                             max_ahead_days=366))
