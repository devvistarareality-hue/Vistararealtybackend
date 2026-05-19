from datetime import date, timedelta
from decimal import Decimal

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from rest_framework import status

from .models import AttendanceRecord, LeaveBalance, LeaveApplication, LeaveTransaction
from .serializers import AttendanceRecordSerializer, LeaveApplicationSerializer, LeaveTransactionSerializer


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
        transactions = LeaveTransaction.objects.filter(user=request.user)
        serializer   = LeaveTransactionSerializer(transactions, many=True)
        return Response(serializer.data)


class LeaveHistoryView(APIView):
    """
    GET /api/attendance/leave-history/
    Returns all leave applications for the authenticated user grouped by month.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        applications = LeaveApplication.objects.filter(user=request.user)
        from collections import defaultdict

        groups = defaultdict(list)
        for app in applications:
            month_key = app.applied_on.strftime('%B %Y')
            session = app.get_day_type_display()
            if app.day_type == 'half_day' and app.session:
                session = app.get_session_display()
            groups[month_key].append({
                'id':         app.id,
                'name':       request.user.name,
                'avatar':     request.user.avatar_url,
                'session':    session,
                'date':       app.from_date.strftime('%a, %b %-d'),
                'leave_type': app.get_leave_type_display(),
                'status':     app.get_status_display(),
            })

        sections = [
            {'month': month, 'data': items}
            for month, items in groups.items()
        ]
        return Response(sections)


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
