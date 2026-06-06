from django.db.models import Count, Q

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import User
from .models import Lead, LeadActivity, Project
from .serializers import LeadSerializer, ProjectSerializer


# ─── helpers ────────────────────────────────────────────────────────────────

def _team_queryset():
    return (
        User.objects
        .filter(is_active=True, role__in=['STM', 'Sales Executive'])
        .annotate(lead_count=Count('assigned_leads'))
    )


def _team_data(qs):
    return [
        {
            'id':         m.id,
            'name':       m.name,
            'role':       m.role,
            'initials':   ''.join(n[0] for n in m.name.split()[:2]).upper() if m.name else '??',
            'lead_count': m.lead_count,
        }
        for m in qs
    ]


# ─── Dashboard ──────────────────────────────────────────────────────────────

class PreSalesDashboardView(APIView):
    """GET /api/presales/dashboard/"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        stats = {
            'total': Lead.objects.count(),
            'new':   Lead.objects.filter(status='New').count(),
            'cold':  Lead.objects.filter(status='Cold').count(),
            'warm':  Lead.objects.filter(status='Warm').count(),
            'lost':  Lead.objects.filter(status='Lost').count(),
        }

        recent_leads = (
            Lead.objects
            .select_related('project', 'assigned_to')
            .prefetch_related('activities')
            .order_by('-created_at')[:5]
        )

        team = _team_queryset().order_by('-lead_count')

        active_projects = (
            Project.objects
            .annotate(_lead_count=Count('leads'))
            .filter(status='Active')
            .order_by('-_lead_count')[:4]
        )
        for p in active_projects:
            p._lead_count = p._lead_count

        return Response({
            'stats':           stats,
            'recent_leads':    LeadSerializer(recent_leads, many=True).data,
            'team_queue':      _team_data(team),
            'active_projects': ProjectSerializer(active_projects, many=True).data,
        })


# ─── Team ───────────────────────────────────────────────────────────────────

class TeamMembersView(APIView):
    """GET /api/presales/team/"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(_team_data(_team_queryset().order_by('name')))


# ─── Projects ───────────────────────────────────────────────────────────────

class ProjectListCreateView(APIView):
    """GET /api/presales/projects/   POST /api/presales/projects/"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = Project.objects.annotate(_lead_count=Count('leads'))

        type_   = request.query_params.get('type')
        status_ = request.query_params.get('status')
        search  = request.query_params.get('search')

        if type_:
            qs = qs.filter(type=type_)
        if status_:
            qs = qs.filter(status=status_)
        if search:
            qs = qs.filter(Q(name__icontains=search) | Q(location__icontains=search))

        for p in qs:
            p._lead_count = p._lead_count

        return Response(ProjectSerializer(qs, many=True).data)

    def post(self, request):
        serializer = ProjectSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save(created_by=request.user)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class ProjectDetailView(APIView):
    """GET / PATCH / DELETE /api/presales/projects/<pk>/"""
    permission_classes = [IsAuthenticated]

    def _get(self, pk):
        try:
            p = Project.objects.annotate(_lead_count=Count('leads')).get(pk=pk)
            p._lead_count = p._lead_count
            return p
        except Project.DoesNotExist:
            return None

    def get(self, request, pk):
        project = self._get(pk)
        if not project:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(ProjectSerializer(project).data)

    def patch(self, request, pk):
        project = self._get(pk)
        if not project:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        serializer = ProjectSerializer(project, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(ProjectSerializer(self._get(pk)).data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, pk):
        project = self._get(pk)
        if not project:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        project.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ─── Leads ──────────────────────────────────────────────────────────────────

class LeadListCreateView(APIView):
    """GET /api/presales/leads/   POST /api/presales/leads/"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = Lead.objects.select_related('project', 'assigned_to').prefetch_related('activities')

        status_ = request.query_params.get('status')
        project = request.query_params.get('project')
        assignee = request.query_params.get('assigned_to')
        search  = request.query_params.get('search')

        if status_:
            qs = qs.filter(status=status_)
        if project:
            qs = qs.filter(project_id=project)
        if assignee:
            qs = qs.filter(assigned_to_id=assignee)
        if search:
            qs = qs.filter(
                Q(name__icontains=search) |
                Q(phone__icontains=search) |
                Q(project__name__icontains=search),
            )

        return Response(LeadSerializer(qs, many=True).data)

    def post(self, request):
        serializer = LeadSerializer(data=request.data)
        if serializer.is_valid():
            lead = serializer.save(created_by=request.user)
            LeadActivity.objects.create(
                lead=lead,
                type='Enquiry',
                note=f'Lead created via {lead.get_source_display()}.',
                created_by=request.user,
            )
            return Response(
                LeadSerializer(
                    Lead.objects.select_related('project', 'assigned_to')
                    .prefetch_related('activities')
                    .get(pk=lead.pk)
                ).data,
                status=status.HTTP_201_CREATED,
            )
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class LeadDetailView(APIView):
    """GET / PATCH / DELETE /api/presales/leads/<pk>/"""
    permission_classes = [IsAuthenticated]

    def _get(self, pk):
        try:
            return (
                Lead.objects
                .select_related('project', 'assigned_to')
                .prefetch_related('activities')
                .get(pk=pk)
            )
        except Lead.DoesNotExist:
            return None

    def get(self, request, pk):
        lead = self._get(pk)
        if not lead:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(LeadSerializer(lead).data)

    def patch(self, request, pk):
        lead = self._get(pk)
        if not lead:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        serializer = LeadSerializer(lead, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(LeadSerializer(self._get(pk)).data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, pk):
        lead = self._get(pk)
        if not lead:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        lead.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ─── Lead Status Change ──────────────────────────────────────────────────────

class LeadStatusChangeView(APIView):
    """PATCH /api/presales/leads/<pk>/status/"""
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        try:
            lead = Lead.objects.get(pk=pk)
        except Lead.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

        new_status = request.data.get('status')
        note       = request.data.get('note', '').strip()

        if new_status not in ['New', 'Cold', 'Warm', 'Lost']:
            return Response(
                {'detail': 'Status must be one of: New, Cold, Warm, Lost.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        old_status  = lead.status
        lead.status = new_status
        lead.save()

        activity_note = f'Status changed from {old_status} to {new_status}.'
        if note:
            activity_note += f'\nRemark: {note}'

        LeadActivity.objects.create(
            lead=lead,
            type='Status Change',
            note=activity_note,
            created_by=request.user,
        )

        return Response(
            LeadSerializer(
                Lead.objects.select_related('project', 'assigned_to')
                .prefetch_related('activities')
                .get(pk=lead.pk)
            ).data
        )


# ─── Lead Transfer ───────────────────────────────────────────────────────────

class LeadTransferView(APIView):
    """POST /api/presales/leads/<pk>/transfer/"""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        try:
            lead = Lead.objects.get(pk=pk)
        except Lead.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

        assignee_id = request.data.get('assignee_id')
        note        = request.data.get('note', '').strip()

        if assignee_id:
            try:
                assignee = User.objects.get(pk=assignee_id)
            except User.DoesNotExist:
                return Response({'detail': 'Assignee not found.'}, status=status.HTTP_404_NOT_FOUND)
        else:
            # Auto-assign: team member with fewest active leads
            assignee = (
                _team_queryset()
                .order_by('lead_count', 'name')
                .first()
            )
            if not assignee:
                return Response(
                    {'detail': 'No team members available for auto-assign.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        lead.assigned_to = assignee
        lead.status      = 'Warm'
        lead.save()

        activity_note = f'Lead transferred to {assignee.name} ({assignee.role}) and marked Warm.'
        if note:
            activity_note += f'\nRemark: {note}'

        LeadActivity.objects.create(
            lead=lead,
            type='Transfer',
            note=activity_note,
            created_by=request.user,
        )

        return Response(
            LeadSerializer(
                Lead.objects.select_related('project', 'assigned_to')
                .prefetch_related('activities')
                .get(pk=lead.pk)
            ).data
        )


# ─── Lead Follow-up ──────────────────────────────────────────────────────────

class LeadFollowupView(APIView):
    """PATCH /api/presales/leads/<pk>/followup/"""
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        from datetime import date as date_cls

        try:
            lead = Lead.objects.get(pk=pk)
        except Lead.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

        followup_str = request.data.get('next_followup')

        if followup_str:
            try:
                parsed = date_cls.fromisoformat(str(followup_str))
            except ValueError:
                return Response(
                    {'detail': 'Invalid date format. Use YYYY-MM-DD.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            lead.next_followup = parsed
            note = f'Follow-up scheduled for {parsed.strftime("%-d %b %Y")}.'
        else:
            lead.next_followup = None
            note = 'Follow-up date cleared.'

        lead.save()

        LeadActivity.objects.create(
            lead=lead, type='Note', note=note, created_by=request.user,
        )

        return Response(
            LeadSerializer(
                Lead.objects.select_related('project', 'assigned_to')
                .prefetch_related('activities')
                .get(pk=lead.pk)
            ).data
        )
