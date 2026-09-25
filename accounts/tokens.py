from rest_framework_simplejwt.tokens import RefreshToken


class SessionRefreshToken(RefreshToken):
    """Embeds session_token + platform in the JWT payload.
    platform='app'  → tracks session_token_app on the User
    platform='web'  → tracks session_token_web on the User
    Rotating the relevant token on login invalidates only that platform's sessions.

    `impersonator` is set only on a token minted by ImpersonateView: it is the id
    of the platform admin who opened the session, so every request made through
    the token stays attributable to a real person rather than looking like the
    target user acting alone. The claim rides through token refresh (the access
    token copies the refresh payload), so it cannot be shed by refreshing.
    """

    @classmethod
    def for_user(cls, user, platform='app', impersonator=None):
        token = super().for_user(user)
        token['platform'] = platform
        if platform == 'web':
            token['session_token'] = str(user.session_token_web)
        else:
            token['session_token'] = str(user.session_token_app)
        if impersonator is not None:
            token['impersonator'] = impersonator
        return token
