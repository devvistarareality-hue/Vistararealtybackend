from rest_framework_simplejwt.tokens import RefreshToken


class SessionRefreshToken(RefreshToken):
    """Embeds the user's current session_token in both access and refresh JWTs.
    When a new login happens, session_token rotates — any old token that carries
    the previous session_token will fail validation and force re-login."""

    @classmethod
    def for_user(cls, user):
        token = super().for_user(user)
        token['session_token'] = str(user.session_token)
        return token
