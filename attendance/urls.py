from django.urls import path
from .views import (
    DashboardView, ApplyLeaveView, LeaveBalanceView, LeaveHistoryView,
    LeaveActionView, MonthlyAttendanceView, TodayAttendanceView, SignInView, SignOutView,
    ModifyAttendanceView,
)

urlpatterns = [
    path('dashboard/',              DashboardView.as_view(),          name='dashboard'),
    path('monthly/',                MonthlyAttendanceView.as_view(),  name='monthly-attendance'),
    path('today/',                  TodayAttendanceView.as_view(),    name='today-attendance'),
    path('sign-in/',                SignInView.as_view(),              name='sign-in'),
    path('sign-out/',               SignOutView.as_view(),             name='sign-out'),
    path('modify/',                 ModifyAttendanceView.as_view(),   name='modify-attendance'),
    path('apply-leave/',            ApplyLeaveView.as_view(),   name='apply-leave'),
    path('leave-balance/',          LeaveBalanceView.as_view(), name='leave-balance'),
    path('leave-history/',          LeaveHistoryView.as_view(), name='leave-history'),
    path('leave-action/<int:pk>/',  LeaveActionView.as_view(),  name='leave-action'),
]
