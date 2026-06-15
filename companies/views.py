from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAdminUser

from .models import Company
from .serializers import (
    CompanyVerifySerializer, CompanySerializer,
    CompanyAdminSerializer, CompanyCodeUpdateSerializer,
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
    Returns all companies. Restricted to staff (platform admins).
    """
    permission_classes = [IsAdminUser]

    def get(self, request):
        companies = Company.objects.all().order_by('name')
        return Response(CompanyAdminSerializer(companies, many=True).data)


class CompanyDetailView(APIView):
    """
    PATCH /api/company/<pk>/
    Update a company's code or name. Restricted to staff.
    """
    permission_classes = [IsAdminUser]

    def patch(self, request, pk):
        try:
            company = Company.objects.get(pk=pk)
        except Company.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

        serializer = CompanyCodeUpdateSerializer(company, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(CompanyAdminSerializer(company).data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
