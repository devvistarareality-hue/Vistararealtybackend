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
    """VRL Admin or Django staff = platform-level super admin — EXCEPT a single-module
    departmental admin (e.g. Sales Admin), who stays scoped to their own company."""
    if not (user and user.is_authenticated):
        return False
    if user.is_staff:
        return True
    if getattr(user, 'role', '') == 'Admin' and len(getattr(user, 'modules', None) or []) == 1:
        return False
    return bool(
        getattr(user, 'company', None) and
        getattr(user.company, 'code', '').upper() == VRL_CODE and
        getattr(user, 'role', '') == 'Admin'
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

        # Deleting a company takes every module's data with it (all of it cascades)
        # and, unlike a reset, leaves nothing to restore into — so it gets at least
        # the same gates as Data Reset, plus two of its own.
        import hmac, os, logging
        from sales.views import CompanyResetView

        # 1. Never the company you are signed in under: that locks you out.
        if company.id == getattr(request.user, 'company_id', None):
            return Response({'detail': 'You cannot delete the company you are signed in under.'},
                            status=status.HTTP_400_BAD_REQUEST)
        # `check_only` validates the key and code and changes nothing, so the page
        # can refuse a wrong key before it spends minutes taking a backup.
        check_only = str(request.data.get('check_only') or '').lower() in ('1', 'true')
        # 2. A recent full backup, the same rule a reset uses.
        if not check_only and not CompanyResetView()._covering_backup(company):
            return Response(
                {'detail': "Take this company's backup first. Deleting is only allowed within "
                           '2 hours of a full backup.'},
                status=status.HTTP_409_CONFLICT)
        # 3. The reset key from the server environment. No key configured, no delete.
        expected = (os.getenv('DATA_RESET_KEY') or '').strip()
        if not expected:
            return Response({'detail': 'Deleting companies is disabled: no DATA_RESET_KEY is '
                                       'configured on the server.'},
                            status=status.HTTP_403_FORBIDDEN)
        supplied = str(request.data.get('reset_key') or '').strip()
        if not hmac.compare_digest(supplied, expected):
            logging.getLogger(__name__).warning(
                'Company delete refused: bad key from user %s for company %s',
                request.user.id, company.id)
            return Response({'detail': 'Incorrect reset key.'}, status=status.HTTP_403_FORBIDDEN)
        # 4. Type the company's own code, so the wrong row cannot be deleted by a slip.
        if str(request.data.get('confirm') or '').strip().upper() != company.code.upper():
            return Response({'detail': f'Type the company code ({company.code}) to confirm.'},
                            status=status.HTTP_400_BAD_REQUEST)
        if check_only:
            return Response({'ok': True})

        logging.getLogger(__name__).warning('Company %s (%s) deleted by user %s',
                                            company.id, company.code, request.user.id)
        company.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
