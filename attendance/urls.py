from django.urls import path
from .views import DashboardView, ApplyLeaveView, LeaveBalanceView, LeaveHistoryView

urlpatterns = [
    path('dashboard/',      DashboardView.as_view(),    name='dashboard'),
    path('apply-leave/',    ApplyLeaveView.as_view(),   name='apply-leave'),
    path('leave-balance/',  LeaveBalanceView.as_view(), name='leave-balance'),
    path('leave-history/',  LeaveHistoryView.as_view(), name='leave-history'),
]
