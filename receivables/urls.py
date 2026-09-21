from django.urls import path

from . import views

urlpatterns = [
    path('accounts/', views.ARRegisterView.as_view()),
    path('accounts/<int:pk>/', views.ARAccountView.as_view()),
    path('accounts/<int:pk>/statement/', views.ARStatementView.as_view()),
    path('accounts/<int:pk>/receipts/', views.ARReceiptCreateView.as_view()),
    path('receipts/<int:rid>/', views.ARReceiptView.as_view()),
    path('receipts/<int:rid>/audit/', views.ARReceiptAuditView.as_view()),
    path('dashboard/', views.ARDashboardView.as_view()),
    path('import/', views.ARImportView.as_view()),
    path('import/template/', views.ARImportTemplateView.as_view()),
]
