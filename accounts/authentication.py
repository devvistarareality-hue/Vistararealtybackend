from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework.exceptions import AuthenticationFailed


class SessionJWTAuthentication(JWTAuthentication):
    """Extends JWT auth to validate session_token on every request.
    If the user has logged in on another device since this token was issued,
    session_token in the DB will differ from the one in the JWT → 401."""

    def get_user(self, validated_token):
        user = super().get_user(validated_token)
        token_session = validated_token.get('session_token')
        if not token_session or str(token_session) != str(user.session_token):
            raise AuthenticationFailed(
                'Session expired. Please log in again.',
                code='session_expired',
            )
        return user
