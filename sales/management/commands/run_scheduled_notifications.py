"""Phase-2 scheduled notifications — run on a cron (every 15-30 min is ideal).

Three jobs, all idempotent so repeated runs never double-notify:
  1. Overdue follow-ups   -> nudge the assignee, then escalate to their manager.
  2. Overdue site visits  -> nudge the STM/telecaller, then escalate to manager.
  3. Mark-available        -> morning reminder to TC/STM who haven't checked in.

Follow-ups/site visits carry `reminder_sent_at` + `escalated_at` markers that are
stamped once each. The availability reminder de-dups against today's Notification
rows. Safe to call from Railway cron; never raises out of a single record.

Usage:  python manage.py run_scheduled_notifications [--escalate-hours 24] [--dry-run]
"""
from datetime import timedelta, time as dtime

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from sales.models import FollowUp, SiteVisit, UserAvailability
from accounts.models import User
from notifications import notify, notify_many, reporting_chain


def _is_tc_or_stm(user):
    d = (getattr(user, 'designation', '') or '').lower()
    return any(k in d for k in (
        'telecaller', 'tele caller', 'stm', 'sales team', 'sales executive',
    ))


def _overdue_phrase(scheduled_at, now):
    hrs = int((now - scheduled_at).total_seconds() // 3600)
    if hrs < 1:
        return 'just now'
    if hrs < 24:
        return f'{hrs}h ago'
    return f'{hrs // 24}d ago'


class Command(BaseCommand):
    help = 'Send overdue follow-up/SV reminders + escalations and morning mark-available reminders.'

    def add_arguments(self, parser):
        parser.add_argument('--escalate-hours', type=int, default=24,
                            help='Hours past due before escalating to the manager (default 24).')
        parser.add_argument('--dry-run', action='store_true',
                            help='Log what would be sent without notifying or stamping markers.')
        parser.add_argument('--backfill', action='store_true',
                            help='One-time: stamp every currently-overdue follow-up/SV as already '
                                 'handled (both markers) WITHOUT notifying, so the first real cron '
                                 'run only fires for items that go overdue afterwards.')

    def handle(self, *args, **opts):
        now = timezone.now()
        if opts['backfill']:
            return self._backfill(now)
        dry = opts['dry_run']
        cutoff = now - timedelta(hours=opts['escalate_hours'])

        c = {
            'fu_reminder': self._followup_reminders(now, dry),
            'fu_escalate': self._followup_escalations(now, cutoff, dry),
            'sv_reminder': self._sv_reminders(now, dry),
            'sv_escalate': self._sv_escalations(now, cutoff, dry),
            'availability': self._availability_reminders(now, dry),
        }
        tag = '[dry-run] ' if dry else ''
        self.stdout.write(self.style.SUCCESS(
            f'{tag}follow-up: {c["fu_reminder"]} nudged / {c["fu_escalate"]} escalated · '
            f'site-visit: {c["sv_reminder"]} nudged / {c["sv_escalate"]} escalated · '
            f'availability: {c["availability"]} reminded'
        ))

    # ── One-time backfill (suppress the existing backlog) ─────────────────
    def _backfill(self, now):
        fu = FollowUp.objects.filter(
            status='pending', scheduled_at__lt=now,
        ).filter(Q(reminder_sent_at__isnull=True) | Q(escalated_at__isnull=True))
        fu_n = fu.update(reminder_sent_at=now, escalated_at=now)
        sv = SiteVisit.objects.filter(
            status='scheduled', scheduled_at__isnull=False, scheduled_at__lt=now,
        ).filter(Q(reminder_sent_at__isnull=True) | Q(escalated_at__isnull=True))
        sv_n = sv.update(reminder_sent_at=now, escalated_at=now)
        self.stdout.write(self.style.SUCCESS(
            f'Backfill complete — stamped {fu_n} overdue follow-up(s) and {sv_n} overdue '
            'site-visit(s) as already handled. No notifications were sent.'
        ))

    # ── Follow-ups ────────────────────────────────────────────────────────
    def _followup_reminders(self, now, dry):
        qs = FollowUp.objects.select_related('lead', 'assigned_to').filter(
            status='pending', scheduled_at__lt=now,
            reminder_sent_at__isnull=True, assigned_to__is_active=True,
        )
        n = 0
        for f in qs.iterator():
            try:
                notify(f.assigned_to, 'followup_overdue', 'Follow-up overdue',
                       f'Your follow-up with {f.lead.name} was due {_overdue_phrase(f.scheduled_at, now)}.',
                       {'lead_id': f.lead_id, 'followup_id': f.id})
                if not dry:
                    FollowUp.objects.filter(pk=f.pk).update(reminder_sent_at=now)
                n += 1
            except Exception:
                pass
        return n

    def _followup_escalations(self, now, cutoff, dry):
        qs = FollowUp.objects.select_related('lead', 'assigned_to').filter(
            status='pending', scheduled_at__lt=cutoff,
            escalated_at__isnull=True, assigned_to__is_active=True,
        )
        n = 0
        for f in qs.iterator():
            try:
                mgrs = reporting_chain(f.assigned_to)
                if mgrs:
                    notify_many(mgrs, 'followup_overdue', 'Team follow-up overdue',
                                f"{f.assigned_to.name}'s follow-up with {f.lead.name} is overdue "
                                f'({_overdue_phrase(f.scheduled_at, now)}).',
                                {'lead_id': f.lead_id, 'followup_id': f.id})
                if not dry:
                    FollowUp.objects.filter(pk=f.pk).update(escalated_at=now)
                n += 1
            except Exception:
                pass
        return n

    # ── Site visits ───────────────────────────────────────────────────────
    def _sv_reminders(self, now, dry):
        qs = SiteVisit.objects.select_related('lead', 'stm', 'referred_by_telecaller').filter(
            status='scheduled', scheduled_at__isnull=False, scheduled_at__lt=now,
            reminder_sent_at__isnull=True,
        )
        n = 0
        for sv in qs.iterator():
            try:
                recips = [u for u in (sv.stm, sv.referred_by_telecaller)
                          if u and getattr(u, 'is_active', True)]
                if recips:
                    lead_name = sv.lead.name if sv.lead_id else 'a lead'
                    notify_many(recips, 'sv_overdue', 'Site visit overdue',
                                f'Site visit with {lead_name} was scheduled {_overdue_phrase(sv.scheduled_at, now)} '
                                'and is not marked done.',
                                {'lead_id': sv.lead_id, 'sv_id': sv.id})
                if not dry:
                    SiteVisit.objects.filter(pk=sv.pk).update(reminder_sent_at=now)
                n += 1
            except Exception:
                pass
        return n

    def _sv_escalations(self, now, cutoff, dry):
        qs = SiteVisit.objects.select_related('lead', 'stm', 'referred_by_telecaller').filter(
            status='scheduled', scheduled_at__isnull=False, scheduled_at__lt=cutoff,
            escalated_at__isnull=True,
        )
        n = 0
        for sv in qs.iterator():
            try:
                owner = sv.stm or sv.referred_by_telecaller
                mgrs = reporting_chain(owner) if owner else []
                if mgrs:
                    lead_name = sv.lead.name if sv.lead_id else 'a lead'
                    who = owner.name if owner else 'A rep'
                    notify_many(mgrs, 'sv_overdue', 'Team site visit overdue',
                                f"{who}'s site visit with {lead_name} is overdue "
                                f'({_overdue_phrase(sv.scheduled_at, now)}).',
                                {'lead_id': sv.lead_id, 'sv_id': sv.id})
                if not dry:
                    SiteVisit.objects.filter(pk=sv.pk).update(escalated_at=now)
                n += 1
            except Exception:
                pass
        return n

    # ── Mark-available (morning) ──────────────────────────────────────────
    def _availability_reminders(self, now, dry):
        from accounts.models import Notification
        local = timezone.localtime(now)
        today = local.date()
        n = 0
        # Group eligible reps by company so we only fetch dist_settings once each.
        reps = User.objects.select_related('company', 'company__dist_settings').filter(
            is_active=True, company__isnull=False,
        )
        for u in reps.iterator():
            try:
                if not _is_tc_or_stm(u):
                    continue
                ds = getattr(u.company, 'dist_settings', None)
                signin = dtime(10, 20)
                if ds:
                    signin = min(ds.tc_signin_time or signin, ds.stm_signin_time or signin)
                # Only nudge inside a 3h window after sign-in time.
                start = signin
                end = (timezone.datetime.combine(today, signin) + timedelta(hours=3)).time()
                if not (start <= local.time() < end):
                    continue
                if UserAvailability.objects.filter(user=u, date=today, is_available=True).exists():
                    continue
                if Notification.objects.filter(recipient=u, type='availability_reminder',
                                               created_at__date=today).exists():
                    continue
                notify(u, 'availability_reminder', 'Mark yourself available',
                       'Tap to mark available so today’s new leads route to you.', {})
                n += 1
            except Exception:
                pass
        return n
