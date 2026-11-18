"""bpmn8n's settings and their defaults (environment variables first). bpmn8n is its own Django
project and also an app another project can install (INSTALLED_APPS += ["bpmn8n"]): there the host
sets only what differs, e.g. N8N_URL."""
import os

from django.conf import settings as django_settings

DEFAULTS = {
    "LAYA_URL": os.environ.get("LAYA_URL", "http://localhost:8001/v1/systemone"),
    "LAYA_API_KEY": os.environ.get("LAYA_API_KEY", ""),
    "EXTRACTOR_URL": os.environ.get("EXTRACTOR_URL", "http://localhost:8001/v1/extract"),  # GLiNER2.5
    "OCR_URL": os.environ.get("OCR_URL", "http://localhost:8001/v1/ocr"),  # Vision, on a Mac
    "MODELS_API_KEY": os.environ.get("MODELS_API_KEY", ""),
    "LAYA_TIMEOUT": float(os.environ.get("LAYA_TIMEOUT", 10)),  # per attempt; a decision takes ~20 ms on GPU
    "LAYA_BUSY_TIMEOUT": float(os.environ.get("LAYA_BUSY_TIMEOUT", 60)),  # total seconds of retries
    "API_TOKEN": os.environ.get("BPMN8N_API_TOKEN", ""),  # empty = API open (dev only)
    "N8N_URL": os.environ.get("N8N_URL", "http://localhost:5678"),  # where bpmn8n reaches n8n
    "N8N_PUBLIC_URL": os.environ.get("N8N_PUBLIC_URL", "http://localhost:5678"),  # where a browser does
    "N8N_PREVIEW_URL": os.environ.get("N8N_PREVIEW_URL", "http://localhost:5679"),  # read-only canvas, embedded
    "N8N_API_KEY": os.environ.get("N8N_API_KEY", ""),  # n8n public API: read (and, for Studio, create) workflows
    "N8N_CREDENTIAL": os.environ.get("N8N_CREDENTIAL", ""),  # id of the n8n credential for bpmn8n's nodes
    "GUNICORN_PID": os.environ.get("GUNICORN_PID", "/tmp/gunicorn.pid"),  # Studio reloads it with new tasks
    "SEARCH_URL": os.environ.get("SEARCH_URL", ""),  # SearXNG, for Studio's domain research; empty: none
    # global variables and the event stream (core/shared.py, core/streaming.py); empty: the database
    "REDIS_URL": os.environ.get("REDIS_URL", ""),
    "EVENT_STREAM_MAXLEN": int(os.environ.get("EVENT_STREAM_MAXLEN", "100000")),  # Redis stream bpmn8n:events
    "EVENT_WEBHOOKS": [u for u in os.environ.get("EVENT_WEBHOOKS", "").split(",") if u.strip()],  # get every event
}


def project_apps(folder, draft=""):
    """The apps of `folder` (projects/): each subfolder with a tasks.py or a tasks/ package. A project
    Studio is building (`draft`, "_<slug>") is loaded only by its own checks, and there it replaces its
    published version."""
    names = {p.parent.name for p in folder.glob("*/tasks.py")} | {p.parents[1].name for p in folder.glob("*/tasks/__init__.py")}
    return sorted(f"{folder.name}.{name}" for name in names
                  if name == draft or not (name.startswith("_") or f"_{name}" == draft))


class Settings:
    """Django's settings, falling back to DEFAULTS for bpmn8n's own."""

    def __getattr__(self, name):
        return getattr(django_settings, name, DEFAULTS.get(name)) if name in DEFAULTS else getattr(django_settings, name)


settings = Settings()
