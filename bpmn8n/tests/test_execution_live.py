from unittest.mock import Mock, patch

from django.test import SimpleTestCase, TestCase, override_settings

from bpmn8n.api.conditional import revision
from bpmn8n.api.stream import ChangeStream
from bpmn8n.core.observability import Trace, measure, metrics_snapshot, record
from bpmn8n.models import Event


@override_settings(API_TOKEN="")
class ExecutionActivityTest(TestCase):
    def test_parallel_invocations_are_visible_before_completion_with_distinct_inputs(self):
        with measure("function_a", Trace("workflow:123", "A", "Process", "nodeA", 0, 0)):
            record(input={"branch": "left"})
            with measure("function_a", Trace("workflow:123", "A", "Process", "nodeA", 1, 0)):
                record(input={"branch": "right"})
                response = self.client.get("/api/executions/workflow/123/activity")
                self.assertEqual(response.status_code, 200, response.content)
                rows = response.json()["active"]
                self.assertEqual(len(rows), 2)
                self.assertNotEqual(rows[0]["id"], rows[1]["id"])
                self.assertEqual([r["invocation"]["run_index"] for r in rows], [0, 1])
                self.assertNotIn("input", rows[0])
                left = self.client.get(f'/api/executions/workflow/123/activity/{rows[0]["id"]}').json()
                right = self.client.get(f'/api/executions/workflow/123/activity/{rows[1]["id"]}').json()
                self.assertEqual(left["input"]["branch"], "left")
                self.assertEqual(right["input"]["branch"], "right")
                self.assertNotIn("function_a", metrics_snapshot())
                self.assertEqual(Event.objects.cases().count(), 0)
        self.assertEqual(metrics_snapshot()["function_a"]["calls"], 2)
        self.assertEqual(Event.objects.filter(status="running").count(), 0)

    def test_invocation_access_is_scoped_to_the_execution(self):
        with measure("function", Trace("wf:1", "Task")):
            record(input={"secret": "value"})
        event = Event.objects.get()
        self.assertEqual(self.client.get(f"/api/executions/wf/2/activity/{event.pk}").status_code, 404)


class StreamTest(SimpleTestCase):
    def test_sse_emits_only_changed_revisions_and_resumes_from_last_event(self):
        source = Mock(side_effect=[1, 1, 2])
        with patch("bpmn8n.api.stream.time.sleep"):
            stream = ChangeStream(source).events(revision(1))
            self.assertEqual(next(stream), "retry: 1500\n\n")
            change = next(stream)
            self.assertIn("event: change", change)
            self.assertIn(revision(2), change)
            self.assertEqual(source.call_count, 3)
            stream.close()
