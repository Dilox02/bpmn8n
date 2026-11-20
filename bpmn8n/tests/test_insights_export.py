"""The log export (CSV, XES) and its anonymisation."""
from django.test import TestCase, override_settings

from ..analytics import importer
from ..models import AuditEntry
from .test_insights_n4 import table

LOG = """case;task;ts;who;customer;amount;placed
order-17;Register;2026-01-03 09:00:00;Anna Verdi;Maria Rossi;120.50;2026-01-02
order-17;Approve;2026-01-03 09:30:00;Bob Neri;Maria Rossi;120.50;2026-01-02
order-18;Register;2026-01-04 10:00:00;Anna Verdi;Luca Bianchi;80;2026-01-03
"""
SECRETS = ("order-17", "order-18", "Anna", "Verdi", "Bob", "Neri", "Maria", "Rossi", "Luca", "Bianchi")


@override_settings(API_TOKEN="secret")
class ExportTest(TestCase):
    def setUp(self):
        headers, rows = table(LOG)
        events, _ = importer.prepare(rows, importer.Mapping.parse("case=case,activity=task,end=ts,resource=who", headers))
        importer.write(events, "Orders")

    def get(self, **query):
        return self.client.get("/api/analytics/export", {"process": "Orders", **query}, HTTP_AUTHORIZATION="Bearer secret")

    def test_needs_the_token(self):
        self.assertEqual(self.client.get("/api/analytics/export").status_code, 401)

    def test_csv_carries_the_log_and_its_attributes_and_is_audited(self):
        r = self.get()
        self.assertEqual(r["Content-Type"], "text/csv")
        lines = r.content.decode().splitlines()
        self.assertEqual(lines[0], "case_id,process,activity,start_timestamp,end_timestamp,status,resource,amount,customer,placed")
        self.assertEqual(len(lines), 4)
        self.assertIn("order-17,Orders,Register,", lines[1])
        self.assertIn("Anna Verdi,120.50,Maria Rossi,2026-01-02", lines[1])
        entry = AuditEntry.objects.get(action="eventlog.export")
        self.assertEqual((entry.actor, entry.details["format"], entry.details["anonymised"], entry.details["events"]),
                         ("api", "csv", False, 3))

    def test_the_filters_narrow_the_export(self):
        only = '[{"type": "attribute", "key": "resource", "values": ["Bob Neri"], "mode": "include"}]'
        self.assertEqual(len(self.get(filters=only).content.decode().splitlines()), 3)  # order-17's two events
        self.assertEqual(self.get(format="pdf").status_code, 422)

    def test_anonymised_output_holds_no_original_identifier_and_keeps_numbers_and_times(self):
        text = self.get(anonymise=1).content.decode()
        for secret in SECRETS:
            self.assertNotIn(secret, text)
        lines = text.splitlines()[1:]
        self.assertIn("120.50", lines[0])
        self.assertIn("2026-01-02", lines[0])  # a time is not an identifier
        self.assertIn(",Register,", lines[0])  # activities stay
        resources = [line.split(",")[6] for line in lines]
        self.assertEqual(resources[0], resources[2])  # Anna Verdi, twice: one pseudonym
        self.assertNotEqual(resources[0], resources[1])
        self.assertEqual(lines[0].split(",")[0], lines[1].split(",")[0])  # the same case
        self.assertNotEqual(lines[0].split(",")[0], lines[2].split(",")[0])

    def test_pseudonyms_are_stable_and_keyed_with_the_secret(self):
        before = self.get(anonymise=1).content
        self.assertEqual(self.get(anonymise=1).content, before)
        with override_settings(SECRET_KEY="another-secret-key-for-this-test-0123456789"):
            self.assertNotEqual(self.get(anonymise=1).content, before)

    def test_xes_through_pm4py_anonymised_too(self):
        r = self.get(format="xes", anonymise=1)
        raw = r.content.decode()
        self.assertTrue(raw.lstrip().startswith("<?xml"))
        for secret in SECRETS:
            self.assertNotIn(secret, raw)
        headers, rows = importer.read_table(r.content, "orders.xes")  # and the wizard reads it back
        self.assertEqual(len(rows), 3)
        self.assertIn("case:concept:name", headers)
        plain = self.get(format="xes").content.decode()
        self.assertIn("order-17", plain)
        self.assertIn("Anna Verdi", plain)
