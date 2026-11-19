"""bpmn8n's configuration: every setting it reads from the environment, typed and documented in one place.
Django's settings (bpmn8n/settings.py, bpmn8n/conf.py) and the `bpmn8n` CLI read it, and the CLI hands docker
compose the derived addresses. `manage.py config` prints what is in effect (secrets masked); `--md` prints the
table of docs/GUIDE.md ("Configuration")."""
import os
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator


def _flag(value):  # "1" is on, anything else off: what these variables have always meant
    return value == "1" if isinstance(value, str) else value


def _items(value):  # comma-separated
    return [v.strip() for v in value.split(",") if v.strip()] if isinstance(value, str) else value


Flag = Annotated[bool, BeforeValidator(_flag)]
Items = Annotated[list[str], BeforeValidator(_items)]

# ponytail: PLAN B1's gateway layout (Django at /, the editor at /n8n, the preview on its own host); set the three
# addresses explicitly for another layout
N8N_PATH = "/n8n"
LOCAL = {"BPMN8N_PUBLIC_URL": "http://localhost:8000", "N8N_PUBLIC_URL": "http://localhost:5678",
         "N8N_PREVIEW_URL": "http://localhost:5679"}
SECRETS = frozenset({"SECRET_KEY", "BPMN8N_API_TOKEN", "FIELD_ENCRYPTION_KEY", "N8N_API_KEY", "N8N_PASSWORD",
                     "LAYA_API_KEY", "MODELS_API_KEY", "POSTGRES_PASSWORD", "SEARXNG_SECRET"})
# bpmn8n.conf.settings: the names Django's settings know them by (API_TOKEN: BPMN8N_API_TOKEN in the environment)
APP_SETTINGS = {"API_TOKEN": "BPMN8N_API_TOKEN", **{name: name for name in (
    "LAYA_URL", "LAYA_API_KEY", "EXTRACTOR_URL", "OCR_URL", "MODELS_API_KEY", "LAYA_TIMEOUT", "LAYA_BUSY_TIMEOUT",
    "FIELD_ENCRYPTION_KEY", "N8N_URL", "N8N_PUBLIC_URL", "N8N_PREVIEW_URL", "N8N_API_KEY", "N8N_CREDENTIAL",
    "GUNICORN_PID", "SEARCH_URL", "REDIS_URL", "EVENT_STREAM_MAXLEN", "EVENT_WEBHOOKS", "PRUNE_DAYS")}}


def derived(public_url):
    """The three public addresses from one PUBLIC_URL (the local ports without it)."""
    if not public_url:
        return dict(LOCAL)
    parts = urlsplit(public_url.rstrip("/"))
    origin = f"{parts.scheme}://{parts.netloc}"
    return {"BPMN8N_PUBLIC_URL": origin + parts.path, "N8N_PUBLIC_URL": origin + parts.path + N8N_PATH,
            "N8N_PREVIEW_URL": f"{parts.scheme}://preview.{parts.netloc}"}


class Config(BaseModel):
    """One field per environment variable, named like it."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    # where people reach bpmn8n
    PUBLIC_URL: str = Field("", description="The one address people open (e.g. `https://bpmn8n.example.org`): the "
                            "three below derive from it when they are not set")
    BPMN8N_PUBLIC_URL: str = Field("", description="Where a browser opens bpmn8n's pages (the human task nodes link "
                                   "to them). Derived: `PUBLIC_URL`, else `http://localhost:8000`")
    N8N_PUBLIC_URL: str = Field("", description="Where a browser opens the n8n editor (links, CORS for the editor's "
                                "hooks). Derived: `PUBLIC_URL` + `/n8n`, else `http://localhost:5678`")
    N8N_PREVIEW_URL: str = Field("", description="The read-only canvas Studio and the case page embed. Derived: "
                                 "`preview.` + `PUBLIC_URL`'s host, else `http://localhost:5679`")
    # Django
    SECRET_KEY: str = Field("dev-only-insecure-key", description="Django's secret key; `DEBUG=0` refuses the "
                            "default")
    DEBUG: Flag = Field(True, description="`1` development, `0` production (the web image sets it)")
    ALLOWED_HOSTS: Items = Field(["*"], description="Host names this server answers to, comma-separated; "
                                 "`DEBUG=0` refuses `*`")
    CSRF_TRUSTED_ORIGINS: Items = Field([], description="Origins allowed to post forms, e.g. "
                                        "`https://bpmn8n.example.org`")
    SECURE_PROXY_SSL_HEADER: str = Field("", description="The header a TLS proxy sets to `https`, e.g. "
                                         "`X-Forwarded-Proto`")
    SECURE_SSL_REDIRECT: Flag = Field(False, description="`1`: Django redirects to HTTPS (`/api/` exempt)")
    HSTS_SECONDS: int = Field(31_536_000, description="HSTS max-age with `DEBUG=0`")
    HSTS_INCLUDE_SUBDOMAINS: Flag = Field(True, description="HSTS covers subdomains")
    HSTS_PRELOAD: Flag = Field(False, description="`1`: ask for the browsers' preload lists")
    BPMN8N_API_TOKEN: str = Field("", description="Bearer token of the API (n8n's credential and Prometheus send "
                                  "it); empty: the API is open (development only)")
    FIELD_ENCRYPTION_KEY: str = Field("", description="Encrypts the LLM providers' keys at rest; empty: derived "
                                      "from `SECRET_KEY`")
    BPMN8N_DRAFT: str = Field("", description="Set by Studio's checks: the draft project loaded in place of its "
                              "published version")
    # database
    POSTGRES_HOST: str = Field("", description="Postgres host; empty: SQLite at `DB_PATH`")
    POSTGRES_DB: str = Field("bpmn8n", description="Postgres database")
    POSTGRES_USER: str = Field("bpmn8n", description="Postgres user")
    POSTGRES_PASSWORD: str = Field("", description="Postgres password (compose: `bpmn8n-dev` unless set)")
    DB_PATH: str = Field("", description="SQLite file; empty: `db.sqlite3` in the checkout")
    # n8n
    N8N_URL: str = Field("http://localhost:5678", description="Where bpmn8n reaches n8n (compose: "
                         "`http://n8n:5678`)")
    N8N_API_KEY: str = Field("", description="n8n's public API key (the CLI creates it): conformance, Studio and "
                             "`workflows diff/push/pull`")
    PRUNE_DAYS: str = Field("", description="Days of events and task runs the scheduler keeps; older ones are "
                            "archived (events) or deleted by `manage.py prune` daily. Empty: nothing is pruned")
    N8N_CREDENTIAL: str = Field("", description="Id of the n8n credential bpmn8n's nodes use (the CLI creates it)")
    N8N_EMAIL: str = Field("", description="n8n owner login the CLI uses to create the credential and the API key")
    N8N_PASSWORD: str = Field("", description="Its password")
    # models
    LAYA_URL: str = Field("http://localhost:8001/v1/systemone", description="Laya, the decision model")
    LAYA_API_KEY: str = Field("", description="Laya's bearer token, if it needs one")
    LAYA_TIMEOUT: float = Field(10.0, description="Seconds per Laya attempt")
    LAYA_BUSY_TIMEOUT: float = Field(60.0, description="Seconds of retries while a model server is busy")
    EXTRACTOR_URL: str = Field("http://localhost:8001/v1/extract", description="GLiNER2.5 extraction")
    OCR_URL: str = Field("http://localhost:8001/v1/ocr", description="OCR (Vision, on a Mac)")
    MODELS_API_KEY: str = Field("", description="The models server's bearer token, if it needs one")
    # services
    SEARCH_URL: str = Field("", description="SearXNG, for Studio's domain research; empty: none")
    REDIS_URL: str = Field("", description="Global variables and the event stream; empty: the database")
    EVENT_STREAM_MAXLEN: int = Field(100_000, description="Length of the Redis stream `bpmn8n:events`")
    EVENT_WEBHOOKS: Items = Field([], description="URLs that receive every finished step, comma-separated")
    EMAIL_BACKEND: str = Field("django.core.mail.backends.console.EmailBackend", description="Django's e-mail "
                               "backend")
    # operations
    LOG_FORMAT: str = Field("", description="`json`: one JSON object per log line")
    STATIC_ROOT: str = Field("", description="Where collectstatic writes; empty: `staticfiles/` in the checkout")
    GUNICORN_PID: str = Field("/tmp/gunicorn.pid", description="gunicorn's pid file: Studio reloads it with new "
                              "tasks")
    WEB_WORKERS: int = Field(4, description="gunicorn worker processes (compose)")
    SEARXNG_SECRET: str = Field("bpmn8n-dev-searxng", description="SearXNG's secret (compose)")
    BPMN8N_WEB_IMAGE: str = Field("bpmn8n-web:latest", description="The web image of `docker-compose.prod.yml`")

    @model_validator(mode="before")
    @classmethod
    def _derive(cls, values):
        """An address left empty takes its value from PUBLIC_URL: one set explicitly keeps priority."""
        values = dict(values)
        for name, url in derived(values.get("PUBLIC_URL", "")).items():
            values[name] = values.get(name) or url
        return values

    @classmethod
    def from_env(cls, env=None):
        """From `env` (default os.environ): only the variables it sets, so the rest keep their defaults."""
        env = os.environ if env is None else env
        return cls(**{name: env[name] for name in cls.model_fields if name in env})

    def app_settings(self):
        """bpmn8n's own Django settings (bpmn8n/conf.py's DEFAULTS)."""
        return {name: getattr(self, field) for name, field in APP_SETTINGS.items()}

    def compose_env(self):
        """What docker compose cannot derive itself: the three public addresses and, with PUBLIC_URL, the one-origin
        gateway (compose profile one-origin, deploy/Caddyfile) with n8n under N8N_PATH behind it."""
        env = {name: getattr(self, name) for name in LOCAL}
        if not self.PUBLIC_URL:
            return env
        public, preview = urlsplit(self.BPMN8N_PUBLIC_URL), urlsplit(self.N8N_PREVIEW_URL)
        port = public.port or (80 if public.scheme == "http" else None)  # https: a TLS proxy in front of the gateway
        return {**env, "COMPOSE_PROFILES": "one-origin", "N8N_PATH": f"{N8N_PATH}/",
                "N8N_EDITOR_BASE_URL": f"{self.N8N_PUBLIC_URL}/", "N8N_WEBHOOK_URL": f"{self.BPMN8N_PUBLIC_URL}/",
                "PREVIEW_ADDRESS": f"http://{preview.hostname}", **({"GATEWAY_PORT": str(port)} if port else {}),
                # n8n's editor refuses plain HTTP with a Secure cookie on any host but localhost
                "N8N_PREVIEW_SECURE_COOKIE": "true" if preview.scheme == "https" else "false"}

    def shown(self):
        """name -> value as text, the secrets as "set" or "empty"."""
        return {name: ("set" if getattr(self, name) else "empty") if name in SECRETS else text(getattr(self, name))
                for name in type(self).model_fields}


def text(value):
    """A value as the environment writes it."""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, list):
        return ",".join(value)
    return str(value)


def markdown():
    """The table of docs/GUIDE.md, "Configuration"."""
    rows = ["| Variable | Default | What it sets |", "|---|---|---|"]
    for name, field in Config.model_fields.items():
        default = text(field.default)
        cell = "derived" if name in LOCAL else f"`{default}`" if default else "(empty)"
        rows.append(f"| `{name}` | {cell} | {field.description} |")
    return "\n".join(rows)
