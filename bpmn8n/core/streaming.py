"""Log streaming (paid in n8n): every finished step (an Event, complete or error) goes to the configured
sinks, and external consumers can follow them live.

Sinks: the Redis stream `bpmn8n:events` (XADD, about EVENT_STREAM_MAXLEN entries kept) when REDIS_URL is
set, and the URLs of EVENT_WEBHOOKS (a JSON POST each, from a background thread). Best effort: a sink that
fails is logged, it never slows or fails the step. What is sent says what happened (process, case, step,
status, times, resource, error), not the data the step read and wrote: that stays in the database.

GET /api/events/stream (EventStream) serves them as server-sent events, reading the Redis stream, or
polling the database without Redis, and resumes after the Last-Event-ID a client sends back."""
import json
import logging
import queue
import re
import threading
import time
from datetime import datetime, timedelta

from django.core.serializers.json import DjangoJSONEncoder
from django.db import close_old_connections, transaction
from django.db.models import Q
from django.http import StreamingHttpResponse
from django.utils import timezone

from ..clients.http import ServiceUnavailable, post_json
from ..conf import settings
from ..models.cases import Event  # not ..models: models/waiting.py imports this module
from .shared import connection

log = logging.getLogger("bpmn8n")

STREAM = "bpmn8n:events"
FIELDS = ("id", "case_id", "process", "activity", "endpoint", "status", "resource", "started", "ended",
          "duration_ms", "error")
WEBHOOK_TIMEOUT = 3  # seconds per POST
_webhooks = queue.Queue(maxsize=10_000)  # what the background thread still has to send, per process
_sender = None
_sender_lock = threading.Lock()


def publish(event):
    """Sends a finished Event, or the Event with this id (measure() updates a row it does not reload), to
    the sinks once its transaction commits (a rolled-back step is never announced). Never raises."""
    try:
        client, hooks = connection(), settings.EVENT_WEBHOOKS
        if not (client or hooks):
            return
        if not isinstance(event, Event):
            event = Event.objects.get(pk=event)
        payload = json.dumps({f: getattr(event, f) for f in FIELDS}, cls=DjangoJSONEncoder)
        transaction.on_commit(lambda: _send(client, hooks, payload), robust=True)
    except Exception:  # log streaming must never be why a step fails
        log.exception("event %s not published", getattr(event, "pk", event))


def _send(client, hooks, payload):
    if client:
        try:
            client.xadd(STREAM, {"event": payload}, maxlen=settings.EVENT_STREAM_MAXLEN, approximate=True)
        except Exception as e:  # noqa: BLE001 Redis down: the step went on, the stream misses it
            log.warning("event stream: %s", e)
    if hooks:
        _start_sender()
        try:
            _webhooks.put_nowait(payload)
        except queue.Full:
            log.warning("event webhooks: %d events waiting, one dropped", _webhooks.qsize())


def _start_sender():
    global _sender
    with _sender_lock:
        if _sender is None or not _sender.is_alive():  # started lazily: after gunicorn forks its workers
            _sender = threading.Thread(target=_deliver, name="bpmn8n-event-webhooks", daemon=True)
            _sender.start()


def _deliver():
    while True:
        payload = json.loads(_webhooks.get())
        for url in settings.EVENT_WEBHOOKS:
            try:
                post_json(url, payload, timeout=WEBHOOK_TIMEOUT)
            except (ServiceUnavailable, ValueError) as e:
                log.warning("event webhook %s: %s", url, e)
        _webhooks.task_done()


class EventStream:
    """Server-sent events, one per finished step: `id: <cursor>` and `data: <the event as JSON>`. The
    response ends after `duration` seconds (the client reconnects with Last-Event-ID); comments keep it
    open while nothing happens."""

    REDIS_ID = re.compile(r"\d+-\d+")
    SETTLE = timedelta(seconds=2)  # database polling: a step that finished earlier but is written late

    def __init__(self, duration=55, block=5, interval=1):
        self.duration, self.block, self.interval = duration, block, interval

    def response(self, request):
        response = StreamingHttpResponse(self.events(request.headers.get("Last-Event-ID", "")),
                                         content_type="text/event-stream")
        response["Cache-Control"] = "no-cache, no-transform"
        response["X-Accel-Buffering"] = "no"
        return response

    def events(self, last_id=""):
        yield "retry: 1500\n\n"
        client = connection()
        try:
            yield from self._redis(client, last_id) if client else self._database(last_id)
        finally:
            close_old_connections()

    def _redis(self, client, last_id):
        deadline = time.monotonic() + self.duration
        try:
            cursor = last_id if self.REDIS_ID.fullmatch(last_id) else next(
                (xid for xid, _ in client.xrevrange(STREAM, count=1)), "0-0")  # from now on
            while time.monotonic() < deadline:
                reply = client.xread({STREAM: cursor}, count=100, block=int(self.block * 1000))
                if not reply:
                    yield ": waiting\n\n"
                for cursor, fields in reply[0][1] if reply else ():
                    yield f"id: {cursor}\ndata: {fields['event']}\n\n"
        except Exception as e:  # noqa: BLE001 Redis gone: end the response, the client reconnects
            log.warning("event stream: %s", e)

    def _database(self, last_id):
        # ponytail: one query a second per open stream, ordered by `ended`; Redis is the scalable path
        deadline, quiet = time.monotonic() + self.duration, time.monotonic()
        ended, pk = self._cursor(last_id)
        while time.monotonic() < deadline:
            close_old_connections()
            rows = list(Event.objects.exclude(status="running").filter(ended__lte=timezone.now() - self.SETTLE)
                        .filter(Q(ended__gt=ended) | Q(ended=ended, id__gt=pk)).order_by("ended", "id")
                        .values(*FIELDS)[:200])
            for row in rows:
                ended, pk = row["ended"], row["id"]
                yield f"id: {ended.isoformat()}/{pk}\ndata: {json.dumps(row, cls=DjangoJSONEncoder)}\n\n"
            if rows:
                quiet = time.monotonic()
            elif time.monotonic() - quiet >= 10:
                quiet = time.monotonic()
                yield ": waiting\n\n"
            time.sleep(self.interval)

    def _cursor(self, last_id):
        """(ended, id) after which to send; from now on when the id is not one of ours."""
        when, _, pk = last_id.rpartition("/")
        try:
            return datetime.fromisoformat(when), int(pk)
        except ValueError:
            return timezone.now() - self.SETTLE, 0
