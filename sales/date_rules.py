"""Sane dates on the records people type them into (Nexora-wide, web and app).

A typed year like 2202 or 0026, or a visit "completed" next month, silently skews
every report and Ask Nexora answer that counts by date. These checks run on save:

  - any date: the year must be between MIN_YEAR and this year + FUTURE_YEARS
  - booking date, closure date and a completed visit: not after today
  - a scheduled visit: no more than a year ahead
"""
from datetime import date, datetime, timedelta

from django.utils import timezone

MIN_YEAR = 2015
FUTURE_YEARS = 2


def _as_date(v):
    if isinstance(v, datetime):
        return timezone.localtime(v).date() if timezone.is_aware(v) else v.date()
    return v if isinstance(v, date) else None


def problem(value, what, allow_future=False, max_ahead_days=None):
    """A message if `value` is not a believable date, else ''."""
    d = _as_date(value)
    if d is None:
        return ''
    today = timezone.localdate()
    if not (MIN_YEAR <= d.year <= today.year + FUTURE_YEARS):
        return f'Check the year — {d.strftime("%d %b %Y")} is not a believable {what}.'
    if not allow_future and d > today:
        return f'The {what} cannot be in the future ({d.strftime("%d %b %Y")}).'
    if max_ahead_days is not None and d > today + timedelta(days=max_ahead_days):
        return f'The {what} is more than a year ahead ({d.strftime("%d %b %Y")}) — check the year.'
    return ''
