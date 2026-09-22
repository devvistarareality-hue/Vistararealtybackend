from django.urls import path

from . import collections, views

urlpatterns = [
    path('accounts/', views.ARRegisterView.as_view()),
    path('accounts/<int:pk>/', views.ARAccountView.as_view()),
    path('accounts/<int:pk>/statement/', views.ARStatementView.as_view()),
    path('accounts/<int:pk>/booking/', views.ARBookingView.as_view()),
    path('accounts/<int:pk>/loi-url/', views.ARLoiUrlView.as_view()),
    path('accounts/<int:pk>/receipts/', views.ARReceiptCreateView.as_view()),
    path('receipts/<int:rid>/', views.ARReceiptView.as_view()),
    path('receipts/<int:rid>/audit/', views.ARReceiptAuditView.as_view()),
    path('collections/', collections.ARCollectionsView.as_view()),
    path('accounts/<int:pk>/followups/', collections.ARAccountFollowUpsView.as_view()),
    path('followups/', collections.ARFollowUpListView.as_view()),
    path('followups/<int:fid>/', collections.ARFollowUpView.as_view()),
    path('assignees/', collections.ARAssigneesView.as_view()),
    path('dashboard/', views.ARDashboardView.as_view()),
    path('import/', views.ARImportView.as_view()),
    path('import/template/', views.ARImportTemplateView.as_view()),
]
