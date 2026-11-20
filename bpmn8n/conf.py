"""bpmn8n's settings and their defaults (bpmn8n/config.py reads them from the environment). bpmn8n is its own
Django project and also an app another project can install (INSTALLED_APPS += ["bpmn8n"]): there the host
sets only what differs, e.g. N8N_URL."""
from django.conf import settings as django_settings

from .config import Config

CONFIG = Config.from_env()
DEFAULTS = CONFIG.app_settings()


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
