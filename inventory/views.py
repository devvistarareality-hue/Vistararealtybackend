from rest_framework import generics, permissions, filters, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from django_filters.rest_framework import DjangoFilterBackend
from django.shortcuts import get_object_or_404

from .models import GRNHeader, GRNLine, StockLedger
from .serializers import (
    GRNHeaderSerializer, GRNHeaderListSerializer,
    GRNQCSerializer, StockLedgerSerializer, StockBalanceSerializer,
)
from .stock_utils import post_grn_to_ledger, get_stock_balance


class GRNListCreateView(generics.ListCreateAPIView):
    permission_classes = [permissions.IsAuthenticated]
    filter_backends    = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields   = ['project', 'vendor', 'status', 'po']
    search_fields      = ['grn_no', 'dc_no']

    def get_serializer_class(self):
        if self.request.method == 'GET':
            return GRNHeaderListSerializer
        return GRNHeaderSerializer

    def get_queryset(self):
        return GRNHeader.objects.select_related(
            'project', 'vendor', 'po', 'received_by'
        ).order_by('-created_at')

    def perform_create(self, serializer):
        serializer.save(received_by=self.request.user)


class GRNDetailView(generics.RetrieveAPIView):
    queryset           = GRNHeader.objects.prefetch_related('lines')
    serializer_class   = GRNHeaderSerializer
    permission_classes = [permissions.IsAuthenticated]


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def grn_qc_update(request, grn_id):
    """Update QC results for each line and set overall GRN status."""
    grn = get_object_or_404(GRNHeader, pk=grn_id)
    ser = GRNQCSerializer(data=request.data)
    ser.is_valid(raise_exception=True)

    for line_data in ser.validated_data['lines']:
        line_id     = line_data.get('id')
        qc_status   = line_data.get('qc_status')
        qty_accepted = line_data.get('qty_accepted')
        qty_rejected = line_data.get('qty_rejected')
        qc_remarks   = line_data.get('qc_remarks', '')
        try:
            line = grn.lines.get(pk=line_id)
        except GRNLine.DoesNotExist:
            continue
        if qc_status:
            line.qc_status = qc_status
        if qty_accepted is not None:
            line.qty_accepted = qty_accepted
        if qty_rejected is not None:
            line.qty_rejected = qty_rejected
        line.qc_remarks = qc_remarks
        line.save()

    grn.status = ser.validated_data['overall_status']
    grn.save(update_fields=['status'])

    # If QC passed, write stock ledger entries
    if grn.status in ('QC Passed', 'Stocked'):
        # Re-post — delete old GRN_IN entries first to avoid duplicates
        StockLedger.objects.filter(ref_doc_type='GRN', ref_doc_no=grn.grn_no).delete()
        post_grn_to_ledger(grn)
        # Transition PR lines to Received
        for line in grn.lines.all():
            try:
                line.po_line.pr_line.transition('Received')
            except Exception:
                pass

    return Response(GRNHeaderSerializer(grn).data)


class StockLedgerListView(generics.ListAPIView):
    serializer_class   = StockLedgerSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends    = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields   = ['project', 'item_code', 'txn_type']
    search_fields      = ['ref_doc_no']

    def get_queryset(self):
        return StockLedger.objects.select_related(
            'project', 'item_code'
        ).order_by('-txn_date', '-created_at')


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def stock_balance(request, project_id):
    """Current stock balance per item for a project."""
    item_id = request.query_params.get('item_code')
    data    = get_stock_balance(project_id, item_id)
    ser     = StockBalanceSerializer(data, many=True)
    return Response(ser.data)
