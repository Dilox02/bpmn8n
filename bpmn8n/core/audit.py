"""Who is acting: the request being served (middleware), read by AuditEntry.write wherever an action happens,
so the models' transitions know the person, or the API token, and the client's address."""
from contextvars import ContextVar

_request = ContextVar("request", default=None)


def middleware(get_response):
    """Keeps the request at hand for the code it calls (after AuthenticationMiddleware)."""
    def call(request):
        token = _request.set(request)
        try:
            return get_response(request)
        finally:
            _request.reset(token)
    return call


def current_request():
    return _request.get()


def actor_of(actor=None, request=None):
    """A name for who acts: `actor` (a user or a name) when given; else the signed-in person of a page, "api" for an
    API call (the token is the identity), "system" outside any request (commands, schedules)."""
    if actor is not None:
        return getattr(actor, "username", None) or str(actor)
    if request is None:
        return "system"
    user = getattr(request, "user", None)
    if request.path.startswith("/api/") or not (user and user.is_authenticated):
        return "api"
    return user.username


def client_ip(request):
    """The address the request came from. ponytail: REMOTE_ADDR, i.e. the TLS proxy's behind one; read the proxy's
    X-Forwarded-For only once the deployment says which proxy to trust."""
    return (request.META.get("REMOTE_ADDR") or None) if request is not None else None
