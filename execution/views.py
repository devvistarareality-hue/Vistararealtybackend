from rest_framework import generics, permissions, filters, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from django_filters.rest_framework import DjangoFilterBackend
from django.shortcuts import get_object_or_404

from .models import (
    PRHeader, PRLine,
    MaterialIssueHeader, MaterialIssueLine,
    MeasurementBook, MBLine, RABill,
)
from .serializers import (
    PRHeaderSerializer, PRHeaderListSerializer, PRLineSerializer, PRTransitionSerializer,
    MaterialIssueHeaderSerializer,
    MeasurementBookSerializer, RABillSerializer,
)


# ─── Purchase Requisition ─────────────────────────────────────────────────────

class PRListCreateView(generics.ListCreateAPIView):
    permission_classes = [permissions.IsAuthenticated]
    filter_backends    = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields   = ['project', 'status']
    search_fields      = ['pr_no']

    def get_serializer_class(self):
        if self.request.method == 'GET':
            return PRHeaderListSerializer
        return PRHeaderSerializer

    def get_queryset(self):
        return PRHeader.objects.select_related(
            'project', 'raised_by', 'approved_by'
        ).order_by('-created_at')

    def perform_create(self, serializer):
        serializer.save(raised_by=self.request.user)


class PRDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset           = PRHeader.objects.prefetch_related('lines')
    serializer_class   = PRHeaderSerializer
    permission_classes = [permissions.IsAuthenticated]


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def pr_transition(request, pr_id):
    """Transition a single PR line to a new status."""
    ser = PRTransitionSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    line = get_object_or_404(PRLine, pk=ser.validated_data['line_id'], pr_id=pr_id)
    line.transition(ser.validated_data['new_status'])
    return Response(PRLineSerializer(line).data)


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def pr_approve(request, pr_id):
    """Approve all lines in a PR (bulk transition Raised → Approved)."""
    pr = get_object_or_404(PRHeader, pk=pr_id)
    for line in pr.lines.filter(status='Raised'):
        line.transition('Approved')
    return Response(PRHeaderSerializer(pr).data)


# ─── Material Issue ───────────────────────────────────────────────────────────

class MaterialIssueListCreateView(generics.ListCreateAPIView):
    serializer_class   = MaterialIssueHeaderSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends    = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields   = ['project', 'activity']
    search_fields      = ['issue_no']

    def get_queryset(self):
        return MaterialIssueHeader.objects.select_related(
            'project', 'activity', 'issued_by'
        ).order_by('-created_at')

    def perform_create(self, serializer):
        from inventory.stock_utils import post_issue_to_ledger
        issue = serializer.save(issued_by=self.request.user)
        post_issue_to_ledger(issue)


class MaterialIssueDetailView(generics.RetrieveAPIView):
    queryset           = MaterialIssueHeader.objects.prefetch_related('lines')
    serializer_class   = MaterialIssueHeaderSerializer
    permission_classes = [permissions.IsAuthenticated]


# ─── Measurement Book ─────────────────────────────────────────────────────────

class MeasurementBookListCreateView(generics.ListCreateAPIView):
    serializer_class   = MeasurementBookSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends    = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields   = ['project', 'status']
    search_fields      = ['mb_no']

    def get_queryset(self):
        return MeasurementBook.objects.select_related(
            'project', 'prepared_by', 'certified_by'
        ).order_by('-created_at')

    def perform_create(self, serializer):
        serializer.save(prepared_by=self.request.user)


class MeasurementBookDetailView(generics.RetrieveUpdateAPIView):
    queryset           = MeasurementBook.objects.prefetch_related('lines')
    serializer_class   = MeasurementBookSerializer
    permission_classes = [permissions.IsAuthenticated]


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def mb_certify(request, mb_id):
    """Certify a Measurement Book."""
    mb = get_object_or_404(MeasurementBook, pk=mb_id)
    if mb.status != 'Submitted':
        return Response({'error': 'MB must be in Submitted status to certify.'},
                        status=status.HTTP_400_BAD_REQUEST)
    mb.status       = 'Certified'
    mb.certified_by = request.user
    mb.save(update_fields=['status', 'certified_by'])
    return Response(MeasurementBookSerializer(mb).data)


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def mb_submit(request, mb_id):
    """Submit a Draft MB."""
    mb = get_object_or_404(MeasurementBook, pk=mb_id)
    if mb.status != 'Draft':
        return Response({'error': 'Only Draft MBs can be submitted.'},
                        status=status.HTTP_400_BAD_REQUEST)
    mb.status = 'Submitted'
    mb.save(update_fields=['status'])
    return Response(MeasurementBookSerializer(mb).data)


# ─── RA Bill ──────────────────────────────────────────────────────────────────

class RABillListCreateView(generics.ListCreateAPIView):
    serializer_class   = RABillSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends    = [DjangoFilterBackend]
    filterset_fields   = ['project', 'status']

    def get_queryset(self):
        return RABill.objects.select_related('project', 'mb').order_by('-created_at')


class RABillDetailView(generics.RetrieveUpdateAPIView):
    queryset           = RABill.objects.all()
    serializer_class   = RABillSerializer
    permission_classes = [permissions.IsAuthenticated]
