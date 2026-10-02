"""AgentCore Platform v1.0 - inner Smaregi workflow graph (Cat 2 domain workflow).

Instantiated by SmaregiWorkflowGraphNode.get_subgraph() in graph.py. Inherits
BaseGraph directly for a fully custom linear topology:

    START -> validate_input -> classify_intent -> infer_smaregi_fields
          -> call_smaregi_api -> confirm -> END

Config (forwarded from the outer graph via _parent_config(), under
config["configurable"]):
    smaregi  - runtime integration section (base_url, ...); injected into
               State as the JSON `smaregi_config` field via
               _extra_initial_state() so the no-arg nodes can read it
    agent    - runtime values from config/config.yaml (max_retry, timeout_s)

Nodes are registered WITHOUT constructor arguments (SDK v1 nodes are no-arg;
ctor args raise TypeError at graph build).
"""

from typing import Any

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.nodes.validate_input_node import ValidateInputNode
from src.nodes.classify_intent_node import ClassifyIntentNode
from src.nodes.infer_smaregi_fields_node import InferSmaregiFieldsNode
from src.nodes.call_smaregi_api_node import CallSmaregiApiNode
from src.nodes.confirm_node import ConfirmNode
from src.graph.context_bridge import get_caller_input_context
from src.schemas.state import State, to_json


class SmaregiWorkflowGraph(BaseGraph):
    """Inner graph: NL -> validate -> classify -> infer -> call -> confirm."""

    @property
    def name(self) -> str:
        return "smaregi_pos_transaction_workflow"

    @property
    def state_schema(self) -> type:
        return State

    def _validate_config(self) -> None:
        # No mandatory config: the smaregi section is optional (the client
        # falls back to the documented default base_url + the network-free
        # built-in transport), and a missing/unusable setting is handled at
        # CallSmaregiApiNode.execute() as a graceful status=error rather than
        # a compile-time crash.
        pass

    def register_nodes(self) -> None:
        # No super() - BaseGraph.register_nodes() is abstract. Do NOT register
        # initialize / finalize (outer backbone concern). All nodes are no-arg.
        self._nodes["validate_input"] = ValidateInputNode()
        self._nodes["classify_intent"] = ClassifyIntentNode()
        self._nodes["infer_smaregi_fields"] = InferSmaregiFieldsNode()
        self._nodes["call_smaregi_api"] = CallSmaregiApiNode()
        self._nodes["confirm"] = ConfirmNode()

    def add_edges(self) -> None:
        self._sg.add_edge(START, "validate_input")
        self._sg.add_edge("validate_input", "classify_intent")
        self._sg.add_edge("classify_intent", "infer_smaregi_fields")
        self._sg.add_edge("infer_smaregi_fields", "call_smaregi_api")
        self._sg.add_edge("call_smaregi_api", "confirm")
        self._sg.add_edge("confirm", END)

    def route(self, state: State) -> str:
        # Required by the base ABC. Linear topology -> never called unless an
        # add_conditional_edges() references it.
        #
        # The annotation is this graph's OWN State on purpose. The graph
        # runtime reads a path callable's annotation as that callable's input
        # schema and PROJECTS AWAY every field the annotation does not declare:
        # annotating a route with the generic base state hands it a state where
        # the domain routing fields are always absent, so the branch that
        # depends on them never runs - while unit tests that call route()
        # directly keep passing, because they pass a full dict themselves.
        return END if state.get("status") == AgentStatus.ERROR.value else "confirm"

    def _extra_initial_state(self) -> "dict[str, Any]":
        # Forward the `smaregi` section (arriving under config["configurable"]
        # from _parent_config()) into State as a JSON string (msgpack-safe) so
        # the no-arg CallSmaregiApiNode can read it via
        # state.get("smaregi_config").
        extra: dict[str, Any] = {}
        configurable = self.config.get("configurable") or {}
        smaregi = configurable.get("smaregi") or {}
        if smaregi:
            extra["smaregi_config"] = to_json(smaregi)
        # The framework does not forward input_context into a subgraph, so the
        # validated caller contract is read back off the bridge here - this
        # hook runs inside subgraph.invoke(), after the outer state is out of
        # reach.
        extra["input_context"] = get_caller_input_context()
        return extra

    def get_output(self, state: AgentState) -> "dict[str, Any]":
        return {
            "output": state.get("result") or state.get("confirmation"),
            "status": state.get("status"),
            "intent": state.get("intent", ""),
            "store_id": state.get("store_id", ""),
            "transaction_id": state.get("transaction_id", ""),
            "product_code": state.get("product_code", ""),
            "record_id": state.get("record_id", ""),
            "record_ref": state.get("record_ref", ""),
            "record_label": state.get("record_label", ""),
            "confirmation": state.get("confirmation", ""),
            "smaregi_payload": state.get("smaregi_payload", ""),
            "redaction_flags": state.get("redaction_flags", ""),
            "error_log": state.get("error_log", []),
            "trace_id": state.get("trace_id", ""),
            "correlation_id": state.get("correlation_id", ""),
            "node_history": state.get("node_history", []),
        }
