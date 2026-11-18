"""Global variables: values every workflow shares ({{vars.<key>}}, the Global Variable node, /api/variables),
bpmn8n's open alternative to n8n's paid Variables.

Two backends with the same behaviour: Redis when REDIS_URL is set (the stack's Redis, a database n8n's queue
does not use), else the database (GlobalVariable): tests, SQLite, bpmn8n inside another Django project.
Values are JSON. increment and compare_and_set are atomic across workers and processes; lock() is a short
mutual exclusion with a TTL, so a holder that crashes blocks the others for `ttl` seconds at most."""
import json
import re
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from ..conf import settings

KEY = re.compile(r"[A-Za-z0-9_.-]{1,200}")
LOCK = "lock:"  # the database backend's lock rows: a ":" no variable key can have


class InvalidKey(ValueError):
    pass


class NotANumber(TypeError):
    pass


class LockTimeout(TimeoutError):
    pass


@dataclass(frozen=True)
class Variable:
    key: str
    value: object
    updated: datetime | None = None
    expires: datetime | None = None  # None: never


def check_key(key):
    if not isinstance(key, str) or not KEY.fullmatch(key):
        raise InvalidKey(f"{key!r}: a key is 1 to 200 letters, digits, _ . or -")
    return key


def check_prefix(prefix):
    return check_key(prefix) if prefix else ""


def as_json(value):
    """The value as JSON text; ValueError if it is not JSON (NaN and infinity included)."""
    try:
        return json.dumps(value, allow_nan=False)
    except TypeError as e:
        raise ValueError(f"not a JSON value: {e}") from e


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def add(current, by, key):
    """current + by; a variable that does not exist counts as 0, as in Redis."""
    current = 0 if current is None else current
    if not is_number(current):
        raise NotANumber(f"{key!r} is {json.dumps(current)[:60]}, not a number")
    if not is_number(by):
        raise NotANumber(f"cannot add {by!r}")
    return current + by


def same(a, b):
    """JSON equality: 1 equals 1.0, true is not 1."""
    return isinstance(a, bool) == isinstance(b, bool) and a == b


def check_ttl(ttl):
    if ttl is not None and ttl <= 0:
        raise ValueError("ttl: seconds, more than 0 (none: never expires)")
    return ttl


class Store:
    """The operations both backends offer; lock() is built on their _acquire and _release."""

    def get(self, key, default=None):
        return self.get_many([key]).get(key, default)

    @contextmanager
    def lock(self, name, ttl=10, wait=10):
        """Holds `name` for at most `ttl` seconds; waits up to `wait` seconds for it, else LockTimeout."""
        check_key(name)
        check_ttl(ttl)
        token, deadline = uuid.uuid4().hex, time.monotonic() + wait
        while not self._acquire(name, token, ttl):
            if time.monotonic() >= deadline:
                raise LockTimeout(f"the lock {name!r} is held by someone else")
            time.sleep(0.05)
        try:
            yield
        finally:
            self._release(name, token)


class RedisStore(Store):
    """One hash per variable, bpmn8n:var:<key> = {value: JSON, updated: epoch seconds}, with the TTL on the
    key. increment and compare-and-set are optimistic transactions (WATCH/MULTI), retried when another
    client wrote in between."""

    PREFIX, LOCKS = "bpmn8n:var:", "bpmn8n:lock:"

    def __init__(self, client):
        self.r = client

    def get_many(self, keys):
        keys = [check_key(k) for k in keys]
        pipe = self.r.pipeline(transaction=False)
        for key in keys:
            pipe.hget(self.PREFIX + key, "value")
        return {k: json.loads(v) for k, v in zip(keys, pipe.execute()) if v is not None}

    def read(self, key):
        return next(iter(self._read([check_key(key)])), None)

    def _read(self, keys):
        pipe = self.r.pipeline(transaction=False)
        for key in keys:
            pipe.hgetall(self.PREFIX + key)
            pipe.pttl(self.PREFIX + key)
        replies, now = pipe.execute(), datetime.now(UTC)
        return [Variable(key, json.loads(data["value"]), datetime.fromtimestamp(float(data["updated"]), UTC),
                         now + timedelta(milliseconds=pttl) if pttl > 0 else None)
                for key, data, pttl in zip(keys, replies[::2], replies[1::2]) if data]  # gone since the scan

    def list(self, prefix="", limit=1000):
        # ponytail: SCAN of every matching key per call; an index set if thousands of variables appear
        names = sorted(self.r.scan_iter(match=f"{self.PREFIX}{check_prefix(prefix)}*", count=500))[:limit]
        return self._read([n.removeprefix(self.PREFIX) for n in names])

    def set(self, key, value, ttl=None):
        pipe = self.r.pipeline()  # MULTI: the value and its TTL change together
        self._write(pipe, self.PREFIX + check_key(key), as_json(value), check_ttl(ttl))
        pipe.execute()

    def _write(self, pipe, name, text, ttl, keep_ttl=False):
        pipe.hset(name, mapping={"value": text, "updated": repr(time.time())})
        if ttl:
            pipe.pexpire(name, int(ttl * 1000))
        elif not keep_ttl:
            pipe.persist(name)

    def delete(self, key):
        return bool(self.r.delete(self.PREFIX + check_key(key)))

    def increment(self, key, by=1, ttl=None):
        """The new value. `ttl` applies when the increment creates the variable (a counter per time window)."""
        return self._atomic(key, lambda current, exists: (True, add(current, by, key), None if exists else check_ttl(ttl)))

    def compare_and_set(self, key, expected, value, ttl=None):
        """Sets `value` if the current one equals `expected` (a missing variable counts as null)."""
        text, ttl = as_json(value), check_ttl(ttl)
        return self._atomic(key, lambda current, exists: (same(current, expected), value, ttl), text)

    def _atomic(self, key, change, text=None):
        """change(current value, exists) -> (write?, new value, ttl); returns the new value, or False."""
        from redis import WatchError
        name = self.PREFIX + check_key(key)
        with self.r.pipeline() as pipe:
            while True:
                try:
                    pipe.watch(name)
                    raw = pipe.hget(name, "value")
                    write, value, ttl = change(None if raw is None else json.loads(raw), raw is not None)
                    if not write:
                        pipe.unwatch()
                        return False
                    pipe.multi()
                    self._write(pipe, name, text or as_json(value), ttl, keep_ttl=raw is not None and text is None)
                    pipe.execute()
                    return True if text else value
                except WatchError:
                    continue

    def _acquire(self, name, token, ttl):
        return bool(self.r.set(self.LOCKS + name, token, nx=True, px=int(ttl * 1000)))

    def _release(self, name, token):
        from redis import WatchError
        with self.r.pipeline() as pipe:
            try:
                pipe.watch(self.LOCKS + name)
                if pipe.get(self.LOCKS + name) == token:  # not a lock someone took after ours expired
                    pipe.multi()
                    pipe.delete(self.LOCKS + name)
                    pipe.execute()
            except WatchError:
                pass


class DatabaseStore(Store):
    """GlobalVariable rows; increment and compare-and-set lock the row (select_for_update). An expired row
    counts as missing; list() deletes the expired ones."""

    @staticmethod
    def _rows():
        from ..models import GlobalVariable
        return GlobalVariable.objects

    def _live(self):
        return self._rows().filter(Q(expires__isnull=True) | Q(expires__gt=timezone.now()))

    @staticmethod
    def _expiry(ttl):
        return timezone.now() + timedelta(seconds=check_ttl(ttl)) if ttl else None

    def get_many(self, keys):
        return dict(self._live().filter(key__in=[check_key(k) for k in keys]).values_list("key", "value"))

    def read(self, key):
        row = self._live().filter(key=check_key(key)).first()
        return Variable(row.key, row.value, row.updated, row.expires) if row else None

    def list(self, prefix="", limit=1000):
        self._rows().filter(expires__lte=timezone.now()).delete()
        rows = self._live().filter(key__startswith=check_prefix(prefix)).exclude(key__startswith=LOCK)
        return [Variable(r.key, r.value, r.updated, r.expires) for r in rows.order_by("key")[:limit]]

    def set(self, key, value, ttl=None):
        as_json(value)
        self._rows().update_or_create(key=check_key(key), defaults={"value": value, "expires": self._expiry(ttl)})

    def delete(self, key):
        live = self._live().filter(key=check_key(key)).exists()
        self._rows().filter(key=key).delete()
        return live

    def increment(self, key, by=1, ttl=None):
        """The new value. `ttl` applies when the increment creates the variable (a counter per time window)."""
        check_key(key)
        with transaction.atomic():
            row, _ = self._rows().select_for_update().get_or_create(
                key=key, defaults={"value": 0, "expires": self._expiry(ttl)})
            if row.expires and row.expires <= timezone.now():  # expired: it starts again
                row.value, row.expires = 0, self._expiry(ttl)
            row.value = add(row.value, by, key)
            row.save()
        return row.value

    def compare_and_set(self, key, expected, value, ttl=None):
        """Sets `value` if the current one equals `expected` (a missing variable counts as null)."""
        as_json(value)
        return self._swap(check_key(key), expected, value, self._expiry(ttl))

    def _swap(self, key, expected, value, expires):
        with transaction.atomic():
            row = self._rows().select_for_update().filter(key=key).first()
            live = row and not (row.expires and row.expires <= timezone.now())
            if not same(row.value if live else None, expected):
                return False
            if row:
                row.value, row.expires = value, expires
                row.save()
                return True
            try:
                with transaction.atomic():
                    self._rows().create(key=key, value=value, expires=expires)
            except IntegrityError:  # someone else created it first
                return False
        return True

    def _acquire(self, name, token, ttl):
        return self._swap(LOCK + name, None, token, self._expiry(ttl))

    def _release(self, name, token):
        with transaction.atomic():
            row = self._rows().select_for_update().filter(key=LOCK + name).first()
            if row and row.value == token:
                row.delete()


@lru_cache(maxsize=4)
def _client(url):
    import redis
    # socket_timeout above the event stream's 5 s blocking read
    return redis.Redis.from_url(url, decode_responses=True, socket_connect_timeout=2, socket_timeout=10,
                                health_check_interval=30)


def connection():
    """The Redis client of REDIS_URL, None when it is empty."""
    return _client(settings.REDIS_URL) if settings.REDIS_URL else None


def store():
    """The configured store: Redis when REDIS_URL is set, else the database."""
    client = connection()
    return RedisStore(client) if client else DatabaseStore()
