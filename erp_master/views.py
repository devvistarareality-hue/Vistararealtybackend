from rest_framework import generics, permissions, filters
from django_filters.rest_framework import DjangoFilterBackend
from .models import Vendor, Material, GLAccount, ERPProject, WBSActivity, DocumentTrail
from .serializers import (
    VendorSerializer, MaterialSerializer, GLAccountSerializer,
    ERPProjectSerializer, WBSActivitySerializer, WBSActivityFlatSerializer,
    DocumentTrailSerializer,
)


class VendorListCreateView(generics.ListCreateAPIView):
    queryset         = Vendor.objects.filter(is_active=True).order_by('name')
    serializer_class = VendorSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends  = [filters.SearchFilter]
    search_fields    = ['name', 'code', 'gstin']


class VendorDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset         = Vendor.objects.all()
    serializer_class = VendorSerializer
    permission_classes = [permissions.IsAuthenticated]


class GLAccountListCreateView(generics.ListCreateAPIView):
    queryset         = GLAccount.objects.filter(is_active=True).order_by('code')
    serializer_class = GLAccountSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends  = [filters.SearchFilter, DjangoFilterBackend]
    search_fields    = ['code', 'name']
    filterset_fields = ['account_type']


class GLAccountDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset         = GLAccount.objects.all()
    serializer_class = GLAccountSerializer
    permission_classes = [permissions.IsAuthenticated]


class MaterialListCreateView(generics.ListCreateAPIView):
    queryset         = Material.objects.filter(is_active=True).order_by('item_code')
    serializer_class = MaterialSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends  = [filters.SearchFilter, DjangoFilterBackend]
    search_fields    = ['item_code', 'name']
    filterset_fields = ['category', 'uom']


class MaterialDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset         = Material.objects.all()
    serializer_class = MaterialSerializer
    permission_classes = [permissions.IsAuthenticated]


class ERPProjectListCreateView(generics.ListCreateAPIView):
    queryset         = ERPProject.objects.filter(is_active=True).order_by('-created_at')
    serializer_class = ERPProjectSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends  = [filters.SearchFilter, DjangoFilterBackend]
    search_fields    = ['code', 'name', 'client_name']
    filterset_fields = ['status']


class ERPProjectDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset         = ERPProject.objects.all()
    serializer_class = ERPProjectSerializer
    permission_classes = [permissions.IsAuthenticated]


class WBSActivityListCreateView(generics.ListCreateAPIView):
    serializer_class   = WBSActivityFlatSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends    = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields   = ['project', 'parent_activity']
    search_fields      = ['wbs_code', 'description']

    def get_queryset(self):
        return WBSActivity.objects.filter(is_active=True).select_related(
            'project', 'item_code', 'parent_activity'
        ).order_by('wbs_code')


class WBSActivityDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset         = WBSActivity.objects.all()
    serializer_class = WBSActivityFlatSerializer
    permission_classes = [permissions.IsAuthenticated]


class WBSTreeView(generics.ListAPIView):
    """Returns root-level activities with nested children for a project."""
    serializer_class   = WBSActivitySerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        project_id = self.kwargs['project_id']
        return WBSActivity.objects.filter(
            project_id=project_id, parent_activity__isnull=True, is_active=True
        ).order_by('wbs_code')


class DocumentTrailListView(generics.ListAPIView):
    serializer_class   = DocumentTrailSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends    = [DjangoFilterBackend]
    filterset_fields   = ['doc_type', 'doc_no', 'ref_doc_type']

    def get_queryset(self):
        return DocumentTrail.objects.order_by('-created_at')
