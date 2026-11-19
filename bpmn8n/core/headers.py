"""The X-Bpmn8n-* headers between bpmn8n and its n8n nodes: one list, read by core/observability.Trace and the
contract check (api/contract.py), and generated into n8n-nodes-bpmn8n/nodes/shared/contract.ts. The nodes
URI-encode the names they send (n8n names need not be ASCII)."""

HEADERS = {
    "case": "X-Bpmn8n-Case",  # "<workflow id>:<execution id>" = the process instance (case)
    "process": "X-Bpmn8n-Process",  # n8n workflow name = the process the case belongs to
    "activity": "X-Bpmn8n-Activity",  # n8n node name = the activity label
    "node": "X-Bpmn8n-Node",  # n8n node id: with run and item, a run's identity (idempotent task runs)
    "run": "X-Bpmn8n-Run",  # the node's run index
    "item": "X-Bpmn8n-Item",  # the item's index
    "profile": "X-Bpmn8n-Profile",  # "1" runs the call under Profiler
    "contract": "X-Bpmn8n-Contract",  # the contract version the nodes were built for
    "warning": "X-Bpmn8n-Warning",  # the server's answer to another contract version: the node shows it
}
