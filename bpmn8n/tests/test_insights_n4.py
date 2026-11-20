"""Insights N4: the process map's level of detail, replay, the import wizard, the anonymised export, snapshots."""
import os
import tempfile
from datetime import UTC, datetime
from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, TestCase, override_settings

from ..analytics import Log, dfg, importer
from ..models import AuditEntry, Event
from .support import add_events, step


def cases():
    """A-B-D x5, A-C-D x2, A-D x1, and one odd A-E-D."""
    rows = [(f"w:{i}", "P", [step("A", 0), step("B", 2), step("D", 5)]) for i in range(5)]
    rows += [(f"w:c{i}", "P", [step("A", 0), step("C", 2), step("D", 5)]) for i in range(2)]
    rows += [("w:d", "P", [step("A", 0), step("D", 3)]), ("w:e", "P", [step("A", 0), step("E", 2), step("D", 5)])]
    add_events(*rows)


class ProcessMapTest(TestCase):
    def setUp(self):
        cases()
        self.acts, self.edges = dfg.discover(Log.load())

    def test_discovery_counts_activities_and_edges(self):
        self.assertEqual(self.acts["A"]["frequency"], 9)
        self.assertEqual(self.edges[("A", "B")]["frequency"], 5)
        self.assertEqual(self.edges[("B", "D")]["frequency"], 5)
        self.assertEqual(self.edges[("A", "D")]["frequency"], 1)
        self.assertAlmostEqual(self.edges[("A", "B")]["mean"], 119.0)  # 2 min after A starts, A lasts 1 s

    def test_everything_at_100_percent(self):
        acts, edges = dfg.prune(self.acts, self.edges, 100, 100)
        self.assertEqual((set(acts), set(edges)), (set(self.acts), set(self.edges)))

    def test_the_activities_slider_keeps_the_most_frequent_covering_the_share(self):
        acts, _ = dfg.prune(self.acts, self.edges, 70, 100)
        self.assertEqual(set(acts), {"A", "D", "B"})  # 9+9+5 of 29 events = 79 % >= 70 %
        self.assertEqual(set(dfg.prune(self.acts, self.edges, 0, 100)[0]), {"A"})  # never empty

    def test_the_paths_slider_drops_rare_edges_but_never_disconnects(self):
        acts, edges = dfg.prune(self.acts, self.edges, 100, 0)
        self.assertEqual(set(acts), set(self.acts))
        for a in acts:
            self.assertTrue(any(a in k for k in edges), a)
        self.assertNotIn(("A", "D"), edges)  # the rare shortcut goes while A and D stay connected
        mid = dfg.prune(self.acts, self.edges, 100, 60)[1]
        self.assertIn(("A", "B"), mid)

    def test_the_page_draws_the_map_and_takes_the_sliders_from_the_url(self):
        self.client.force_login(User.objects.create_user("u", password="pw-123456"))
        page = self.client.get("/insights/?process=P&acts=80&paths=10&map=performance").content.decode()
        self.assertIn('<svg class="pmap"', page)
        self.assertIn('name="acts" min="0" max="100" step="5" value="80"', page)
        self.assertIn("performance", page)
        self.assertNotIn("E: ", self.client.get("/insights/?process=P&acts=50").content.decode())  # E is dropped
        self.assertIn("pmap", self.client.get("/insights/?process=P&acts=junk&paths=-3").content.decode())


@override_settings(API_TOKEN="secret", N8N_PUBLIC_URL="http://localhost:5678")
class ReplayApiTest(TestCase):
    def setUp(self):
        cases()

    def get(self, **query):
        return self.client.get("/api/analytics/replay", {"workflow_id": "w", **query}, HTTP_AUTHORIZATION="Bearer secret")

    def test_needs_the_overlay_auth(self):
        self.assertEqual(self.client.get("/api/analytics/replay", {"workflow_id": "w"}).status_code, 401)

    def test_ordered_steps_per_case_bounded_by_the_limit(self):
        body = self.get(limit=3).json()
        self.assertEqual(len(body["cases"]), 3)
        first = body["cases"][0]["steps"]
        self.assertEqual(first[0]["node"], "A")
        self.assertTrue(all(s["end"] >= s["start"] for s in first))
        self.assertEqual([s["start"] for s in first], sorted(s["start"] for s in first))
        self.assertEqual(len(self.get().json()["cases"]), 9)
        self.assertEqual(self.get(workflow_id="other").json()["cases"], [])
        self.assertEqual(self.get(limit=0).status_code, 422)


CSV = """Case ID;Task;Begin;Finish;Who;Amount
c1;Register;03/01/2026 09:00;03/01/2026 09:05;anna;120
c1;Approve;03/01/2026 09:30;03/01/2026 09:20;bob;120
c2;Register;04/01/2026 10:00;04/01/2026 10:00;anna;80
c2;Approve;04/01/2026 10:00;04/01/2026 10:00;anna;80
c3;Register;;;anna;5
;Register;04/01/2026 10:00;04/01/2026 10:00;anna;5
"""


def table(text=CSV):
    return importer.read_table(text.encode(), "log.csv")


class ImporterTest(TestCase):
    def test_columns_are_recognised_and_the_delimiter_sniffed(self):
        headers, rows = table()
        self.assertEqual(headers[:2], ["Case ID", "Task"])
        roles = importer.guess_roles(["Case ID", "Task", "Begin", "end_timestamp", "Amount", "org:resource"])
        self.assertEqual((roles["Case ID"], roles["Task"], roles["end_timestamp"], roles["Amount"]), ("case", "activity", "end", "attribute"))
        self.assertEqual(roles["org:resource"], "resource")

    def test_timestamp_formats_are_detected(self):
        self.assertEqual(importer.detect_format(["2026-01-03 09:00:00", "2026-01-03T09:00:00Z"]), "iso")
        self.assertEqual(importer.detect_format(["25/12/2026 09:00:00"]), "%d/%m/%Y %H:%M:%S")  # 25 cannot be a month
        self.assertEqual(importer.detect_format(["12/25/2026 09:00"]), "%m/%d/%Y %H:%M")
        self.assertEqual(importer.detect_format(["1767430800"]), "epoch")
        self.assertEqual(importer.parse_time("1767430800000", "epoch_ms"), datetime(2026, 1, 3, 9, tzinfo=UTC))
        self.assertIsNone(importer.parse_time("not a date", "iso"))

    def test_the_readiness_report_counts_what_is_wrong(self):
        headers, rows = table()
        mapping = importer.Mapping.parse("case=Case ID,activity=Task,start=Begin,end=Finish,resource=Who", headers)
        events, report = importer.prepare(rows, mapping)
        self.assertEqual((report["rows"], report["events"], report["cases"]), (6, 4, 2))
        self.assertEqual((report["missing_case_or_activity"], report["missing_timestamps"], report["start_after_end"], report["ties"]), (1, 1, 1, 1))
        self.assertEqual((report["format"], report["attributes"], report["existing"]), ("%d/%m/%Y %H:%M", ["Amount"], 0))
        self.assertEqual(events[0]["data"], {"Amount": "120"})
        self.assertEqual(events[1]["end"], events[1]["start"])  # start after end: an instant

    def test_a_mapping_needs_a_case_an_activity_and_a_timestamp(self):
        with self.assertRaisesRegex(importer.LogImportError, "no column is the activity"):
            importer.prepare([{"a": "1"}], importer.Mapping({"case": "a", "end": "a"}))
        with self.assertRaisesRegex(importer.LogImportError, "role=column"):
            importer.Mapping.parse("case=Nope", ["a"])

    def test_write_adds_events_under_the_process_in_one_transaction(self):
        headers, rows = table()
        events, _ = importer.prepare(rows, importer.Mapping.from_roles(importer.guess_roles(headers)))
        self.assertEqual(importer.write(events, "Claims"), 4)
        e = Event.objects.get(case_id="c1", activity="Approve")
        self.assertEqual((e.process, e.resource, e.data), ("Claims", "bob", {"attributes": {"Amount": "120"}}))
        with patch.object(Event.objects, "bulk_create", side_effect=RuntimeError("disk full")), self.assertRaises(RuntimeError):
            importer.write(events, "Other")
        self.assertFalse(Event.objects.filter(process="Other").exists())
        self.assertEqual(Log.load(importer_scope("Claims")).traces[0].case_id, "c1")


def importer_scope(process):
    from ..analytics import Scope
    return Scope(process)


class WizardTest(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user("staff", password="pw-123456", is_staff=True)
        self.client.force_login(self.staff)

    def upload(self, text=CSV, name="log.csv"):
        return self.client.post("/insights/import/", {"action": "upload", "file": SimpleUploadedFile(name, text.encode())})

    def form(self, **extra):
        return {"role_0": "case", "role_1": "activity", "role_2": "start", "role_3": "end", "role_4": "resource",
                "role_5": "attribute", "fmt": "auto", "process": "Claims", **extra}

    def test_only_staff_see_it(self):
        self.client.force_login(User.objects.create_user("person", password="pw-123456"))
        self.assertNotEqual(self.client.get("/insights/import/").status_code, 200)
        self.assertEqual(Client().get("/insights/import/").status_code, 302)

    def test_posts_need_the_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.staff)
        self.assertEqual(client.post("/insights/import/", {"action": "upload"}).status_code, 403)

    def test_upload_preview_check_then_import(self):
        page = self.upload().content.decode()
        self.assertIn("Case ID", page)
        self.assertIn("03/01/2026 09:00", page)
        self.assertIn('<option value="case" selected>', page)  # guessed
        checked = self.client.post("/insights/import/", {**self.form(), "action": "check"}).content.decode()
        self.assertIn("Readiness", checked)
        self.assertIn("1 row without a case id or an activity", checked)
        self.assertEqual(Event.objects.count(), 0)  # checking writes nothing
        done = self.client.post("/insights/import/", {**self.form(), "action": "import"})
        self.assertEqual(done.status_code, 302)
        self.assertIn("process=Claims", done["Location"])
        self.assertEqual(Event.objects.filter(process="Claims").count(), 4)
        self.assertTrue(AuditEntry.objects.filter(action="eventlog.import", object_id="Claims").exists())
        self.assertIn("Claims", self.client.get(done["Location"]).content.decode())  # and Insights reads it
        self.assertEqual(self.client.post("/insights/import/", {**self.form(), "action": "import"}).status_code, 302)  # the file is gone

    def test_a_missing_name_or_role_writes_nothing(self):
        self.upload()
        r = self.client.post("/insights/import/", {**self.form(process=""), "action": "import"})
        self.assertIn("Name the process", r.content.decode())
        r = self.client.post("/insights/import/", {**self.form(role_1="ignore"), "action": "import"})
        self.assertIn("no column is the activity", r.content.decode())
        self.assertEqual(Event.objects.count(), 0)

    def test_the_size_cap(self):
        with patch.object(importer, "MAX_BYTES", 10):
            self.assertIn("larger than", self.upload().content.decode())
        self.assertIn("no rows", self.upload("", "empty.csv").content.decode())

    def test_the_command_takes_the_same_mapping(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "claims.csv")
            with open(path, "w") as f:
                f.write(CSV)
            out = StringIO()
            call_command("import_eventlog", path, "--map", "case=Case ID,activity=Task,start=Begin,end=Finish", "--check", stdout=out)
            self.assertIn("events: 4", out.getvalue())
            self.assertEqual(Event.objects.count(), 0)
            call_command("import_eventlog", path, "--map", "case=Case ID,activity=Task,start=Begin,end=Finish,resource=Who",
                         "--process", "Claims", stdout=out)
            self.assertEqual(Event.objects.filter(process="Claims", resource="bob").count(), 1)
            call_command("import_eventlog", path, stdout=out)  # no --map: guessed, the process is the file's name
            with self.assertRaises(CommandError):
                call_command("import_eventlog", path, "--map", "case=Nope", stdout=out)
        self.assertTrue(Event.objects.filter(process="claims").exists())
