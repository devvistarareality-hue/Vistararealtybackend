import os
import requests

ONESIGNAL_APP_ID  = os.environ.get('ONESIGNAL_APP_ID', '')
ONESIGNAL_API_KEY = os.environ.get('ONESIGNAL_REST_API_KEY', '')

def send_push_to_user(user_code, title, message, data=None):
    if not ONESIGNAL_APP_ID or not ONESIGNAL_API_KEY:
        return

    payload = {
        'app_id': ONESIGNAL_APP_ID,
        'include_external_user_ids': [str(user_code)],
        'channel_for_external_user_ids': 'push',
        'headings': {'en': title},
        'contents': {'en': message},
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


def send_push_to_all(title, message, data=None):
    if not ONESIGNAL_APP_ID or not ONESIGNAL_API_KEY:
        return

    payload = {
        'app_id': ONESIGNAL_APP_ID,
        'included_segments': ['All'],
        'headings': {'en': title},
        'contents': {'en': message},
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
