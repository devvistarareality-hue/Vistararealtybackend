import uuid
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.throttling import ScopedRateThrottle
from rest_framework_simplejwt.exceptions import TokenError

from companies.models import Company
from .models import User, Designation, Notification
from .serializers import (
    LoginSerializer, UserSerializer,
    UserListSerializer, UserCreateSerializer, UserUpdateSerializer,
    DesignationSerializer,
)
from .tokens import SessionRefreshToken

VRL_CODE = 'VRL'


def is_platform_admin(user):
    return bool(
        user and user.is_authenticated and (
            user.is_staff or (
                getattr(user, 'company', None) and
                getattr(user.company, 'code', '').upper() == VRL_CODE and
                getattr(user, 'role', '') == 'Admin'
            )
        )
    )


def get_tokens_for_user(user, platform='app'):
    refresh = SessionRefreshToken.for_user(user, platform=platform)
    return {
        'refresh': str(refresh),
        'access':  str(refresh.access_token),
    }


class LoginView(APIView):
    permission_classes = [AllowAny]
    # Brute-force / credential-stuffing protection: cap login attempts per client IP.
    # Rate configured by REST_FRAMEWORK['DEFAULT_THROTTLE_RATES']['login'].
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'login'

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        company_code = serializer.validated_data['company_code'].upper().strip()
        user_code    = serializer.validated_data['user_code'].upper().strip()
        password     = serializer.validated_data['password']

        try:
            company = Company.objects.get(code=company_code, is_active=True)
        except Company.DoesNotExist:
            return Response({'detail': 'Invalid company code.'}, status=status.HTTP_401_UNAUTHORIZED)

        try:
            user = User.objects.select_related('company', 'reporting_manager').get(
                company=company, user_code=user_code, is_active=True
            )
        except User.DoesNotExist:
            return Response({'detail': 'Invalid user code or password.'}, status=status.HTTP_401_UNAUTHORIZED)

        if not user.check_password(password):
            return Response({'detail': 'Invalid user code or password.'}, status=status.HTTP_401_UNAUTHORIZED)

        # Rotate only the platform-specific session token so web logins don't
        # affect app sessions and vice versa.
        platform = serializer.validated_data.get('platform', 'app')
        if platform == 'web':
            user.session_token_web = uuid.uuid4()
            user.save(update_fields=['session_token_web'])
        else:
            user.session_token_app = uuid.uuid4()
            user.save(update_fields=['session_token_app'])

        tokens = get_tokens_for_user(user, platform=platform)
        return Response({'tokens': tokens, 'user': UserSerializer(user).data}, status=status.HTTP_200_OK)


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = User.objects.select_related('company', 'reporting_manager').get(pk=request.user.pk)
        return Response(UserSerializer(user).data)


class UserListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        # select_related avoids an N+1: UserListSerializer reads company.code/name and
        # the nested reporting_manager for every row.
        base = User.objects.select_related('company', 'reporting_manager')
        if is_platform_admin(request.user):
            company_id = request.query_params.get('company_id')
            if company_id:
                users = base.filter(company_id=company_id).order_by('name')
            else:
                users = base.order_by('company__name', 'name')
        else:
            users = base.filter(company=request.user.company).order_by('name')
        return Response(UserListSerializer(users, many=True).data)

    def post(self, request):
        serializer = UserCreateSerializer(data=request.data, context={'request': request})
        if serializer.is_valid():
            user = serializer.save()
            return Response(UserListSerializer(user).data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class UserDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def _get_user(self, pk, request):
        try:
            if is_platform_admin(request.user):
                return User.objects.get(pk=pk)
            return User.objects.get(pk=pk, company=request.user.company)
        except User.DoesNotExist:
            return None

    def get(self, request, pk):
        user = self._get_user(pk, request)
        if not user:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(UserListSerializer(user).data)

    def patch(self, request, pk):
        user = self._get_user(pk, request)
        if not user:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        serializer = UserUpdateSerializer(user, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(UserListSerializer(user).data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, pk):
        user = self._get_user(pk, request)
        if not user:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        user.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class NotificationTestView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from notifications import notify
        notify(
            request.user, 'test', 'Test Notification',
            f'Hello {request.user.name}! Notifications are working.',
        )
        return Response({'detail': 'Test notification sent.'})


class NotificationListView(APIView):
    """The bell — recent notifications + unread count for the logged-in user."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = Notification.objects.filter(recipient=request.user)
        unread = qs.filter(is_read=False).count()
        rows = [{
            'id': n.id, 'type': n.type, 'title': n.title, 'body': n.body,
            'data': n.data, 'is_read': n.is_read, 'created_at': n.created_at,
        } for n in qs[:50]]
        return Response({'unread': unread, 'results': rows})


class NotificationReadView(APIView):
    """Mark one notification read (with pk) or all (no pk)."""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk=None):
        qs = Notification.objects.filter(recipient=request.user, is_read=False)
        if pk is not None:
            qs = qs.filter(pk=pk)
        n = qs.update(is_read=True)
        return Response({'ok': True, 'marked': n})


class SessionTokenRefreshView(APIView):
    """Custom refresh endpoint that validates session_token before issuing new tokens.
    If the user has logged in elsewhere since this refresh token was issued, reject it."""
    permission_classes = [AllowAny]

    def post(self, request):
        refresh_str = request.data.get('refresh')
        if not refresh_str:
            return Response({'detail': 'Refresh token required.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            refresh = SessionRefreshToken(refresh_str)
        except TokenError:
            return Response(
                {'detail': 'Invalid or expired refresh token.', 'code': 'token_not_valid'},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        token_session = refresh.payload.get('session_token')
        platform      = refresh.payload.get('platform', 'app')
        user_id       = refresh.payload.get('user_id')

        try:
            user = User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return Response({'detail': 'User not found.'}, status=status.HTTP_401_UNAUTHORIZED)

        current = str(user.session_token_web) if platform == 'web' else str(user.session_token_app)
        if not token_session or token_session != current:
            return Response(
                {'detail': 'Session expired. Please log in again.', 'code': 'session_expired'},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        tokens = get_tokens_for_user(user, platform=platform)
        return Response(tokens, status=status.HTTP_200_OK)


class DesignationListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if is_platform_admin(request.user):
            desigs = Designation.objects.all()
        else:
            desigs = Designation.objects.filter(company=request.user.company)
        return Response(DesignationSerializer(desigs.select_related('company'), many=True).data)

    def post(self, request):
        serializer = DesignationSerializer(data=request.data)
        if serializer.is_valid():
            company = request.user.company
            company_id = request.data.get('company_id')
            if company_id and is_platform_admin(request.user):
                company = Company.objects.filter(pk=company_id).first()
                if company is None:
                    return Response({'company_id': 'Company not found.'}, status=status.HTTP_400_BAD_REQUEST)
            serializer.save(company=company)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class DesignationDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, pk):
        qs = Designation.objects.all() if is_platform_admin(request.user) else Designation.objects.filter(company=request.user.company)
        try:
            desig = qs.get(pk=pk)
        except Designation.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        desig.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


