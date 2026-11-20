"""Is what bpmn8n needs up? One check per dependency, each with a short timeout, run in parallel. Read by
GET /api/health (statuses and latencies only: no URLs, no errors) and by `manage.py doctor` (the same, with
the errors and hints from the configuration)."""
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import urlsplit

from django.conf import settings as django_settings
from django.core.cache import cache
from django.db import DatabaseError, connection

from ..clients.http import request_json
from ..conf import settings

TIMEOUT = 2.0  # seconds per check
CRITICAL = "database"  # this server cannot answer without it: /api/health says 503
REQUIRED = ("database", "redis", "n8n")  # cases cannot run without them: doctor exits with 1


@dataclass(frozen=True)
class Result:
    name: str
    status: str  # up | down | off (not configured)
    ms: float | None = None
    error: str = ""


def origin(url):
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def database():
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")


def redis_ping(url):
    import redis

    redis.Redis.from_url(url, socket_timeout=TIMEOUT, socket_connect_timeout=TIMEOUT).ping()


def http_get(url):
    request_json("GET", url, timeout=TIMEOUT)


def checks():
    """name -> the call that raises when it is down, or None when it is not configured."""
    redis_url = getattr(settings, "REDIS_URL", "")
    return {
        "redis": redis_url and (lambda: redis_ping(redis_url)),
        "n8n": lambda: http_get(f"{settings.N8N_URL.rstrip('/')}/healthz"),
        "laya": settings.LAYA_URL and (lambda: http_get(f"{origin(settings.LAYA_URL)}/health")),
        "extractor": settings.EXTRACTOR_URL and (lambda: http_get(f"{origin(settings.EXTRACTOR_URL)}/health")),
        "search": settings.SEARCH_URL and (lambda: http_get(f"{settings.SEARCH_URL.rstrip('/')}/healthz")),
    }


def timed(name, call):
    if not call:
        return Result(name, "off")
    t0 = time.perf_counter()
    try:
        call()
    except Exception as e:  # whatever fails, the dependency is down: say why (doctor) and go on
        return Result(name, "down", round((time.perf_counter() - t0) * 1000, 1), f"{type(e).__name__}: {e}")
    return Result(name, "up", round((time.perf_counter() - t0) * 1000, 1))


def run():
    """Every check: the database in this thread (Django's connection is per thread), the others in parallel."""
    others = checks()
    with ThreadPoolExecutor(len(others)) as pool:
        pending = pool.map(lambda item: timed(*item), others.items())
        return [timed("database", database), *pending]


DOTS = {"n8n": ("n8n",), "models": ("laya", "extractor"), "redis": ("redis",), "search": ("search",)}
DOTS_KEY, DOTS_TTL = "bpmn8n:status-dots", 15  # seconds: the top bar of every page reads it, one check serves them all


def dots():
    """The top bar's dots: [{"name", "state": up | down | off}] for n8n, the models, Redis and search, cached for
    DOTS_TTL seconds (a page never waits on the checks twice in a row)."""
    if (found := cache.get(DOTS_KEY)) is not None:
        return found
    others = checks()
    with ThreadPoolExecutor(len(others)) as pool:
        results = {r.name: r.status for r in pool.map(lambda item: timed(*item), others.items())}
    found = []
    for name, parts in DOTS.items():
        states = {results[p] for p in parts} - {"off"}
        found.append({"name": name, "state": "down" if "down" in states else "up" if states else "off"})
    cache.set(DOTS_KEY, found, DOTS_TTL)
    return found


def overall(results):
    """down: the database is; degraded: something else is; ok."""
    down = {r.name for r in results if r.status == "down"}
    return "down" if CRITICAL in down else "degraded" if down else "ok"


def report(results=None):
    """What /api/health answers: statuses and latencies, nothing a stranger could use."""
    results = run() if results is None else results
    return {"status": overall(results),
            "checks": {r.name: {"status": r.status, **({"ms": r.ms} if r.ms is not None else {})} for r in results}}


def hints():
    """What the configuration leaves out, and how to fix it."""
    found = []
    if not settings.N8N_API_KEY:
        found.append("no N8N_API_KEY: conformance, Studio's import and `manage.py workflows` cannot read or "
                     "write n8n's workflows (./start.sh creates the key)")
    if not settings.N8N_CREDENTIAL:
        found.append("no N8N_CREDENTIAL: the workflows Studio builds get no bpmn8n credential "
                     "(./start.sh creates it in n8n and writes its id to .env)")
    if django_settings.DEBUG:
        found.append("DEBUG is on and BPMN8N_API_TOKEN is empty: the API is open to anyone (development only)"
                     if not settings.API_TOKEN else "DEBUG is on: development only, DEBUG=0 in production")
    if not settings.SEARCH_URL:
        found.append("no SEARCH_URL: Studio designs without researching the process's domain")
    try:
        from ..models import Settings

        if not Settings.objects.filter(pk=1, llm__isnull=False).exists():
            found.append("no default LLM provider: Studio cannot interview or design (/admin/ → Settings)")
    except DatabaseError:
        pass  # the database check says it is down
    return found
