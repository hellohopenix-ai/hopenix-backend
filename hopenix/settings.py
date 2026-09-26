from pathlib import Path
from decouple import config, Csv
import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent

# --- Core security settings -----------------------------------------------
# SECRET_KEY and DEBUG now come from .env instead of being hardcoded, so
# the real production key never sits in source control. Generate one with:
#   python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
# and put it in .env as SECRET_KEY=... . The fallback below is ONLY so the
# project still boots for local dev if you haven't set one yet — replace
# it before deploying anywhere real.
SECRET_KEY = config(
    'SECRET_KEY',
    default='django-insecure-ymce#b*dw)74p7fb*4nty3wyj9h5tp@j%7gy5spqj+u3a@8ow4',
)
DEBUG = config('DEBUG', default=False, cast=bool)
ALLOWED_HOSTS = config('ALLOWED_HOSTS', default='localhost,127.0.0.1', cast=Csv())

# Set these in .env for production, e.g.:
#   ALLOWED_HOSTS=api.yourdomain.com
#   CSRF_TRUSTED_ORIGINS=https://yourdomain.com,https://your-netlify-site.netlify.app
CSRF_TRUSTED_ORIGINS = config('CSRF_TRUSTED_ORIGINS', default='', cast=Csv())

# Railway (and most PaaS hosts) terminate HTTPS at their proxy and forward
# plain HTTP to the app. Without this line, SECURE_SSL_REDIRECT below sees
# every request as "http" and redirects forever (ERR_TOO_MANY_REDIRECTS).
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# HTTPS/cookie/header hardening — auto-enabled whenever DEBUG is off, so
# local development (plain http://localhost) still works without extra
# config. If you deploy behind a proxy/load balancer that terminates TLS
# for you (nginx, Netlify, Render, etc.), also set:
#   SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
if not DEBUG:
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
CSRF_COOKIE_SAMESITE = 'Lax'
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = 'same-origin'
X_FRAME_OPTIONS = 'DENY'

# Hard caps on request body size so a malicious/huge payload can't be used
# to exhaust memory before your own per-field validation even runs.
DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024  # 10 MB
FILE_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024

INSTALLED_APPS = [
    'daphne',

    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    'channels',

    'rest_framework',
    'rest_framework.authtoken',
    'corsheaders',
    'anymail',

    'users',
    'settings',
    'dashboard',
    'projects',
    'messaging',
    'tasks',
    'sales', 
    'expenses',
    'meetings',
    'employees',
    'reports',
    'visitors',
    'coworking',
       'cloudinary_storage',
    'cloudinary',
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    # Serves collected static files (admin CSS/JS) in production.
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    # Records who did what for the Reports page — must stay AFTER AuthenticationMiddleware.
    'reports.middleware.ActivityContextMiddleware',
]

ROOT_URLCONF = 'hopenix.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'hopenix.wsgi.application'

# Channels (websocket) — the ASGI app itself is defined in hopenix/asgi.py
# and wraps this same Django app, so WSGI_APPLICATION above still works
# fine for plain HTTP/admin if you ever run under a WSGI server too.
ASGI_APPLICATION = 'hopenix.asgi.application'

# In dev, this stays empty and Channels uses the in-memory layer below.
# In production, set REDIS_URL (e.g. redis://127.0.0.1:6379/0) in your
# .env — required as soon as you run more than one server process/worker,
# because the in-memory layer only sees connections on ITS OWN process, so
# a message pushed from a request handled by worker #1 would never reach
# a user whose websocket landed on worker #2.
REDIS_URL = config('REDIS_URL', default='')

CHANNEL_LAYERS = {
    "default": (
        {
            "BACKEND": "channels_redis.core.RedisChannelLayer",
            "CONFIG": {"hosts": [REDIS_URL]},
        }
        if REDIS_URL
        else {"BACKEND": "channels.layers.InMemoryChannelLayer"}
    ),
}

# Production (Railway): set DATABASE_URL and it is used automatically.
# Local dev: leave DATABASE_URL unset and the DB_* values from .env are used.
DATABASE_URL = config('DATABASE_URL', default='')
if DATABASE_URL:
    DATABASES = {
        'default': dj_database_url.parse(DATABASE_URL, conn_max_age=600),
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': config('DB_NAME'),
            'USER': config('DB_USER'),
            'PASSWORD': config('DB_PASSWORD'),
            'HOST': config('DB_HOST', default='localhost'),
            'PORT': config('DB_PORT', default='5432'),
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

# Media files (user-uploaded CVs, etc.) — served by Django's dev server
# only while DEBUG=True (see hopenix/urls.py for the static() helper that
# wires MEDIA_URL up in development).
MEDIA_URL = '/media/'
# On Railway, attach a Volume and set MEDIA_ROOT to its mount path
# (e.g. /data/media) so uploads survive redeploys.
MEDIA_ROOT = Path(config('MEDIA_ROOT', default=str(BASE_DIR / 'media')))

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

AUTH_USER_MODEL = 'users.User'

REST_FRAMEWORK = {
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework.authentication.TokenAuthentication',
        'rest_framework.authentication.SessionAuthentication',
    ],
    # Blanket rate limits for every endpoint, plus named "scopes" that
    # individual auth views opt into for tighter limits (see
    # users/throttles.py and users/views.py). Backed by Django's cache
    # framework — see CACHES below for the multi-process/production note.
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.AnonRateThrottle',
        'rest_framework.throttling.UserRateThrottle',
    ],
    'DEFAULT_THROTTLE_RATES': {
        'anon': '1000/hour',
        'user': '5000/hour',
        'login': '60/min',
        'register': '30/hour',
        'otp': '15/min',
        'password_reset': '20/hour',
        'verify_password': '30/min',
        'google_auth': '60/hour',
        'email_lockout': '10/min',
        'portal_login': '5/min',
        'portal_email_lockout': '10/min',
    },
}

# Cache backend for both DRF throttling and any future caching. Falls back
# to per-process local memory (fine for a single dev server) and switches
# to Redis automatically once REDIS_URL is set in .env (same variable
# already used for Channels) — required in production as soon as you run
# more than one worker process, otherwise each process has its own
# throttle counters and the rate limits above become easy to dodge.
CACHES = {
    'default': (
        {
            'BACKEND': 'django.core.cache.backends.redis.RedisCache',
            'LOCATION': REDIS_URL,
        }
        if REDIS_URL
        else {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}
    )
}

# Explicit allow-list only — never CORS_ALLOW_ALL_ORIGINS = True, that
# would let any website's JS make authenticated requests using a
# visitor's browser session/cookies. Add real domains via .env in
# production instead of editing this list by hand each time.
CORS_ALLOWED_ORIGINS = config(
    'CORS_ALLOWED_ORIGINS',
    default='http://localhost:5173,http://127.0.0.1:5173,http://localhost:3000,http://127.0.0.1:3000,https://your-netlify-site.netlify.app',
    cast=Csv(),
)
CORS_ALLOW_ALL_ORIGINS = config('CORS_ALLOW_ALL_ORIGINS', default=DEBUG, cast=bool)
CORS_ALLOW_CREDENTIALS = True
CORS_ALLOW_HEADERS = [
    'accept',
    'accept-encoding',
    'authorization',
    'content-type',
    'dnt',
    'origin',
    'user-agent',
    'x-csrftoken',
    'x-requested-with',
]

EMAIL_BACKEND = 'anymail.backends.resend.EmailBackend'
ANYMAIL = {
    'RESEND_API_KEY': config('RESEND_API_KEY', default=''),
}
DEFAULT_FROM_EMAIL = config('DEFAULT_FROM_EMAIL', default='onboarding@resend.dev')

# Where Client Portal support-form submissions get emailed
# (dashboard.views.SupportRequestView). Falls back to EMAIL_HOST_USER if unset.
SUPPORT_EMAIL = config('SUPPORT_EMAIL', default='')

# Web Push (VAPID) — lets a call still ring a user when their tab/browser
# is fully closed and there's no live websocket to push over (see
# messaging/push_utils.py + CallStartView). Generate a keypair with:
#   npx web-push generate-vapid-keys
# then put PUBLIC key here AND in the frontend's pushSubscription.js, and
# the PRIVATE key here only (add both to your .env, same as DB_* above).
VAPID_PUBLIC_KEY = config('VAPID_PUBLIC_KEY', default='')
VAPID_PRIVATE_KEY = config('VAPID_PRIVATE_KEY', default='')
VAPID_CLAIMS = {'sub': 'mailto:' + (SUPPORT_EMAIL or config('EMAIL_HOST_USER', default='admin@example.com'))}

# Reports / activity log (see reports/).
# Set True only if you run behind a proxy (nginx etc.) that sets X-Forwarded-For; otherwise
# a client could spoof the IP that gets logged.
ACTIVITY_LOG_TRUST_PROXY_HEADERS = config('ACTIVITY_LOG_TRUST_PROXY_HEADERS', default=False, cast=bool)
# Extra URL regexes the request-level fallback logger should ignore (noisy endpoints).
ACTIVITY_LOG_IGNORE_PATHS = []
CLOUDINARY_STORAGE = {
    'CLOUD_NAME': config('CLOUDINARY_CLOUD_NAME', default=''),
    'API_KEY': config('CLOUDINARY_API_KEY', default=''),
    'API_SECRET': config('CLOUDINARY_API_SECRET', default=''),
}
# Django 5+/6+ replaced the old DEFAULT_FILE_STORAGE setting with this
# STORAGES dict — DEFAULT_FILE_STORAGE alone is silently ignored on
# Django 6.1, which is why avatars kept saving to local disk on Railway
# even after Cloudinary env vars were set.
#
# CONDITIONAL, not hardcoded to Cloudinary: only switch to Cloudinary in
# production (DEBUG=False, e.g. Railway). This is keyed off DEBUG rather
# than "is a cloud name present", because a developer's local .env may
# well have real, valid Cloudinary credentials (for occasionally testing
# cloud storage by hand) -- but `manage.py test` uploads tiny fake/dummy
# image bytes that Cloudinary's real API rejects ("Invalid image file"),
# even though Django's local FileSystemStorage doesn't care and just
# writes the bytes to disk. Keying off DEBUG keeps local/test runs on
# FileSystemStorage regardless of what's in .env, while still using
# Cloudinary in production where DEBUG is always False.
USE_CLOUDINARY = not DEBUG
STORAGES = {
    "default": (
        {"BACKEND": "cloudinary_storage.storage.MediaCloudinaryStorage"}
        if USE_CLOUDINARY
        else {"BACKEND": "django.core.files.storage.FileSystemStorage"}
    ),
    "staticfiles": (
        # The Manifest variant needs staticfiles.json, which only exists
        # after `collectstatic` has run (Railway's pre-deploy command does
        # this in production). Locally/in tests nobody runs collectstatic
        # first, so any template using {% static %} (e.g. Django admin)
        # would crash with "Missing staticfiles manifest entry" without
        # this DEBUG-based fallback to the plain, non-manifest variant.
        {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"}
        if not DEBUG
        else {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"}
    ),
}