from pathlib import Path
from datetime import timedelta
import os
import dj_database_url
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

_SECRET_KEY = os.getenv('SECRET_KEY', '')
if not _SECRET_KEY:
    raise ValueError('SECRET_KEY environment variable must be set. Add it to your .env file or Railway variables.')
SECRET_KEY = _SECRET_KEY

DEBUG = os.getenv('DEBUG', 'False') == 'True'
ALLOWED_HOSTS = os.getenv('ALLOWED_HOSTS', '*').split(',')

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    # third party
    'rest_framework',
    'rest_framework_simplejwt',
    'corsheaders',
    # local
    'companies',
    'accounts',
    'attendance',
    'sales',
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'vistararealtybackend.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'vistararealtybackend.wsgi.application'

# ── Database ─────────────────────────────────────────────────────────
# Railway injects DATABASE_URL automatically; fall back to local SQLite
if os.getenv('DATABASE_URL'):
    DATABASES = {
        'default': dj_database_url.parse(
            os.getenv('DATABASE_URL'),
            # 0 by default: with Neon's pooled (PgBouncer) endpoint, app-side persistent
            # connections hold pooler slots — especially now with multiple gunicorn
            # workers × threads. Override via DB_CONN_MAX_AGE if on a session pooler.
            conn_max_age=int(os.getenv('DB_CONN_MAX_AGE', '0')),
            ssl_require=not DEBUG,
        )
    }
    # PgBouncer transaction mode doesn't keep server-side cursors across pooled
    # connections; disable them so .iterator()/large queries stay correct.
    DATABASES['default']['DISABLE_SERVER_SIDE_CURSORS'] = True
else:
    _db_engine = os.getenv('DB_ENGINE', 'django.db.backends.sqlite3')
    _is_postgres = _db_engine == 'django.db.backends.postgresql'
    DATABASES = {
        'default': {
            'ENGINE': _db_engine,
            'NAME': os.getenv('DB_NAME', 'db.sqlite3') if _is_postgres else BASE_DIR / os.getenv('DB_NAME', 'db.sqlite3'),
            'USER':     os.getenv('DB_USER', ''),
            'PASSWORD': os.getenv('DB_PASSWORD', ''),
            'HOST':     os.getenv('DB_HOST', 'localhost'),
            'PORT':     os.getenv('DB_PORT', '5432'),
            **({'OPTIONS': {'sslmode': 'require'}} if _is_postgres else {}),
            'CONN_MAX_AGE': 60 if _is_postgres else 0,
        }
    }

AUTH_USER_MODEL = 'accounts.User'

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Kolkata'
USE_I18N = True
USE_TZ = True

# ── Static files (whitenoise serves in production) ────────────────────
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ── Django REST Framework ─────────────────────────────────────────────
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticated',
    ),
    # Throttle rates for scoped throttles (applied per-view via throttle_scope).
    # 'login' guards /api/auth/login/ against brute-force / credential stuffing.
    'DEFAULT_THROTTLE_RATES': {
        'login': os.getenv('LOGIN_THROTTLE_RATE', '10/min'),
    },
}

# ── JWT Settings ──────────────────────────────────────────────────────
SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME':  timedelta(days=1),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=30),
    'ROTATE_REFRESH_TOKENS':  True,
}

# ── CORS ──────────────────────────────────────────────────────────────
# Mobile apps don't send an Origin header (CORS is browser-only), so locking
# this down does NOT affect the Expo app — only the web frontend's browser.
# Set CORS_ALLOWED_ORIGINS in the environment (comma-separated) to restrict to
# your web origins, e.g. "https://app.vistara.example,https://vrl.vercel.app".
# Falls back to allow-all only when unset, so existing deploys never break.
_cors_origins = os.getenv('CORS_ALLOWED_ORIGINS', '').strip()
if _cors_origins:
    CORS_ALLOWED_ORIGINS = [o.strip() for o in _cors_origins.split(',') if o.strip()]
    CORS_ALLOW_CREDENTIALS = True
else:
    CORS_ALLOW_ALL_ORIGINS = True

# ── Cache ─────────────────────────────────────────────────────────────
# Uses Redis when REDIS_URL is set (shared across gunicorn workers → makes the
# login throttle global and caches consistent). Falls back to per-process local
# memory otherwise, so nothing breaks before Redis is provisioned.
_redis_url = os.getenv('REDIS_URL', '').strip()
if _redis_url:
    CACHES = {
        'default': {
            'BACKEND': 'django_redis.cache.RedisCache',
            'LOCATION': _redis_url,
            'OPTIONS': {'CLIENT_CLASS': 'django_redis.client.DefaultClient'},
            'KEY_PREFIX': 'vistara',
        }
    }
else:
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            'LOCATION': 'vistara-local',
        }
    }

# ── Error monitoring (Sentry) ─────────────────────────────────────────
# No-op unless SENTRY_DSN is set; guarded so a missing package never breaks boot.
_sentry_dsn = os.getenv('SENTRY_DSN', '').strip()
if _sentry_dsn:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.django import DjangoIntegration
        sentry_sdk.init(
            dsn=_sentry_dsn,
            integrations=[DjangoIntegration()],
            traces_sample_rate=float(os.getenv('SENTRY_TRACES_RATE', '0.1')),
            send_default_pii=False,
            environment=os.getenv('RAILWAY_ENVIRONMENT', 'production'),
        )
    except Exception:
        pass
