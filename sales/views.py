import secrets
import requests as http_requests
from django.db.models import Q, Count
from django.utils import timezone
from django.http import HttpResponse
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated, AllowAny

from accounts.models import User
from .models import (
    Lead, LeadSource, Project, Plot, FollowUp, SiteVisit, Closure, LeadStatusHistory,
    DistributionSettings, UserAvailability, UserDistributionWeight, DistributionLog,
    SalesTeamMember, MetaWebhookConfig,
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


class StatsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        today = timezone.localdate()
        # Single aggregate query instead of 6 separate COUNTs
        agg = Lead.objects.aggregate(
            total_leads=Count('id'),
            new_leads=Count('id', filter=Q(status='new')),
            leads_today=Count('id', filter=Q(created_at__date=today)),
        )
        sv_done, closures, active_projects = (
            SiteVisit.objects.count(),
            Closure.objects.count(),
            Project.objects.filter(is_active=True).count(),
        )
        recent = Lead.objects.select_related('project', 'source', 'telecaller', 'stm').only(
            'id', 'name', 'phone', 'status', 'created_at',
            'project__name', 'source__name', 'telecaller__name', 'stm__name',
        ).order_by('-created_at')[:8]
        return Response({
            'total_leads':     agg['total_leads'],
            'new_leads':       agg['new_leads'],
            'leads_today':     agg['leads_today'],
            'sv_done':         sv_done,
            'closures':        closures,
            'active_projects': active_projects,
            'recent_leads':    LeadListSerializer(recent, many=True).data,
        })


class LeadListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        # Defer heavy text blobs not needed for list view
        qs = Lead.objects.select_related('project', 'source', 'telecaller', 'stm').defer(
            'telecaller_remarks', 'stm_remarks', 'requirement',
            'preferred_location', 'budget_min', 'budget_max',
            'meta_campaign_name', 'meta_ad_name',
        )

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
        if request.query_params.get('project_id'):
            qs = qs.filter(project_id=request.query_params['project_id'])
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

        total = qs.count()
        page = int(request.query_params.get('page', 1))
        offset = (page - 1) * PAGE_SIZE
        leads = qs[offset: offset + PAGE_SIZE]

        return Response({
            'count': total,
            'results': LeadListSerializer(leads, many=True).data,
        })

    def post(self, request):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)

        ser = LeadCreateSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)

        # Duplicate check
        phone = ser.validated_data['phone']
        project = ser.validated_data.get('project')
        clean = ''.join(c for c in phone if c.isdigit())[-10:]
        dup_qs = Lead.objects.filter(phone__endswith=clean)
        if project:
            dup_qs = dup_qs.filter(project=project)
        existing = dup_qs.first()

        lead = ser.save(
            is_duplicate=bool(existing),
            duplicate_of=existing if existing else None,
        )
        if existing:
            existing.duplicate_count += 1
            existing.save(update_fields=['duplicate_count'])

        return Response(LeadDetailSerializer(lead).data, status=status.HTTP_201_CREATED)


class LeadDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def _get_lead(self, pk):
        try:
            return Lead.objects.select_related('project', 'source', 'telecaller', 'stm').get(pk=pk)
        except Lead.DoesNotExist:
            return None

    def get(self, request, pk):
        lead = self._get_lead(pk)
        if not lead:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        data = LeadDetailSerializer(lead).data
        data['history'] = LeadStatusHistorySerializer(lead.history.all()[:20], many=True).data
        data['follow_ups'] = FollowUpSerializer(lead.follow_ups.all(), many=True).data
        data['site_visits'] = SiteVisitSerializer(lead.site_visits.all(), many=True).data
        return Response(data)

    def patch(self, request, pk):
        lead = self._get_lead(pk)
        if not lead:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

        old_status = lead.status
        old_tc_status = lead.telecaller_status
        old_stm_status = lead.stm_status

        ser = LeadUpdateSerializer(lead, data=request.data, partial=True)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        lead = ser.save()

        # Log status changes
        if old_status != lead.status:
            LeadStatusHistory.objects.create(
                lead=lead, changed_by=request.user,
                field_changed='status', old_value=old_status, new_value=lead.status,
            )
        if old_tc_status != lead.telecaller_status:
            LeadStatusHistory.objects.create(
                lead=lead, changed_by=request.user,
                field_changed='telecaller_status', old_value=old_tc_status, new_value=lead.telecaller_status,
            )
        if old_stm_status != lead.stm_status:
            LeadStatusHistory.objects.create(
                lead=lead, changed_by=request.user,
                field_changed='stm_status', old_value=old_stm_status, new_value=lead.stm_status,
            )

        return Response(LeadDetailSerializer(lead).data)

    def delete(self, request, pk):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        lead = self._get_lead(pk)
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
        deleted, _ = Lead.objects.filter(id__in=ids).delete()
        return Response({'deleted': deleted})


def _sync_plots(project):
    existing_count = project.plots.count()
    target = project.total_plots or 0
    if target > existing_count:
        Plot.objects.bulk_create([
            Plot(project=project, number=i)
            for i in range(existing_count + 1, target + 1)
        ])


class ProjectListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        projects = Project.objects.annotate(lead_count=Count('leads')).prefetch_related('plots')
        if request.query_params.get('active_only') == 'true':
            projects = projects.filter(is_active=True)
        return Response(ProjectSerializer(projects, many=True).data)

    def post(self, request):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        ser = ProjectSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        project = ser.save()
        _sync_plots(project)
        project = Project.objects.annotate(lead_count=Count('leads')).prefetch_related('plots').get(pk=project.pk)
        return Response(ProjectSerializer(project).data, status=status.HTTP_201_CREATED)


class ProjectDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        try:
            project = Project.objects.annotate(lead_count=Count('leads')).prefetch_related('plots').get(pk=pk)
        except Project.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(ProjectSerializer(project).data)

    def patch(self, request, pk):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        try:
            project = Project.objects.get(pk=pk)
        except Project.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        ser = ProjectSerializer(project, data=request.data, partial=True)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        project = ser.save()
        _sync_plots(project)
        project = Project.objects.annotate(lead_count=Count('leads')).prefetch_related('plots').get(pk=project.pk)
        return Response(ProjectSerializer(project).data)

    def delete(self, request, pk):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        try:
            project = Project.objects.get(pk=pk)
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
        plots = Plot.objects.filter(project_id=project_id)
        return Response(PlotSerializer(plots, many=True).data)


class PlotDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        try:
            plot = Plot.objects.get(pk=pk)
        except Plot.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        ser = PlotSerializer(plot, data=request.data, partial=True)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        return Response(PlotSerializer(ser.save()).data)


class LeadSourceListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        sources = LeadSource.objects.filter(is_active=True)
        return Response(LeadSourceSerializer(sources, many=True).data)

    def post(self, request):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        ser = LeadSourceSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        return Response(LeadSourceSerializer(ser.save()).data, status=status.HTTP_201_CREATED)


class FollowUpListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = FollowUp.objects.select_related('lead', 'assigned_to')
        if not is_admin_or_manager(request.user):
            qs = qs.filter(assigned_to=request.user)
        if request.query_params.get('lead_id'):
            qs = qs.filter(lead_id=request.query_params['lead_id'])
        return Response(FollowUpSerializer(qs, many=True).data)

    def post(self, request):
        ser = FollowUpSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        followup = ser.save(created_by=request.user)
        return Response(FollowUpSerializer(followup).data, status=status.HTTP_201_CREATED)


class FollowUpDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        try:
            followup = FollowUp.objects.get(pk=pk)
        except FollowUp.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        ser = FollowUpSerializer(followup, data=request.data, partial=True)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        return Response(FollowUpSerializer(ser.save()).data)


class SiteVisitListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = SiteVisit.objects.select_related('lead', 'project', 'stm')
        if not is_admin_or_manager(request.user):
            qs = qs.filter(stm=request.user)
        if request.query_params.get('lead_id'):
            qs = qs.filter(lead_id=request.query_params['lead_id'])
        return Response(SiteVisitSerializer(qs, many=True).data)

    def post(self, request):
        ser = SiteVisitSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        return Response(SiteVisitSerializer(ser.save()).data, status=status.HTTP_201_CREATED)


class SiteVisitDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        try:
            sv = SiteVisit.objects.get(pk=pk)
        except SiteVisit.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        ser = SiteVisitSerializer(sv, data=request.data, partial=True)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        return Response(SiteVisitSerializer(ser.save()).data)


class ClosureListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = Closure.objects.select_related('lead', 'project', 'stm')
        if not is_admin_or_manager(request.user):
            qs = qs.filter(stm=request.user)
        return Response(ClosureSerializer(qs, many=True).data)

    def post(self, request):
        ser = ClosureSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        return Response(ClosureSerializer(ser.save()).data, status=status.HTTP_201_CREATED)


class TelecallerListView(APIView):
    """Users who have 'Sales' in their modules — used for lead assignment."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        users = User.objects.filter(is_active=True, modules__contains=['Sales'])
        data = [{'id': u.id, 'name': u.name, 'user_code': u.user_code, 'role': u.role} for u in users]
        return Response(data)


class CompanyUsersSlimView(APIView):
    """Lightweight user list for Sales CRM — only fields the UI needs, no heavy JSONField serialization."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        users = (
            User.objects
            .filter(company=request.user.company, is_active=True)
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
        company = request.user.company
        members = SalesTeamMember.objects.select_related('user').filter(
            user__company=company,
            is_active=True,
        )
        crm_role = request.query_params.get('crm_role')
        if crm_role:
            members = members.filter(crm_role=crm_role)
        data = [{
            'id':          m.id,
            'user_id':     m.user.id,
            'name':        m.user.name,
            'email':       m.user.email,
            'phone':       m.user.phone,
            'user_code':   m.user.user_code,
            'designation': m.user.designation,
            'crm_role':    m.crm_role,
            'is_active':   m.is_active,
        } for m in members]
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
        s = self._get_or_create(request.user.company)
        return Response({
            'tc_signin_time':   str(s.tc_signin_time)[:5],
            'tc_signout_time':  str(s.tc_signout_time)[:5],
            'stm_signin_time':  str(s.stm_signin_time)[:5],
            'stm_signout_time': str(s.stm_signout_time)[:5],
        })

    def put(self, request):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        s = self._get_or_create(request.user.company)
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
        desig_map = {'TELECALLER': 'telecaller', 'STM': 'stm'}
        users = (
            User.objects
            .filter(company=request.user.company, is_active=True)
            .exclude(role='Admin')
            .filter(designation__in=['TELECALLER', 'STM'])
            .only('id', 'name', 'designation')
            .order_by('name')
        )
        avail_map = {
            a.user_id: a.is_available
            for a in UserAvailability.objects.filter(
                user__company=request.user.company, date=today
            )
        }
        data = []
        for u in users:
            data.append({
                'user_id':      u.id,
                'name':         u.name,
                'role':         desig_map.get(u.designation.upper(), u.designation.lower()),
                'is_available': avail_map.get(u.id, False),
            })
        return Response(data)

    def post(self, request):
        """Admin toggles any user's availability for today."""
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        from datetime import date as date_cls
        user_id      = request.data.get('user_id')
        is_available = request.data.get('is_available', True)
        today        = str(date_cls.today())
        try:
            user = User.objects.get(pk=user_id, company=request.user.company)
        except User.DoesNotExist:
            return Response({'detail': 'User not found.'}, status=404)
        obj, _ = UserAvailability.objects.update_or_create(
            user=user, date=today,
            defaults={'is_available': is_available, 'checked_in_at': timezone.now()},
        )
        return Response({'user_id': user.id, 'is_available': obj.is_available})


# ── Distribution Weights ──────────────────────────────────────────────────────
class DistributionWeightView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        users = (
            User.objects
            .filter(company=request.user.company, is_active=True, designation__in=['TELECALLER', 'STM'])
            .only('id', 'name', 'designation')
        )
        weight_map = {
            w.user_id: w.weight
            for w in UserDistributionWeight.objects.filter(user__company=request.user.company)
        }
        return Response([
            {'user_id': u.id, 'name': u.name, 'role': u.designation.upper(), 'weight': weight_map.get(u.id, 1)}
            for u in users
        ])

    def patch(self, request):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        updates = request.data.get('updates', [])  # [{user_id, weight}]
        for item in updates:
            uid = item.get('user_id')
            w   = max(1, int(item.get('weight', 1)))
            try:
                user = User.objects.get(pk=uid, company=request.user.company)
                UserDistributionWeight.objects.update_or_create(user=user, defaults={'weight': w})
            except User.DoesNotExist:
                pass
        return Response({'detail': 'Weights saved.'})


# ── Distribution ─────────────────────────────────────────────────────────────
class DistributeView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)

        from datetime import date as date_cls
        from zoneinfo import ZoneInfo

        dist_type = request.data.get('type', 'telecaller')   # 'telecaller' | 'stm'
        desig_map = {'telecaller': 'TELECALLER', 'stm': 'STM'}
        desig     = desig_map.get(dist_type, 'TELECALLER')

        # Check signout window (IST-aware)
        settings = DistributionSettings.objects.filter(company=request.user.company).first()
        if settings:
            ist = ZoneInfo('Asia/Kolkata')
            now_ist = timezone.now().astimezone(ist)
            current_time = now_ist.strftime('%H:%M')
            signout = str(getattr(settings, f'{dist_type}_signout_time'))[:5]
            if current_time >= signout:
                return Response({
                    'distributed': 0,
                    'message': f'Distribution window closed (signout at {signout}). Leads will remain unassigned.',
                })

        today = str(date_cls.today())

        # Only users who are signed in today
        avail_ids = set(
            UserAvailability.objects.filter(
                user__company=request.user.company,
                user__designation__iexact=desig,
                date=today,
                is_available=True,
            ).values_list('user_id', flat=True)
        )
        if not avail_ids:
            return Response({'distributed': 0, 'message': f'No {desig}s have signed in today.'})

        members = list(
            User.objects.filter(pk__in=avail_ids, is_active=True)
            .only('id', 'name')
        )
        if not members:
            return Response({'distributed': 0, 'message': f'No active {desig} users available.'})

        # Load weights
        weight_map = {
            w.user_id: w.weight
            for w in UserDistributionWeight.objects.filter(user__in=members)
        }

        # Get unassigned leads
        if dist_type == 'telecaller':
            qs = Lead.objects.filter(
                telecaller__isnull=True, status='new'
            ).order_by('created_at')
        else:
            qs = Lead.objects.filter(
                status='warm_transferred', stm__isnull=True
            ).order_by('created_at')

        leads = list(qs)
        if not leads:
            return Response({'distributed': 0, 'message': 'No unassigned leads found.'})

        # Today's existing assignment counts (for fair weighted continuation)
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

        # Weighted round-robin: pick member with lowest (assigned / weight) ratio
        member_ids = [m.id for m in members]
        id_to_member = {m.id: m for m in members}
        user_leads: dict[int, list] = {m.id: [] for m in members}
        now = timezone.now()

        for lead in leads:
            best = min(member_ids, key=lambda uid: counts[uid] / (weight_map.get(uid, 1)))
            user_leads[best].append(lead.pk)
            counts[best] += 1

        # Batch update
        assignments = []
        for uid, pks in user_leads.items():
            if not pks:
                continue
            if dist_type == 'telecaller':
                Lead.objects.filter(pk__in=pks).update(
                    telecaller_id=uid, status='assigned', telecaller_assigned_at=now,
                )
            else:
                Lead.objects.filter(pk__in=pks).update(stm_id=uid, stm_assigned_at=now)
            assignments.append({'name': id_to_member[uid].name, 'count': len(pks)})

        distributed = sum(a['count'] for a in assignments)
        DistributionLog.objects.create(
            dist_type=dist_type,
            triggered_by=request.user,
            leads_distributed=distributed,
            details={'assignments': assignments},
        )
        return Response({'distributed': distributed, 'assignments': {a['name']: a['count'] for a in assignments}})


class DistributionLogView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        logs = DistributionLog.objects.select_related('triggered_by').all()[:30]
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
        DistributionLog.objects.all().delete()
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

        if not rows:
            return Response({'detail': 'No leads provided.'}, status=status.HTTP_400_BAD_REQUEST)

        imported = 0
        duplicates = 0
        errors = 0
        failed = []

        # Build existing phone set for quick dup check
        existing_phones = set(
            Lead.objects.filter(project_id=project_id).values_list('phone', flat=True)
        ) if project_id else set(Lead.objects.values_list('phone', flat=True))

        to_create = []
        for i, row in enumerate(rows):
            name  = str(row.get('name', '')).strip()
            phone = str(row.get('phone', '')).strip()
            if not name or not phone:
                errors += 1
                failed.append({'row': i + 1, 'name': name, 'phone': phone, 'reason': 'Missing name or phone'})
                continue

            clean = ''.join(c for c in phone if c.isdigit())[-10:]
            is_dup = any(''.join(c for c in p if c.isdigit())[-10:] == clean for p in existing_phones)

            to_create.append(Lead(
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
                existing_phones.add(phone)

        Lead.objects.bulk_create(to_create, ignore_conflicts=True)
        return Response({'imported': imported, 'duplicates': duplicates, 'errors': errors, 'failed': failed})


# ── Reports ───────────────────────────────────────────────────────────────────
class ReportsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from django.db.models import Count, Sum, Q
        from concurrent.futures import ThreadPoolExecutor

        def get_campaigns():
            return list(
                Lead.objects.exclude(meta_campaign_name='')
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
                Lead.objects.exclude(telecaller__isnull=True)
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
                Lead.objects.exclude(stm__isnull=True)
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
            agg = Closure.objects.aggregate(total=Sum('booking_amount'), cnt=Count('id'))
            return {
                'total_sv':       SiteVisit.objects.count(),
                'completed_sv':   SiteVisit.objects.filter(status='completed').count(),
                'total_closures': agg['cnt'] or 0,
                'total_revenue':  float(agg['total'] or 0),
                'meta_leads':     Lead.objects.exclude(meta_campaign_name='').count(),
            }

        def get_closures():
            return Closure.objects.select_related('lead', 'project', 'stm', 'referred_by_telecaller').order_by('-closure_date')[:20]

        # Run all 5 queries in parallel threads
        with ThreadPoolExecutor(max_workers=5) as ex:
            f_camp  = ex.submit(get_campaigns)
            f_tc    = ex.submit(get_telecallers)
            f_stm   = ex.submit(get_stms)
            f_summ  = ex.submit(get_summary)
            f_close = ex.submit(get_closures)

        return Response({
            'campaigns':  f_camp.result(),
            'telecallers': f_tc.result(),
            'stms':        f_stm.result(),
            'closures':    ClosureSerializer(f_close.result(), many=True).data,
            'summary':     f_summ.result(),
        })


# ──────────────────────────────────────────────
#  Meta Lead Ads Webhook
# ──────────────────────────────────────────────

def _fetch_meta_lead_data(leadgen_id, page_access_token):
    """Call Meta Graph API to get lead field data."""
    try:
        url = f'https://graph.facebook.com/v19.0/{leadgen_id}'
        r = http_requests.get(url, params={'access_token': page_access_token}, timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def _create_lead_from_meta(field_data, config, campaign_name='', ad_name=''):
    """Parse Meta field_data list and create a Lead."""
    fields = {f['name']: f['values'][0] for f in field_data if f.get('values')}
    name  = fields.get('full_name') or fields.get('name') or fields.get('first_name', '') + ' ' + fields.get('last_name', '')
    phone = fields.get('phone_number') or fields.get('phone') or ''
    email = fields.get('email', '')
    name  = name.strip()
    phone = phone.strip()
    if not name and not phone:
        return None
    source, _ = LeadSource.objects.get_or_create(name='meta', defaults={'is_active': True})
    lead = Lead.objects.create(
        name=name or 'Meta Lead',
        phone=phone,
        email=email,
        source=source,
        project=config.default_project,
        meta_campaign_name=campaign_name,
        meta_ad_name=ad_name,
        status='new',
    )
    MetaWebhookConfig.objects.filter(pk=config.pk).update(
        total_leads_received=config.total_leads_received + 1,
        last_lead_at=timezone.now(),
        is_active=True,
    )
    return lead


class MetaWebhookView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        """Meta webhook verification challenge."""
        mode      = request.GET.get('hub.mode')
        token     = request.GET.get('hub.verify_token')
        challenge = request.GET.get('hub.challenge')
        config = MetaWebhookConfig.objects.first()
        if mode == 'subscribe' and config and token == config.verify_token:
            return HttpResponse(challenge, content_type='text/plain')
        return HttpResponse(status=403)

    def post(self, request):
        """Receive lead notification from Meta."""
        data = request.data
        if data.get('object') != 'page':
            return Response({'ok': True})
        config = MetaWebhookConfig.objects.filter(page_access_token__gt='').first()
        if not config:
            return Response({'ok': True})
        for entry in data.get('entry', []):
            for change in entry.get('changes', []):
                if change.get('field') == 'leadgen':
                    val          = change.get('value', {})
                    leadgen_id   = val.get('leadgen_id')
                    campaign     = val.get('campaign_name', '')
                    ad           = val.get('ad_name', '')
                    if leadgen_id:
                        meta_data = _fetch_meta_lead_data(leadgen_id, config.page_access_token)
                        if meta_data and meta_data.get('field_data'):
                            _create_lead_from_meta(meta_data['field_data'], config, campaign, ad)
        return Response({'ok': True})


class MetaWebhookConfigView(APIView):
    permission_classes = [IsAuthenticated]

    def _ensure_config(self):
        config = MetaWebhookConfig.objects.first()
        if not config:
            config = MetaWebhookConfig.objects.create(
                verify_token=secrets.token_urlsafe(32),
            )
        return config

    def get(self, request):
        config = self._ensure_config()
        projects = list(Project.objects.filter(is_active=True).values('id', 'name'))
        return Response({
            'verify_token':         config.verify_token,
            'page_access_token':    config.page_access_token,
            'default_project_id':   config.default_project_id,
            'is_active':            config.is_active,
            'total_leads_received': config.total_leads_received,
            'last_lead_at':         config.last_lead_at,
            'projects':             projects,
        })

    def post(self, request):
        config = self._ensure_config()
        action = request.data.get('action')
        if action == 'regenerate_token':
            config.verify_token = secrets.token_urlsafe(32)
            config.save(update_fields=['verify_token'])
            return Response({'verify_token': config.verify_token})
        if action == 'save':
            pat = request.data.get('page_access_token', '').strip()
            pid = request.data.get('default_project_id')
            config.page_access_token = pat
            config.default_project_id = pid if pid else None
            config.is_active = bool(pat)
            config.save(update_fields=['page_access_token', 'default_project_id', 'is_active'])
            return Response({'ok': True, 'is_active': config.is_active})
        return Response({'detail': 'Unknown action'}, status=400)
