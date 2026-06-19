from django.urls import path
from .views import (
    VendorInvoiceListCreateView, VendorInvoiceDetailView,
    invoice_run_match, invoice_approve,
    PaymentListCreateView, PaymentDetailView,
    payables_summary,
)

urlpatterns = [
    path('invoices/',                            VendorInvoiceListCreateView.as_view(), name='invoice-list'),
    path('invoices/<int:pk>/',                   VendorInvoiceDetailView.as_view(),     name='invoice-detail'),
    path('invoices/<int:invoice_id>/run-match/', invoice_run_match,                     name='invoice-run-match'),
    path('invoices/<int:invoice_id>/approve/',   invoice_approve,                       name='invoice-approve'),
    path('payments/',                            PaymentListCreateView.as_view(),       name='payment-list'),
    path('payments/<int:pk>/',                   PaymentDetailView.as_view(),           name='payment-detail'),
    path('payables/',                            payables_summary,                      name='payables-summary'),
]
