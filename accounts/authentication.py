from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework.exceptions import AuthenticationFailed


class SessionJWTAuthentication(JWTAuthentication):
    """Validates session_token per platform on every request.
    Web logins only invalidate web sessions; app logins only invalidate app sessions."""

    def get_user(self, validated_token):
        user = super().get_user(validated_token)
        token_session = validated_token.get('session_token')
        platform      = validated_token.get('platform', 'app')

        current = str(user.session_token_web) if platform == 'web' else str(user.session_token_app)

        if not token_session or token_session != current:
            raise AuthenticationFailed(
                'Session expired. Please log in again.',
                code='session_expired',
            )
        return user
