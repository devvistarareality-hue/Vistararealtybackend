import logging
import secrets
from datetime import timedelta
import requests as http_requests
from django.db import transaction
from django.db.models import Q, Count
from django.utils import timezone
from django.http import HttpResponse
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated, AllowAny

logger = logging.getLogger(__name__)

from accounts.models import User
from accounts.permissions import is_platform_admin, scope_to_company


def _resolve_company(request):
    """Return the company for the request, honouring ?company_id for platform admins."""
    cid = request.query_params.get('company_id') or request.data.get('company_id')
    if cid and is_platform_admin(request.user):
        Company = __import__('companies.models', fromlist=['Company']).Company
        return Company.objects.filter(pk=cid).first() or request.user.company
    return request.user.company
from .models import (
    Lead, LeadSource, Project, Plot, FollowUp, SiteVisit, Closure, LeadStatusHistory,
    DistributionSettings, UserAvailability, UserDistributionWeight, DistributionLog,
    SalesTeamMember, MetaWebhookConfig, MetaFormMapping,
    UserProjectAssignment, Booking,
)
from .serializers import (
    LeadListSerializer, LeadDetailSerializer, LeadCreateSerializer, LeadUpdateSerializer,
    LeadSourceSerializer, ProjectSerializer, PlotSerializer,
    FollowUpSerializer, SiteVisitSerializer, ClosureSerializer,
    LeadStatusHistorySerializer, BookingSerializer,
)

PAGE_SIZE = 25


def is_admin_or_manager(user):
    return user.role in ('Admin', 'Manager') or user.is_staff


def _designation(user):
    return (getattr(user, 'designation', '') or '').lower()


def is_telecaller(user):
    d = _designation(user)
    return 'telecaller' in d or 'tele caller' in d


def is_stm(user):
    d = _designation(user)
    return 'stm' in d or 'sales team' in d or 'sales executive' in d


def is_cp(user):
    """CP Executive — an employee-level Channel Partner who sources & works their
    own leads (no Meta distribution). Scoped like an STM (by the lead's stm field)."""
    d = _designation(user)
    return 'cp executive' in d or 'channel partner' in d


# ── Hierarchy-based visibility ───────────────────────────────────────────────
# Data visibility is driven by the org tree (User.reporting_manager), NOT by
# designation strings. A user sees records owned (as STM or telecaller) by
# themselves or by anyone reporting to them, transitively. This scales to any
# designation/role without code changes — you only maintain reporting_manager.

def _sees_all_company(user):
    """Users who see ALL company data: platform admins, staff, the Admin role, and
    top-of-tree department heads (report to no one but manage others, e.g. a CMO)."""
    if is_platform_admin(user) or user.is_staff or getattr(user, 'role', '') == 'Admin':
        return True
    # Top of the tree: reports to nobody, but has active reports under them.
    if user.reporting_manager_id is None and User.objects.filter(
        company=user.company, reporting_manager_id=user.id, is_active=True
    ).exists():
        return True
    return False


def _visible_user_ids(user):
    """Requester's own id + every user reporting to them, transitively, in the same
    company. Cycle-safe (tracked via the `ids` set) and depth-capped."""
    ids = {user.id}
    frontier = [user.id]
    for _ in range(50):  # safety cap on tree depth
        children = list(
            User.objects.filter(
                company=user.company, reporting_manager_id__in=frontier, is_active=True
            ).exclude(id__in=ids).values_list('id', flat=True)
        )
        if not children:
            break
        ids.update(children)
        frontier = children
    return ids


def can_assign_leads(user):
    """Telecallers, STMs & CP Executives cannot (re)assign leads — only everyone
    else (admins/managers/Sales CRM)."""
    return not (is_telecaller(user) or is_stm(user) or is_cp(user))


def _dist_type_for(user):
    """'telecaller' | 'stm' | None for a user based on their designation."""
    if is_telecaller(user):
        return 'telecaller'
    if is_stm(user):
        return 'stm'
    return None


# Self-marked availability stays active for this many hours, then auto-resets.
AVAILABILITY_TTL_HOURS = 12



def _role_signout(company, designation):
    """Configured sign-out time for a TC/STM role, or None (no settings / other role)."""
    s = DistributionSettings.objects.filter(company=company).first()
    if not s:
        return None
    d = (designation or '').lower()
    if 'telecaller' in d or 'tele caller' in d:
        return s.tc_signout_time
    if 'stm' in d or 'sales team' in d or 'sales executive' in d:
        return s.stm_signout_time
    return None


def _availability_expires_at(user):
    """ISO timestamp when the user's availability auto-expires today = the role's
    sign-out time. None if no sign-out is configured (caller falls back to the TTL)."""
    signout = _role_signout(getattr(user, 'company', None), getattr(user, 'designation', ''))
    if signout is None:
        return None
    from zoneinfo import ZoneInfo
    from datetime import datetime as _dt
    tz = ZoneInfo('Asia/Kolkata')
    return _dt.combine(timezone.now().astimezone(tz).date(), signout, tzinfo=tz).isoformat()


def _availability_active(avail, user=None):
    """True if marked available *today* and it's still before the role's configured
    sign-out time — availability auto-expires at sign-out. Falls back to a 12h TTL
    when the company has no distribution sign-out configured."""
    if not avail or not avail.is_available or not avail.checked_in_at:
        return False
    from zoneinfo import ZoneInfo
    now_ist = timezone.now().astimezone(ZoneInfo('Asia/Kolkata'))
    if avail.date != now_ist.date():          # a stale prior-day record is expired
        return False
    u = user or avail.user
    signout = _role_signout(u.company, u.designation)
    if signout is None:                        # no sign-out configured → legacy 12h TTL
        return (timezone.now() - avail.checked_in_at) < timedelta(hours=AVAILABILITY_TTL_HOURS)
    return now_ist.time() < signout            # auto-expires at sign-out


def scope_leads_to_role(qs, user, lead_prefix=''):
    """Restrict a Lead-related queryset by org hierarchy: a user sees leads OWNED (as
    STM or telecaller) by themselves or by anyone reporting to them, transitively.
    Admins / staff / top-of-tree heads see all company data. `lead_prefix` lets callers
    scope related models (e.g. 'lead__' for SiteVisit / Closure)."""
    if _sees_all_company(user):
        return qs
    ids = _visible_user_ids(user)
    return qs.filter(
        Q(**{f'{lead_prefix}stm__in': ids}) | Q(**{f'{lead_prefix}telecaller__in': ids})
    )


def _lead_in_scope(request, lead_id):
    """True if the given lead belongs to the requester's company (or requester is platform admin)."""
    if not lead_id:
        return False
    return scope_to_company(Lead.objects.filter(pk=lead_id), request.user).exists()


def _project_in_scope(request, project_id):
    """True if the given project belongs to the requester's company (or requester is platform admin)."""
    if not project_id:
        return False
    return scope_to_company(Project.objects.filter(pk=project_id), request.user).exists()


class StatsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from django.core.cache import cache

        # Dashboard stats are ~5 COUNT/aggregate queries; cache briefly per
        # (user, company) so repeated dashboard loads don't re-hit Postgres.
        # 20s TTL keeps numbers near-live. Shared (consistent) once Redis is on.
        company_id = request.query_params.get('company_id')
        date_from  = request.query_params.get('date_from')
        date_to    = request.query_params.get('date_to')

        # Include date range in cache key so different date windows don't collide
        cache_key = f'sales_stats:{request.user.id}:{company_id or "own"}:{date_from or ""}:{date_to or ""}'
        cached = cache.get(cache_key)
        if cached is not None:
            return Response(cached)

        today = timezone.localdate()
        leads_qs = scope_to_company(Lead.objects.all(), request.user)

        # Telecallers / STMs only see stats for leads assigned to them.
        leads_qs = scope_leads_to_role(leads_qs, request.user)

        # Platform admin: filter by a specific company (used by admin company picker)
        if company_id and is_platform_admin(request.user):
            leads_qs   = leads_qs.filter(company_id=company_id)
            sv_filter  = {'lead__company_id': company_id}
            cl_filter  = {'lead__company_id': company_id}
            prj_filter = {'company_id': company_id}
        else:
            sv_filter = cl_filter = prj_filter = {}

        # Apply optional date range filter
        if date_from:
            leads_qs = leads_qs.filter(created_at__date__gte=date_from)
        if date_to:
            leads_qs = leads_qs.filter(created_at__date__lte=date_to)

        # Single aggregate query instead of 6 separate COUNTs
        agg = leads_qs.aggregate(
            total_leads=Count('id'),
            new_leads=Count('id', filter=Q(status='new')),
            leads_today=Count('id', filter=Q(created_at__date=today)),
            called_count=Count('id', filter=~Q(telecaller_status='') & Q(telecaller_status__isnull=False)),
            hot_count=Count('id', filter=Q(telecaller_status='hot')),
            warm_count=Count('id', filter=Q(telecaller_status='warm')),
            callback_count=Count('id', filter=Q(telecaller_status='callback')),
            not_reachable_count=Count('id', filter=Q(telecaller_status='not_reachable')),
            cold_count=Count('id', filter=Q(telecaller_status='cold')),
            # STM-pipeline counts (by stm_status) for the STM/CP dashboard.
            stm_hot_count=Count('id', filter=Q(stm_status='hot')),
            stm_warm_count=Count('id', filter=Q(stm_status='warm')),
            stm_cold_count=Count('id', filter=Q(stm_status='cold')),
            stm_sv_scheduled_count=Count('id', filter=Q(stm_status='sv_scheduled')),
        )
        sv_qs = scope_to_company(SiteVisit.objects.all(), request.user, 'lead__company')
        cl_qs = scope_to_company(Closure.objects.all(), request.user, 'lead__company')
        if not _sees_all_company(request.user):
            _ids = _visible_user_ids(request.user)
            sv_qs = sv_qs.filter(Q(stm__in=_ids) | Q(referred_by_telecaller__in=_ids))
            cl_qs = cl_qs.filter(Q(stm__in=_ids) | Q(referred_by_telecaller__in=_ids))
        if date_from:
            sv_qs = sv_qs.filter(created_at__date__gte=date_from)
            cl_qs = cl_qs.filter(closure_date__gte=date_from)
        if date_to:
            sv_qs = sv_qs.filter(created_at__date__lte=date_to)
            cl_qs = cl_qs.filter(closure_date__lte=date_to)
        sv_done, closures, active_projects = (
            sv_qs.filter(**sv_filter).count(),
            cl_qs.filter(**cl_filter).count(),
            scope_to_company(Project.objects.filter(is_active=True), request.user).filter(**prj_filter).count(),
        )
        # No .only() here: LeadListSerializer reads ~11 more fields (meta_*, statuses,
        # is_duplicate, …); deferring them caused a per-field query per lead (N+1).
        recent = leads_qs.select_related('project', 'source', 'telecaller', 'stm').order_by('-created_at')[:8]
        payload = {
            'total_leads':        agg['total_leads'],
            'new_leads':          agg['new_leads'],
            'leads_today':        agg['leads_today'],
            'called_count':       agg['called_count'],
            'hot_count':          agg['hot_count'],
            'warm_count':         agg['warm_count'],
            'callback_count':     agg['callback_count'],
            'not_reachable_count':agg['not_reachable_count'],
            'cold_count':         agg['cold_count'],
            'stm_hot_count':          agg['stm_hot_count'],
            'stm_warm_count':         agg['stm_warm_count'],
            'stm_cold_count':         agg['stm_cold_count'],
            'stm_sv_scheduled_count': agg['stm_sv_scheduled_count'],
            'sv_done':            sv_done,
            'closures':           closures,
            'active_projects':    active_projects,
            'recent_leads':       LeadListSerializer(recent, many=True).data,
        }
        cache.set(cache_key, payload, timeout=20)
        return Response(payload)


class StatsTrendView(APIView):
    """Daily MQL and SV counts for the last 30 days (or within date_from/date_to)."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from django.db.models.functions import TruncDate
        from datetime import date

        company_id = request.query_params.get('company_id')
        date_from  = request.query_params.get('date_from')
        date_to    = request.query_params.get('date_to')

        today = timezone.localdate()
        if not date_from:
            date_from = str(today - timedelta(days=29))
        if not date_to:
            date_to = str(today)

        leads_qs = scope_to_company(Lead.objects.all(), request.user)
        leads_qs = scope_leads_to_role(leads_qs, request.user)
        if company_id and is_platform_admin(request.user):
            leads_qs = leads_qs.filter(company_id=company_id)

        # MQL: leads that have been called, grouped by updated_at (when telecaller set the status)
        mql_rows = (
            leads_qs
            .filter(
                updated_at__date__gte=date_from,
                updated_at__date__lte=date_to,
                telecaller_status__isnull=False,
            )
            .exclude(telecaller_status='')
            .annotate(day=TruncDate('updated_at'))
            .values('day')
            .annotate(count=Count('id'))
            .order_by('day')
        )

        sv_qs = scope_to_company(SiteVisit.objects.all(), request.user, 'lead__company')
        if not _sees_all_company(request.user):
            ids = _visible_user_ids(request.user)
            sv_qs = sv_qs.filter(Q(stm__in=ids) | Q(referred_by_telecaller__in=ids))
        if company_id and is_platform_admin(request.user):
            sv_qs = sv_qs.filter(lead__company_id=company_id)

        # SV: site visits created per day
        sv_rows = (
            sv_qs
            .filter(created_at__date__gte=date_from, created_at__date__lte=date_to)
            .annotate(day=TruncDate('created_at'))
            .values('day')
            .annotate(count=Count('id'))
            .order_by('day')
        )

        # Warm/SQL: count by the date the lead actually BECAME warm — the status-history
        # entry where telecaller_status changed to 'warm' — not when the lead arrived or
        # was last edited (updated_at). Scoped to the same visible leads.
        warm_rows = (
            LeadStatusHistory.objects
            .filter(
                lead__in=leads_qs,
                field_changed='telecaller_status',
                new_value='warm',
                created_at__date__gte=date_from,
                created_at__date__lte=date_to,
            )
            .annotate(day=TruncDate('created_at'))
            .values('day')
            .annotate(count=Count('id'))
            .order_by('day')
        )

        return Response({
            'mql':  [{'date': str(r['day']), 'count': r['count']} for r in mql_rows],
            'sv':   [{'date': str(r['day']), 'count': r['count']} for r in sv_rows],
            'warm': [{'date': str(r['day']), 'count': r['count']} for r in warm_rows],
            'date_from': date_from,
            'date_to':   date_to,
        })


class LeadListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        # Defer heavy text blobs not needed for list view
        qs = scope_to_company(
            Lead.objects.select_related('project', 'source', 'telecaller', 'stm'),
            request.user,
        ).defer(
            'telecaller_remarks', 'stm_remarks', 'requirement',
            'preferred_location', 'budget_min', 'budget_max',
        )

        # Telecallers / STMs only see leads assigned to them.
        qs = scope_leads_to_role(qs, request.user)

        # Filters
        search = request.query_params.get('search', '').strip()
        if search:
            qs = qs.filter(Q(name__icontains=search) | Q(phone__icontains=search) | Q(email__icontains=search))

        if request.query_params.get('status'):
            qs = qs.filter(status=request.query_params['status'])
        if request.query_params.get('telecaller_status'):
            qs = qs.filter(telecaller_status=request.query_params['telecaller_status'])
        if request.query_params.get('stm_status'):
            qs = qs.filter(stm_status=request.query_params['stm_status'])
        project_id = request.query_params.get('project_id')
        if project_id == 'none':
            qs = qs.filter(project__isnull=True)   # unmapped leads (no project)
        elif project_id:
            qs = qs.filter(project_id=project_id)
        if request.query_params.get('source_id'):
            qs = qs.filter(source_id=request.query_params['source_id'])
        if request.query_params.get('telecaller_id'):
            qs = qs.filter(telecaller_id=request.query_params['telecaller_id'])
        if request.query_params.get('stm_id'):
            qs = qs.filter(stm_id=request.query_params['stm_id'])
        if request.query_params.get('is_duplicate') == 'true':
            qs = qs.filter(is_duplicate=True)
        if request.query_params.get('date_from'):
            qs = qs.filter(created_at__date__gte=request.query_params['date_from'])
        if request.query_params.get('date_to'):
            qs = qs.filter(created_at__date__lte=request.query_params['date_to'])
        if request.query_params.get('campaign'):
            qs = qs.filter(meta_campaign_name__icontains=request.query_params['campaign'])

        # Platform admin: filter by a specific company (used by admin company picker)
        if request.query_params.get('company_id') and is_platform_admin(request.user):
            qs = qs.filter(company_id=request.query_params['company_id'])

        # Work split for telecaller / STM portals: separate the leads they still have
        # to call ('pending') from the ones they've already actioned ('called'),
        # keyed off their own status field. Admins/managers fall back to overall status.
        work = request.query_params.get('work')
        if work == 'pending':
            if is_telecaller(request.user):
                qs = qs.filter(telecaller_status='')
            elif is_stm(request.user) or is_cp(request.user):
                qs = qs.filter(stm_status='')
            else:
                qs = qs.filter(status='new')
        elif work == 'called':
            if is_telecaller(request.user):
                qs = qs.exclude(telecaller_status='')
            elif is_stm(request.user) or is_cp(request.user):
                qs = qs.exclude(stm_status='')
            else:
                qs = qs.exclude(status='new')

        # Optional ordering override (default is newest-first from the model Meta).
        # 'pending' lists use oldest-first (FIFO) so fresh leads queue at the bottom
        # and never push down the lead currently being worked.
        ordering = request.query_params.get('ordering')
        if ordering in ('created_at', '-created_at', 'updated_at', '-updated_at'):
            qs = qs.order_by(ordering)

        total = qs.count()
        page = int(request.query_params.get('page', 1))
        offset = (page - 1) * PAGE_SIZE
        leads = qs[offset: offset + PAGE_SIZE]

        return Response({
            'count': total,
            'results': LeadListSerializer(leads, many=True).data,
        })

    def post(self, request):
        # Any authenticated Sales user (incl. telecallers) may add a lead.
        # Consistent with PATCH (lead update), which has no admin/manager gate.
        # Only admins/managers may assign a telecaller/STM on create; strip those
        # fields for callers (they self-source) so they can't assign to others.
        data = {k: v for k, v in request.data.items()}
        can_assign = can_assign_leads(request.user)
        if not can_assign:
            data.pop('telecaller', None)
            data.pop('stm', None)
        ser = LeadCreateSerializer(data=data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)

        company = request.user.company

        # If a project is supplied it must belong to the requester's company.
        project = ser.validated_data.get('project')
        if project and not _project_in_scope(request, project.id):
            return Response({'detail': 'Invalid project for your company.'}, status=status.HTTP_400_BAD_REQUEST)

        # Duplicate check — match last 10 digits regardless of +91 prefix. Scoped to
        # the creator's own bucket (telecaller→their leads, STM/CP→their leads) so a
        # CP's lead is only a duplicate of another CP lead, not of someone else's.
        # Admins/managers keep the company-wide check.
        phone = ser.validated_data['phone']
        clean = ''.join(c for c in phone if c.isdigit())[-10:]
        dup_qs = (
            scope_leads_to_role(scope_to_company(Lead.objects.all(), request.user), request.user)
            .filter(phone__endswith=clean)
            if clean else Lead.objects.none()
        )
        existing = dup_qs.first()

        # Self-sourced (manually added) leads are assigned to their creator so they
        # land in that person's pipeline, and are marked actioned (status defaults to
        # 'warm' if none given) so they appear in the "Called" bucket, not "To Call" —
        # the creator already has the contact, there's nothing to call fresh.
        extra = {}
        if not can_assign:
            # Callers self-source: own the lead + mark actioned → their "Called" bucket.
            if is_cp(request.user) or is_stm(request.user):
                extra['stm'] = request.user
                if not ser.validated_data.get('stm_status'):
                    extra['stm_status'] = 'warm'
            elif is_telecaller(request.user):
                extra['telecaller'] = request.user
                if not ser.validated_data.get('telecaller_status'):
                    # 'callback' (not 'warm') so a blank status lands in the telecaller's
                    # "Called" bucket WITHOUT auto-transferring — 'warm' is a deliberate
                    # transfer-to-STM action handled below.
                    extra['telecaller_status'] = 'callback'
        else:
            # Admin/manager assigned via the form → stamp assignment time. Status is
            # left empty so the lead lands in the assignee's "To Call" bucket.
            if ser.validated_data.get('telecaller'):
                extra['telecaller_assigned_at'] = timezone.now()
            if ser.validated_data.get('stm'):
                extra['stm_assigned_at'] = timezone.now()

        lead = ser.save(
            company=company,
            is_duplicate=bool(existing),
            duplicate_of=existing if existing else None,
            **extra,
        )
        if existing:
            existing.duplicate_count += 1
            existing.save(update_fields=['duplicate_count'])

        _record_lead_created(lead, by=request.user)
        # Notify the assignee when an admin/manager hand-picks them on create.
        if can_assign:
            from notifications import notify
            if lead.telecaller_id:
                notify(lead.telecaller, 'new_lead', 'New Lead Assigned',
                       f'{lead.name} has been assigned to you.', {'lead_id': lead.id})
            if lead.stm_id:
                notify(lead.stm, 'new_lead', 'New Lead Assigned',
                       f'{lead.name} has been assigned to you.', {'lead_id': lead.id})
        # Telecaller marked the new lead "warm" → warm-transfer into the STM pipeline
        # (mirrors the PATCH behaviour): overall status = warm_transferred, then
        # auto-assign an STM. Applies whether warm came from a caller or an admin form.
        if lead.telecaller_status == 'warm' and lead.status != 'warm_transferred':
            lead.status = 'warm_transferred'
            lead.save(update_fields=['status'])
            LeadStatusHistory.objects.create(
                lead=lead, changed_by=request.user,
                field_changed='warm_transfer', old_value='', new_value='Transferred to STM',
            )
        if lead.status == 'warm_transferred' and lead.stm_id is None:
            _run_distribution(lead.company, 'stm')
        # Auto-distribute to a telecaller only when the lead is still unassigned
        # (admin didn't pick one and it isn't self-sourced / warm-transferred).
        elif not lead.telecaller_id and not lead.stm_id:
            _run_distribution(company, 'telecaller')
        lead.refresh_from_db()

        return Response(LeadDetailSerializer(lead).data, status=status.HTTP_201_CREATED)


class LeadDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def _get_lead(self, request, pk):
        try:
            qs = scope_to_company(
                Lead.objects.select_related('project', 'source', 'telecaller', 'stm'),
                request.user,
            )
            # Telecallers / STMs can only open leads assigned to them.
            qs = scope_leads_to_role(qs, request.user)
            return qs.get(pk=pk)
        except Lead.DoesNotExist:
            return None

    def get(self, request, pk):
        lead = self._get_lead(request, pk)
        if not lead:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        data = LeadDetailSerializer(lead).data
        # Most recent 30 events, returned oldest→newest so the timeline reads in order.
        # Tie-break by id keeps same-second events in their logical creation order
        # (e.g. status change → warm transfer → STM assigned).
        recent = list(lead.history.order_by('-created_at', '-id')[:30])
        recent.reverse()
        data['history'] = LeadStatusHistorySerializer(recent, many=True).data
        data['follow_ups'] = FollowUpSerializer(lead.follow_ups.all(), many=True).data
        data['site_visits'] = SiteVisitSerializer(lead.site_visits.all(), many=True).data
        return Response(data)

    def patch(self, request, pk):
        lead = self._get_lead(request, pk)
        if not lead:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

        old_status       = lead.status
        old_tc_status    = lead.telecaller_status
        old_stm_status   = lead.stm_status
        old_tc_id        = lead.telecaller_id
        old_stm_id       = lead.stm_id
        old_tc_name      = lead.telecaller.name if lead.telecaller else ''
        old_stm_name     = lead.stm.name        if lead.stm        else ''

        # Field-level write restrictions (mirrors the portal UI):
        #  - Telecallers may only write telecaller (TC) fields.
        #  - STMs may only write STM fields.
        #  - Neither may (re)assign leads. Admins/managers/Sales CRM may edit everything.
        data = {k: v for k, v in request.data.items()}
        if not can_assign_leads(request.user):
            for f in ('telecaller', 'stm'):
                data.pop(f, None)
        if is_telecaller(request.user):
            for f in ('stm', 'stm_status', 'stm_remarks'):
                data.pop(f, None)
        elif is_stm(request.user):
            for f in ('telecaller', 'telecaller_status', 'telecaller_remarks'):
                data.pop(f, None)

        ser = LeadUpdateSerializer(lead, data=data, partial=True)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        lead = ser.save()

        # A lead is "warm" when EITHER the telecaller sets TC Status = warm OR the
        # overall status is set to warm_transferred. Keep both in sync so the TC Status
        # column always shows 'warm' and Overall always shows 'warm_transferred',
        # then hand the lead to the STM pipeline. (TC's warm ≠ STM status — stm_status
        # stays blank.)
        warm_now = (
            (old_tc_status != 'warm' and lead.telecaller_status == 'warm') or
            (old_status != 'warm_transferred' and lead.status == 'warm_transferred')
        )
        if warm_now:
            sync = []
            if lead.status != 'warm_transferred':
                lead.status = 'warm_transferred'; sync.append('status')
            if lead.telecaller_status != 'warm':
                lead.telecaller_status = 'warm'; sync.append('telecaller_status')
            if sync:
                lead.save(update_fields=sync)

        # Once the lead is with sales, the Overall Status mirrors the STM's status
        # exactly (assigned → on TC assignment; warm_transferred → on TC warm; then
        # whatever the STM sets — cold, sv_scheduled, sv_done, closed, …).
        if lead.stm_status and old_stm_status != lead.stm_status:
            if lead.status != lead.stm_status:
                lead.status = lead.stm_status
                lead.save(update_fields=['status'])

        history_entries = []
        if old_status != lead.status:
            history_entries.append(LeadStatusHistory(
                lead=lead, changed_by=request.user,
                field_changed='status', old_value=old_status, new_value=lead.status,
            ))
        if old_tc_status != lead.telecaller_status:
            history_entries.append(LeadStatusHistory(
                lead=lead, changed_by=request.user,
                field_changed='telecaller_status', old_value=old_tc_status, new_value=lead.telecaller_status,
            ))
        if old_stm_status != lead.stm_status:
            history_entries.append(LeadStatusHistory(
                lead=lead, changed_by=request.user,
                field_changed='stm_status', old_value=old_stm_status, new_value=lead.stm_status,
            ))
        if old_tc_id != lead.telecaller_id:
            new_tc_name = lead.telecaller.name if lead.telecaller else ''
            history_entries.append(LeadStatusHistory(
                lead=lead, changed_by=request.user,
                field_changed='telecaller', old_value=old_tc_name, new_value=new_tc_name,
            ))
            if lead.telecaller:
                from notifications import notify
                notify(lead.telecaller, 'new_lead', 'New Lead Assigned',
                       f'{lead.name} has been assigned to you.', {'lead_id': lead.id})
        if old_stm_id != lead.stm_id:
            new_stm_name = lead.stm.name if lead.stm else ''
            history_entries.append(LeadStatusHistory(
                lead=lead, changed_by=request.user,
                field_changed='stm', old_value=old_stm_name, new_value=new_stm_name,
            ))
            if lead.stm:
                from notifications import notify
                notify(lead.stm, 'new_lead', 'New Lead Assigned',
                       f'{lead.name} has been assigned to you.', {'lead_id': lead.id})
        if warm_now:
            history_entries.append(LeadStatusHistory(
                lead=lead, changed_by=request.user,
                field_changed='warm_transfer', old_value='', new_value='Transferred to STM',
            ))
        if history_entries:
            LeadStatusHistory.objects.bulk_create(history_entries)

        # Auto-assign whenever the lead is in the warm bucket and has no STM yet —
        # whether it got there via TC Status = warm OR by setting Overall Status
        # to 'warm_transferred' directly. Window-gated; no-op if no STM available.
        if lead.status == 'warm_transferred' and lead.stm_id is None:
            _run_distribution(lead.company, 'stm')
            lead.refresh_from_db()

        return Response(LeadDetailSerializer(lead).data)

    def delete(self, request, pk):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        lead = self._get_lead(request, pk)
        if not lead:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        lead.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class BulkDeleteLeadsView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        ids = request.data.get('ids', [])
        if not ids:
            return Response({'detail': 'No IDs provided.'}, status=status.HTTP_400_BAD_REQUEST)
        deleted, _ = scope_to_company(Lead.objects.filter(id__in=ids), request.user).delete()
        return Response({'deleted': deleted})


def _sync_plots(project):
    existing_count = project.plots.count()
    target = project.total_plots or 0
    # Only auto-create numbered plots if NO plots exist yet.
    # This prevents re-triggering on PATCH (e.g. after bulk typed-plot creation).
    if target > 0 and existing_count == 0:
        Plot.objects.bulk_create([
            Plot(project=project, number=str(i))
            for i in range(1, target + 1)
        ])


class ProjectListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        projects = scope_to_company(
            Project.objects.annotate(lead_count=Count('leads')).prefetch_related('plots'),
            request.user,
        )
        if request.query_params.get('active_only') == 'true':
            projects = projects.filter(is_active=True)
        if request.query_params.get('company_id') and is_platform_admin(request.user):
            projects = projects.filter(company_id=request.query_params['company_id'])
        return Response(ProjectSerializer(projects, many=True).data)

    def post(self, request):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        ser = ProjectSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        project = ser.save(company=request.user.company)
        _sync_plots(project)
        project = Project.objects.annotate(lead_count=Count('leads')).prefetch_related('plots').get(pk=project.pk)
        return Response(ProjectSerializer(project).data, status=status.HTTP_201_CREATED)


class ProjectDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        try:
            project = scope_to_company(
                Project.objects.annotate(lead_count=Count('leads')).prefetch_related('plots'),
                request.user,
            ).get(pk=pk)
        except Project.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(ProjectSerializer(project).data)

    def patch(self, request, pk):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        try:
            project = scope_to_company(Project.objects.all(), request.user).get(pk=pk)
        except Project.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        ser = ProjectSerializer(project, data=request.data, partial=True)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        project = ser.save()
        # _sync_plots intentionally NOT called on PATCH — plots are managed via /plots/bulk/
        project = Project.objects.annotate(lead_count=Count('leads')).prefetch_related('plots').get(pk=project.pk)
        return Response(ProjectSerializer(project).data)

    def delete(self, request, pk):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        try:
            project = scope_to_company(Project.objects.all(), request.user).get(pk=pk)
        except Project.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        project.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class PlotListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        project_id = request.query_params.get('project')
        if not project_id or not str(project_id).isdigit():
            return Response({'detail': 'A valid numeric project query param is required.'}, status=status.HTTP_400_BAD_REQUEST)
        if not _project_in_scope(request, project_id):
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        plots = Plot.objects.filter(project_id=project_id)
        return Response(PlotSerializer(plots, many=True).data)


class PlotDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        try:
            plot = scope_to_company(Plot.objects.all(), request.user, 'project__company').get(pk=pk)
        except Plot.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        ser = PlotSerializer(plot, data=request.data, partial=True)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        return Response(PlotSerializer(ser.save()).data)


class LeadSourceListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        sources = scope_to_company(LeadSource.objects.filter(is_active=True), request.user)
        if request.query_params.get('company_id') and is_platform_admin(request.user):
            sources = sources.filter(company_id=request.query_params['company_id'])
        return Response(LeadSourceSerializer(sources, many=True).data)

    def post(self, request):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        ser = LeadSourceSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        return Response(LeadSourceSerializer(ser.save(company=request.user.company)).data, status=status.HTTP_201_CREATED)


class BackfillDuplicatesView(APIView):
    """One-time endpoint to mark existing duplicate leads based on last 10 phone digits."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=403)
        from collections import defaultdict
        # Stream rows with .iterator() so the whole Lead table is never materialised in
        # memory at once (prevents OOM on large tenants). Only id/phone are accumulated.
        leads = (
            scope_to_company(Lead.objects.all(), request.user)
            .only('id', 'phone', 'created_at')
            .order_by('created_at')
            .iterator(chunk_size=2000)
        )
        phone_map = defaultdict(list)
        for l in leads:
            clean = ''.join(c for c in (l.phone or '') if c.isdigit())[-10:]
            if clean:
                phone_map[clean].append(l.id)
        marked = 0
        for clean, ids in phone_map.items():
            if len(ids) > 1:
                original_id = ids[0]
                dup_ids = ids[1:]
                Lead.objects.filter(id__in=dup_ids).update(is_duplicate=True, duplicate_of_id=original_id)
                Lead.objects.filter(id=original_id).update(duplicate_count=len(dup_ids))
                marked += len(dup_ids)
        return Response({'marked_duplicates': marked})


class LeadSourceDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, pk):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        try:
            source = scope_to_company(LeadSource.objects.all(), request.user).get(pk=pk)
        except LeadSource.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        source.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class FollowUpListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = scope_to_company(
            FollowUp.objects.select_related('lead', 'assigned_to'),
            request.user, 'lead__company',
        )
        if not _sees_all_company(request.user):
            qs = qs.filter(assigned_to__in=_visible_user_ids(request.user))
        if request.query_params.get('company_id') and is_platform_admin(request.user):
            qs = qs.filter(lead__company_id=request.query_params['company_id'])
        if request.query_params.get('lead_id'):
            qs = qs.filter(lead_id=request.query_params['lead_id'])
        if request.query_params.get('status'):
            qs = qs.filter(status=request.query_params['status'])
        return Response(FollowUpSerializer(qs, many=True).data)

    def post(self, request):
        ser = FollowUpSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        if not _lead_in_scope(request, request.data.get('lead')):
            return Response({'detail': 'Invalid lead for your company.'}, status=status.HTTP_400_BAD_REQUEST)
        followup = ser.save(created_by=request.user)
        if followup.assigned_to and followup.assigned_to_id != request.user.id:
            from notifications import notify
            when = followup.scheduled_at.strftime('%d %b %I:%M %p') if followup.scheduled_at else ''
            notify(followup.assigned_to, 'followup', 'New Follow-Up',
                   (f'{followup.lead.name} · {when}').strip(' ·'),
                   {'lead_id': followup.lead_id, 'followup_id': followup.id})
        return Response(FollowUpSerializer(followup).data, status=status.HTTP_201_CREATED)


class FollowUpDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        try:
            followup = scope_to_company(FollowUp.objects.all(), request.user, 'lead__company').get(pk=pk)
        except FollowUp.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        ser = FollowUpSerializer(followup, data=request.data, partial=True)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        return Response(FollowUpSerializer(ser.save()).data)


class SiteVisitListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = scope_to_company(
            SiteVisit.objects.select_related('lead', 'project', 'stm'),
            request.user, 'lead__company',
        )
        if not _sees_all_company(request.user):
            _ids = _visible_user_ids(request.user)
            qs = qs.filter(Q(stm__in=_ids) | Q(referred_by_telecaller__in=_ids))
        if request.query_params.get('lead_id'):
            qs = qs.filter(lead_id=request.query_params['lead_id'])
        return Response(SiteVisitSerializer(qs, many=True).data)

    def post(self, request):
        ser = SiteVisitSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        if not _lead_in_scope(request, request.data.get('lead')):
            return Response({'detail': 'Invalid lead for your company.'}, status=status.HTTP_400_BAD_REQUEST)
        sv = ser.save()
        sched = sv.scheduled_at.strftime('%d %b %I:%M %p') if sv.scheduled_at else ''
        LeadStatusHistory.objects.create(
            lead=sv.lead, changed_by=request.user, field_changed='site_visit',
            old_value='', new_value=(f'Scheduled · {sched}' if sched else 'Scheduled')[:100],
            remarks='Site visit scheduled',
        )
        from notifications import notify
        for who in (sv.stm, sv.referred_by_telecaller):
            if who and who.id != request.user.id:
                notify(who, 'sv', 'Site Visit Scheduled',
                       (f'{sv.lead.name} · {sched}').strip(' ·'),
                       {'lead_id': sv.lead_id, 'sv_id': sv.id})
        return Response(SiteVisitSerializer(sv).data, status=status.HTTP_201_CREATED)


class SiteVisitDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        try:
            sv = scope_to_company(SiteVisit.objects.all(), request.user, 'lead__company').get(pk=pk)
        except SiteVisit.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        old_status = sv.status
        ser = SiteVisitSerializer(sv, data=request.data, partial=True)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        sv = ser.save()
        if sv.status != old_status:
            LeadStatusHistory.objects.create(
                lead=sv.lead, changed_by=request.user, field_changed='site_visit',
                old_value=old_status, new_value=sv.get_status_display(),
                remarks='Site visit updated',
            )
            if sv.status == 'completed':
                # Telecaller who referred the lead + the STM both hear that the SV is done.
                from notifications import notify
                for who in (sv.referred_by_telecaller, sv.stm):
                    if who and who.id != request.user.id:
                        notify(who, 'sv_done', 'Site Visit Done',
                               f"{sv.lead.name}'s site visit is complete.",
                               {'lead_id': sv.lead_id, 'sv_id': sv.id})
        return Response(SiteVisitSerializer(sv).data)


class ClosureListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = scope_to_company(
            Closure.objects.select_related('lead', 'project', 'stm'),
            request.user, 'lead__company',
        )
        if not _sees_all_company(request.user):
            _ids = _visible_user_ids(request.user)
            qs = qs.filter(Q(stm__in=_ids) | Q(referred_by_telecaller__in=_ids))
        return Response(ClosureSerializer(qs, many=True).data)

    def post(self, request):
        ser = ClosureSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        if not _lead_in_scope(request, request.data.get('lead')):
            return Response({'detail': 'Invalid lead for your company.'}, status=status.HTTP_400_BAD_REQUEST)
        closure = ser.save()
        parts = [closure.get_status_display()]
        unit = f'{closure.unit_type} {closure.unit_no}'.strip()
        if unit:
            parts.append(unit)
        if closure.total_amount:
            parts.append(f'₹{closure.total_amount:g}')
        LeadStatusHistory.objects.create(
            lead=closure.lead, changed_by=request.user, field_changed='closure',
            old_value='', new_value=' · '.join(parts)[:100], remarks='Closure recorded',
        )
        if closure.stm:
            from notifications import notify_many, reporting_chain
            notify_many(reporting_chain(closure.stm), 'closure', 'New Closure',
                        (f'{closure.stm.name} closed {closure.lead.name} · {unit}').strip(' ·'),
                        {'lead_id': closure.lead_id, 'closure_id': closure.id})
        return Response(ClosureSerializer(closure).data, status=status.HTTP_201_CREATED)


class TelecallerListView(APIView):
    """Users for lead assignment. Filters by User.designation icontains crm_role param.
    Falls back to all Sales-module users if no designation match found."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        crm_role = request.query_params.get('crm_role')
        cid      = request.query_params.get('company_id')
        if is_platform_admin(request.user):
            if cid:
                from companies.models import Company as Co
                co = Co.objects.filter(pk=cid).first()
                base_qs = User.objects.filter(company=co, is_active=True) if co else User.objects.none()
            else:
                base_qs = User.objects.filter(is_active=True)
        else:
            base_qs = User.objects.filter(company=request.user.company, is_active=True)
        sales_qs = base_qs.filter(modules__contains=['Sales']).order_by('name')

        if crm_role in ('telecaller', 'stm'):
            users = base_qs.filter(designation__icontains=crm_role).order_by('name')
            if not users.exists():
                users = sales_qs
        elif crm_role == 'cp':
            # CP executives (channel partners) — for CP managers assigning leads.
            users = base_qs.filter(
                Q(designation__icontains='cp executive') | Q(designation__icontains='channel partner')
            ).order_by('name')
            if not users.exists():
                users = sales_qs
        else:
            users = sales_qs

        data = [
            {'id': u.id, 'name': u.name, 'user_code': u.user_code, 'role': u.role, 'designation': u.designation}
            for u in users
        ]
        return Response(data)


class CompanyUsersSlimView(APIView):
    """Lightweight user list for Sales CRM — only fields the UI needs, no heavy JSONField serialization."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        company_id = request.query_params.get('company_id')
        company = (
            __import__('companies.models', fromlist=['Company']).Company.objects.filter(pk=company_id).first()
            if company_id and is_platform_admin(request.user)
            else request.user.company
        )
        users = (
            User.objects
            .filter(company=company, is_active=True)
            .exclude(role='Admin')
            .only('id', 'name', 'user_code', 'designation', 'role', 'phone', 'email')
            .order_by('name')
        )
        data = [{
            'id':          u.id,
            'name':        u.name,
            'user_code':   u.user_code,
            'designation': u.designation,
            'role':        u.role,
            'phone':       u.phone,
            'email':       u.email,
        } for u in users]
        return Response(data)


# ── Sales Team Members ──────────────────────────────────────────────────────
# models already imported at top of file


class SalesTeamView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        cid = request.query_params.get('company_id')
        if is_platform_admin(request.user):
            if cid:
                from companies.models import Company as Co
                company = Co.objects.filter(pk=cid).first()
                users = User.objects.filter(company=company, is_active=True, department__icontains='sales') if company else User.objects.none()
            else:
                users = User.objects.filter(is_active=True, department__icontains='sales')
        else:
            users = User.objects.filter(company=request.user.company, is_active=True, department__icontains='sales')
        users = users.order_by('name')

        data = [{
            'id':          u.id,
            'name':        u.name,
            'email':       u.email,
            'phone':       u.phone,
            'user_code':   u.user_code,
            'designation': u.designation,
            'role':        u.role,
        } for u in users]
        return Response(data)

    def post(self, request):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        user_id  = request.data.get('user_id')
        crm_role = request.data.get('crm_role', 'telecaller')
        try:
            user = User.objects.get(pk=user_id, company=request.user.company)
        except User.DoesNotExist:
            return Response({'detail': 'User not found in your company.'}, status=status.HTTP_404_NOT_FOUND)
        member, created = SalesTeamMember.objects.get_or_create(user=user, defaults={'crm_role': crm_role})
        if not created:
            member.crm_role  = crm_role
            member.is_active = True
            member.save()
        return Response({'id': member.id, 'user_id': user.id, 'name': user.name, 'crm_role': member.crm_role, 'designation': user.designation, 'user_code': user.user_code}, status=status.HTTP_201_CREATED)


class SalesTeamMemberDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        try:
            m = SalesTeamMember.objects.get(pk=pk, user__company=request.user.company)
        except SalesTeamMember.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        if 'crm_role' in request.data:
            m.crm_role = request.data['crm_role']
        if 'is_active' in request.data:
            m.is_active = request.data['is_active']
        m.save()
        return Response({'id': m.id, 'crm_role': m.crm_role, 'is_active': m.is_active})

    def delete(self, request, pk):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        try:
            m = SalesTeamMember.objects.get(pk=pk, user__company=request.user.company)
        except SalesTeamMember.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        m.is_active = False
        m.save()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ── Distribution Settings ─────────────────────────────────────────────────────
class DistributionSettingsView(APIView):
    permission_classes = [IsAuthenticated]

    def _get_or_create(self, company):
        obj, _ = DistributionSettings.objects.get_or_create(company=company)
        return obj

    def get(self, request):
        company = _resolve_company(request)
        s = self._get_or_create(company)
        managers = list(
            User.objects.filter(company=company, is_active=True, role='Manager')
            .order_by('name').values('id', 'name', 'designation')
        )
        return Response({
            'tc_signin_time':   str(s.tc_signin_time)[:5],
            'tc_signout_time':  str(s.tc_signout_time)[:5],
            'stm_signin_time':  str(s.stm_signin_time)[:5],
            'stm_signout_time': str(s.stm_signout_time)[:5],
            'managers': managers,   # for the per-project booking-approver picker
        })

    def put(self, request):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        s = self._get_or_create(_resolve_company(request))
        for field in ('tc_signin_time', 'tc_signout_time', 'stm_signin_time', 'stm_signout_time'):
            if field in request.data:
                setattr(s, field, request.data[field])
        s.save()
        return Response({'detail': 'Saved.'})


# ── Availability ──────────────────────────────────────────────────────────────
class AvailabilityView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from datetime import date as date_cls
        today = request.query_params.get('date', str(date_cls.today()))
        company = _resolve_company(request)
        desig_map = {'TELECALLER': 'telecaller', 'STM': 'stm'}
        users = (
            User.objects
            .filter(company=company, is_active=True)
            .exclude(role='Admin')
            .filter(designation__in=['TELECALLER', 'STM'])
            .only('id', 'name', 'designation')
            .order_by('name')
        )
        avail_map = {}
        checkin_map = {}
        for a in UserAvailability.objects.filter(user__company=request.user.company, date=today).select_related('user', 'user__company'):
            active = _availability_active(a)
            avail_map[a.user_id] = active
            if active and a.checked_in_at:
                checkin_map[a.user_id] = a.checked_in_at.isoformat()
        # Assigned projects per user (for the availability label).
        proj_map: dict[int, list] = {}
        for uid, pname in (
            UserProjectAssignment.objects
            .filter(user__in=users)
            .values_list('user_id', 'project__name')
        ):
            proj_map.setdefault(uid, []).append(pname)
        data = []
        for u in users:
            data.append({
                'user_id':      u.id,
                'name':         u.name,
                'role':         desig_map.get(u.designation.upper(), u.designation.lower()),
                'is_available': avail_map.get(u.id, False),
                'checked_in_at': checkin_map.get(u.id),
                'projects':     proj_map.get(u.id, []),
            })
        return Response(data)

    def post(self, request):
        """Admin toggles any user's availability for today (by user_id)."""
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        from datetime import date as date_cls
        user_id      = request.data.get('user_id')
        is_available = request.data.get('is_available', True)
        today        = str(date_cls.today())
        company      = _resolve_company(request)
        try:
            user = User.objects.get(pk=user_id, company=company)
        except User.DoesNotExist:
            return Response({'detail': 'User not found.'}, status=404)
        obj, _ = UserAvailability.objects.update_or_create(
            user=user, date=today,
            defaults={'is_available': is_available, 'checked_in_at': timezone.now() if is_available else None},
        )
        # Marking available flushes the unassigned bucket to this role (window-gated).
        dist_type = _dist_type_for(user)
        if obj.is_available and dist_type:
            _run_distribution(user.company, dist_type)
        return Response({'user_id': user.id, 'is_available': obj.is_available})


class MyAvailabilityView(APIView):
    """Self-service availability for telecallers / STMs.
    Marking available stays active for AVAILABILITY_TTL_HOURS, then auto-resets."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from datetime import date as date_cls
        today = str(date_cls.today())
        avail = UserAvailability.objects.filter(user=request.user, date=today).first()
        active = _availability_active(avail, request.user)
        expires_at = None
        if active:
            # Auto-expires at the role sign-out time; fall back to the TTL if unset.
            expires_at = _availability_expires_at(request.user)
            if expires_at is None and avail and avail.checked_in_at:
                expires_at = (avail.checked_in_at + timedelta(hours=AVAILABILITY_TTL_HOURS)).isoformat()
        return Response({
            'is_available':  active,
            'checked_in_at': avail.checked_in_at.isoformat() if (avail and avail.checked_in_at) else None,
            'expires_at':    expires_at,
            'ttl_hours':     AVAILABILITY_TTL_HOURS,
        })

    def post(self, request):
        from datetime import date as date_cls
        if not (is_telecaller(request.user) or is_stm(request.user)):
            return Response({'detail': 'Only telecallers and STMs can mark their own availability.'},
                            status=status.HTTP_403_FORBIDDEN)
        is_available = request.data.get('is_available', True)
        today = str(date_cls.today())
        obj, _ = UserAvailability.objects.update_or_create(
            user=request.user, date=today,
            defaults={'is_available': is_available, 'checked_in_at': timezone.now() if is_available else None},
        )
        active = _availability_active(obj, request.user)
        # Marking available flushes the unassigned bucket to this user's role (window-gated).
        if active:
            _run_distribution(request.user.company, _dist_type_for(request.user))
        expires_at = None
        if active:
            expires_at = _availability_expires_at(request.user)
            if expires_at is None and obj.checked_in_at:
                expires_at = (obj.checked_in_at + timedelta(hours=AVAILABILITY_TTL_HOURS)).isoformat()
        return Response({'is_available': active, 'expires_at': expires_at, 'ttl_hours': AVAILABILITY_TTL_HOURS})


# ── Distribution Weights ──────────────────────────────────────────────────────
class DistributionWeightView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        company = _resolve_company(request)
        users = (
            User.objects
            .filter(company=company, is_active=True, designation__in=['TELECALLER', 'STM'])
            .only('id', 'name', 'designation')
        )
        weight_map = {
            w.user_id: w.weight
            for w in UserDistributionWeight.objects.filter(user__company=company)
        }
        return Response([
            {'user_id': u.id, 'name': u.name, 'role': u.designation.upper(), 'weight': weight_map.get(u.id, 1)}
            for u in users
        ])

    def patch(self, request):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        company = _resolve_company(request)
        updates = request.data.get('updates', [])  # [{user_id, weight}]
        for item in updates:
            uid = item.get('user_id')
            w   = max(1, int(item.get('weight', 1)))
            try:
                user = User.objects.get(pk=uid, company=company)
                UserDistributionWeight.objects.update_or_create(user=user, defaults={'weight': w})
            except User.DoesNotExist:
                pass
        return Response({'detail': 'Weights saved.'})


# ── Distribution ─────────────────────────────────────────────────────────────
def _window_state(company, dist_type):
    """Return 'open' | 'before_signin' | 'after_signout' for the company's
    sign-in/sign-out window (IST). No settings → treated as 'open'."""
    from zoneinfo import ZoneInfo
    settings = DistributionSettings.objects.filter(company=company).first()
    if not settings:
        return 'open'
    field_prefix = 'tc' if dist_type == 'telecaller' else 'stm'
    now_ist = timezone.now().astimezone(ZoneInfo('Asia/Kolkata')).strftime('%H:%M')
    signin  = str(getattr(settings, f'{field_prefix}_signin_time'))[:5]
    signout = str(getattr(settings, f'{field_prefix}_signout_time'))[:5]
    if now_ist < signin:
        return 'before_signin'
    if now_ist >= signout:
        return 'after_signout'
    return 'open'


def _run_distribution(company, dist_type, triggered_by=None, gate='full'):
    """Weighted, project-aware, window-gated assignment of the current unassigned
    bucket to available telecallers/STMs. Reusable by both the manual Distribute
    button and the automatic triggers (lead created / marked available / warm).

    gate='full'    → only runs when the window is 'open' (auto-assignment).
    gate='signout' → runs unless 'after_signout' (manual admin override).

    triggered_by=None marks the assignment as automatic ("System") in history.
    Returns the same dict shape the API has always returned.
    """
    from datetime import date as date_cls

    desig = 'TELECALLER' if dist_type == 'telecaller' else 'STM'

    state = _window_state(company, dist_type)
    if state == 'after_signout':
        return {'distributed': 0, 'message': f'Distribution window closed for {desig}. Leads remain unassigned.'}
    if gate == 'full' and state != 'open':
        return {'distributed': 0, 'message': f'Outside {desig} distribution window. Leads remain unassigned.'}

    today = str(date_cls.today())

    # Users marked available today. Availability auto-expires at the role's sign-out
    # time, which the window gate above already enforces (distribution never runs
    # after sign-out), so a same-day check-in stays valid through the whole window.
    avail_ids = set(
        UserAvailability.objects.filter(
            user__company=company,
            user__designation__iexact=desig,
            date=today,
            is_available=True,
        ).values_list('user_id', flat=True)
    )
    if not avail_ids:
        return {'distributed': 0, 'message': f'No {desig}s have marked available today.'}

    members = list(User.objects.filter(pk__in=avail_ids, is_active=True).only('id', 'name'))
    if not members:
        return {'distributed': 0, 'message': f'No active {desig} users available.'}

    weight_map = {
        w.user_id: w.weight
        for w in UserDistributionWeight.objects.filter(user__in=members)
    }

    with transaction.atomic():
        # Lock unassigned leads row-by-row so concurrent distribution calls
        # (auto + manual firing simultaneously) can't grab the same leads.
        company_leads = Lead.objects.filter(company=company)
        if dist_type == 'telecaller':
            qs = company_leads.filter(telecaller__isnull=True, status='new').select_for_update(skip_locked=True).order_by('created_at')
        else:
            qs = company_leads.filter(status='warm_transferred', stm__isnull=True).select_for_update(skip_locked=True).order_by('created_at')

        leads = list(qs)
        if not leads:
            return {'distributed': 0, 'message': 'No unassigned leads found.'}

        # Today's existing assignment counts (for fair weighted continuation across runs).
        today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        if dist_type == 'telecaller':
            count_qs = Lead.objects.filter(
                telecaller__in=members, telecaller_assigned_at__gte=today_start
            ).values('telecaller_id').annotate(n=Count('id'))
            counts = {row['telecaller_id']: row['n'] for row in count_qs}
        else:
            count_qs = Lead.objects.filter(
                stm__in=members, stm_assigned_at__gte=today_start
            ).values('stm_id').annotate(n=Count('id'))
            counts = {row['stm_id']: row['n'] for row in count_qs}
        for m in members:
            counts.setdefault(m.id, 0)

        # Project assignments (STRICT): a member only receives leads of the project(s)
        # assigned to them. A member with NO project assigned receives NOTHING — and a
        # lead with no project can't be routed to anyone.
        proj_map = {}
        for uid, pid in UserProjectAssignment.objects.filter(
            user__in=members
        ).values_list('user_id', 'project_id'):
            proj_map.setdefault(uid, set()).add(pid)

        member_ids   = [m.id for m in members]
        id_to_member = {m.id: m for m in members}
        user_leads   = {m.id: [] for m in members}
        now = timezone.now()
        skipped = 0

        # Pre-bucket eligible members by project so each lead is matched in O(1)
        # instead of scanning every member (O(L×M) → O(L+M)). Members are added in
        # member_ids order, so the weighted-min tie-break stays identical to before.
        proj_to_uids = {}
        for uid in member_ids:
            for pid in proj_map.get(uid, ()):
                proj_to_uids.setdefault(pid, []).append(uid)

        for lead in leads:
            eligible = proj_to_uids.get(lead.project_id) if lead.project_id is not None else None
            if not eligible:
                skipped += 1
                continue
            best = min(eligible, key=lambda uid: counts[uid] / (weight_map.get(uid, 1)))
            user_leads[best].append(lead.pk)
            counts[best] += 1

        assignments = []
        history_rows = []
        note = 'Auto-assigned' if triggered_by is None else 'Manually assigned'
        for uid, pks in user_leads.items():
            if not pks:
                continue
            if dist_type == 'telecaller':
                Lead.objects.filter(pk__in=pks).update(
                    telecaller_id=uid, status='assigned', telecaller_assigned_at=now,
                )
            else:
                Lead.objects.filter(pk__in=pks).update(stm_id=uid, stm_assigned_at=now)
            for pk in pks:
                history_rows.append(LeadStatusHistory(
                    lead_id=pk, changed_by=triggered_by,
                    field_changed=dist_type, old_value='', new_value=id_to_member[uid].name,
                    remarks=note,
                ))
            assignments.append({'name': id_to_member[uid].name, 'count': len(pks)})
            from notifications import notify
            notify(id_to_member[uid], 'new_lead', 'New Leads Assigned',
                   f'{len(pks)} new lead{"s" if len(pks) > 1 else ""} assigned to you.')

        if history_rows:
            LeadStatusHistory.objects.bulk_create(history_rows)

        distributed = sum(a['count'] for a in assignments)
        if distributed:
            DistributionLog.objects.create(
                company=company,
                dist_type=dist_type,
                triggered_by=triggered_by,
                leads_distributed=distributed,
                details={'assignments': assignments, 'auto': triggered_by is None},
            )

    resp = {'distributed': distributed, 'assignments': {a['name']: a['count'] for a in assignments}}
    if skipped:
        resp['message'] = f'{skipped} lead(s) left unassigned — no available {desig} is assigned to their project.'
    return resp


def _record_lead_created(lead, by=None):
    """Add the opening 'Lead created' entry to a lead's history (with its source)."""
    src = lead.source.name if lead.source_id else 'manual'
    campaign = lead.meta_campaign_name or ''
    new_value = (f'{src} · {campaign}' if campaign else src)[:100]
    LeadStatusHistory.objects.create(
        lead=lead, changed_by=by, field_changed='created',
        old_value='', new_value=new_value, remarks='Lead created',
    )


class DistributeView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        dist_type = request.data.get('dist_type', request.data.get('type', 'telecaller'))
        company   = _resolve_company(request)
        # Manual admin trigger: weight-based, allowed before sign-in, blocked after sign-out.
        resp = _run_distribution(company, dist_type, triggered_by=request.user, gate='signout')
        return Response(resp)


class DistributionLogView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        logs = scope_to_company(
            DistributionLog.objects.select_related('triggered_by'), request.user
        )
        if request.query_params.get('company_id') and is_platform_admin(request.user):
            logs = logs.filter(company_id=request.query_params['company_id'])
        logs = logs[:30]
        data = [{
            'id':                  log.id,
            'dist_type':           log.dist_type,
            'leads_distributed':   log.leads_distributed,
            'triggered_by_name':   log.triggered_by.name if log.triggered_by else 'System',
            'details':             log.details,
            'created_at':          log.created_at,
        } for log in logs]
        return Response(data)

    def delete(self, request):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        qs = scope_to_company(DistributionLog.objects.all(), request.user)
        if request.query_params.get('company_id') and is_platform_admin(request.user):
            qs = qs.filter(company_id=request.query_params['company_id'])
        qs.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ── Bulk Import ───────────────────────────────────────────────────────────────
# ── Lead-import helpers (flexible cell parsing for the lifecycle template) ──────
def _imp_dt(val):
    """Parse a cell into an aware datetime. Accepts ISO, yyyy-mm-dd, dd-mm-yyyy, dd/mm/yyyy."""
    from datetime import datetime as _dt, time as _time
    from django.utils.dateparse import parse_datetime, parse_date
    import re as _re
    s = str(val or '').strip()
    if not s:
        return None
    dt = parse_datetime(s)
    if dt:
        dt = timezone.make_aware(dt) if timezone.is_naive(dt) else dt
        # Midnight (incl. Excel date cells) → noon so the calendar date is timezone-stable.
        if dt.hour == 0 and dt.minute == 0 and dt.second == 0:
            dt = dt.replace(hour=12)
        return dt
    d = parse_date(s)
    if d:
        # Anchor date-only values at noon so the calendar date is stable across timezones.
        return timezone.make_aware(_dt.combine(d, _time(12, 0)))
    m = _re.match(r'^(\d{1,2})[-/](\d{1,2})[-/](\d{2,4})$', s)
    if m:
        dd, mm, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if yy < 100:
            yy += 2000
        try:
            return timezone.make_aware(_dt(yy, mm, dd, 12, 0))
        except ValueError:
            return None
    return None


def _imp_date(val):
    dt = _imp_dt(val)
    return dt.date() if dt else None


def _imp_int(val):
    s = str(val or '').replace(',', '').strip()
    if not s:
        return None
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return None


def _imp_dec(val):
    s = str(val or '').replace(',', '').replace('₹', '').strip()
    if not s:
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


# Canonical row keys the importer understands (the full-pipeline columns).
IMPORT_COLUMNS = [
    'name', 'phone', 'alt_phone', 'email', 'project', 'source', 'campaign', 'adset', 'ad_name',
    'requirement', 'budget_min', 'budget_max', 'preferred_location', 'lead_date', 'overall_status',
    'telecaller_id', 'telecaller_status', 'telecaller_remarks',
    'stm_id', 'stm_status', 'stm_remarks',
    'sv_scheduled_date', 'sv_visited_date', 'sv_status', 'sv_referred_by_id', 'sv_remarks',
    'closure_date', 'closure_status', 'unit_no', 'unit_type', 'booking_amount', 'total_amount', 'closure_remarks',
]
# Header → canonical-key aliases (the per-row loop reads 'creative', not 'ad_name').
_IMP_ALIASES = {
    'name': {'name', 'full_name', 'fullname', 'customer_name', 'lead_name', 'first_name'},
    'phone': {'phone', 'phone_number', 'phonenumber', 'mobile', 'mobile_number', 'contact', 'cell'},
    'alt_phone': {'alt_phone', 'alternate_phone', 'phone_2', 'secondary_phone', 'other_phone'},
    'email': {'email', 'e_mail', 'email_address'},
    'campaign': {'campaign', 'campaign_name', 'meta_campaign', 'utm_campaign', 'ad_campaign'},
    'adset': {'adset', 'adset_name', 'ad_set', 'ad_group_name', 'adgroup'},
    'creative': {'creative', 'ad_name', 'creative_name', 'ad_creative', 'advertisement_name'},
    'lead_date': {'lead_date', 'date', 'created', 'created_at', 'submission_date', 'timestamp'},
}
_IMP_CANON = set(IMPORT_COLUMNS) | {'creative'}


def _imp_canon_key(header):
    import re as _re
    k = _re.sub(r'[\s\-]+', '_', str(header or '').strip().lower())
    for field, aliases in _IMP_ALIASES.items():
        if k in aliases:
            return field
    return k if k in _IMP_CANON else None


def _imp_parse_file(f):
    """Parse an uploaded .xlsx/.csv into a list of row dicts keyed by canonical column names."""
    import io
    fname = (getattr(f, 'name', '') or '').lower()
    headers, raw_rows = [], []
    if fname.endswith('.csv') or fname.endswith('.txt'):
        import csv
        data = f.read()
        text = data.decode('utf-8-sig', errors='ignore') if isinstance(data, bytes) else data
        reader = csv.DictReader(io.StringIO(text))
        headers = reader.fieldnames or []
        raw_rows = [dict(r) for r in reader]
    else:
        import openpyxl
        wb = openpyxl.load_workbook(f, read_only=True, data_only=True)
        ws = wb['Leads'] if 'Leads' in wb.sheetnames else wb[wb.sheetnames[0]]
        it = ws.iter_rows(values_only=True)
        headers = [('' if h is None else str(h).strip()) for h in (next(it, []) or [])]
        for r in it:
            raw_rows.append({headers[i]: r[i] for i in range(min(len(headers), len(r)))})
    colmap = {h: _imp_canon_key(h) for h in headers}
    rows = []
    for rr in raw_rows:
        out = {}
        for h, v in rr.items():
            c = colmap.get(h)
            if c and v is not None and str(v).strip() != '':
                out[c] = v
        if out.get('name') or out.get('phone'):
            rows.append(out)
    return rows


class BulkImportLeadsView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)

        from .models import LEAD_STATUS, TC_STATUS, STM_STATUS, SV_STATUS, CLOSURE_STATUS

        rows       = request.data.get('leads', [])
        project_id = request.data.get('project_id')   # default project for every row
        source_id  = request.data.get('source_id')    # default source for every row
        company    = request.user.company

        # App/web may upload the spreadsheet itself (multipart) instead of pre-parsed
        # JSON rows — parse it server-side into the same canonical row dicts.
        if not rows and request.FILES.get('file'):
            try:
                rows = _imp_parse_file(request.FILES['file'])
            except Exception as e:
                return Response({'detail': 'Could not read the file: %s' % e}, status=status.HTTP_400_BAD_REQUEST)

        if not rows:
            return Response({'detail': 'No leads provided.'}, status=status.HTTP_400_BAD_REQUEST)

        # A supplied default project/source must belong to the requester's company.
        if project_id and not _project_in_scope(request, project_id):
            return Response({'detail': 'Invalid project for your company.'}, status=status.HTTP_400_BAD_REQUEST)
        if source_id and not scope_to_company(LeadSource.objects.filter(pk=source_id), request.user).exists():
            return Response({'detail': 'Invalid source for your company.'}, status=status.HTTP_400_BAD_REQUEST)

        # Allowed status values + company-scoped lookup tables (resolved once).
        LEAD_ST = {k for k, _ in LEAD_STATUS}
        TC_ST   = {k for k, _ in TC_STATUS}
        STM_ST  = {k for k, _ in STM_STATUS}
        SV_ST   = {k for k, _ in SV_STATUS}
        CL_ST   = {k for k, _ in CLOSURE_STATUS}
        proj_by_name = {p.name.strip().lower(): p.id for p in scope_to_company(Project.objects.all(), request.user)}
        src_by_name  = {s.name.strip().lower(): s.id for s in scope_to_company(LeadSource.objects.all(), request.user)}
        uq = User.objects.filter(is_active=True)
        if company:
            uq = uq.filter(company=company)
        valid_user_ids = set(uq.values_list('id', flat=True))

        def _uid(v):
            i = _imp_int(v)
            return i if i in valid_user_ids else None

        imported = 0
        duplicates = 0
        errors = 0
        bare_new = 0
        failed = []

        # Build existing dup set (last-10-digits) scoped to this company — O(n) once.
        company_leads = scope_to_company(Lead.objects.all(), request.user)
        existing_keys = {
            ''.join(c for c in (p or '') if c.isdigit())[-10:]
            for p in company_leads.values_list('phone', flat=True)
        }
        existing_keys.discard('')

        to_create = []   # Lead objects
        meta      = []   # parallel per-row dict carrying lead_date + SV/closure raw data
        for i, row in enumerate(rows):
            name  = str(row.get('name', '')).strip()
            phone = str(row.get('phone', '')).strip()
            if not name or not phone:
                errors += 1
                failed.append({'row': i + 1, 'name': name, 'phone': phone, 'reason': 'Missing name or phone'})
                continue

            clean = ''.join(c for c in phone if c.isdigit())[-10:]
            is_dup = bool(clean) and clean in existing_keys

            rproj = proj_by_name.get(str(row.get('project', '')).strip().lower()) or project_id or None
            rsrc  = src_by_name.get(str(row.get('source', '')).strip().lower()) or source_id or None
            tc_id  = _uid(row.get('telecaller_id'))
            stm_id = _uid(row.get('stm_id'))

            tc_status  = str(row.get('telecaller_status', '')).strip().lower()
            tc_status  = tc_status if tc_status in TC_ST else ''
            stm_status = str(row.get('stm_status', '')).strip().lower()
            stm_status = stm_status if stm_status in STM_ST else ''

            lead_dt = _imp_dt(row.get('lead_date'))

            # SV / closure presence
            sv_sched = _imp_dt(row.get('sv_scheduled_date'))
            sv_vis   = _imp_dt(row.get('sv_visited_date'))
            sv_stat  = str(row.get('sv_status', '')).strip().lower()
            sv_stat  = sv_stat if sv_stat in SV_ST else ''
            has_sv   = bool(sv_sched or sv_vis or sv_stat or str(row.get('sv_remarks', '')).strip())
            cl_date  = _imp_date(row.get('closure_date'))

            # Overall lead status: explicit wins; otherwise derive from the furthest stage reached.
            overall = str(row.get('overall_status', '')).strip().lower()
            if overall not in LEAD_ST:
                if cl_date:
                    overall = 'closed'
                elif has_sv:
                    overall = 'sv_done' if sv_stat == 'completed' else 'sv_scheduled'
                elif stm_id:
                    overall = 'warm_transferred'
                elif tc_id:
                    overall = 'assigned'
                else:
                    overall = 'new'

            to_create.append(Lead(
                company=company,
                name=name,
                phone=phone,
                alt_phone=str(row.get('alt_phone', '')).strip(),
                email=str(row.get('email', '')).strip(),
                project_id=rproj,
                source_id=rsrc,
                meta_campaign_name=str(row.get('campaign', '')).strip(),
                meta_adset_name=str(row.get('adset', '')).strip(),
                meta_ad_name=str(row.get('creative', '')).strip(),
                requirement=str(row.get('requirement', '')).strip(),
                preferred_location=str(row.get('preferred_location', '')).strip(),
                budget_min=_imp_int(row.get('budget_min')),
                budget_max=_imp_int(row.get('budget_max')),
                status=overall,
                telecaller_id=tc_id,
                telecaller_status=tc_status,
                telecaller_remarks=str(row.get('telecaller_remarks', '')).strip(),
                telecaller_assigned_at=(lead_dt or timezone.now()) if tc_id else None,
                stm_id=stm_id,
                stm_status=stm_status,
                stm_remarks=str(row.get('stm_remarks', '')).strip(),
                stm_assigned_at=(lead_dt or timezone.now()) if stm_id else None,
                is_duplicate=is_dup,
            ))
            meta.append({
                'lead_dt': lead_dt,
                'has_sv': has_sv, 'sv_sched': sv_sched, 'sv_vis': sv_vis, 'sv_stat': sv_stat or 'scheduled',
                'sv_ref': _uid(row.get('sv_referred_by_id')), 'sv_remarks': str(row.get('sv_remarks', '')).strip(),
                'cl_date': cl_date, 'cl_status': (str(row.get('closure_status', '')).strip().lower() if str(row.get('closure_status', '')).strip().lower() in CL_ST else 'booked'),
                'unit_no': str(row.get('unit_no', '')).strip(), 'unit_type': str(row.get('unit_type', '')).strip(),
                'booking_amount': _imp_dec(row.get('booking_amount')), 'total_amount': _imp_dec(row.get('total_amount')),
                'cl_remarks': str(row.get('closure_remarks', '')).strip(),
            })

            if is_dup:
                duplicates += 1
            else:
                imported += 1
                if clean:
                    existing_keys.add(clean)  # catch in-batch duplicates too
            if not tc_id and overall == 'new':
                bare_new += 1

        with transaction.atomic():
            created = Lead.objects.bulk_create(to_create)

            # Honour historical lead_date by overriding the auto_now_add created_at.
            dated = []
            for lead, m in zip(created, meta):
                if m['lead_dt']:
                    lead.created_at = m['lead_dt']
                    dated.append(lead)
            if dated:
                Lead.objects.bulk_update(dated, ['created_at'])

            # Materialise Site Visits + Closures linked to each freshly created lead.
            svs, closures = [], []
            for lead, m in zip(created, meta):
                if m['has_sv']:
                    svs.append(SiteVisit(
                        lead=lead, project_id=lead.project_id,
                        scheduled_at=m['sv_sched'], visited_at=m['sv_vis'], status=m['sv_stat'],
                        stm_id=lead.stm_id, referred_by_telecaller_id=(m['sv_ref'] or lead.telecaller_id),
                        remarks=('[Imported] ' + m['sv_remarks']).strip(),
                    ))
                if m['cl_date']:
                    # Historical closure (no Booking/LOI) — tagged so it's distinguishable
                    # from closures produced by the booking form.
                    closures.append(Closure(
                        lead=lead, project_id=lead.project_id, stm_id=lead.stm_id,
                        referred_by_telecaller_id=lead.telecaller_id, status=m['cl_status'],
                        closure_date=m['cl_date'], unit_no=m['unit_no'], unit_type=m['unit_type'],
                        booking_amount=m['booking_amount'], total_amount=m['total_amount'],
                        remarks=('[Imported] ' + m['cl_remarks']).strip(),
                    ))
            if svs:
                SiteVisit.objects.bulk_create(svs)
            if closures:
                Closure.objects.bulk_create(closures)

        # Auto-assign only the genuinely bare/new bucket (rows that carried an STM/TC
        # or a later stage are already placed and must not be redistributed).
        if bare_new:
            _run_distribution(company, 'telecaller')
        return Response({
            'imported': imported, 'duplicates': duplicates, 'errors': errors, 'failed': failed,
            'site_visits': len([m for m in meta if m['has_sv']]),
            'closures': len([m for m in meta if m['cl_date']]),
        })


class LeadImportTemplateView(APIView):
    """Generates the Full-Pipeline import template (.xlsx) server-side with dropdowns,
    a styled table, coloured required/closure headers and a Reference sheet — so the
    mobile app (which can't build a rich xlsx on-device) downloads the same template
    the web generates."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)

        import openpyxl
        from io import BytesIO
        from openpyxl.worksheet.table import Table, TableStyleInfo
        from openpyxl.worksheet.datavalidation import DataValidation
        from openpyxl.styles import PatternFill, Font
        from openpyxl.utils import get_column_letter

        company = request.user.company
        projects = list(scope_to_company(Project.objects.all(), request.user).values_list('name', flat=True))
        sources  = list(scope_to_company(LeadSource.objects.all(), request.user).values_list('name', flat=True))
        uq = User.objects.filter(is_active=True).exclude(role='Admin')
        if company:
            uq = uq.filter(company=company)
        users = list(uq.values('id', 'name', 'designation', 'role', 'phone').order_by('name'))

        cols = [
            'name', 'phone', 'alt_phone', 'email', 'project', 'source', 'campaign', 'adset', 'ad_name',
            'requirement', 'budget_min', 'budget_max', 'preferred_location', 'lead_date', 'overall_status',
            'telecaller_id', 'telecaller_status', 'telecaller_remarks', 'stm_id', 'stm_status', 'stm_remarks',
            'sv_scheduled_date', 'sv_visited_date', 'sv_status', 'sv_referred_by_id', 'sv_remarks',
            'closure_date', 'closure_status', 'unit_no', 'unit_type', 'booking_amount', 'total_amount', 'closure_remarks',
        ]
        STATUS = {
            'overall_status': 'new,assigned,contacted,not_reachable,warm_transferred,hot,warm,cold,not_interested,sv_scheduled,sv_done,closed,lost',
            'telecaller_status': 'warm,cold,not_interested,not_reachable,callback',
            'stm_status': 'hot,warm,cold,not_interested,sv_scheduled,sv_done,closed',
            'sv_status': 'scheduled,completed,cancelled,no_show',
            'closure_status': 'booked,cancelled,refunded',
        }
        def _role(u):
            return (u['designation'] or u['role'] or '').lower()
        tc_id  = next((u['id'] for u in users if 'tele' in _role(u)), (users[0]['id'] if users else ''))
        stm_id = next((u['id'] for u in users if any(k in _role(u) for k in ('stm', 'sales', 'manager'))),
                      (users[1]['id'] if len(users) > 1 else (users[0]['id'] if users else '')))

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = 'Leads'
        ws.append(cols)
        ex1 = {'name': 'Rahul Sharma', 'phone': '9876543210', 'email': 'rahul@example.com', 'source': (sources[0] if sources else 'meta'), 'campaign': 'Meta - Luxury Homes', 'ad_name': 'Video 2BHK', 'lead_date': '01-05-2025', 'overall_status': 'new', 'telecaller_id': tc_id, 'telecaller_status': 'callback', 'telecaller_remarks': 'Call back evening'}
        ex2 = {'name': 'Priya Mehta', 'phone': '9988776655', 'email': 'priya@example.com', 'project': (projects[0] if projects else 'Kalrav'), 'source': (sources[0] if sources else 'walk-in'), 'lead_date': '02-04-2025', 'overall_status': 'closed', 'telecaller_id': tc_id, 'telecaller_status': 'warm', 'stm_id': stm_id, 'stm_status': 'closed', 'sv_scheduled_date': '05-04-2025', 'sv_visited_date': '06-04-2025', 'sv_status': 'completed', 'sv_remarks': 'Liked plot A-12', 'closure_date': '08-04-2025', 'closure_status': 'booked', 'unit_no': 'A-12', 'unit_type': '2BHK', 'booking_amount': 200000, 'total_amount': 5000000, 'closure_remarks': 'Token received'}
        for ex in (ex1, ex2):
            ws.append([ex.get(c, '') for c in cols])

        last_col = get_column_letter(len(cols))
        table = Table(displayName='LeadsImport', ref='A1:%s3' % last_col)
        table.tableStyleInfo = TableStyleInfo(name='TableStyleMedium2', showRowStripes=True)
        ws.add_table(table)
        ws.freeze_panes = 'A2'
        for i, c in enumerate(cols, start=1):
            ws.column_dimensions[get_column_letter(i)].width = min(26, max(12, len(c) + 3))

        def col_of(name):
            return get_column_letter(cols.index(name) + 1)
        red, purple, white = PatternFill('solid', fgColor='C62828'), PatternFill('solid', fgColor='7C3AED'), Font(bold=True, color='FFFFFF')
        for f in ('name', 'phone'):
            ws['%s1' % col_of(f)].fill = red
            ws['%s1' % col_of(f)].font = white
        for f in ('closure_date', 'closure_status', 'unit_no', 'unit_type', 'booking_amount', 'total_amount', 'closure_remarks'):
            ws['%s1' % col_of(f)].fill = purple
            ws['%s1' % col_of(f)].font = white

        lists = wb.create_sheet('Lists')
        lists.sheet_state = 'hidden'
        for i, n in enumerate(projects, start=1):
            lists['A%d' % i] = n
        for i, n in enumerate(sources, start=1):
            lists['B%d' % i] = n

        MAXROW = 1000
        def add_dv(name, formula):
            dv = DataValidation(type='list', formula1=formula, allow_blank=True, showErrorMessage=True, errorStyle='warning')
            ws.add_data_validation(dv)
            dv.add('%s2:%s%d' % (col_of(name), col_of(name), MAXROW))
        for field, vals in STATUS.items():
            add_dv(field, '"%s"' % vals)
        if projects:
            add_dv('project', 'Lists!$A$1:$A$%d' % len(projects))
        if sources:
            add_dv('source', 'Lists!$B$1:$B$%d' % len(sources))

        ref = wb.create_sheet('Reference — IDs & values')
        ref.append(['— TEAM — put this id in telecaller_id / stm_id / sv_referred_by_id —'])
        ref.append(['id', 'name', 'role / designation', 'phone'])
        ref['A2'].font = Font(bold=True)
        for u in users:
            ref.append([u['id'], u['name'], (u['designation'] or u['role'] or ''), u['phone'] or ''])
        ref.append([])
        ref.append(['— ALLOWED VALUES (the Leads sheet has dropdowns for these) —'])
        for k, v in STATUS.items():
            ref.append([k, v.replace(',', ', ')])
        ref.append([])
        ref.append(['— NOTES —'])
        ref.append(['Header colours: RED = required (name, phone). PURPLE = closure columns.'])
        ref.append(['Dates: dd-mm-yyyy. project/source are matched by name. Leave a cell blank to skip.'])
        ref.append(['Fill any sv_* column to create a Site Visit; fill closure_date to create a Closure.'])
        ref.column_dimensions['A'].width = 24
        ref.column_dimensions['B'].width = 62

        buf = BytesIO()
        wb.save(buf)
        buf.seek(0)
        resp = HttpResponse(buf.read(), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        resp['Content-Disposition'] = 'attachment; filename="vistara_pipeline_import_template.xlsx"'
        return resp


# ── Reports ───────────────────────────────────────────────────────────────────
class ReportsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from django.db.models import Count, Sum, Q

        user      = request.user
        leads_qs  = scope_to_company(Lead.objects.all(), user)
        sv_qs     = scope_to_company(SiteVisit.objects.all(), user, 'lead__company')
        closure_qs = scope_to_company(Closure.objects.all(), user, 'lead__company')
        company_id = request.query_params.get('company_id')
        if company_id and is_platform_admin(user):
            leads_qs   = leads_qs.filter(company_id=company_id)
            sv_qs      = sv_qs.filter(lead__company_id=company_id)
            closure_qs = closure_qs.filter(lead__company_id=company_id)

        # Optional date window — bounds the aggregate scans. No default, so the
        # existing all-time behaviour is unchanged unless the client sends dates.
        date_from = request.query_params.get('date_from')
        date_to   = request.query_params.get('date_to')
        if date_from:
            leads_qs   = leads_qs.filter(created_at__date__gte=date_from)
            sv_qs      = sv_qs.filter(created_at__date__gte=date_from)
            closure_qs = closure_qs.filter(closure_date__gte=date_from)
        if date_to:
            leads_qs   = leads_qs.filter(created_at__date__lte=date_to)
            sv_qs      = sv_qs.filter(created_at__date__lte=date_to)
            closure_qs = closure_qs.filter(closure_date__lte=date_to)

        # Hierarchy scope: managers (anyone with reports below them) get a team report
        # over their subtree; leaf users get a personal report. Admins/top heads see all.
        if _sees_all_company(user):
            team_view = True
        else:
            _ids = _visible_user_ids(user)
            leads_qs   = leads_qs.filter(Q(stm__in=_ids) | Q(telecaller__in=_ids))
            sv_qs      = sv_qs.filter(Q(stm__in=_ids) | Q(referred_by_telecaller__in=_ids))
            closure_qs = closure_qs.filter(Q(stm__in=_ids) | Q(referred_by_telecaller__in=_ids))
            team_view  = len(_ids) > 1  # has at least one subordinate → manager view

        def get_campaigns():
            return list(
                leads_qs.exclude(meta_campaign_name='')
                .values('meta_campaign_name')
                .annotate(
                    total=Count('id'),
                    warm=Count('id', filter=Q(status__in=['warm_transferred', 'sv_scheduled', 'sv_done', 'closed'])),
                    sv=Count('id', filter=Q(status__in=['sv_done', 'closed'])),
                    closed=Count('id', filter=Q(status='closed')),
                )
                .order_by('-total')[:20]
            )

        def get_telecallers():
            return list(
                leads_qs.exclude(telecaller__isnull=True)
                .values('telecaller__id', 'telecaller__name')
                .annotate(
                    total=Count('id'),
                    warm=Count('id', filter=Q(telecaller_status='warm')),
                    transferred=Count('id', filter=Q(status='warm_transferred')),
                    sv=Count('id', filter=Q(status__in=['sv_done', 'closed'])),
                )
                .order_by('-total')
            )

        def get_stms():
            return list(
                leads_qs.exclude(stm__isnull=True)
                .values('stm__id', 'stm__name')
                .annotate(
                    total=Count('id'),
                    hot=Count('id', filter=Q(stm_status='hot')),
                    sv_scheduled=Count('id', filter=Q(stm_status='sv_scheduled')),
                    sv_done=Count('id', filter=Q(stm_status__in=['sv_done'])),
                    closed=Count('id', filter=Q(status='closed')),
                )
                .order_by('-total')
            )

        def get_summary():
            # Amounts are encrypted at rest → can't SQL-Sum; sum in Python. Revenue is
            # the FULL closure value (total_amount = final amount), falling back to
            # booking_amount (plot basic) for older closures with no total.
            cnt = closure_qs.count()
            total = sum((c.total_amount or c.booking_amount or 0) for c in closure_qs.only('id', 'booking_amount', 'total_amount'))
            return {
                'total_sv':       sv_qs.count(),
                'completed_sv':   sv_qs.filter(status='completed').count(),
                'total_closures': cnt,
                'total_revenue':  float(total or 0),
                'meta_leads':     leads_qs.exclude(meta_campaign_name='').count(),
            }

        def get_closures():
            return closure_qs.select_related('lead', 'project', 'stm', 'referred_by_telecaller').order_by('-closure_date')[:20]

        # Run sequentially. These are indexed aggregates (fast); the previous
        # ThreadPoolExecutor opened 5 DB connections per request and didn't close
        # them in the worker threads — a connection leak that, with the pooled
        # endpoint + multiple gunicorn workers, risked exhausting Neon.
        return Response({
            # Team-performance tables are management-only; personal reports omit them.
            'team_view':   team_view,
            'campaigns':   get_campaigns()   if team_view else [],
            'telecallers': get_telecallers() if team_view else [],
            'stms':        get_stms()        if team_view else [],
            'closures':    ClosureSerializer(get_closures(), many=True).data,
            'summary':     get_summary(),
        })


class MyTeamView(APIView):
    """Everyone reporting under the requester (their org subtree), with lead/closure
    counts — powers the manager 'My Team' view. Returns [] for users with no reports."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        # Honour the admin "Viewing Company" filter (?company_id) for platform admins.
        company = _resolve_company(request)
        module = (request.query_params.get('module') or '').strip()  # department/module org chart
        scope  = request.query_params.get('scope')                   # 'all' → full company org
        ids = _visible_user_ids(user) - {user.id}   # subtree, excluding self
        is_admin = is_platform_admin(user) or user.is_staff or getattr(user, 'role', '') == 'Admin'

        def _full_company():
            # Everyone in a reporting relationship + all Managers (leadership shows
            # even before anyone reports to them); standalone users stay out.
            return list(
                User.objects.filter(company=company, is_active=True)
                .filter(
                    Q(reporting_manager__isnull=False)
                    | Q(subordinates__isnull=False)
                    | Q(role='Manager')
                )
                .distinct().select_related('reporting_manager').order_by('name')
            )

        if is_admin and module:
            # Department/module org chart — users assigned to this module.
            all_users = (User.objects.filter(company=company, is_active=True)
                         .select_related('reporting_manager').order_by('name'))
            members = [u for u in all_users
                       if module in (u.modules or []) or module in (u.manager_modules or [])]
            ids = {u.id for u in members}
        elif is_admin and (scope == 'all' or not ids):
            # Full company org (User Management / admin default).
            members = _full_company()
            ids = {u.id for u in members}
        elif not ids:
            return Response([])
        else:
            members = list(
                User.objects.filter(id__in=ids, company=company)
                .select_related('reporting_manager').order_by('name')
            )
        # Admins never appear in the org chart — it reflects the operational hierarchy.
        members = [m for m in members
                   if getattr(m, 'role', '') != 'Admin' and not getattr(m, 'is_staff', False)]
        ids = {m.id for m in members}
        # Owned-lead counts (as STM or telecaller) and closure counts, in a few aggregates.
        lead_counts, closure_counts = {}, {}
        for fld in ('stm_id', 'telecaller_id'):
            for row in Lead.objects.filter(company=company, **{f'{fld}__in': ids}).values(fld).annotate(c=Count('id')):
                lead_counts[row[fld]] = lead_counts.get(row[fld], 0) + row['c']
        for fld in ('stm_id', 'referred_by_telecaller_id'):
            for row in Closure.objects.filter(lead__company=company, **{f'{fld}__in': ids}).values(fld).annotate(c=Count('id')):
                closure_counts[row[fld]] = closure_counts.get(row[fld], 0) + row['c']
        data = [{
            'id':                u.id,
            'name':              u.name,
            'user_code':         u.user_code,
            'designation':       u.designation,
            'role':              u.role,
            'phone':             u.phone,
            'email':             u.email,
            'reporting_manager':    u.reporting_manager.name if u.reporting_manager_id else None,
            'reporting_manager_id': u.reporting_manager_id,
            'is_direct_report':     u.reporting_manager_id == user.id,
            'leads':             lead_counts.get(u.id, 0),
            'closures':          closure_counts.get(u.id, 0),
        } for u in members]
        return Response(data)


# ──────────────────────────────────────────────
#  Booking  (native plot booking — replaces the GAS web app for Vistara)
# ──────────────────────────────────────────────

def _loi_path(b):
    """GAS-style object path: <Project>/Plot <no> - <Client>/R<rev>_LOI_Plot<no>_<Client>.pdf"""
    import re
    san = lambda s: (re.sub(r'[\\/:*?"<>|]+', '', str(s or '')).strip() or 'NA')
    proj = san(b.project.name if b.project_id else 'Project')
    plot = san(b.plot.number if b.plot_id else b.area)
    client = san(b.client_name)
    rev = b.revision_no or 0
    return f'{proj}/Plot {plot} - {client}/R{rev}_LOI_Plot{plot}_{client}.pdf'


class BookingListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        company = _resolve_company(request)
        qs = Booking.objects.filter(company=company).select_related('project', 'plot', 'stm')
        if not _sees_all_company(request.user):
            qs = qs.filter(stm__in=_visible_user_ids(request.user))
        if request.query_params.get('mine'):           # "My Bookings" — only this user's
            qs = qs.filter(stm=request.user)
        if request.query_params.get('closure'):
            qs = qs.filter(closure_id=request.query_params['closure'])
        if request.query_params.get('plot'):
            qs = qs.filter(plot_id=request.query_params['plot'])
        if request.query_params.get('status'):
            qs = qs.filter(status=request.query_params['status'])
        return Response(BookingSerializer(qs[:200], many=True).data)

    def post(self, request):
        company = _resolve_company(request)
        data = request.data

        # Resolve or create the lead (Book Unit flow types a new client; Record Closure
        # passes an existing lead).
        lead_id = data.get('lead') or None
        if not lead_id and (data.get('client_name') or '').strip():
            src = None
            sname = (data.get('source') or '').strip()
            if sname:
                src = LeadSource.objects.filter(company=company, name__iexact=sname).first()
            lead = Lead.objects.create(
                company=company, name=data.get('client_name', '').strip(),
                phone=(data.get('phone') or '').strip(), status='new',
                project_id=data.get('project') or None, source=src,
            )
            lead_id = lead.id

        # Revision of an existing (sold) booking — carries the prior lead, bumps the
        # revision number, and leaves the plot/closure untouched until approved.
        prior = None
        rev_of = data.get('revision_of')
        if rev_of:
            prior = Booking.objects.filter(id=rev_of, company=company).first()
            if prior:
                lead_id = prior.lead_id

        ser = BookingSerializer(data=data)
        ser.is_valid(raise_exception=True)
        if prior:
            extra = dict(revision_no=prior.revision_no + 1, closure=prior.closure, plot=prior.plot,
                         approval_status='REVISION R%d PENDING' % (prior.revision_no + 1))
        else:
            extra = dict(revision_no=0, approval_status='PENDING')
        booking = ser.save(company=company, stm=request.user, lead_id=lead_id, status='pending', **extra)

        # Multi-plot: resolve ALL selected plots. `plot` stays the primary (first);
        # plot_ids holds every selected id and plot_numbers is the comma display.
        pids = data.get('plot_ids')
        if isinstance(pids, list) and pids:
            pids = [int(x) for x in pids if str(x).isdigit()]
        elif prior and prior.plot_ids:
            pids = list(prior.plot_ids)
        elif booking.plot_id:
            pids = [booking.plot_id]
        else:
            pids = []
        if pids:
            num_map = dict(Plot.objects.filter(id__in=pids).values_list('id', 'number'))
            booking.plot_ids = pids
            booking.plot_numbers = ', '.join(num_map[p] for p in pids if p in num_map)
            if not booking.plot_id:
                booking.plot_id = pids[0]
            booking.save(update_fields=['plot_ids', 'plot_numbers', 'plot'])

        # Signed LOI (sent as base64 {name,type,data}). Stored GAS-style:
        # <Project>/Plot <no> - <Client>/R<rev>_LOI_Plot<no>_<Client>.pdf
        lf = data.get('loi_file')
        if isinstance(lf, dict) and lf.get('data'):
            import base64
            from django.core.files.base import ContentFile
            try:
                booking.loi_document.save(_loi_path(booking),
                                          ContentFile(base64.b64decode(lf['data'])), save=True)
            except Exception:
                pass

        if not prior:
            # New booking: reserve ALL selected plots. The Closure is mirrored into
            # My Conversions on APPROVAL (see BookingActionView) — so a booking that
            # is still pending approval does NOT appear as a booked closure.
            if pids:
                Plot.objects.filter(id__in=pids).update(status='hold')

        # Notify the admin-selected approvers (managers) via push.
        _notify_booking_approvers(company, booking, request.user)

        return Response(BookingSerializer(booking).data, status=status.HTTP_201_CREATED)


def _notify_booking_approvers(company, booking, submitter):
    try:
        from notifications import notify, reporting_chain
        # 1) Per-project configured approvers (most precise).
        ids = (booking.project.booking_approvers if booking.project_id else None) or []
        recipients = list(User.objects.filter(id__in=ids, company=company, is_active=True)) if ids else []
        # 2) Fallback: the submitting STM's reporting-manager chain.
        if not recipients and booking.stm_id:
            recipients = reporting_chain(booking.stm)
        # 3) Last resort: every manager/admin in the company (so it's never silent).
        if not recipients:
            recipients = list(User.objects.filter(company=company, is_active=True).filter(Q(role='Manager') | Q(is_staff=True)))
        # Never notify the person who submitted it; de-dup.
        sub_id = getattr(submitter, 'id', None)
        recipients = [u for u in recipients if u and u.id != sub_id]
        if not recipients:
            return
        unit = booking.plot_numbers or (booking.plot.number if booking.plot_id else booking.area)
        rev = (' (R%d)' % booking.revision_no) if booking.revision_no else ''
        title = 'Booking approval needed%s' % rev
        msg = '%s · %s Unit %s · ₹%s — by %s' % (
            booking.client_name or '—', booking.project.name if booking.project_id else '',
            unit, int(booking.final_amount or 0), getattr(submitter, 'name', ''),
        )
        seen = set()
        for u in recipients:
            if u.id not in seen:
                seen.add(u.id)
                notify(u, 'booking_approval', title, msg, {'booking_id': booking.id})
    except Exception:
        pass


class BookingActionView(APIView):
    """Approve / reject a pending booking (approver = admin or manager)."""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        company = _resolve_company(request)
        try:
            b = Booking.objects.get(pk=pk, company=company)
        except Booking.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        action = request.data.get('action')
        is_rev = b.revision_no and b.revision_no > 0

        if action == 'approve':
            _pids = b.plot_ids or ([b.plot_id] if b.plot_id else [])
            if _pids:
                Plot.objects.filter(id__in=_pids).update(status='sold')
            b.status = 'sold'
            b.approval_status = ('REVISION R%d APPROVED' % b.revision_no) if is_rev else 'APPROVED'
            if b.closure_id:
                # Existing closure (revision / re-approval) → just sync the amounts.
                b.save(update_fields=['status', 'approval_status'])
                Closure.objects.filter(id=b.closure_id).update(
                    booking_amount=b.plot_basic or None, total_amount=b.final_amount or None)
            else:
                # First approval of a new booking → mirror it into My Conversions now.
                if b.lead_id:
                    Lead.objects.filter(id=b.lead_id).update(stm=b.stm, stm_status='closed')
                closure = Closure.objects.create(
                    lead_id=b.lead_id, project_id=b.project_id, stm=b.stm,
                    status='booked', closure_date=b.booking_date or timezone.now().date(),
                    unit_no=(b.plot_numbers or (b.plot.number if b.plot_id else b.area)),
                    unit_type=b.villa_type or b.bunglow_type or '',
                    booking_amount=b.plot_basic or None, total_amount=b.final_amount or None,
                )
                b.closure = closure
                b.save(update_fields=['status', 'approval_status', 'closure'])
            # Notify the STM (approved) and — on a fresh closure — their manager chain.
            from notifications import notify, notify_many, reporting_chain
            _unit = (b.plot_numbers or (b.plot.number if b.plot_id else b.area))
            _rev = (' (R%d)' % b.revision_no) if is_rev else ''
            if b.stm:
                notify(b.stm, 'booking_approved', 'Booking Approved%s' % _rev,
                       f'{b.client_name or "Your booking"} · Unit {_unit} was approved.', {'booking_id': b.id})
                if not is_rev:
                    notify_many(reporting_chain(b.stm), 'closure', 'New Closure',
                                f'{b.stm.name} closed {b.client_name or "a unit"} · Unit {_unit} · ₹{int(b.final_amount or 0)}',
                                {'booking_id': b.id})
        elif action == 'reject':
            b.status = 'rejected'
            b.approval_status = ('REVISION R%d REJECTED' % b.revision_no) if is_rev else 'REJECTED'
            # Remove the rejected signed LOI PDF from Supabase storage.
            if b.loi_document:
                try: b.loi_document.delete(save=False)
                except Exception: pass
            b.save(update_fields=['status', 'approval_status', 'loi_document'])
            if not is_rev:
                _pids = b.plot_ids or ([b.plot_id] if b.plot_id else [])
                if _pids:
                    Plot.objects.filter(id__in=_pids).update(status='available')
                if b.closure_id:
                    Closure.objects.filter(id=b.closure_id).delete()
            from notifications import notify
            _unit = (b.plot_numbers or (b.plot.number if b.plot_id else b.area))
            _rev = (' (R%d)' % b.revision_no) if is_rev else ''
            if b.stm:
                notify(b.stm, 'booking_rejected', 'Booking Rejected%s' % _rev,
                       f'{b.client_name or "Your booking"} · Unit {_unit} was rejected.', {'booking_id': b.id})
        else:
            return Response({'detail': 'action must be approve or reject.'}, status=status.HTTP_400_BAD_REQUEST)
        return Response(BookingSerializer(b).data)


class BookingLOIUrlView(APIView):
    """Returns a short-lived signed URL for a booking's confidential LOI PDF.
    Authorised viewers only (admin/manager or the booking's STM). The bucket is
    private, so this signed URL is the *only* way to open the document."""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        b = scope_to_company(Booking.objects.all(), request.user).filter(pk=pk).first()
        if not b or not b.loi_document:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        if not (is_admin_or_manager(request.user) or b.stm_id == request.user.id):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        from sales.supabase_storage import create_signed_url
        url = create_signed_url(b.loi_document.name, expires_in=120)
        if not url:
            # Local dev (FileSystem storage) fallback.
            try:
                url = request.build_absolute_uri(b.loi_document.url)
            except Exception:
                url = None
        if not url:
            return Response({'detail': 'LOI unavailable.'}, status=status.HTTP_404_NOT_FOUND)
        return Response({'url': url})


class MediaUploadView(APIView):
    """Authenticated media upload to the public erp-media bucket via the service-role
    key. Lets the frontend stop using the anon key for writes (so anon INSERT can be
    revoked in Supabase). Returns {url, path}."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        import time, random, string
        from sales.supabase_storage import upload_public
        f = request.FILES.get('file')
        if not f:
            return Response({'detail': 'No file provided.'}, status=status.HTTP_400_BAD_REQUEST)
        if f.size and f.size > 25 * 1024 * 1024:
            return Response({'detail': 'File too large (max 25 MB).'}, status=status.HTTP_400_BAD_REQUEST)
        folder = (request.data.get('folder') or 'erp/media').strip('/')
        ext = (f.name.rsplit('.', 1)[-1].lower() if '.' in (f.name or '') else 'bin')[:10]
        rand = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
        path = f'{folder}/{int(time.time() * 1000)}_{rand}.{ext}'
        try:
            url = upload_public(f.read(), path, f.content_type or 'application/octet-stream')
        except Exception as e:
            return Response({'detail': str(e)[:200]}, status=status.HTTP_502_BAD_GATEWAY)
        if not url:
            return Response({'detail': 'Storage not configured.'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response({'url': url, 'path': path})


class MediaDeleteView(APIView):
    """Delete a media object from erp-media via the service-role key (anon can't)."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from sales.supabase_storage import delete_object
        path = request.data.get('path')
        if not path:
            return Response({'detail': 'path required.'}, status=status.HTTP_400_BAD_REQUEST)
        delete_object(path)
        return Response({'ok': True})


class ClosureCancelView(APIView):
    """Cancel a closure: deletes the closure, frees the plot(s), removes the
    signed LOI PDFs from Supabase, and marks the related booking(s) cancelled."""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        closure = scope_to_company(
            Closure.objects.filter(pk=pk), request.user, 'lead__company').first()
        if not closure:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        # Admin/manager, or the STM who owns the closure, may cancel.
        if not (is_admin_or_manager(request.user) or closure.stm_id == request.user.id):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        for b in Booking.objects.filter(closure=closure):
            if b.loi_document:
                try: b.loi_document.delete(save=False)
                except Exception: pass
            _pids = b.plot_ids or ([b.plot_id] if b.plot_id else [])
            if _pids:
                Plot.objects.filter(id__in=_pids).update(status='available')
            b.status = 'rejected'
            b.approval_status = 'CANCELLED'
            b.save(update_fields=['status', 'approval_status', 'loi_document'])
        if closure.lead_id:
            Lead.objects.filter(id=closure.lead_id).update(stm_status='')
        closure.delete()
        return Response({'detail': 'Closure cancelled.'})


# ──────────────────────────────────────────────
#  Meta Lead Ads Webhook
# ──────────────────────────────────────────────

def _fetch_meta_lead_data(leadgen_id, page_access_token):
    """Call Meta Graph API to get lead field data and ad info."""
    try:
        url = f'https://graph.facebook.com/v19.0/{leadgen_id}'
        r = http_requests.get(url, params={
            'access_token': page_access_token,
            # form_id decides project routing — fetch it authoritatively here so we
            # don't depend on the webhook payload always including it.
            'fields': 'field_data,ad_id,ad_name,form_id',
        }, timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception:
        logger.exception('Meta: failed to fetch lead data for leadgen_id=%s', leadgen_id)
    return None


def _fetch_ad_campaign_info(ad_id, page_access_token):
    """Given an ad_id, fetch campaign name and adset name from Meta Graph API."""
    if not ad_id:
        return '', ''
    try:
        url = f'https://graph.facebook.com/v19.0/{ad_id}'
        r = http_requests.get(url, params={
            'access_token': page_access_token,
            'fields': 'campaign{name},adset{name}',
        }, timeout=10)
        if r.status_code == 200:
            data = r.json()
            campaign_name = (data.get('campaign') or {}).get('name', '')
            adset_name    = (data.get('adset') or {}).get('name', '')
            return campaign_name, adset_name
    except Exception:
        logger.exception('Meta: failed to fetch campaign info for ad_id=%s', ad_id)
    return '', ''


def _create_lead_from_meta(field_data, config, campaign_name='', adset_name='', ad_name='', form_id=''):
    """Parse Meta field_data list and create a Lead."""
    fields = {f['name']: f['values'][0] for f in field_data if f.get('values') and f.get('name')}
    name  = fields.get('full_name') or fields.get('name') or (fields.get('first_name', '') + ' ' + fields.get('last_name', '')).strip()
    phone = (fields.get('phone_number') or fields.get('phone') or '').strip()[:20]
    email = fields.get('email', '')[:254]
    if not name and not phone:
        return None

    # Resolve project: form mapping takes priority over default
    project = config.default_project
    if form_id:
        mapping = MetaFormMapping.objects.filter(form_id=form_id).select_related('project').first()
        if mapping:
            project = mapping.project
            MetaFormMapping.objects.filter(pk=mapping.pk).update(total_leads=mapping.total_leads + 1)

    # Tenant for the incoming lead: project's company → config's company
    company = (project.company if project and project.company_id else None) or config.company
    if company is None:
        return None  # Can't attribute to a tenant — drop rather than leak globally.

    source, _ = LeadSource.objects.get_or_create(
        company=company, name='meta', defaults={'is_active': True},
    )

    # Duplicate detection using last 10 digits, scoped to this company
    clean = ''.join(c for c in phone if c.isdigit())[-10:]
    existing = (
        Lead.objects.filter(company=company, phone__endswith=clean).first()
        if clean else None
    )
    if existing:
        existing.duplicate_count = (existing.duplicate_count or 0) + 1
        existing.save(update_fields=['duplicate_count'])

    lead = Lead.objects.create(
        company=company,
        name=(name or 'Meta Lead')[:200],
        phone=phone,
        email=email,
        source=source,
        project=project,
        meta_campaign_name=campaign_name[:200] if campaign_name else '',
        meta_adset_name=adset_name[:200] if adset_name else '',
        meta_ad_name=ad_name[:200] if ad_name else '',
        meta_form_id=str(form_id or '')[:100],
        status='new',
        is_duplicate=bool(existing),
        duplicate_of=existing if existing else None,
    )
    MetaWebhookConfig.objects.filter(pk=config.pk).update(
        total_leads_received=config.total_leads_received + 1,
        last_lead_at=timezone.now(),
        is_active=True,
    )
    _record_lead_created(lead)  # source = 'meta'
    # Auto-assign the live lead to an available telecaller (window-gated).
    _run_distribution(company, 'telecaller')
    return lead


class MetaWebhookView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        """Meta webhook verification challenge."""
        mode      = request.GET.get('hub.mode')
        token     = request.GET.get('hub.verify_token')
        challenge = request.GET.get('hub.challenge')
        # Match any company's verify token (each tenant has its own config).
        if mode == 'subscribe' and token and MetaWebhookConfig.objects.filter(verify_token=token).exists():
            return HttpResponse(challenge, content_type='text/plain')
        return HttpResponse(status=403)

    def _config_for_page(self, page_id):
        """Find the tenant config that owns the given Meta page id."""
        configs = list(MetaWebhookConfig.objects.filter(page_access_token__gt=''))
        if page_id:
            for cfg in configs:
                for p in (cfg.pages_data or []):
                    if str(p.get('page_id')) == str(page_id):
                        return cfg
        return configs[0] if configs else None

    def post(self, request):
        """Receive lead notification from Meta."""
        try:
            data = request.data
            if data.get('object') != 'page':
                return Response({'ok': True})
            for entry in data.get('entry', []):
                config = self._config_for_page(entry.get('id'))
                if not config:
                    continue
                for change in entry.get('changes', []):
                    if change.get('field') == 'leadgen':
                        val        = change.get('value', {})
                        leadgen_id = val.get('leadgen_id')
                        campaign   = val.get('campaign_name', '') or ''
                        adset      = val.get('adset_name', '') or val.get('adgroup_name', '') or ''
                        ad         = val.get('ad_name', '') or ''
                        form_id    = str(val.get('form_id', '') or '')
                        if leadgen_id:
                            meta_data = _fetch_meta_lead_data(leadgen_id, config.page_access_token)
                            if meta_data and meta_data.get('field_data'):
                                ad    = meta_data.get('ad_name') or ad
                                ad_id = meta_data.get('ad_id')
                                # Prefer the form_id from the Graph lead object; the
                                # webhook payload doesn't always include it.
                                form_id = str(meta_data.get('form_id') or form_id or '')
                                if ad_id and not campaign and not adset:
                                    campaign, adset = _fetch_ad_campaign_info(ad_id, config.page_access_token)
                                _create_lead_from_meta(meta_data['field_data'], config, campaign, adset, ad, form_id)
        except Exception:
            logger.exception('Meta webhook: unhandled error processing payload')
        return Response({'ok': True})


class MetaWebhookConfigView(APIView):
    permission_classes = [IsAuthenticated]

    def _ensure_config(self, request):
        company = _resolve_company(request)
        config, created = MetaWebhookConfig.objects.get_or_create(
            company=company,
            defaults={'verify_token': secrets.token_urlsafe(32)},
        )
        if not config.verify_token:
            config.verify_token = secrets.token_urlsafe(32)
            config.save(update_fields=['verify_token'])
        return config

    def _fetch_pages_and_forms(self, pat):
        """Fetch all subscribed pages and their lead forms from Meta API."""
        pages_data, subscribed = [], []
        try:
            pages_r = http_requests.get(
                'https://graph.facebook.com/v19.0/me/accounts',
                params={'access_token': pat, 'limit': 50}, timeout=10
            )
            if pages_r.status_code == 200:
                for page in pages_r.json().get('data', []):
                    page_token = page.get('access_token')
                    page_id    = page.get('id')
                    page_name  = page.get('name', page_id)
                    if not page_token or not page_id:
                        continue
                    subscribed.append(page_name)
                    forms = []
                    try:
                        forms_r = http_requests.get(
                            f'https://graph.facebook.com/v19.0/{page_id}/leadgen_forms',
                            params={'access_token': page_token, 'fields': 'id,name', 'limit': 50},
                            timeout=10
                        )
                        if forms_r.status_code == 200:
                            forms = [{'id': f['id'], 'name': f.get('name', '')}
                                     for f in forms_r.json().get('data', [])]
                    except Exception:
                        logger.exception('Meta: failed to fetch forms for page_id=%s', page_id)
                    pages_data.append({'page_id': page_id, 'page_name': page_name, 'forms': forms})
        except Exception:
            logger.exception('Meta: failed to fetch pages list')
        return subscribed, pages_data

    def get(self, request):
        config = self._ensure_config(request)
        # Auto-refresh pages/forms if stale (older than 2 hours) or never fetched
        if config.page_access_token:
            stale = (
                not config.pages_refreshed_at or
                (timezone.now() - config.pages_refreshed_at).total_seconds() > 7200
            )
            if stale:
                subscribed, pages_data = self._fetch_pages_and_forms(config.page_access_token)
                if pages_data:
                    config.subscribed_pages  = subscribed
                    config.pages_data        = pages_data
                    config.pages_refreshed_at = timezone.now()
                    config.save(update_fields=['subscribed_pages', 'pages_data', 'pages_refreshed_at'])
        projects = list(
            scope_to_company(Project.objects.filter(is_active=True), request.user).values('id', 'name')
        )
        return Response({
            'verify_token':         config.verify_token,
            'page_access_token':    config.page_access_token,
            'default_project_id':   config.default_project_id,
            'is_active':            config.is_active,
            'total_leads_received': config.total_leads_received,
            'last_lead_at':         config.last_lead_at,
            'subscribed_pages':     config.subscribed_pages or [],
            'pages_data':           config.pages_data or [],
            'projects':             projects,
        })

    def post(self, request):
        config = self._ensure_config(request)
        action = request.data.get('action')
        if action == 'debug_forms':
            pat = config.page_access_token
            debug = {}
            pages_r = http_requests.get('https://graph.facebook.com/v19.0/me/accounts',
                                        params={'access_token': pat, 'limit': 50}, timeout=10)
            debug['accounts_status'] = pages_r.status_code
            debug['pages'] = []
            if pages_r.status_code == 200:
                for page in pages_r.json().get('data', []):
                    page_id = page.get('id')
                    page_name = page.get('name', page_id)
                    page_tok = page.get('access_token')
                    forms_r = http_requests.get(
                        f'https://graph.facebook.com/v19.0/{page_id}/leadgen_forms',
                        params={'access_token': page_tok, 'fields': 'id,name', 'limit': 50}, timeout=10)
                    debug['pages'].append({
                        'page': page_name,
                        'page_id': page_id,
                        'forms_status': forms_r.status_code,
                        'forms_response': forms_r.json(),
                    })
            else:
                debug['accounts_error'] = pages_r.json()
            return Response(debug)
        if action == 'regenerate_token':
            config.verify_token = secrets.token_urlsafe(32)
            config.save(update_fields=['verify_token'])
            return Response({'verify_token': config.verify_token})
        if action == 'save':
            pat = request.data.get('page_access_token', '').strip()
            pid = request.data.get('default_project_id')
            if pid and not _project_in_scope(request, pid):
                return Response({'detail': 'Invalid project for your company.'}, status=400)
            config.page_access_token = pat
            config.default_project_id = pid if pid else None
            config.is_active = bool(pat)
            config.save(update_fields=['page_access_token', 'default_project_id', 'is_active'])
            # Subscribe app to all accessible pages' leadgen events
            subscribed, failed, pages_data = [], [], []
            if pat:
                try:
                    pages_r = http_requests.get(
                        'https://graph.facebook.com/v19.0/me/accounts',
                        params={'access_token': pat, 'limit': 50}, timeout=10
                    )
                    if pages_r.status_code == 200:
                        for page in pages_r.json().get('data', []):
                            page_token = page.get('access_token')
                            page_id    = page.get('id')
                            page_name  = page.get('name', page_id)
                            if not page_token or not page_id:
                                continue
                            sub_r = http_requests.post(
                                f'https://graph.facebook.com/v19.0/{page_id}/subscribed_apps',
                                params={'access_token': page_token,
                                        'subscribed_fields': 'leadgen'}, timeout=10
                            )
                            if sub_r.status_code == 200 and sub_r.json().get('success'):
                                subscribed.append(page_name)
                            else:
                                failed.append(page_name)
                except Exception:
                    logger.exception('Meta: failed to subscribe pages to app')
            _, pages_data = self._fetch_pages_and_forms(pat) if pat else ([], [])
            config.subscribed_pages   = subscribed
            config.pages_data         = pages_data
            config.pages_refreshed_at = timezone.now()
            config.save(update_fields=['subscribed_pages', 'pages_data', 'pages_refreshed_at'])
            return Response({'ok': True, 'is_active': config.is_active,
                             'subscribed_pages': subscribed, 'failed_pages': failed,
                             'pages_data': pages_data})
        return Response({'detail': 'Unknown action'}, status=400)


def _backfill_form_mapping(company, form_id, project, page_access_token=None):
    """Assign `project` to existing UNMAPPED leads that belong to this form, so a
    mapping added/fixed after leads arrived also fixes those leads. Two passes:
      1) leads already tagged with this form_id (stored on the lead);
      2) best-effort — leads with no/blank project that match (by phone) a lead in
         this form on Meta, covering leads that arrived before form_id was stored
         or without a form_id in the webhook payload.
    Returns the number of leads updated."""
    fid = str(form_id)
    n = Lead.objects.filter(company=company, project__isnull=True, meta_form_id=fid).update(project=project)
    if page_access_token:
        try:
            import urllib.request, json as _json
            phones, url, pages = set(), (
                f'https://graph.facebook.com/v19.0/{fid}/leads?fields=field_data&limit=200&access_token={page_access_token}'), 0
            while url and pages < 6:
                d = _json.load(urllib.request.urlopen(url, timeout=25))
                for r in d.get('data', []):
                    for f in r.get('field_data', []):
                        if 'phone' in (f.get('name', '').lower()):
                            digits = ''.join(c for c in (f.get('values') or [''])[0] if c.isdigit())[-10:]
                            if len(digits) >= 10:
                                phones.add(digits)
                url = d.get('paging', {}).get('next'); pages += 1
            for digits in phones:
                # endswith (not a (^|\D)…$ boundary regex): a +91-prefixed number like
                # +919510188522 has its 10-digit core preceded by the '1' of +91, so a
                # \D boundary never matches. Last-10 endswith matches the same number.
                n += Lead.objects.filter(
                    company=company, project__isnull=True, phone__endswith=digits
                ).update(project=project, meta_form_id=fid)
        except Exception:
            logger.exception('Meta backfill failed for form_id=%s', fid)
    return n


class MetaFormMappingView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        company = _resolve_company(request)
        mappings = MetaFormMapping.objects.select_related('project').filter(
            company=company
        ).order_by('-created_at')
        return Response([{
            'id':          m.id,
            'form_id':     m.form_id,
            'form_name':   m.form_name,
            'project_id':  m.project_id,
            'project_name':m.project.name,
            'total_leads': m.total_leads,
        } for m in mappings])

    def post(self, request):
        form_id   = request.data.get('form_id', '').strip()
        form_name = request.data.get('form_name', '').strip()
        project_id = request.data.get('project_id')
        if not form_id or not project_id:
            return Response({'detail': 'form_id and project_id are required.'}, status=400)
        company = _resolve_company(request)
        try:
            project = Project.objects.filter(company=company).get(pk=project_id)
        except Project.DoesNotExist:
            return Response({'detail': 'Project not found.'}, status=404)
        mapping, created = MetaFormMapping.objects.update_or_create(
            form_id=form_id,
            defaults={'form_name': form_name, 'project': project, 'company': project.company},
        )
        # Retroactively map existing unmapped leads from this form.
        cfg = MetaWebhookConfig.objects.filter(company=company).first()
        backfilled = _backfill_form_mapping(
            company, form_id, project, cfg.page_access_token if cfg else None)
        return Response({
            'id': mapping.id, 'form_id': mapping.form_id,
            'form_name': mapping.form_name, 'project_id': mapping.project_id,
            'project_name': mapping.project.name, 'total_leads': mapping.total_leads,
            'backfilled': backfilled,
        }, status=201 if created else 200)

    def delete(self, request):
        mid = request.data.get('id')
        MetaFormMapping.objects.filter(pk=mid, company=_resolve_company(request)).delete()
        return Response({'ok': True})


# ── User Project Assignments ──────────────────────────────────────────────────
class UserProjectAssignmentView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user_id = request.query_params.get('user_id')
        if not user_id:
            return Response({'detail': 'user_id required.'}, status=400)
        assigned = scope_to_company(
            UserProjectAssignment.objects.filter(user_id=user_id),
            request.user, 'user__company',
        ).values_list('project_id', flat=True)
        return Response(list(assigned))

    def post(self, request):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=403)
        user_id     = request.data.get('user_id')
        project_ids = request.data.get('project_ids', [])
        try:
            user = User.objects.get(pk=user_id, company=request.user.company)
        except User.DoesNotExist:
            return Response({'detail': 'User not found.'}, status=404)
        # Only allow assigning projects that belong to the requester's company.
        valid_ids = list(
            scope_to_company(Project.objects.filter(pk__in=project_ids), request.user)
            .values_list('id', flat=True)
        )
        UserProjectAssignment.objects.filter(user=user).delete()
        UserProjectAssignment.objects.bulk_create([
            UserProjectAssignment(user=user, project_id=pid) for pid in valid_ids
        ], ignore_conflicts=True)
        return Response({'user_id': user_id, 'project_ids': valid_ids})


# ── Bulk Plot Creation ────────────────────────────────────────────────────────
class PlotBulkCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=403)
        project_id = request.data.get('project_id')
        plots_data = request.data.get('plots', [])
        try:
            project = scope_to_company(Project.objects.all(), request.user).get(pk=project_id)
        except Project.DoesNotExist:
            return Response({'detail': 'Project not found.'}, status=404)
        plots = [
            Plot(
                project=project,
                number=p.get('number', ''),
                cluster_type=p.get('cluster_type', ''),
                status='available',
            )
            for p in plots_data
            if p.get('number')
        ]
        Plot.objects.bulk_create(plots)
        return Response({'created': len(plots)}, status=201)


class PlotBulkDeleteView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=403)
        project_id = request.data.get('project_id')
        if not project_id:
            return Response({'detail': 'project_id is required.'}, status=400)
        if not _project_in_scope(request, project_id):
            return Response({'detail': 'Project not found.'}, status=404)
        deleted, _ = Plot.objects.filter(project_id=project_id).delete()
        Project.objects.filter(pk=project_id).update(total_plots=0)
        return Response({'deleted': deleted})


class PlotRenameTypeView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=403)
        project_id = request.data.get('project_id')
        old_name   = request.data.get('old_name', '').strip()
        new_name   = request.data.get('new_name', '').strip()
        if not project_id or not old_name or not new_name:
            return Response({'detail': 'project_id, old_name and new_name are required.'}, status=400)
        if not _project_in_scope(request, project_id):
            return Response({'detail': 'Project not found.'}, status=404)
        updated = Plot.objects.filter(project_id=project_id, cluster_type=old_name).update(cluster_type=new_name)
        return Response({'updated': updated})


class SalesDataResetView(APIView):
    """Admin-only, company-scoped: wipe TRIAL transactional data (leads + their
    history/follow-ups/site-visits/closures, bookings, distribution log,
    availability, notifications) and reset all plots to 'available'. KEEPS setup:
    company, users, projects, plot definitions, lead sources, team/distribution
    config. POST requires confirm='DELETE'. GET returns current counts."""
    permission_classes = [IsAuthenticated]

    def _is_admin(self, user):
        return bool(getattr(user, 'is_staff', False) or getattr(user, 'role', '') == 'Admin' or is_platform_admin(user))

    def _counts(self, co):
        from accounts.models import Notification
        return {
            'leads':            Lead.objects.filter(company=co).count(),
            'follow_ups':       FollowUp.objects.filter(lead__company=co).count(),
            'site_visits':      SiteVisit.objects.filter(lead__company=co).count(),
            'bookings':         Booking.objects.filter(company=co).count(),
            'closures':         Closure.objects.filter(lead__company=co).count(),
            'lead_history':     LeadStatusHistory.objects.filter(lead__company=co).count(),
            'distribution_log': DistributionLog.objects.filter(company=co).count(),
            'availability':     UserAvailability.objects.filter(user__company=co).count(),
            'notifications':    Notification.objects.filter(recipient__company=co).count(),
            'plots_to_reset':   Plot.objects.filter(project__company=co).exclude(status='available').count(),
        }

    def get(self, request):
        if not self._is_admin(request.user):
            return Response({'detail': 'Admin only.'}, status=status.HTTP_403_FORBIDDEN)
        return Response(self._counts(_resolve_company(request)))

    def post(self, request):
        if not self._is_admin(request.user):
            return Response({'detail': 'Admin only.'}, status=status.HTTP_403_FORBIDDEN)
        if (request.data.get('confirm') or '') != 'DELETE':
            return Response({'detail': 'Type DELETE to confirm.'}, status=status.HTTP_400_BAD_REQUEST)
        co = _resolve_company(request)
        before = self._counts(co)
        with_attendance = bool(request.data.get('with_attendance'))
        with_loi        = bool(request.data.get('with_loi_files'))

        # Optionally purge confidential LOI PDFs from Supabase before deleting bookings.
        if with_loi:
            for b in Booking.objects.filter(company=co).exclude(loi_document=''):
                try: b.loi_document.delete(save=False)
                except Exception: pass

        from django.db import transaction
        from accounts.models import Notification
        with transaction.atomic():
            Booking.objects.filter(company=co).delete()
            Closure.objects.filter(lead__company=co).delete()
            SiteVisit.objects.filter(lead__company=co).delete()
            FollowUp.objects.filter(lead__company=co).delete()
            LeadStatusHistory.objects.filter(lead__company=co).delete()
            DistributionLog.objects.filter(company=co).delete()
            UserAvailability.objects.filter(user__company=co).delete()
            Notification.objects.filter(recipient__company=co).delete()
            Lead.objects.filter(company=co).delete()
            Plot.objects.filter(project__company=co).update(status='available')
            if with_attendance:
                from attendance.models import AttendanceRecord, LeaveApplication, LeaveTransaction, LeaveBalance
                AttendanceRecord.objects.filter(user__company=co).delete()
                LeaveApplication.objects.filter(user__company=co).delete()
                LeaveTransaction.objects.filter(user__company=co).delete()
                LeaveBalance.objects.filter(user__company=co).delete()
        return Response({'detail': 'Trial data cleared.', 'deleted': before})
