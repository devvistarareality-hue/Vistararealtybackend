from rest_framework import generics, permissions, filters, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from django_filters.rest_framework import DjangoFilterBackend
from django.shortcuts import get_object_or_404

from .models import POHeader, POLine
from .serializers import (
    POHeaderSerializer, POHeaderListSerializer,
    POLineSerializer, POStatusSerializer,
)


class POListCreateView(generics.ListCreateAPIView):
    permission_classes = [permissions.IsAuthenticated]
    filter_backends    = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields   = ['project', 'vendor', 'status']
    search_fields      = ['po_no']

    def get_serializer_class(self):
        if self.request.method == 'GET':
            return POHeaderListSerializer
        return POHeaderSerializer

    def get_queryset(self):
        return POHeader.objects.select_related(
            'project', 'vendor', 'created_by'
        ).order_by('-created_at')

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)


class PODetailView(generics.RetrieveUpdateAPIView):
    queryset           = POHeader.objects.prefetch_related('lines')
    serializer_class   = POHeaderSerializer
    permission_classes = [permissions.IsAuthenticated]


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def po_update_status(request, po_id):
    """Update PO status (Draft → Confirmed → Dispatched → Closed / Cancelled)."""
    po  = get_object_or_404(POHeader, pk=po_id)
    ser = POStatusSerializer(data=request.data)
    ser.is_valid(raise_exception=True)

    TRANSITIONS = {
        'Draft':      ['Confirmed', 'Cancelled'],
        'Confirmed':  ['Dispatched', 'Cancelled'],
        'Dispatched': ['Closed'],
        'Closed':     [],
        'Cancelled':  [],
    }
    new_status = ser.validated_data['status']
    if new_status not in TRANSITIONS.get(po.status, []):
        return Response(
            {'error': f'Cannot move PO from "{po.status}" to "{new_status}".'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    # When PO is dispatched, transition each PR line to In Transit
    if new_status == 'Dispatched':
        for line in po.lines.all():
            try:
                line.pr_line.transition('In Transit')
            except Exception:
                pass

    po.status = new_status
    po.save(update_fields=['status'])
    return Response(POHeaderSerializer(po).data)
