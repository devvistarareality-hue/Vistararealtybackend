from rest_framework import generics, permissions, filters, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from django_filters.rest_framework import DjangoFilterBackend
from django.shortcuts import get_object_or_404
from django.db.models import Sum

from .models import VendorInvoice, VendorInvoiceLine, Payment
from .serializers import (
    VendorInvoiceSerializer, VendorInvoiceListSerializer,
    PaymentSerializer,
    _run_3way_match, _update_invoice_match_status,
)


class VendorInvoiceListCreateView(generics.ListCreateAPIView):
    permission_classes = [permissions.IsAuthenticated]
    filter_backends    = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields   = ['project', 'vendor', 'match_status', 'payment_status']
    search_fields      = ['invoice_no']

    def get_serializer_class(self):
        if self.request.method == 'GET':
            return VendorInvoiceListSerializer
        return VendorInvoiceSerializer

    def get_queryset(self):
        return VendorInvoice.objects.select_related(
            'vendor', 'project', 'po', 'created_by'
        ).order_by('-created_at')

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)


class VendorInvoiceDetailView(generics.RetrieveUpdateAPIView):
    queryset           = VendorInvoice.objects.prefetch_related('lines')
    serializer_class   = VendorInvoiceSerializer
    permission_classes = [permissions.IsAuthenticated]


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def invoice_run_match(request, invoice_id):
    """Re-run 3-way match logic on all lines and refresh header match_status."""
    invoice = get_object_or_404(VendorInvoice, pk=invoice_id)
    for line in invoice.lines.all():
        _run_3way_match(line)
    _update_invoice_match_status(invoice)
    return Response(VendorInvoiceSerializer(invoice).data)


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def invoice_approve(request, invoice_id):
    """Manually approve an invoice (after dispute resolution)."""
    invoice = get_object_or_404(VendorInvoice, pk=invoice_id)
    invoice.match_status = 'Approved'
    invoice.save(update_fields=['match_status'])
    return Response(VendorInvoiceSerializer(invoice).data)


class PaymentListCreateView(generics.ListCreateAPIView):
    serializer_class   = PaymentSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends    = [DjangoFilterBackend]
    filterset_fields   = ['invoice', 'vendor']

    def get_queryset(self):
        return Payment.objects.select_related(
            'invoice', 'vendor', 'created_by'
        ).order_by('-created_at')

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)


class PaymentDetailView(generics.RetrieveAPIView):
    queryset           = Payment.objects.all()
    serializer_class   = PaymentSerializer
    permission_classes = [permissions.IsAuthenticated]


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def payables_summary(request):
    """Vendor payables summary — total outstanding per vendor for a project."""
    project_id = request.query_params.get('project')
    qs = VendorInvoice.objects.filter(payment_status__in=['Pending', 'Partial'])
    if project_id:
        qs = qs.filter(project_id=project_id)

    result = []
    for inv in qs.select_related('vendor'):
        paid = inv.payments.aggregate(total=Sum('amount'))['total'] or 0
        result.append({
            'invoice_id':      inv.pk,
            'invoice_no':      inv.invoice_no,
            'vendor':          inv.vendor.name,
            'total_amount':    inv.total_amount,
            'amount_paid':     paid,
            'amount_due':      inv.total_amount - paid,
            'due_date':        inv.due_date,
            'payment_status':  inv.payment_status,
        })
    return Response(result)
