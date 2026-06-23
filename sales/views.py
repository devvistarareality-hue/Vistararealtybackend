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
    UserProjectAssignment,
)
from .serializers import (
    LeadListSerializer, LeadDetailSerializer, LeadCreateSerializer, LeadUpdateSerializer,
    LeadSourceSerializer, ProjectSerializer, PlotSerializer,
    FollowUpSerializer, SiteVisitSerializer, ClosureSerializer,
    LeadStatusHistorySerializer,
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



def _availability_active(avail):
    """True only if the user is marked available AND checked in within the last 12 hours."""
    if not avail or not avail.is_available or not avail.checked_in_at:
        return False
    return (timezone.now() - avail.checked_in_at) < timedelta(hours=AVAILABILITY_TTL_HOURS)


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
        cache_key = f'sales_stats:{request.user.id}:{company_id or "own"}'
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

        # Single aggregate query instead of 6 separate COUNTs
        agg = leads_qs.aggregate(
            total_leads=Count('id'),
            new_leads=Count('id', filter=Q(status='new')),
            leads_today=Count('id', filter=Q(created_at__date=today)),
        )
        sv_qs = scope_to_company(SiteVisit.objects.all(), request.user, 'lead__company')
        cl_qs = scope_to_company(Closure.objects.all(), request.user, 'lead__company')
        if not _sees_all_company(request.user):
            _ids = _visible_user_ids(request.user)
            sv_qs = sv_qs.filter(Q(stm__in=_ids) | Q(referred_by_telecaller__in=_ids))
            cl_qs = cl_qs.filter(Q(stm__in=_ids) | Q(referred_by_telecaller__in=_ids))
        sv_done, closures, active_projects = (
            sv_qs.filter(**sv_filter).count(),
            cl_qs.filter(**cl_filter).count(),
            scope_to_company(Project.objects.filter(is_active=True), request.user).filter(**prj_filter).count(),
        )
        # No .only() here: LeadListSerializer reads ~11 more fields (meta_*, statuses,
        # is_duplicate, …); deferring them caused a per-field query per lead (N+1).
        recent = leads_qs.select_related('project', 'source', 'telecaller', 'stm').order_by('-created_at')[:8]
        payload = {
            'total_leads':     agg['total_leads'],
            'new_leads':       agg['new_leads'],
            'leads_today':     agg['leads_today'],
            'sv_done':         sv_done,
            'closures':        closures,
            'active_projects': active_projects,
            'recent_leads':    LeadListSerializer(recent, many=True).data,
        }
        cache.set(cache_key, payload, timeout=20)
        return Response(payload)


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
        ser = LeadCreateSerializer(data=request.data)
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
            .filter(phone__regex=r'(^|\D)' + clean + r'$')
            if clean else Lead.objects.none()
        )
        existing = dup_qs.first()

        # CP Executives & STMs self-source leads — assign the new lead to the
        # creator so it lands in their own pipeline (CPs aren't in Meta distribution).
        extra = {}
        if is_cp(request.user) or is_stm(request.user):
            extra = {'stm': request.user}

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
        # CP/STM-sourced leads are already owned; only run telecaller distribution
        # for unowned (e.g. admin-added) leads.
        if not extra:
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
                from notifications import send_push_to_user
                send_push_to_user(
                    lead.telecaller.user_code,
                    'New Lead Assigned',
                    f'A new lead has been assigned to you.',
                )
        if old_stm_id != lead.stm_id:
            new_stm_name = lead.stm.name if lead.stm else ''
            history_entries.append(LeadStatusHistory(
                lead=lead, changed_by=request.user,
                field_changed='stm', old_value=old_stm_name, new_value=new_stm_name,
            ))
            if lead.stm:
                from notifications import send_push_to_user
                send_push_to_user(
                    lead.stm.user_code,
                    'New Lead Assigned',
                    f'A new lead has been assigned to you.',
                )
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
        if not project_id:
            return Response({'detail': 'project query param required.'}, status=status.HTTP_400_BAD_REQUEST)
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
        s = self._get_or_create(_resolve_company(request))
        return Response({
            'tc_signin_time':   str(s.tc_signin_time)[:5],
            'tc_signout_time':  str(s.tc_signout_time)[:5],
            'stm_signin_time':  str(s.stm_signin_time)[:5],
            'stm_signout_time': str(s.stm_signout_time)[:5],
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
        for a in UserAvailability.objects.filter(user__company=request.user.company, date=today):
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
        active = _availability_active(avail)
        expires_at = None
        if active and avail and avail.checked_in_at:
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
        active = _availability_active(obj)
        # Marking available flushes the unassigned bucket to this user's role (window-gated).
        if active:
            _run_distribution(request.user.company, _dist_type_for(request.user))
        expires_at = (obj.checked_in_at + timedelta(hours=AVAILABILITY_TTL_HOURS)).isoformat() if (active and obj.checked_in_at) else None
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

    # Only users marked available today and still within the 12h availability window.
    cutoff = timezone.now() - timedelta(hours=AVAILABILITY_TTL_HOURS)
    avail_ids = set(
        UserAvailability.objects.filter(
            user__company=company,
            user__designation__iexact=desig,
            date=today,
            is_available=True,
            checked_in_at__gte=cutoff,
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

        def _eligible(uid, lead):
            assigned = proj_map.get(uid)
            if not assigned or lead.project_id is None:
                return False
            return lead.project_id in assigned

        member_ids   = [m.id for m in members]
        id_to_member = {m.id: m for m in members}
        user_leads   = {m.id: [] for m in members}
        now = timezone.now()
        skipped = 0

        for lead in leads:
            eligible = [uid for uid in member_ids if _eligible(uid, lead)]
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
            from notifications import send_push_to_user
            send_push_to_user(
                id_to_member[uid].user_code,
                'New Lead Assigned',
                f'{len(pks)} new lead{"s" if len(pks) > 1 else ""} assigned to you.',
            )

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
class BulkImportLeadsView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)

        rows       = request.data.get('leads', [])
        project_id = request.data.get('project_id')
        source_id  = request.data.get('source_id')
        company    = request.user.company

        if not rows:
            return Response({'detail': 'No leads provided.'}, status=status.HTTP_400_BAD_REQUEST)

        # A supplied project/source must belong to the requester's company.
        if project_id and not _project_in_scope(request, project_id):
            return Response({'detail': 'Invalid project for your company.'}, status=status.HTTP_400_BAD_REQUEST)
        if source_id and not scope_to_company(LeadSource.objects.filter(pk=source_id), request.user).exists():
            return Response({'detail': 'Invalid source for your company.'}, status=status.HTTP_400_BAD_REQUEST)

        imported = 0
        duplicates = 0
        errors = 0
        failed = []

        # Build existing dup set (last-10-digits) scoped to this company — O(n) once.
        company_leads = scope_to_company(Lead.objects.all(), request.user)
        existing_keys = {
            ''.join(c for c in (p or '') if c.isdigit())[-10:]
            for p in company_leads.values_list('phone', flat=True)
        }
        existing_keys.discard('')

        to_create = []
        for i, row in enumerate(rows):
            name  = str(row.get('name', '')).strip()
            phone = str(row.get('phone', '')).strip()
            if not name or not phone:
                errors += 1
                failed.append({'row': i + 1, 'name': name, 'phone': phone, 'reason': 'Missing name or phone'})
                continue

            clean = ''.join(c for c in phone if c.isdigit())[-10:]
            is_dup = bool(clean) and clean in existing_keys

            to_create.append(Lead(
                company=company,
                name=name,
                phone=phone,
                alt_phone=str(row.get('alt_phone', '')).strip(),
                email=str(row.get('email', '')).strip(),
                project_id=project_id or None,
                source_id=source_id or None,
                meta_campaign_name=str(row.get('campaign', '')).strip(),
                meta_ad_name=str(row.get('creative', '')).strip(),
                is_duplicate=is_dup,
            ))
            if is_dup:
                duplicates += 1
            else:
                imported += 1
                if clean:
                    existing_keys.add(clean)  # catch in-batch duplicates too

        Lead.objects.bulk_create(to_create, ignore_conflicts=True)
        # Auto-assign the freshly imported bucket to available telecallers (window-gated).
        if imported:
            _run_distribution(company, 'telecaller')
        return Response({'imported': imported, 'duplicates': duplicates, 'errors': errors, 'failed': failed})


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
            agg = closure_qs.aggregate(total=Sum('booking_amount'), cnt=Count('id'))
            return {
                'total_sv':       sv_qs.count(),
                'completed_sv':   sv_qs.filter(status='completed').count(),
                'total_closures': agg['cnt'] or 0,
                'total_revenue':  float(agg['total'] or 0),
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
        company = user.company
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
        Lead.objects.filter(company=company, phone__regex=r'(^|\D)' + clean + r'$').first()
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
        return Response({
            'id': mapping.id, 'form_id': mapping.form_id,
            'form_name': mapping.form_name, 'project_id': mapping.project_id,
            'project_name': mapping.project.name, 'total_leads': mapping.total_leads,
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
