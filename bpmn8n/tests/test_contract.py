"""B7: one contract with the n8n nodes, generated into TypeScript from the server's own definitions."""
from django.test import RequestFactory, TestCase

from ..api import api
from ..api.contract import API_VERSION, TS_FILE, typescript, version
from ..core.headers import HEADERS
from ..core.observability import Trace


class ContractTest(TestCase):
    def test_contract_ts_is_what_the_server_defines(self):
        self.assertEqual(TS_FILE.read_text(), typescript(),
                         "stale: run `python manage.py contract --ts`, then `npm test` in n8n-nodes-bpmn8n")

    def test_the_shapes_become_interfaces(self):
        ts = typescript()
        self.assertIn(f"export const CONTRACT_VERSION = '{version()}';", ts)
        self.assertIn("\tcontract: 'X-Bpmn8n-Contract',", ts)
        self.assertIn("export interface RunIn {\n\tfields?: { [key: string]: unknown };", ts)
        self.assertIn("\tkind: 'exclusive' | 'inclusive';", ts)
        self.assertIn("\tform?: Array<FormField>;", ts)
        self.assertIn("\tcompletion?: 'all' | 'any' | number;", ts)
        self.assertIn("\texpires?: string | null;", ts)
        self.assertEqual(api.version, API_VERSION)
        self.assertTrue(version().startswith(f"{API_VERSION}."))

    def test_nodes_built_for_another_contract_get_a_warning_not_an_error(self):
        other = self.client.get("/api/health", headers={HEADERS["contract"]: "0.deadbeef"})
        self.assertEqual(other.status_code, 200)
        self.assertIn(f"contract 0.deadbeef, this server {version()}", other[HEADERS["warning"]])
        same = self.client.get("/api/health", headers={HEADERS["contract"]: version()})
        self.assertNotIn(HEADERS["warning"], same)
        self.assertNotIn(HEADERS["warning"], self.client.get("/api/health"))

    def test_trace_reads_the_header_list(self):
        names = {key: HEADERS[key] for key in ("case", "activity", "process", "node", "run", "item")}
        request = RequestFactory().post("/", headers={names["case"]: "w1%3A7", names["activity"]: "Book%20Slot",
                                                       names["process"]: "Booking", names["node"]: "n1",
                                                       names["run"]: "2", names["item"]: "0"})
        self.assertEqual(Trace.of(request), Trace("w1:7", "Book Slot", "Booking", "n1", 2, 0))
