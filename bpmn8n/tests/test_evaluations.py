import io
import tempfile
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings
from pydantic import ValidationError

from bpmn8n.clients.http import ServiceUnavailable
from bpmn8n.core.evaluations import EvaluationStore, Metric, Sample
from bpmn8n.workflows import Workflow


class EvaluationsTest(SimpleTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "projects"
        self.project = self.root / "example"
        self.project.mkdir(parents=True)
        (self.project / "workflow.json").write_text("{}")
        self.store = EvaluationStore(self.root)

    def sample(self, **overrides):
        return Sample.model_validate(dict(project="example", workflow_id="workflow1", execution_id="123",
                                          node_id="node1", node_name="Quality", sample_id="0:0",
                                          metrics=[dict(name="accuracy", value=.9, threshold=.8)], **overrides))

    def test_atomic_parallel_samples_retries_thresholds_and_isolation(self):
        original = self.sample()
        self.store.write(original)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(self.store.write, [original.model_copy(update={"sample_id": f"{i}:0"}) for i in range(8)]))
        run = self.store.read("example", "workflow1", "123")
        self.assertEqual(run["sample_count"], 8)
        self.assertEqual(run["status"], "passed")
        self.assertAlmostEqual(run["metrics"][0]["mean"], .9)
        self.assertEqual(run["metrics"][0]["checked_count"], 8)
        self.assertEqual(self.store.runs("other"), [])
        self.store.write(original.model_copy(update={"metrics": [original.metrics[0].model_copy(update={"value": .5})]}))
        self.assertEqual(self.store.read("example", "workflow1", "123")["status"], "failed")
        self.assertEqual(len(self.store.runs("workflow1")), 1)

    def test_metric_validation(self):
        for value in (float("nan"), float("inf"), True, "1"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                Sample.model_validate({**self.sample().model_dump(), "metrics": [{"name": "bad", "value": value}]})
        with self.assertRaises(ValidationError):
            Sample.model_validate({**self.sample().model_dump(), "metrics": [self.sample().metrics[0]] * 2})
        metric = self.sample().metrics[0].model_copy(update={"direction": "lower", "value": .8})
        self.assertTrue(metric.passed)
        self.assertIsNone(metric.model_copy(update={"threshold": None}).passed)

    def test_paths_symlinks_and_corrupt_samples(self):
        draft = self.root / "_draft"
        draft.mkdir()
        (draft / "workflow.json").write_text("{}")
        self.assertEqual(self.store.runs("workflow1"), [])
        for project in ("../example", "/tmp", "missing"):
            with self.assertRaises(ValueError):
                self.store.write(self.sample().model_copy(update={"project": project}))
        path = self.store.write(self.sample())["path"]
        (self.root.parent / path).write_text("broken")
        # The index remains authoritative after import; polling never rereads exported JSON.
        self.assertEqual(self.store.read("example", "workflow1", "123")["sample_count"], 1)
        (self.project / "run" / "outside").symlink_to(self.root.parent, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.store.write(self.sample().model_copy(update={"workflow_id": "outside"}))

    @override_settings(API_TOKEN="secret", N8N_PUBLIC_URL="http://localhost:5678")
    def test_browser_auth_uses_workflow_permissions_and_cors_without_exposing_token(self):
        url = "/api/evaluations/workflow1/runs"
        self.assertEqual(self.client.get(url).status_code, 401)
        self.client.cookies["n8n-auth"] = "session"
        for query, headers in (("", {"HTTP_BROWSER_ID": "ui-browser-123"}), ("?browserId=ui-browser-123", {})):
            with patch("bpmn8n.api.evaluations.request_json", return_value={"data": {"id": "workflow1"}}) as remote, \
                    patch("bpmn8n.api.evaluations.EvaluationStore", return_value=self.store):
                response = self.client.get(url + query, HTTP_ORIGIN="http://localhost:5678", **headers)
            self.assertEqual(response.status_code, 200)
            self.assertIn("/rest/workflows/workflow1", remote.call_args.args[1])
            self.assertEqual(remote.call_args.kwargs["headers"], {"Cookie": "n8n-auth=session", "browser-id": "ui-browser-123"})
        self.assertEqual(response["Access-Control-Allow-Credentials"], "true")
        self.assertEqual(response["Access-Control-Allow-Origin"], "http://localhost:5678")
        self.assertNotIn("secret", response.content.decode())
        with patch("bpmn8n.api.evaluations.request_json", side_effect=ServiceUnavailable("denied", 403)):
            self.assertEqual(self.client.get(url).status_code, 401)
        self.assertEqual(self.client.post("/api/evaluations/samples", data=self.sample().model_dump(),
                                          content_type="application/json").status_code, 401)

    @override_settings(API_TOKEN="secret")
    def test_api_writes_and_reads_validated_samples(self):
        with patch("bpmn8n.api.evaluations.EvaluationStore", return_value=self.store):
            response = self.client.post("/api/evaluations/samples", data=self.sample().model_dump(),
                                        content_type="application/json", HTTP_AUTHORIZATION="Bearer secret")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue((self.root.parent / response.json()["path"]).exists())
        with patch("bpmn8n.api.evaluations.EvaluationStore", return_value=self.store):
            response = self.client.get("/api/evaluations/workflow1/runs", HTTP_AUTHORIZATION="Bearer secret")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["runs"]), 1)

    def gate(self, *args):
        """(exit code, what eval_gate printed) on the temporary store."""
        out = io.StringIO()
        with override_settings(BASE_DIR=self.root.parent):
            try:
                call_command("eval_gate", *args, stdout=out)
                return 0, out.getvalue()
            except CommandError as e:
                return e.returncode, f"{out.getvalue()}{e}"

    def run_of(self, execution, *metrics, workflow="workflow1"):
        self.store.write(self.sample().model_copy(update={"execution_id": execution, "workflow_id": workflow,
                                                          "metrics": [Metric(**m) for m in metrics]}))

    def test_the_gate_fails_on_a_missed_threshold_of_the_newest_runs(self):
        self.assertEqual(self.gate()[0], 2)  # no run: nothing to judge
        self.run_of("1", dict(name="accuracy", value=.9))
        self.assertEqual(self.gate(), (2, "unscored: example · workflow workflow1 · execution 1\n"
                                          "nothing to judge: no evaluation run with a threshold"))
        self.run_of("2", dict(name="accuracy", value=.9, threshold=.8))
        self.run_of("1", dict(name="accuracy", value=.95, threshold=.9), workflow="workflow2")
        self.assertEqual(self.gate()[0], 0)
        self.run_of("3", dict(name="accuracy", value=.5, unit="ratio", threshold=.8),
                    dict(name="latency", value=900, unit="ms", direction="lower", threshold=500),
                    dict(name="cost", value=1, direction="lower", threshold=2))
        code, out = self.gate()
        self.assertEqual(code, 1)
        self.assertIn("failed: example · workflow workflow1 · execution 3\n"
                      "  Quality · accuracy = 0.5 ratio, threshold >= 0.8\n"
                      "  Quality · latency = 900 ms, threshold <= 500\n", out)
        self.assertNotIn("cost", out)
        self.assertIn("1 of 2 evaluation runs missed a threshold", out)
        self.assertEqual(self.gate("--workflow", "workflow2")[0], 0)  # the other workflow passes alone
        self.assertEqual(self.gate("--project", "example", "--workflow", "nothing")[0], 2)
        self.run_of("4", dict(name="accuracy", value=.85, threshold=.8))  # fixed: the newest run passes again
        self.assertEqual(self.gate("--workflow", "workflow1")[0], 0)
        self.assertEqual(self.gate("--workflow", "workflow1", "--last", "2")[0], 1)  # the one before did not

    def test_builder_exports_metrics_step(self):
        workflow = Workflow("test").metrics("Scores", "example", [{"name": "accuracy", "value": "={{ $json.score }}", "threshold": .8}])
        node = workflow.nodes[0]
        self.assertEqual(node["type"], "CUSTOM.bpmn8nMetrics")
        self.assertEqual(node["parameters"]["metrics"]["values"][0]["checkThreshold"], True)

    def test_index_bounds_charts_and_sample_pages_without_reopening_files(self):
        self.store.write(self.sample())
        with self.store.database() as db:
            for i in range(1000):
                sample = self.sample().model_dump()
                sample.update(sample_id=f"{i}:0", recorded_at=f"2026-11-12T12:{i//60:02d}:{i%60:02d}+00:00", schema_version=1)
                sample["metrics"] = [dict(name="accuracy", value=float(i), unit="", direction="higher", threshold=.8, passed=i >= 1)]
                self.store.index(db, sample)
        with patch.object(Path, "read_text", side_effect=AssertionError("Unexpected JSON scan")):
            runs = self.store.runs("workflow1")
            detail = self.store.read("example", "workflow1", "123", limit=25, points=40)
            page = self.store.read("example", "workflow1", "123", limit=25, offset=25, points=40)
        self.assertEqual(runs[0]["sample_count"], 1000)
        self.assertNotIn("metrics", runs[0])
        self.assertEqual(len(detail["samples"]), 25)
        self.assertEqual(len(detail["metrics"][0]["series"]), 40)
        self.assertAlmostEqual(detail["metrics"][0]["mean"], 499.5)
        self.assertTrue(detail["has_more"])
        self.assertNotEqual(detail["samples"][0]["sample_id"], page["samples"][0]["sample_id"])
        self.assertEqual(len(json.loads("".join(self.store.export("example", "workflow1", "123")))["samples"]), 1000)

    @override_settings(API_TOKEN="secret")
    def test_conditional_reads_skip_unchanged_detail_queries(self):
        self.store.write(self.sample())
        url = "/api/evaluations/workflow1/runs/example/123"
        with patch("bpmn8n.api.evaluations.EvaluationStore", return_value=self.store):
            first = self.client.get(url, HTTP_AUTHORIZATION="Bearer secret")
            self.assertEqual(first.status_code, 200)
            with patch.object(self.store, "read", side_effect=AssertionError("Unchanged data was reread")):
                same = self.client.get(url, HTTP_AUTHORIZATION="Bearer secret", HTTP_IF_NONE_MATCH=first["ETag"])
            self.assertEqual(same.status_code, 304)
            self.store.write(self.sample().model_copy(update={"sample_id": "1:0"}))
            updated = self.client.get(url, HTTP_AUTHORIZATION="Bearer secret", HTTP_IF_NONE_MATCH=first["ETag"])
            self.assertEqual(updated.status_code, 200)
            self.assertEqual(updated.json()["sample_count"], 2)

    @override_settings(N8N_PUBLIC_URL="http://localhost:5678")
    def test_conditional_and_sse_cors_preflight(self):
        response = self.client.options("/api/evaluations/workflow1/runs", HTTP_ORIGIN="http://127.0.0.1:5678",
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="GET", HTTP_ACCESS_CONTROL_REQUEST_HEADERS="If-None-Match")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response["Access-Control-Allow-Origin"], "http://127.0.0.1:5678")
        self.assertIn("If-None-Match", response["Access-Control-Allow-Headers"])

    @override_settings(API_TOKEN="secret", N8N_PUBLIC_URL="http://localhost:5678")
    def test_cors_accepts_both_local_editor_addresses_only_on_configured_port(self):
        for path in ("/api/evaluations/workflow1/runs", "/api/openapi.json"):
            for origin in ("http://localhost:5678", "http://127.0.0.1:5678"):
                with self.subTest(path=path, origin=origin):
                    response = self.client.get(path, HTTP_ORIGIN=origin)
                    self.assertEqual(response["Access-Control-Allow-Origin"], origin)
                    self.assertIn("Origin", response["Vary"])
            for origin in ("http://127.0.0.1:8080", "https://localhost:5678", "http://localhost.evil:5678"):
                with self.subTest(path=path, origin=origin):
                    response = self.client.get(path, HTTP_ORIGIN=origin)
                    self.assertNotIn("Access-Control-Allow-Origin", response)

    @override_settings(API_TOKEN="secret", N8N_PUBLIC_URL="https://n8n.example.com")
    def test_remote_configuration_does_not_allow_loopback_origins(self):
        response = self.client.get("/api/evaluations/workflow1/runs", HTTP_ORIGIN="http://127.0.0.1:5678")
        self.assertNotIn("Access-Control-Allow-Origin", response)
