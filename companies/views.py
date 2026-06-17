from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated, BasePermission

from .models import Company
from .serializers import (
    CompanyVerifySerializer, CompanySerializer,
    CompanyAdminSerializer, CompanyCodeUpdateSerializer, CompanyCreateSerializer,
)

VRL_CODE = 'VRL'


def is_platform_admin(user):
    """VRL Admin or Django staff = platform-level super admin."""
    return bool(
        user and user.is_authenticated and (
            user.is_staff or (
                getattr(user, 'company', None) and
                getattr(user.company, 'code', '').upper() == VRL_CODE and
                getattr(user, 'role', '') == 'Admin'
            )
        )
    )


class IsAdminRoleOrStaff(BasePermission):
    def has_permission(self, request, view):
        return bool(
            request.user and request.user.is_authenticated and
            (is_platform_admin(request.user) or getattr(request.user, 'role', None) == 'Admin')
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
            {'valid': True, 'company': CompanySerializer(company).data},
            status=status.HTTP_200_OK,
        )


class CompanyListView(APIView):
    """
    GET  /api/company/all/  — platform admin: all companies; others: own company only
    POST /api/company/all/  — platform admin only: create a new company
    """
    permission_classes = [IsAdminRoleOrStaff]

    def get(self, request):
        if is_platform_admin(request.user):
            companies = Company.objects.all().order_by('name')
        else:
            companies = Company.objects.filter(pk=request.user.company.pk)
        return Response(CompanyAdminSerializer(companies, many=True).data)

    def post(self, request):
        if not is_platform_admin(request.user):
            return Response(
                {'detail': 'Only platform admins can create companies.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        serializer = CompanyCreateSerializer(data=request.data)
        if serializer.is_valid():
            company = serializer.save()
            return Response(CompanyAdminSerializer(company).data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class CompanyDetailView(APIView):
    """
    PATCH /api/company/<pk>/
    Platform admin: any company. Company Admin: own company only.
    """
    permission_classes = [IsAdminRoleOrStaff]

    def patch(self, request, pk):
        try:
            company = Company.objects.get(pk=pk)
        except Company.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

        if not is_platform_admin(request.user) and request.user.company.pk != company.pk:
            return Response(
                {'detail': 'You can only update your own company.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = CompanyCodeUpdateSerializer(company, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(CompanyAdminSerializer(company).data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, pk):
        if not is_platform_admin(request.user):
            return Response(
                {'detail': 'Only platform admins can delete companies.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        try:
            company = Company.objects.get(pk=pk)
        except Company.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        company.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
