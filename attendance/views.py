from datetime import date, timedelta
from decimal import Decimal

from django.utils import timezone

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.pagination import PageNumberPagination

from rest_framework import status

from .models import AttendanceRecord, LeaveBalance, LeaveApplication, LeaveTransaction
from .serializers import AttendanceRecordSerializer, LeaveApplicationSerializer, LeaveTransactionSerializer


class OptionalPageNumberPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


def _pagination_requested(request):
    return 'page' in request.query_params or 'page_size' in request.query_params


def _paginated_response(request, queryset, serializer_class, response_key):
    paginator = OptionalPageNumberPagination()
    page = paginator.paginate_queryset(queryset, request)
    serializer = serializer_class(page, many=True)
    response = paginator.get_paginated_response(serializer.data)
    response.data[response_key] = response.data.pop('results')
    return response


def _fmt_hours(hours):
    if not hours:
        return '00:00'
    h = int(hours)
    m = int((float(hours) - h) * 60)
    return f'{h:02d}:{m:02d}'


def _get_week_range(today):
    """Returns Monday to Saturday of the current week."""
    monday = today - timedelta(days=today.weekday())  # weekday(): Mon=0
    return [monday + timedelta(days=i) for i in range(6)]  # Mon–Sat


class DashboardView(APIView):
    """
    GET /api/attendance/dashboard/
    Returns all data needed for the home screen.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user  = request.user
        today = date.today()

        # ── User info ──────────────────────────────────────────────
        user_data = {
            'name':         user.name,
            'designation':  user.designation,
            'role':         user.role,
            'user_code':    user.user_code,
            'department':   user.department,
            'organisation': user.company.name if user.company else '',
            'avatar_url':   user.avatar_url,
        }

        # ── Today's attendance ─────────────────────────────────────
        try:
            today_record = AttendanceRecord.objects.get(user=user, date=today)
            work_today   = _fmt_hours(today_record.total_hours)
        except AttendanceRecord.DoesNotExist:
            work_today = '00:00'

        # ── Weekly stats ───────────────────────────────────────────
        week_dates   = _get_week_range(today)
        week_records = AttendanceRecord.objects.filter(user=user, date__in=week_dates)
        total_week   = sum(r.total_hours for r in week_records) or Decimal('0.00')
        worked_this_week = _fmt_hours(total_week)

        # ── Leave balance ──────────────────────────────────────────
        try:
            lb = LeaveBalance.objects.get(user=user)
            leaves_available = float(lb.available)
            leaves_utilised  = float(lb.utilised)
        except LeaveBalance.DoesNotExist:
            leaves_available = 0.0
            leaves_utilised  = 0.0

        # ── Weekly attendance table ────────────────────────────────
        record_map = {r.date: r for r in week_records}
        weekly_attendance = []
        for d in week_dates:
            if d in record_map:
                rec = record_map[d]
                weekly_attendance.append({
                    'date':     d.strftime('%d %b'),
                    'day':      d.strftime('%a'),
                    'in_time':  rec.in_time.strftime('%H:%M') if rec.in_time else '00:00',
                    'out_time': rec.out_time.strftime('%H:%M') if rec.out_time else '00:00',
                    'total':    _fmt_hours(rec.total_hours),
                })
            else:
                weekly_attendance.append({
                    'date':     d.strftime('%d %b'),
                    'day':      d.strftime('%a'),
                    'in_time':  '00:00',
                    'out_time': '00:00',
                    'total':    '00:00',
                })

        return Response({
            'user': user_data,
            'stats': {
                'work_today':        work_today,
                'worked_this_week':  worked_this_week,
                'leaves_available':  leaves_available,
                'leaves_utilised':   leaves_utilised,
            },
            'weekly_attendance': weekly_attendance,
        })


class LeaveBalanceView(APIView):
    """
    GET /api/attendance/leave-balance/
    Returns all leave transactions for the authenticated user.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        transactions = LeaveTransaction.objects.filter(user=request.user).order_by('-date', '-id')
        if _pagination_requested(request):
            return _paginated_response(request, transactions, LeaveTransactionSerializer, 'transactions')

        serializer   = LeaveTransactionSerializer(transactions, many=True)
        return Response(serializer.data)


class LeaveHistoryView(APIView):
    """
    GET /api/attendance/leave-history/
    Returns all leave applications for the authenticated user grouped by month.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        applications = LeaveApplication.objects.filter(user=request.user).order_by('-from_date', '-id')
        if _pagination_requested(request):
            paginator = OptionalPageNumberPagination()
            page = paginator.paginate_queryset(applications, request)
            sections = self._group_applications(page, request.user)
            response = paginator.get_paginated_response(sections)
            response.data['sections'] = response.data.pop('results')
            return response

        sections = self._group_applications(applications, request.user)
        return Response(sections)

    def _group_applications(self, applications, user):
        from collections import defaultdict

        groups = defaultdict(list)
        for app in applications:
            month_key = app.from_date.strftime('%B %Y')
            session = app.get_day_type_display()
            if app.day_type == 'half_day' and app.session:
                session = app.get_session_display()
            groups[month_key].append({
                'id':          app.id,
                'name':        user.name,
                'avatar':      user.avatar_url,
                'session':     session,
                'date':        app.from_date.strftime('%a, %b %-d'),
                'from_date':   app.from_date.strftime('%-d %b %Y'),
                'to_date':     app.to_date.strftime('%-d %b %Y') if app.to_date else None,
                'leave_type':  app.get_leave_type_display(),
                'work_type':   app.get_work_type_display(),
                'day_type':    app.get_day_type_display(),
                'description': app.description,
                'applied_on':  app.applied_on.strftime('%-d %b %Y'),
                'status':      app.get_status_display(),
            })

        return [
            {'month': month, 'data': items}
            for month, items in groups.items()
        ]


class LeaveActionView(APIView):
    """
    PATCH /api/attendance/leave-action/<pk>/
    Approves or rejects a leave application.
    """
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        try:
            application = LeaveApplication.objects.get(pk=pk)
        except LeaveApplication.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

        new_status = request.data.get('status')
        if new_status not in ['approved', 'rejected']:
            return Response({'detail': 'Invalid status. Use approved or rejected.'}, status=status.HTTP_400_BAD_REQUEST)

        old_status = application.status
        application.status = new_status
        application.save()

        if new_status == 'approved' and old_status != 'approved':
            if application.day_type == 'half_day':
                leave_days = Decimal('0.5')
            else:
                if application.to_date and application.to_date > application.from_date:
                    leave_days = Decimal((application.to_date - application.from_date).days + 1)
                else:
                    leave_days = Decimal('1')

            lb, _ = LeaveBalance.objects.get_or_create(user=application.user)
            new_balance = lb.available - leave_days

            LeaveTransaction.objects.create(
                user=application.user,
                date=timezone.now(),
                leave_date=application.from_date,
                leave_type=application.leave_type,
                description='leave_applied',
                change=-leave_days,
                balance=new_balance,
            )

            lb.available = new_balance
            lb.utilised  = lb.utilised + leave_days
            lb.save()

        return Response({'message': f'Leave {new_status} successfully.', 'status': new_status})


class ApplyLeaveView(APIView):
    """
    POST /api/attendance/apply-leave/
    Creates a new leave application for the authenticated user.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = LeaveApplicationSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save(user=request.user)
            return Response(
                {'message': 'Leave application submitted successfully.', 'data': serializer.data},
                status=status.HTTP_201_CREATED,
            )
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
