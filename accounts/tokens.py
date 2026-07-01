from rest_framework_simplejwt.tokens import RefreshToken


class SessionRefreshToken(RefreshToken):
    """Embeds session_token + platform in the JWT payload.
    platform='app'  → tracks session_token_app on the User
    platform='web'  → tracks session_token_web on the User
    Rotating the relevant token on login invalidates only that platform's sessions."""

    @classmethod
    def for_user(cls, user, platform='app'):
        token = super().for_user(user)
        token['platform'] = platform
        if platform == 'web':
            token['session_token'] = str(user.session_token_web)
        else:
            token['session_token'] = str(user.session_token_app)
        return token
