"""The shared foundation: the element registry, the workflow graph and the queueing formulas."""
import math
import unittest

from projects.acme import processes

from ..simulation import queueing
from ..workflows import builder, elements, graph
from ..workflows.check import WorkflowCheck


class ElementsTest(unittest.TestCase):
    def test_self_check(self):
        elements.demo()

    def test_agrees_with_the_builder(self):
        self.assertEqual(elements.TASK_KINDS, builder.TASK_KINDS)
        self.assertEqual(elements.types(chooses=True) | elements.types(splits=True), set(builder.BRANCHING))
        self.assertEqual(elements.types(default_last=True), set(builder.DEFAULT_LAST))
        self.assertEqual(elements.types(timeout_first=True), {builder.TIMEOUT_FIRST})

    def test_every_glyph_exists(self):
        from pathlib import Path
        icons = Path(__file__).resolve().parents[2] / "n8n-nodes-bpmn8n" / "nodes" / "icons" / "bpmn"
        for e in elements.ELEMENTS.values():
            if e.symbol:
                self.assertTrue((icons / f"{e.symbol}.svg").exists(), e.symbol)

    def test_manifest_is_json(self):
        import json
        manifest = json.loads(json.dumps(elements.manifest()))
        self.assertIn("userTask", {e["kind"] for e in manifest["elements"]})


class GraphTest(unittest.TestCase):
    def test_self_check(self):
        graph.demo()

    def test_agrees_with_the_workflow_check(self):
        """Same successors, predecessors and dominators as WorkflowCheck on every ACME process."""
        for name in ("ticket_triage", "expense_approval", "incident_response", "library_lending"):
            workflow = getattr(processes, name)().as_json()
            g, check = graph.ProcessGraph(workflow), WorkflowCheck(workflow, fetch=lambda _: None)
            with self.subTest(name):
                self.assertEqual(g.outs, check.outs)
                self.assertEqual(g.into, check.into)
                self.assertEqual(g.dominators, check.dominators)
                self.assertEqual(g.subnodes, check.subnodes)
                self.assertTrue(set(g.order) >= set(g.flow_nodes))


class QueueingTest(unittest.TestCase):
    def test_self_check(self):
        queueing.demo()

    def test_ggc_is_mmc_for_exponential_times(self):
        for c in (1, 2, 5):
            self.assertAlmostEqual(queueing.ggc(3, 1.5, c + 4).waiting_time, queueing.mmc(3, 1.5, c + 4).waiting_time)

    def test_more_servers_never_wait_longer(self):
        waits = [queueing.mmc(9, 1, c).waiting_time for c in range(10, 20)]
        self.assertEqual(waits, sorted(waits, reverse=True))

    def test_unstable_is_reported_not_raised(self):
        s = queueing.mmc(5, 1, 4)
        self.assertFalse(s.stable)
        self.assertIsNone(s.as_dict()["waiting_time"])

    def test_littles_law_holds(self):
        for s in (queueing.mmc(2, 0.4, 1), queueing.mmc(7, 1, 9), queueing.mm1k(1.2, 1, 5), queueing.mg1(0.8, 1, 3)):
            self.assertAlmostEqual(s.in_system, s.throughput * s.response_time, places=9)

    def test_loop_with_probability_one_never_leaves(self):
        with self.assertRaises(ValueError):
            queueing.traffic([1], [[1.0]])

    def test_delay_station_in_a_network(self):
        net = queueing.network(["work", "wait"], [1, 0], [[0, 1], [0, 0]], [0.5, 10], [1, math.inf])
        self.assertEqual(net.stations["wait"].waiting_time, 0)
        self.assertEqual(net.bottleneck, "work")
