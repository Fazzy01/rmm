import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = "dev-secret-key-change-in-production"
DEBUG = True
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "daphne",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "channels",
    "core",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "core" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# --- Database ---
# Simple sqlite for local dev; swap to Postgres/Timescale for production.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

# --- Channel layer ---
# Default: in-memory, great for `python manage.py runserver` on one process.
# Set USE_REDIS=1 (and run docker-compose) to use Redis, required once you
# run more than one process/worker, or want it to survive server reloads.
if os.environ.get("USE_REDIS") == "1":
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels_redis.core.RedisChannelLayer",
            "CONFIG": {
                "hosts": [os.environ.get("REDIS_URL", "redis://localhost:6379/0")],
            },
        },
    }
else:
    CHANNEL_LAYERS = {
        "default": {"BACKEND": "channels.layers.InMemoryChannelLayer"},
    }

AUTH_PASSWORD_VALIDATORS = []

# --- Admin dashboard login ---
# The dashboard views are gated with @login_required. Unauthenticated
# visitors are sent to LOGIN_URL; after a successful login/logout they go
# to these targets. Create an admin account with:
#   python manage.py createsuperuser
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"

# --- Relay shared-secret auth ---
# If set, every agent/admin WebSocket connection must include a matching
# ?token=<secret> query param or it's refused (see core/consumers.py). If
# unset, behaves exactly as before (open) - fine for local-only testing,
# but MUST be set before this is reachable from beyond a private network,
# since there is otherwise no authentication at all: anyone who knows a
# device_id can connect as that device's agent or admin.
RMM_SHARED_SECRET = os.environ.get("RMM_SHARED_SECRET", "")

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
