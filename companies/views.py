from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated, BasePermission

from .models import Company
from .serializers import (
    CompanyVerifySerializer, CompanySerializer,
    CompanyAdminSerializer, CompanyCodeUpdateSerializer,
)


class IsAdminRoleOrStaff(BasePermission):
    """Allow is_staff (platform admin) or users with role='Admin' (company admin)."""
    def has_permission(self, request, view):
        return bool(
            request.user and request.user.is_authenticated and
            (request.user.is_staff or getattr(request.user, 'role', None) == 'Admin')
        )


class VerifyCompanyView(APIView):
    """
    POST /api/company/verify/
    Body: { "company_code": "VISR" }
    Returns company info if the code is valid and active.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = CompanyVerifySerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        code = serializer.validated_data['company_code'].upper().strip()

        try:
            company = Company.objects.get(code=code, is_active=True)
        except Company.DoesNotExist:
            return Response(
                {'detail': 'Invalid or inactive company code.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response(
            {
                'valid': True,
                'company': CompanySerializer(company).data,
            },
            status=status.HTTP_200_OK,
        )


class CompanyListView(APIView):
    """
    GET /api/company/all/
    Staff: returns all companies.
    Admin role: returns only their own company.
    """
    permission_classes = [IsAdminRoleOrStaff]

    def get(self, request):
        if request.user.is_staff:
            companies = Company.objects.all().order_by('name')
        else:
            companies = Company.objects.filter(pk=request.user.company.pk)
        return Response(CompanyAdminSerializer(companies, many=True).data)


class CompanyDetailView(APIView):
    """
    PATCH /api/company/<pk>/
    Staff: can update any company.
    Admin role: can only update their own company.
    """
    permission_classes = [IsAdminRoleOrStaff]

    def patch(self, request, pk):
        try:
            company = Company.objects.get(pk=pk)
        except Company.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

        if not request.user.is_staff and request.user.company.pk != company.pk:
            return Response({'detail': 'You can only update your own company.'}, status=status.HTTP_403_FORBIDDEN)

        serializer = CompanyCodeUpdateSerializer(company, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(CompanyAdminSerializer(company).data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
