import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

DEV_SECRET_KEY = "dev-only-insecure-key"
SECRET_KEY = os.environ.get("SECRET_KEY", DEV_SECRET_KEY)
# Development unless DEBUG=0 (the tests and `manage.py runserver` need no variables); the web image sets DEBUG=0
DEBUG = os.environ.get("DEBUG", "1") == "1"
ALLOWED_HOSTS = os.environ.get("ALLOWED_HOSTS", "*").split(",")
CSRF_TRUSTED_ORIGINS = [o for o in os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",") if o]


def production(env):
    """The settings DEBUG=0 adds, from the environment `env`. Refuses what would expose the server: the
    development secret key, an API without a token, no host names or the "*" wildcard."""
    if env.get("SECRET_KEY", DEV_SECRET_KEY) == DEV_SECRET_KEY or not env.get("BPMN8N_API_TOKEN"):
        raise ImproperlyConfigured("DEBUG=0 needs SECRET_KEY and BPMN8N_API_TOKEN: the API would be open to anyone")
    hosts = [h for h in env.get("ALLOWED_HOSTS", "").split(",") if h]
    if not hosts or "*" in hosts:
        raise ImproperlyConfigured("DEBUG=0 needs ALLOWED_HOSTS: the host names this server answers to, not *")
    proxy = env.get("SECURE_PROXY_SSL_HEADER", "")  # e.g. X-Forwarded-Proto, which the TLS proxy sets to https
    return {
        "ALLOWED_HOSTS": [*hosts, "localhost"],  # localhost: the image's HEALTHCHECK
        "SESSION_COOKIE_SECURE": True,
        "CSRF_COOKIE_SECURE": True,
        "SECURE_HSTS_SECONDS": int(env.get("HSTS_SECONDS", 31_536_000)),  # sent on HTTPS requests only
        "SECURE_HSTS_INCLUDE_SUBDOMAINS": env.get("HSTS_INCLUDE_SUBDOMAINS", "1") == "1",
        "SECURE_HSTS_PRELOAD": env.get("HSTS_PRELOAD") == "1",  # a submission to the browsers' lists: opt in
        # off by default: TLS ends at the proxy, which redirects; n8n calls the API over HTTP inside the network
        "SECURE_SSL_REDIRECT": env.get("SECURE_SSL_REDIRECT") == "1",
        "SECURE_REDIRECT_EXEMPT": [r"^api/"],
        **({"SECURE_PROXY_SSL_HEADER": (f"HTTP_{proxy.upper().replace('-', '_')}", "https")} if proxy else {}),
        # collectstatic writes hashed, compressed copies; whitenoise serves them from gunicorn
        "STORAGES": {"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
                     "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"}},
    }


# bpmn8n: models, n8n and API token (bpmn8n/conf.py, from the environment)
from .conf import DEFAULTS, project_apps  # noqa: E402

globals().update(DEFAULTS)
if not DEBUG:
    globals().update(production(os.environ))

# Each folder of projects/ with a tasks.py is an app: the ACME examples and what bpmn8n Studio builds
PROJECTS = project_apps(BASE_DIR / "projects", os.environ.get("BPMN8N_DRAFT", ""))

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",  # groups = BPMN lanes (roles that work user tasks)
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "ninja",
    "bpmn8n",
    *PROJECTS,
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "bpmn8n.api.openapi_cors",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
if not DEBUG:  # static files from gunicorn in production; staticfiles_urlpatterns serves them in development
    MIDDLEWARE.insert(1, "whitenoise.middleware.WhiteNoiseMiddleware")
ROOT_URLCONF = "bpmn8n.urls"
WSGI_APPLICATION = "bpmn8n.wsgi.application"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
        "bpmn8n.web.views.navigation",
    ]},
}]
# Postgres when configured (docker compose): several worker processes share tasks, the event log
# and the metrics. SQLite otherwise (tests, a quick local run).
if os.environ.get("POSTGRES_HOST"):
    DATABASES = {"default": {
        "ENGINE": "django.db.backends.postgresql",
        "HOST": os.environ["POSTGRES_HOST"],
        "NAME": os.environ.get("POSTGRES_DB", "bpmn8n"),
        "USER": os.environ.get("POSTGRES_USER", "bpmn8n"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
        "CONN_MAX_AGE": 300,  # keep connections across requests
        "CONN_HEALTH_CHECKS": True,
    }}
else:
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3",
                             "NAME": os.environ.get("DB_PATH", BASE_DIR / "db.sqlite3")}}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "login"
STATIC_URL = "static/"
STATIC_ROOT = os.environ.get("STATIC_ROOT", BASE_DIR / "staticfiles")  # collectstatic
DATA_UPLOAD_MAX_MEMORY_SIZE = 20_000_000  # a document photo for OCR, base64 in JSON (Django's default: 2.5 MB)
USE_TZ = True
EMAIL_BACKEND = os.environ.get("EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")

LOGGING = {  # LOG_FORMAT=json: one JSON object per line, for a log collector
    "version": 1,
    "formatters": {"json": {"()": "bpmn8n.ops.logs.JsonFormatter"}},
    "handlers": {"console": {"class": "logging.StreamHandler",
                             **({"formatter": "json"} if os.environ.get("LOG_FORMAT") == "json" else {})}},
    "loggers": {"bpmn8n": {"handlers": ["console"], "level": "INFO"}},
}
