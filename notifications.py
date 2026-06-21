import logging
import os
import firebase_admin
from firebase_admin import credentials, messaging

logger = logging.getLogger(__name__)

_initialized = False

def _init():
    global _initialized
    if _initialized:
        return
    cred_path = os.path.join(os.path.dirname(__file__), 'firebase-service-account.json')
    if not os.path.exists(cred_path):
        logger.warning('firebase-service-account.json not found — push notifications disabled')
        return
    cred = credentials.Certificate(cred_path)
    firebase_admin.initialize_app(cred)
    _initialized = True


def send_push(tokens, title, body, data=None):
    """Send FCM push notification to a list of tokens."""
    _init()
    if not _initialized or not tokens:
        return
    data = {k: str(v) for k, v in (data or {}).items()}
    message = messaging.MulticastMessage(
        tokens=list(tokens),
        notification=messaging.Notification(title=title, body=body),
        data=data,
        android=messaging.AndroidConfig(priority='high'),
    )
    try:
        resp = messaging.send_each_for_multicast(message)
        if resp.failure_count:
            logger.warning('FCM: %d/%d notifications failed', resp.failure_count, len(tokens))
    except Exception:
        logger.exception('FCM: failed to send push notification')


def send_push_to_user(user, title, body, data=None):
    """Send push notification to all devices of a user."""
    from accounts.models import PushToken
    tokens = list(PushToken.objects.filter(user=user).values_list('token', flat=True))
    send_push(tokens, title, body, data)
