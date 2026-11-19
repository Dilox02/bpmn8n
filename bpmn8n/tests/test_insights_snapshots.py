"""Saved analyses: a name for the Insights page's state, reopened by URL."""
from urllib.parse import parse_qs, urlparse

from django.contrib.auth.models import User
from django.test import Client, TestCase

from ..models import Snapshot
from .test_insights_n4 import cases

FILTERS = '[{"type": "variants", "top": "1"}]'


class SnapshotTest(TestCase):
    def setUp(self):
        cases()
        self.user = User.objects.create_user("ana", password="pw-123456")
        self.client.force_login(self.user)

    def save(self, **extra):
        return self.client.post("/insights/snapshots/", {"name": "Top variant", "process": "P", "since": "2026-01-01",
                                                         "filters": FILTERS, "acts": "70", "paths": "30", "map": "performance",
                                                         "junk": "ignored", **extra})

    def test_a_snapshot_keeps_the_whole_state_and_reopens_by_url(self):
        response = self.save()
        snapshot = Snapshot.objects.get()
        self.assertEqual((snapshot.name, snapshot.author), ("Top variant", "ana"))
        self.assertEqual(snapshot.state, {"process": "P", "since": "2026-01-01", "filters": FILTERS, "acts": "70", "paths": "30",
                                          "map": "performance"})  # only the page's own state
        self.assertEqual(response["Location"], f"/insights/snapshots/{snapshot.pk}/")
        opened = self.client.get(response["Location"])
        query = parse_qs(urlparse(opened["Location"]).query)
        self.assertEqual((urlparse(opened["Location"]).path, query["filters"], query["acts"], query["map"]),
                         ("/insights/", [FILTERS], ["70"], ["performance"]))
        page = self.client.get(opened["Location"]).content.decode()
        self.assertIn("kept by 1 filter", page)

    def test_the_page_lists_them(self):
        self.save()
        self.save(name="Second")
        page = self.client.get("/insights/?process=P").content.decode()
        self.assertIn("Top variant", page)
        self.assertIn("Second", page)
        self.assertIn(f"/insights/snapshots/{Snapshot.objects.last().pk}/", page)

    def test_a_name_is_required_and_a_login(self):
        self.assertEqual(self.save(name="  ").status_code, 302)
        self.assertEqual(Snapshot.objects.count(), 0)
        self.assertEqual(self.client.get("/insights/snapshots/").status_code, 405)
        self.assertEqual(self.client.get("/insights/snapshots/999/").status_code, 404)
        anonymous = Client()
        self.assertIn("/login/", anonymous.post("/insights/snapshots/", {"name": "x"})["Location"])
        self.assertIn("/login/", anonymous.get("/insights/snapshots/1/")["Location"])

    def test_the_form_needs_the_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(client.post("/insights/snapshots/", {"name": "x"}).status_code, 403)
