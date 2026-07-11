import os
import requests

ONESIGNAL_APP_ID  = os.environ.get('ONESIGNAL_APP_ID', '')
ONESIGNAL_API_KEY = os.environ.get('ONESIGNAL_REST_API_KEY', '')


def notify(recipient, ntype, title, body, data=None, push=True):
    """Store an in-app/web notification AND (best-effort) fire a OneSignal push.
    `recipient` is a User instance. Safe to call anywhere — never raises."""
    if not recipient:
        return None
    data = data or {}
    n = None
    try:
        from accounts.models import Notification
        n = Notification.objects.create(
            recipient=recipient, type=ntype, title=title[:180], body=body or '', data=data,
        )
    except Exception:
        pass
    if push:
        code = getattr(recipient, 'user_code', '')
        if code:
            try:
                send_push_to_user(code, title, body, {**data, 'type': ntype, 'notif_id': getattr(n, 'id', None)})
            except Exception:
                pass
    return n


def notify_many(recipients, ntype, title, body, data=None, push=True):
    seen = set()
    for r in recipients:
        if r and r.id not in seen:
            seen.add(r.id)
            notify(r, ntype, title, body, data=data, push=push)


def reporting_chain(user, depth=5):
    """The user's reporting_manager, then theirs, up to `depth` levels (active only)."""
    out, cur, guard = [], getattr(user, 'reporting_manager', None), set()
    while cur and cur.id not in guard and len(out) < depth:
        guard.add(cur.id)
        if getattr(cur, 'is_active', True):
            out.append(cur)
        cur = getattr(cur, 'reporting_manager', None)
    return out

def send_push_to_user(user_code, title, message, data=None):
    if not ONESIGNAL_APP_ID or not ONESIGNAL_API_KEY:
        return

    payload = {
        'app_id': ONESIGNAL_APP_ID,
        'include_external_user_ids': [str(user_code)],
        'channel_for_external_user_ids': 'push',
        'headings': {'en': title},
        'contents': {'en': message},
        # Branded status-bar icon (installed as ic_stat_onesignal_default) tinted in
        # Vistara brand blue; large icon is the full-colour app logo.
        'android_accent_color': 'FF3D5AFE',
        'small_icon': 'ic_stat_onesignal_default',
        'large_icon': 'ic_onesignal_large_icon_default',
    }
    if data:
        payload['data'] = data

    requests.post(
        'https://onesignal.com/api/v1/notifications',
        json=payload,
        headers={
            'Authorization': f'Key {ONESIGNAL_API_KEY}',
            'Content-Type': 'application/json',
        },
        timeout=10,
    )


TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID', '')
TWILIO_AUTH_TOKEN  = os.environ.get('TWILIO_AUTH_TOKEN', '')
TWILIO_FROM_NUMBER = os.environ.get('TWILIO_FROM_NUMBER', '')


def send_sms_otp(phone, code):
    """Send a 6-digit OTP via Twilio SMS."""
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN or not TWILIO_FROM_NUMBER:
        return False
    ph = (phone or '').strip().replace(' ', '').replace('-', '')
    if not ph:
        return False
    if not ph.startswith('+'):
        ph = '+91' + ph.lstrip('0')
    try:
        from twilio.rest import Client
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        client.messages.create(
            body=f'{code} is your Vistara ERP login OTP. Valid for 5 minutes. Do not share.',
            from_=TWILIO_FROM_NUMBER,
            to=ph,
        )
        return True
    except Exception:
        return False


def send_email_otp(email, code):
    """Send a 6-digit OTP via email using Django's configured email backend."""
    if not email:
        return False
    try:
        from django.core.mail import send_mail
        from django.conf import settings as django_settings
        send_mail(
            subject='Your Vistara ERP Login OTP',
            message=(
                f'Your Vistara ERP login OTP is: {code}\n\n'
                f'This OTP is valid for 5 minutes. Do not share it with anyone.'
            ),
            from_email=django_settings.DEFAULT_FROM_EMAIL,
            recipient_list=[email],
            fail_silently=False,
        )
        return True
    except Exception:
        return False


def send_push_to_all(title, message, data=None):
    if not ONESIGNAL_APP_ID or not ONESIGNAL_API_KEY:
        return

    payload = {
        'app_id': ONESIGNAL_APP_ID,
        'included_segments': ['All'],
        'headings': {'en': title},
        'contents': {'en': message},
        'android_accent_color': 'FF3D5AFE',
        'small_icon': 'ic_stat_onesignal_default',
        'large_icon': 'ic_onesignal_large_icon_default',
    }
    if data:
        payload['data'] = data

    requests.post(
        'https://onesignal.com/api/v1/notifications',
        json=payload,
        headers={
            'Authorization': f'Key {ONESIGNAL_API_KEY}',
            'Content-Type': 'application/json',
        },
        timeout=10,
    )
