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


class ProjectListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        projects = Project.objects.annotate(lead_count=Count('leads'))
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
from .models import SalesTeamMember, DistributionLog


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


# ── Distribution ─────────────────────────────────────────────────────────────
class DistributeView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not is_admin_or_manager(request.user):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)

        dist_type  = request.data.get('type', 'telecaller')   # 'telecaller' | 'stm'
        project_id = request.data.get('project_id')
        count      = int(request.data.get('count', 10))

        # Get available team members for this role
        members = SalesTeamMember.objects.filter(crm_role=dist_type, is_active=True).select_related('user')
        if not members:
            return Response({'detail': f'No active {dist_type}s in the sales team.'}, status=status.HTTP_400_BAD_REQUEST)

        # Get unassigned leads
        if dist_type == 'telecaller':
            qs = Lead.objects.filter(telecaller__isnull=True)
        else:
            qs = Lead.objects.filter(status='warm_transferred', stm__isnull=True)

        if project_id:
            qs = qs.filter(project_id=project_id)

        unassigned = list(qs.order_by('created_at')[:count * len(members)])
        if not unassigned:
            return Response({'distributed': 0, 'message': f'No unassigned leads found.'})

        # Round-robin distribution
        assignments = {}
        for i, lead in enumerate(unassigned):
            member = members[i % len(members)]
            if dist_type == 'telecaller':
                lead.telecaller = member.user
                lead.status     = 'assigned'
                lead.telecaller_assigned_at = timezone.now()
            else:
                lead.stm = member.user
                lead.stm_assigned_at = timezone.now()
            Lead.objects.filter(pk=lead.pk).update(
                **({'telecaller': member.user, 'status': 'assigned', 'telecaller_assigned_at': timezone.now()}
                   if dist_type == 'telecaller'
                   else {'stm': member.user, 'stm_assigned_at': timezone.now()})
            )
            name = member.user.name
            assignments[name] = assignments.get(name, 0) + 1

        log = DistributionLog.objects.create(
            dist_type=dist_type,
            triggered_by=request.user,
            leads_distributed=len(unassigned),
            details={'assignments': [{'name': k, 'count': v} for k, v in assignments.items()]},
        )
        return Response({'distributed': len(unassigned), 'assignments': assignments})


class DistributionLogView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        logs = DistributionLog.objects.select_related('triggered_by').all()[:30]
        data = [{
            'id':                log.id,
            'dist_type':         log.dist_type,
            'leads_distributed': log.leads_distributed,
            'triggered_by':      log.triggered_by.name if log.triggered_by else 'System',
            'details':           log.details,
            'created_at':        log.created_at,
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
