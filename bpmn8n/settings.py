import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

DEV_SECRET_KEY = "dev-only-insecure-key"
SECRET_KEY = os.environ.get("SECRET_KEY", DEV_SECRET_KEY)
DEBUG = os.environ.get("DEBUG", "1") == "1"
ALLOWED_HOSTS = os.environ.get("ALLOWED_HOSTS", "*").split(",")

# bpmn8n: models, n8n and API token (bpmn8n/conf.py, from the environment)
from .conf import DEFAULTS, project_apps  # noqa: E402

globals().update(DEFAULTS)
if not DEBUG and (SECRET_KEY == DEV_SECRET_KEY or not DEFAULTS["API_TOKEN"]):
    raise ImproperlyConfigured("DEBUG=0 needs SECRET_KEY and BPMN8N_API_TOKEN: the API would be open to anyone")

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
DATA_UPLOAD_MAX_MEMORY_SIZE = 20_000_000  # a document photo for OCR, base64 in JSON (Django's default: 2.5 MB)
USE_TZ = True
EMAIL_BACKEND = os.environ.get("EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")

LOGGING = {
    "version": 1,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {"bpmn8n": {"handlers": ["console"], "level": "INFO"}},
}
