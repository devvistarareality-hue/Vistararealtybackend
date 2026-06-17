from django.db.models import Q, Count
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated

from accounts.models import User
from .models import Lead, LeadSource, Project, FollowUp, SiteVisit, Closure, LeadStatusHistory
from .serializers import (
    LeadListSerializer, LeadDetailSerializer, LeadCreateSerializer, LeadUpdateSerializer,
    LeadSourceSerializer, ProjectSerializer,
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
        total_leads    = Lead.objects.count()
        new_leads      = Lead.objects.filter(status='new').count()
        leads_today    = Lead.objects.filter(created_at__date=today).count()
        sv_done        = SiteVisit.objects.count()
        closures       = Closure.objects.count()
        active_projects = Project.objects.filter(is_active=True).count()

        recent = Lead.objects.select_related('project', 'source', 'telecaller', 'stm').order_by('-created_at')[:8]
        return Response({
            'total_leads':     total_leads,
            'new_leads':       new_leads,
            'leads_today':     leads_today,
            'sv_done':         sv_done,
            'closures':        closures,
            'active_projects': active_projects,
            'recent_leads':    LeadListSerializer(recent, many=True).data,
        })


class LeadListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = Lead.objects.select_related('project', 'source', 'telecaller', 'stm')

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


class ProjectListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        projects = Project.objects.all()
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
        return Response(ProjectSerializer(project).data, status=status.HTTP_201_CREATED)


class ProjectDetailView(APIView):
    permission_classes = [IsAuthenticated]

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
        return Response(ProjectSerializer(ser.save()).data)

    def delete(self, request, pk):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        try:
            project = Project.objects.get(pk=pk)
        except Project.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        project.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


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
