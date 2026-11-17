"""Small SSE change notifications; payloads stay in the conditional JSON endpoints."""
import time

from django.db import close_old_connections
from django.http import StreamingHttpResponse

from .conditional import revision


class ChangeStream:
    def __init__(self, snapshot, duration=55, interval=1):
        self.snapshot, self.duration, self.interval = snapshot, duration, interval

    def events(self, previous=""):
        deadline, heartbeat = time.monotonic() + self.duration, time.monotonic()
        yield "retry: 1500\n\n"
        try:
            while time.monotonic() < deadline:
                close_old_connections()
                current = revision(self.snapshot())
                if current != previous:
                    previous = current
                    yield f"id: {current}\nevent: change\ndata: {{}}\n\n"
                elif time.monotonic() - heartbeat >= 10:
                    heartbeat = time.monotonic()
                    yield ": heartbeat\n\n"
                time.sleep(self.interval)
        finally:
            close_old_connections()

    def response(self, request):
        response = StreamingHttpResponse(self.events(request.headers.get("Last-Event-ID", "")), content_type="text/event-stream")
        response["Cache-Control"] = "no-cache, no-transform"
        response["X-Accel-Buffering"] = "no"
        return response
