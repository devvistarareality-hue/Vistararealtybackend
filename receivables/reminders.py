"""Scheduled AR notifications, run by `run_scheduled_notifications` on the cron.

  1. A follow-up comes due      -> remind whoever it is assigned to (once).
  2. Still open a day later     -> tell the AR managers (once).
  3. Every morning              -> each AR user gets the day's collections digest:
                                   overdue accounts, what falls due this week, and
                                   their own follow-ups for today.
  4. Every morning              -> an installment falling due in DUE_SOON_DAYS days
                                   is flagged, once, to whoever follows that account
                                   up (else the AR team), so the call happens before
                                   the date, not after.
All idempotent: markers on the follow-up, and one digest per user per day.
"""
from datetime import datetime, time, timedelta

from django.utils import timezone

from accounts.models import Notification, User
from notifications import notify, notify_many
from .models import ARAccount, ARFollowUp
from .permissions import AR_MODULE, has_ar_access

DIGEST_FROM = time(9, 30)
DIGEST_UNTIL = time(13, 0)
DUE_SOON_DAYS = 3


def _inr(n):
    n = int(n or 0)
    if n >= 10_000_000:
        return '₹%.2f Cr' % (n / 10_000_000)
    if n >= 100_000:
        return '₹%.2f L' % (n / 100_000)
    return '₹{:,}'.format(n)


def _unit(f):
    b = f.account.booking
    return '%s · %s Plot %s' % (b.client_name or 'Customer', b.project.name if b.project_id else '',
                                b.plot_numbers or (b.plot.number if b.plot_id else ''))


def ar_managers(company_id):
    return [u for u in User.objects.filter(company_id=company_id, is_active=True)
            if AR_MODULE in (u.manager_modules or []) or AR_MODULE in (u.admin_modules or [])
            or getattr(u, 'role', '') == 'Admin']


def _due_qs():
    return ARFollowUp.objects.select_related(
        'account', 'account__booking', 'account__booking__project', 'account__booking__plot',
        'assigned_to').filter(status='pending', assigned_to__is_active=True)


def followup_reminders(now, dry=False):
    n = 0
    for f in _due_qs().filter(scheduled_at__lte=now, reminder_sent_at__isnull=True).iterator():
        try:
            if not dry:
                notify(f.assigned_to, 'ar_followup_due', 'Collection follow-up due',
                       '%s — %s%s' % (_unit(f), f.get_channel_display(), (': ' + f.note) if f.note else ''),
                       {'account_id': f.account_id, 'followup_id': f.id})
                ARFollowUp.objects.filter(pk=f.pk).update(reminder_sent_at=now)
            n += 1
        except Exception:
            pass
    return n


def followup_escalations(now, cutoff, dry=False):
    n = 0
    for f in _due_qs().filter(scheduled_at__lt=cutoff, escalated_at__isnull=True).iterator():
        try:
            mgrs = [u for u in ar_managers(f.account.company_id) if u.id != f.assigned_to_id]
            if not dry:
                if mgrs:
                    notify_many(mgrs, 'ar_followup_overdue', 'Collection follow-up overdue',
                                "%s's follow-up with %s is overdue" % (f.assigned_to.name, _unit(f)),
                                {'account_id': f.account_id, 'followup_id': f.id})
                ARFollowUp.objects.filter(pk=f.pk).update(escalated_at=now)
            n += 1
        except Exception:
            pass
    return n


def daily_digest(now, dry=False):
    """Morning collections digest for every AR user, once a day."""
    from sales.models import Booking
    from .collections import collections_snapshot
    from .services import current_approved_booking_ids, sync_accounts
    local = timezone.localtime(now)
    if not (DIGEST_FROM <= local.time() < DIGEST_UNTIL):
        return 0
    today = local.date()
    end_of_today = timezone.make_aware(datetime.combine(today, time.max))
    n = 0
    company_ids = ARAccount.objects.filter(status='active').values_list('company_id', flat=True).distinct()
    for cid in company_ids:
        try:
            users = [u for u in User.objects.filter(company_id=cid, is_active=True) if has_ar_access(u)]
            sent = set(Notification.objects.filter(recipient__in=users, type='ar_collections_digest',
                                                   created_at__date=today).values_list('recipient_id', flat=True))
            users = [u for u in users if u.id not in sent]
            if not users:
                continue
            bookings = Booking.objects.filter(company_id=cid)
            sync_accounts(bookings.filter(status='sold'))
            accts = ARAccount.objects.filter(company_id=cid, booking_id__in=current_approved_booking_ids(bookings))
            rows, _ = collections_snapshot(accts, today, days=7)
            overdue = [r for r in rows if r['overdue'] > 0]
            week = [r for r in rows if r['upcoming_amount'] > 0]
            if not overdue and not week:
                continue
            base = 'Overdue: %d accounts · %s. Due in 7 days: %d accounts · %s.' % (
                len(overdue), _inr(sum(r['overdue'] for r in overdue)),
                len(week), _inr(sum(r['upcoming_amount'] for r in week)))
            for u in users:
                mine = ARFollowUp.objects.filter(assigned_to=u, status='pending', scheduled_at__lte=end_of_today).count()
                body = base + (' Your follow-ups today: %d.' % mine if mine else '')
                if not dry:
                    notify(u, 'ar_collections_digest', 'Today\'s collections', body, {'screen': 'collections'})
                n += 1
        except Exception:
            pass
    return n


def ar_team(company_id):
    """People who work AR by assignment (module, manager or admin level) — not every
    company admin, who reach AR implicitly."""
    return [u for u in User.objects.filter(company_id=company_id, is_active=True)
            if AR_MODULE in (u.modules or []) or AR_MODULE in (u.manager_modules or [])
            or AR_MODULE in (u.admin_modules or [])]


def due_soon_reminders(now, dry=False):
    """Installments due in DUE_SOON_DAYS days: one notice per installment."""
    from sales.models import Booking
    from .services import current_approved_booking_ids, sync_accounts, compute_account
    local = timezone.localtime(now)
    if not (DIGEST_FROM <= local.time() < DIGEST_UNTIL):
        return 0
    target = local.date() + timedelta(days=DUE_SOON_DAYS)
    n = 0
    for cid in ARAccount.objects.filter(status='active').values_list('company_id', flat=True).distinct():
        try:
            team = ar_team(cid)
            bookings = Booking.objects.filter(company_id=cid)
            sync_accounts(bookings.filter(status='sold'))
            accts = (ARAccount.objects.filter(company_id=cid, status='active',
                                              booking_id__in=current_approved_booking_ids(bookings))
                     .select_related('booking', 'booking__project', 'booking__plot').prefetch_related('receipts'))
            for acct in accts:
                receipts = [r for r in acct.receipts.all() if not r.is_deleted]
                _, r, _ = compute_account(acct, local.date(), receipts)
                due = [st for st in r.lines if st.line.due == target and st.remaining > 0]
                if not due:
                    continue
                key = {'account_id': acct.id, 'due': target.isoformat()}
                if Notification.objects.filter(type='ar_due_soon', data__account_id=acct.id,
                                               data__due=target.isoformat()).exists():
                    continue
                owner = (ARFollowUp.objects.filter(account=acct, status='pending', assigned_to__is_active=True)
                         .select_related('assigned_to').order_by('scheduled_at').first())
                recipients = [owner.assigned_to] if owner else team
                if not recipients:
                    continue
                b = acct.booking
                amount = sum(st.remaining for st in due)
                body = '%s · %s Plot %s — %s due on %s (%s)' % (
                    b.client_name or 'Customer', b.project.name if b.project_id else '',
                    b.plot_numbers or (b.plot.number if b.plot_id else ''), _inr(amount),
                    target.strftime('%d %b'), ', '.join(st.line.label for st in due))
                if not dry:
                    notify_many(recipients, 'ar_due_soon', 'Payment due in %d days' % DUE_SOON_DAYS, body, key)
                n += 1
        except Exception:
            pass
    return n


def run(now, escalate_hours=24, dry=False):
    cutoff = now - timedelta(hours=escalate_hours)
    return {
        'ar_fu_reminder': followup_reminders(now, dry),
        'ar_fu_escalate': followup_escalations(now, cutoff, dry),
        'ar_digest': daily_digest(now, dry),
        'ar_due_soon': due_soon_reminders(now, dry),
    }
