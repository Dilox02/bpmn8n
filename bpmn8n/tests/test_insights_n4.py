"""Insights N4: the process map's level of detail, replay, the import wizard, the anonymised export, snapshots."""
from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from ..analytics import Log, dfg
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
        self.assertEqual(len(self.get().json()["cases"]), 8)
        self.assertEqual(self.get(workflow_id="other").json()["cases"], [])
        self.assertEqual(self.get(limit=0).status_code, 422)
