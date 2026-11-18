"""The open edition: global variables, workflow history, log streaming and case search."""
import json
import threading
import time
from datetime import timedelta
from unittest.mock import patch

import fakeredis
from django.test import TestCase, override_settings
from django.utils import timezone

from ..core import history, shared, streaming
from ..models import (
    Case,
    Event,
    GlobalVariable,
    MessageSubscription,
    UserTask,
    WorkflowVersion,
)
from ..workflows.changes import changes, content_hash


class StoreContract:
    """The same behaviour from both backends; `make()` returns a fresh store."""

    def setUp(self):
        self.store = self.make()

    def test_get_set_delete(self):
        s = self.store
        self.assertIsNone(s.get("rate"))
        self.assertEqual(s.get("rate", 7), 7)
        s.set("rate", {"eur": 1.08, "tags": ["a"]})
        self.assertEqual(s.get("rate"), {"eur": 1.08, "tags": ["a"]})
        self.assertEqual(s.read("rate").value, {"eur": 1.08, "tags": ["a"]})
        self.assertIsNotNone(s.read("rate").updated)
        s.set("rate", None)  # JSON null is a value, not a missing variable
        self.assertIsNotNone(s.read("rate"))
        self.assertTrue(s.delete("rate"))
        self.assertFalse(s.delete("rate"))
        self.assertIsNone(s.read("rate"))

    def test_keys_and_values_are_checked(self):
        for bad in ("", "a b", "a:b", "x" * 201, "ünï"):
            with self.assertRaises(shared.InvalidKey):
                self.store.set(bad, 1)
        with self.assertRaises(ValueError):
            self.store.set("nan", float("nan"))
        with self.assertRaises(ValueError):
            self.store.set("obj", object())

    def test_list_by_prefix(self):
        for key in ("acme.rate", "acme.limit", "other"):
            self.store.set(key, key.upper())
        self.assertEqual([v.key for v in self.store.list("acme.")], ["acme.limit", "acme.rate"])
        self.assertEqual(len(self.store.list()), 3)
        self.assertEqual(self.store.get_many(["other", "acme.rate", "none"]), {"other": "OTHER", "acme.rate": "ACME.RATE"})

    def test_ttl(self):
        self.store.set("short", 1, ttl=0.15)
        self.assertIsNotNone(self.store.read("short").expires)
        self.store.set("long", 2)
        self.assertIsNone(self.store.read("long").expires)
        time.sleep(0.25)
        self.assertIsNone(self.store.get("short"))
        self.assertEqual([v.key for v in self.store.list()], ["long"])
        with self.assertRaises(ValueError):
            self.store.set("x", 1, ttl=-1)

    def test_increment(self):
        self.assertEqual(self.store.increment("hits"), 1)  # a missing variable counts as 0
        self.assertEqual(self.store.increment("hits", 5), 6)
        self.assertEqual(self.store.increment("hits", -1.5), 4.5)
        self.assertEqual(self.store.increment("ints", 2), 2)
        self.assertIsInstance(self.store.get("ints"), int)  # an int stays an int
        self.store.set("name", "ann")
        with self.assertRaises(shared.NotANumber):
            self.store.increment("name")
        self.store.set("flag", True)
        with self.assertRaises(shared.NotANumber):
            self.store.increment("flag")

    def test_increment_ttl_applies_when_it_creates(self):
        self.store.increment("window", ttl=0.15)
        self.store.increment("window", ttl=100)  # the window keeps its first TTL
        time.sleep(0.25)
        self.assertEqual(self.store.increment("window"), 1)  # expired: it starts again

    def test_compare_and_set(self):
        self.assertTrue(self.store.compare_and_set("state", None, "open"))  # missing counts as null
        self.assertFalse(self.store.compare_and_set("state", None, "again"))
        self.assertFalse(self.store.compare_and_set("state", "closed", "x"))
        self.assertTrue(self.store.compare_and_set("state", "open", {"n": 1}))
        self.assertTrue(self.store.compare_and_set("state", {"n": 1}, 2))
        self.assertTrue(self.store.compare_and_set("state", 2.0, 3))  # 2 == 2.0
        self.store.set("bool", True)
        self.assertFalse(self.store.compare_and_set("bool", 1, False))  # true is not 1
        self.assertEqual(self.store.get("state"), 3)

    def test_lock(self):
        with self.store.lock("job", ttl=5):
            with self.assertRaises(shared.LockTimeout), self.store.lock("job", wait=0.1):
                pass
            with self.store.lock("other", wait=0):  # another name is free
                pass
        with self.store.lock("job", wait=0):  # released
            pass
        self.assertEqual(self.store.list(), [])  # locks are not variables

    def test_an_expired_lock_is_taken_over(self):
        with self.store.lock("job", ttl=0.1), self.store.lock("job", wait=1):  # the first holder overran its TTL
            pass


class RedisStoreTest(StoreContract, TestCase):
    def make(self):
        return shared.RedisStore(fakeredis.FakeRedis(decode_responses=True))

    def test_increments_from_many_threads_are_all_counted(self):
        server = fakeredis.FakeServer()

        def work():
            mine = shared.RedisStore(fakeredis.FakeRedis(server=server, decode_responses=True))
            for _ in range(25):
                mine.increment("counter")

        threads = [threading.Thread(target=work) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(shared.RedisStore(fakeredis.FakeRedis(server=server, decode_responses=True)).get("counter"), 200)

    def test_values_are_json_under_the_prefix(self):
        self.store.set("rate", 1.5)
        self.assertEqual(self.store.r.hget("bpmn8n:var:rate", "value"), "1.5")


class DatabaseStoreTest(StoreContract, TestCase):
    def make(self):
        return shared.DatabaseStore()

    def test_rows(self):
        self.store.set("rate", [1, 2])
        self.assertEqual(GlobalVariable.objects.get(pk="rate").value, [1, 2])


class ConfiguredStoreTest(TestCase):
    def test_redis_when_configured_else_the_database(self):
        self.assertIsInstance(shared.store(), shared.DatabaseStore)
        with override_settings(REDIS_URL="redis://localhost:6379/1"):
            self.assertIsInstance(shared.store(), shared.RedisStore)


@override_settings(N8N_URL="http://n8n:5678")
class GlobalTemplatesTest(TestCase):
    """{{vars.<key>}} reads a global variable wherever bpmn8n fills templates."""

    CASE = {"X-Bpmn8n-Case": "wf1:7", "X-Bpmn8n-Process": "Refunds", "X-Bpmn8n-Activity": "Step"}

    def setUp(self):
        store = shared.store()
        store.set("max_refund", 1000)
        store.set("acme.policy", {"currency": "EUR", "limits": [50, 100]})
        store.set("support-team", "helpdesk")

    def post(self, url, body):
        return self.client.post(url, body, content_type="application/json", headers=self.CASE)

    def test_a_task_field(self):
        r = self.post("/api/tasks/calculate_refund/run", {"fields": {"amount": "{{vars.max_refund}}"}})
        self.assertEqual(r.json()["result"]["refund"], 1000.0)
        self.assertNotIn("vars", Case.objects.get(pk="wf1:7").variables)  # never copied into the case

    def test_a_gateway_condition(self):
        body = {"kind": "exclusive", "item": {"amount": 1500, "vars": {"max_refund": 1}},  # a case "vars" is shadowed
                "conditions": {"big": "{{amount}} > {{vars.max_refund}}", "small": "true"}}
        self.assertEqual(self.post("/api/gateways/conditions", body).json()["branches"], ["big"])
        body["conditions"] = {"eur": '{{vars.acme.policy.currency}} == "EUR"', "no": "{{vars.missing}} == 1"}
        self.assertEqual(self.post("/api/gateways/conditions", {**body, "kind": "inclusive"}).json()["branches"], ["eur"])

    def test_a_user_task_title_and_context(self):
        r = self.post("/api/usertasks", {
            "title": "Refund above {{vars.max_refund}} {{vars.acme.policy.currency}} for {{vars.support-team}}",
            "context": {"limit": "{{vars.acme.policy.limits.1}}"}, "resume_url": "http://n8n/webhook-waiting/1"})
        task = UserTask.objects.get(pk=r.json()[0]["id"])
        self.assertEqual(task.title, "Refund above 1000 EUR for helpdesk")
        self.assertEqual(task.context, {"limit": 100})

    def test_a_correlation_key(self):
        shared.store().set("batch", "B-7")
        self.post("/api/messages/subscriptions", {"name": "paid", "correlation_key": "{{vars.batch}}",
                                                   "resume_url": "http://n8n/webhook-waiting/2"})
        self.assertEqual(MessageSubscription.objects.get().correlation_key, "B-7")

    def test_only_the_names_a_template_uses_are_read(self):
        with patch.object(shared.DatabaseStore, "get_many", return_value={}) as get_many:
            self.post("/api/tasks/send_notification/run", {"fields": {"to": "a@b.c", "subject": "plain"}})
            get_many.assert_not_called()
            self.post("/api/tasks/send_notification/run", {"fields": {"to": "a@b.c", "subject": "{{vars.x.y}}"}})
            self.assertEqual(set(get_many.call_args.args[0]), {"x", "x.y"})


class VariablesApiTest(TestCase):
    def test_status_codes(self):
        c = self.client
        self.assertEqual(c.get("/api/variables/rate").status_code, 404)
        r = c.put("/api/variables/rate", {"value": {"eur": 1.08}, "ttl": 60}, content_type="application/json")
        self.assertEqual((r.status_code, r.json()["value"]), (200, {"eur": 1.08}))
        self.assertIsNotNone(r.json()["expires"])
        self.assertEqual(c.get("/api/variables/rate").json()["value"], {"eur": 1.08})
        self.assertEqual([v["key"] for v in c.get("/api/variables", {"prefix": "ra"}).json()], ["rate"])
        self.assertEqual(c.get("/api/variables/bad:key").status_code, 422)
        self.assertEqual(c.put("/api/variables/x", {"value": 1, "ttl": 0}, content_type="application/json").status_code, 422)
        self.assertEqual(c.delete("/api/variables/rate").status_code, 204)
        self.assertEqual(c.delete("/api/variables/rate").status_code, 404)

    def test_increment_and_compare_and_set(self):
        post = lambda url, body: self.client.post(url, body, content_type="application/json")
        self.assertEqual(post("/api/variables/hits/increment", {}).json()["value"], 1)
        self.assertEqual(post("/api/variables/hits/increment", {"by": 4}).json()["value"], 5)
        self.client.put("/api/variables/name", {"value": "ann"}, content_type="application/json")
        self.assertEqual(post("/api/variables/name/increment", {}).status_code, 409)
        self.assertEqual(post("/api/variables/hits/compare-and-set", {"expected": 5, "value": 6}).status_code, 200)
        stale = post("/api/variables/hits/compare-and-set", {"expected": 5, "value": 7})
        self.assertEqual((stale.status_code, stale.json()["value"]), (409, 6))

    @override_settings(API_TOKEN="s3cret")
    def test_the_api_token_applies(self):
        self.assertEqual(self.client.get("/api/variables").status_code, 401)


def workflow(version, nodes, connections, name="Refunds", **extra):
    return {"id": "wf1", "name": name, "versionId": version, "nodes": nodes, "connections": connections,
            "settings": {"executionOrder": "v1", "binaryMode": "separate"}, **extra}


TASK, XOR = "CUSTOM.bpmn8nServiceTask", "CUSTOM.bpmn8nExclusiveGateway"
V1 = workflow("v1", [
    {"id": "n1", "name": "Start", "type": "n8n-nodes-base.webhook", "position": [0, 0], "parameters": {"path": "r"}},
    {"id": "n2", "name": "Check", "type": XOR, "position": [200, 0],
     "parameters": {"branches": {"values": [{"name": "big"}]}, "conditions": "{{amount}} > 100"}},
    {"id": "n3", "name": "Pay", "type": TASK, "position": [400, 0], "parameters": {"task": "pay"}},
    {"id": "n4", "name": "Old step", "type": TASK, "position": [400, 200], "parameters": {}},
], {"Start": {"main": [[{"node": "Check", "type": "main", "index": 0}]]},
    "Check": {"main": [[{"node": "Pay", "type": "main", "index": 0}], [{"node": "Old step", "type": "main", "index": 0}]]}})
V2 = workflow("v2", [
    {"id": "n1", "name": "Start", "type": "n8n-nodes-base.webhook", "position": [50, 50], "parameters": {"path": "r"}},
    {"id": "n2", "name": "Check", "type": XOR, "position": [200, 0],
     "parameters": {"branches": {"values": [{"name": "big"}]}, "conditions": "{{amount}} > 500"}},
    {"id": "n3", "name": "Pay refund", "type": TASK, "position": [400, 0], "parameters": {"task": "pay"}},
    {"id": "n5", "name": "Review", "type": "CUSTOM.bpmn8nUserTask", "position": [400, 200], "parameters": {}},
], {"Start": {"main": [[{"node": "Check", "type": "main", "index": 0}]]},
    "Check": {"main": [[{"node": "Pay refund", "type": "main", "index": 0}], [{"node": "Review", "type": "main", "index": 0}]]}},
    name="Refunds v2")


class ChangesTest(TestCase):
    def test_a_readable_change_list(self):
        found = changes(V1, V2)
        kinds = [c["change"] for c in found]
        self.assertEqual(kinds, ["name", "node_removed", "node_added", "parameter", "node_renamed",
                                 "connection_removed", "connection_added"])
        param = next(c for c in found if c["change"] == "parameter")
        self.assertEqual((param["node"], param["path"], param["before"], param["after"]),
                         ("Check", "parameters.conditions", "{{amount}} > 100", "{{amount}} > 500"))
        self.assertIn("Renamed “Pay” to “Pay refund”", [c["text"] for c in found])
        self.assertIn("Added the flow “Check” (default) → “Review”", [c["text"] for c in found])  # the branch's name
        self.assertNotIn("position", json.dumps(found))  # moving a step is not a change
        self.assertEqual(changes(V1, V1), [])

    def test_the_hash_covers_nodes_and_connections_only(self):
        self.assertEqual(content_hash(V1), content_hash({**V1, "versionId": "other", "name": "x"}))
        self.assertNotEqual(content_hash(V1), content_hash(V2))


class HistoryApiTest(TestCase):
    def test_versions_and_diff(self):
        old = history.record(V1)
        history.record(V2)
        Case.objects.create(case_id="wf1:1", process="Refunds", process_version="v1")
        versions = self.client.get("/api/history/wf1").json()
        self.assertEqual([(v["version_id"], v["cases"]) for v in versions], [("v1", 1), ("v2", 0)])
        diff = self.client.get("/api/history/wf1/v2/diff").json()
        self.assertEqual(diff["against"], "v1")
        self.assertIn("node_renamed", [c["change"] for c in diff["changes"]])
        self.assertEqual(self.client.get("/api/history/wf1/v1/diff", {"against": "v2"}).json()["against"], "v2")
        self.assertEqual(self.client.get("/api/history/wf1/v1/diff").json()["against"], None)  # the first one
        self.assertEqual(self.client.get("/api/history/wf1/nope/diff").status_code, 404)
        self.assertEqual(history.record(V1), old)  # a version is kept once


N8N = "bpmn8n.clients.n8n.request"


class ProcessVersionTest(TestCase):
    CASE = {"X-Bpmn8n-Case": "wf1:7", "X-Bpmn8n-Process": "Refunds"}

    def setUp(self):
        history._current.clear()

    def start(self, case="wf1:7"):
        return self.client.post("/api/tasks/calculate_refund/run", {"fields": {"amount": 10}},
                                content_type="application/json", headers={**self.CASE, "X-Bpmn8n-Case": case})

    @override_settings(N8N_API_KEY="key")
    def test_recorded_from_a_cached_read_of_n8n(self):
        with patch(N8N, return_value=V1) as n8n:
            self.start("wf1:7")
            self.start("wf1:8")  # a second case within the TTL: no second read
            self.start("wf1:7")  # a later step of a known case: no read either
        self.assertEqual(n8n.call_count, 1)
        self.assertEqual(n8n.call_args.args, ("GET", "/workflows/wf1"))
        self.assertEqual(set(Case.objects.values_list("process_version", flat=True)), {"v1"})
        self.assertEqual(WorkflowVersion.objects.get().content_hash, content_hash(V1))  # unseen: snapshotted

    @override_settings(N8N_API_KEY="key")
    def test_n8n_2_cases_run_the_published_version(self):
        published = {"versionId": "v1", "nodes": V1["nodes"], "connections": V1["connections"]}
        with patch(N8N, return_value={**V2, "activeVersionId": "v1", "activeVersion": published}):
            self.start()
        self.assertEqual(Case.objects.get().process_version, "v1")
        self.assertEqual(list(WorkflowVersion.objects.values_list("version_id", flat=True)), ["v1", "v2"])
        self.assertEqual(WorkflowVersion.objects.get(version_id="v1").workflow["nodes"], V1["nodes"])

    def test_no_api_key_no_call(self):
        with patch(N8N) as n8n:
            self.start()
        n8n.assert_not_called()
        self.assertEqual(Case.objects.get().process_version, "")

    @override_settings(N8N_API_KEY="key")
    def test_n8n_down_leaves_it_empty(self):
        from ..clients.n8n import N8nError
        with patch(N8N, side_effect=N8nError("down")):
            self.assertEqual(self.start().status_code, 200)
        self.assertEqual(Case.objects.get().process_version, "")

    @override_settings(N8N_API_KEY="key")
    def test_restore_saves_a_new_version_without_activating_it(self):
        old = history.record(V1)
        with patch(N8N, side_effect=[{}, {**V1, "versionId": "v3"}]) as n8n:
            self.assertEqual(history.restore(old).version_id, "v3")
        method, path, body = n8n.call_args_list[0].args
        self.assertEqual((method, path), ("PUT", "/workflows/wf1"))
        self.assertEqual(body["nodes"], V1["nodes"])
        self.assertEqual(body["settings"], {"executionOrder": "v1"})  # what n8n's public API takes back
        self.assertNotIn("active", body)


class StreamingTest(TestCase):
    def event(self, status="complete", minutes_ago=1, **fields):
        at = timezone.now() - timedelta(minutes=minutes_ago)
        return Event.objects.create(case_id="wf1:1", activity="Pay", process="Refunds", endpoint="pay", started=at,
                                    ended=at, duration_ms=5, status=status, resource="system", data={"secret": 1}, **fields)

    def redis(self):
        redis = fakeredis.FakeRedis(decode_responses=True)
        return redis, patch("bpmn8n.core.streaming.connection", return_value=redis)

    def test_nothing_configured_nothing_sent(self):
        with self.captureOnCommitCallbacks(execute=True) as sent:
            streaming.publish(self.event())
        self.assertEqual(sent, [])

    def test_publish_to_the_redis_stream(self):
        redis, configured = self.redis()
        with configured, self.captureOnCommitCallbacks(execute=True):
            streaming.publish(self.event("error", error="boom").pk)  # by id, as measure() does
        (_, fields), = redis.xrange(streaming.STREAM)
        sent = json.loads(fields["event"])
        self.assertEqual((sent["case_id"], sent["status"], sent["error"]), ("wf1:1", "error", "boom"))
        self.assertNotIn("data", sent)  # what happened, not the data

    def test_every_finished_step_is_published(self):
        redis, configured = self.redis()
        with configured, self.captureOnCommitCallbacks(execute=True):
            self.client.post("/api/tasks/calculate_refund/run", {"fields": {"amount": 5}},
                             content_type="application/json", headers={"X-Bpmn8n-Case": "wf1:2"})
            self.client.post("/api/events", {"status": "error", "message": "late"}, content_type="application/json",
                             headers={"X-Bpmn8n-Case": "wf1:2", "X-Bpmn8n-Activity": "Fail"})
        sent = [json.loads(f["event"]) for _, f in redis.xrange(streaming.STREAM)]
        self.assertEqual([(e["endpoint"], e["status"]) for e in sent], [("calculate_refund", "complete"), ("event", "error")])

    @override_settings(N8N_URL="http://n8n:5678")
    def test_a_completed_user_task_is_published(self):
        redis, configured = self.redis()
        task = UserTask.objects.create(title="t", activity="Approve", case_id="wf1:3", resume_url="http://n8n/w/3")
        with configured, patch("bpmn8n.models.waiting.post_json"), self.captureOnCommitCallbacks(execute=True):
            task.complete(None, {})
        self.assertEqual(json.loads(redis.xrange(streaming.STREAM)[0][1]["event"])["activity"], "Approve")

    def test_a_rolled_back_step_is_not_published(self):
        from django.db import transaction
        redis, configured = self.redis()
        with configured, self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    streaming.publish(self.event())
                    raise RuntimeError
            except RuntimeError:
                pass
        self.assertEqual(redis.xlen(streaming.STREAM), 0)

    def test_publish_to_a_webhook_in_the_background(self):
        from .support import LocalServer
        with LocalServer() as hook, override_settings(EVENT_WEBHOOKS=[hook.url]):
            with self.captureOnCommitCallbacks(execute=True):
                streaming.publish(self.event())
            streaming._webhooks.join()
        (_, _, body), = hook.requests
        self.assertEqual(json.loads(body)["activity"], "Pay")

    def test_a_failing_webhook_never_fails_the_step(self):
        with override_settings(EVENT_WEBHOOKS=["http://127.0.0.1:9/"]), self.assertLogs("bpmn8n", "WARNING"):
            with self.captureOnCommitCallbacks(execute=True):
                streaming.publish(self.event())
            streaming._webhooks.join()

    def test_sse_from_redis_resumes_after_last_event_id(self):
        redis, configured = self.redis()
        first = redis.xadd(streaming.STREAM, {"event": '{"id": 1}'})
        redis.xadd(streaming.STREAM, {"event": '{"id": 2}'})
        with configured:
            text = "".join(streaming.EventStream(duration=0.2, block=0.05).events(first))
            fresh = "".join(streaming.EventStream(duration=0.1, block=0.05).events(""))
        self.assertIn('data: {"id": 2}', text)
        self.assertNotIn('data: {"id": 1}', text)
        self.assertNotIn("data:", fresh)  # without Last-Event-ID: only what comes next

    def test_sse_from_the_database(self):
        a, b = self.event(minutes_ago=3), self.event(minutes_ago=2)
        self.event(status="running", minutes_ago=1)
        start = f"{(a.ended - timedelta(seconds=1)).isoformat()}/0"
        text = "".join(streaming.EventStream(duration=0.05, interval=0.01).events(start))
        ids = [line[4:] for line in text.splitlines() if line.startswith("id: ")]
        self.assertEqual(ids, [f"{a.ended.isoformat()}/{a.pk}", f"{b.ended.isoformat()}/{b.pk}"])
        again = "".join(streaming.EventStream(duration=0.05, interval=0.01).events(ids[0]))
        self.assertEqual(again.count("data:"), 1)

    def test_the_endpoint(self):
        with patch("bpmn8n.api.observability.EventStream", lambda: streaming.EventStream(duration=0, interval=0)):
            r = self.client.get("/api/events/stream")
        self.assertEqual(r["Content-Type"], "text/event-stream")
        self.assertTrue(b"".join(r.streaming_content).startswith(b"retry:"))


class CaseSearchTest(TestCase):
    def setUp(self):
        now = timezone.now()
        Case.objects.create(case_id="wf1:1", process="Refunds", process_version="v1",
                            variables={"amount": 1500, "customer": {"email": "ann@acme.test"}, "vip": True})
        Case.objects.create(case_id="wf1:2", process="Refunds", process_version="v2",
                            variables={"amount": 90, "customer": {"email": "bob@other.test"}, "vip": False})
        Case.objects.create(case_id="wf2:1", process="Hiring", variables={"amount": 3000})
        Event.objects.create(case_id="wf1:2", activity="Pay", process="Refunds", endpoint="pay", started=now,
                             ended=now, duration_ms=1, status="error", resource="system")
        UserTask.objects.create(title="t", activity="Approve", case_id="wf2:1", resume_url="http://n8n/w/1")

    def ids(self, **params):
        r = self.client.get("/api/cases", params)
        self.assertEqual(r.status_code, 200, r.content)
        return sorted(c["case_id"] for c in r.json()["results"])

    def test_filters(self):
        self.assertEqual(self.ids(), ["wf1:1", "wf1:2", "wf2:1"])
        self.assertEqual(self.ids(process="Refunds"), ["wf1:1", "wf1:2"])
        self.assertEqual(self.ids(version="v2"), ["wf1:2"])
        self.assertEqual(self.ids(status="waiting"), ["wf2:1"])
        self.assertEqual(self.ids(status="error"), ["wf1:2"])
        self.assertEqual(self.ids(status="closed"), ["wf1:1"])
        self.assertEqual(self.ids(**{"var.amount__gt": "1000"}), ["wf1:1", "wf2:1"])
        self.assertEqual(self.ids(**{"var.amount__lte": "1500", "process": "Refunds"}), ["wf1:1", "wf1:2"])
        self.assertEqual(self.ids(**{"var.customer.email__contains": "ACME"}), ["wf1:1"])
        self.assertEqual(self.ids(**{"var.vip": "true"}), ["wf1:1"])
        self.assertEqual(self.ids(**{"var.customer.email": "bob@other.test"}), ["wf1:2"])
        self.assertEqual(self.ids(since=(timezone.now() + timedelta(hours=1)).isoformat()), [])

    def test_matched_variables_and_pages(self):
        r = self.client.get("/api/cases", {"var.amount__gte": "1500", "limit": 1}).json()
        self.assertEqual(r["count"], 2)
        self.assertEqual(len(r["results"]), 1)
        self.assertEqual(r["results"][0]["matched"], {"amount": 3000})  # newest first
        self.assertEqual(self.client.get("/api/cases/wf1:1").json()["process"], "Refunds")  # one case, as before

    def test_bad_queries_are_422(self):
        self.assertEqual(self.client.get("/api/cases", {"status": "lost"}).status_code, 422)
        self.assertEqual(self.client.get("/api/cases", {"var.a b": "1"}).status_code, 422)
