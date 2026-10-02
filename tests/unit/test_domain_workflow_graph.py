# CMN-C2-285 - Unit tests: inner SmaregiWorkflowGraph (BaseGraph) contract.
# The compiled outer path is exercised end to end by the boundary suite; this
# module unit-checks the inner graph's identity, config forwarding, routing,
# output contract, and a direct inner invoke on the network-free transport.

from langgraph.graph import END

from framework.schemas.agent_status import AgentStatus

from src.graph.domain_workflow_graph import SmaregiWorkflowGraph
from src.schemas.state import State, from_json


def _graph(config=None):
    return SmaregiWorkflowGraph(config=config or {})


def test_inner_graph_identity():
    g = _graph()
    assert g.name == "smaregi_pos_transaction_workflow"
    assert g.state_schema is State


def test_extra_initial_state_injects_smaregi_config_as_json():
    g = _graph({"configurable": {"smaregi": {"base_url": "https://smaregi.example.test/pos"}}})
    extra = g._extra_initial_state()
    # Forwarded as a JSON string, not a native dict (msgpack-safe state).
    assert isinstance(extra["smaregi_config"], str)
    assert from_json(extra["smaregi_config"], {}) == {"base_url": "https://smaregi.example.test/pos"}


def test_extra_initial_state_without_smaregi_section_carries_only_the_bridge():
    """No integration section -> no smaregi_config, but the caller-context
    bridge key is always seeded (the framework does not forward input_context
    into a subgraph, so the inner graph must seed it itself)."""
    from src.graph.context_bridge import set_caller_input_context

    set_caller_input_context(None)
    extra = _graph()._extra_initial_state()
    assert "smaregi_config" not in extra
    assert extra["input_context"] == {}


def test_route_error_ends_graph():
    g = _graph()
    assert g.route({"status": AgentStatus.ERROR.value}) == END
    assert g.route({"status": AgentStatus.SUCCESS.value}) == "confirm"


def test_get_output_surfaces_record_fields():
    g = _graph()
    out = g.get_output(
        {
            "result": {"record_id": "t-1001", "record_ref": "smaregi://transactions/t-1001", "confirmation": "ok"},
            "status": AgentStatus.SUCCESS.value,
            "intent": "lookup_transaction",
            "store_id": "s-01",
            "transaction_id": "t-1001",
            "product_code": "",
            "record_id": "t-1001",
            "record_ref": "smaregi://transactions/t-1001",
            "record_label": "POS transaction t-1001 at store s-01",
            "confirmation": "ok",
            "smaregi_payload": "{}",
            "redaction_flags": "[]",
            "error_log": [],
            "trace_id": "tr",
            "correlation_id": "co",
            "node_history": ["ValidateInputNode", "ConfirmNode"],
        }
    )
    assert out["status"] == AgentStatus.SUCCESS.value
    assert out["intent"] == "lookup_transaction"
    assert out["store_id"] == "s-01"
    assert out["transaction_id"] == "t-1001"
    assert out["record_ref"] == "smaregi://transactions/t-1001"
    assert out["confirmation"] == "ok"
    assert out["output"] == {"record_id": "t-1001", "record_ref": "smaregi://transactions/t-1001", "confirmation": "ok"}


def test_get_output_carries_error_log():
    g = _graph()
    out = g.get_output({"status": AgentStatus.ERROR.value, "error_log": ["boom"], "confirmation": ""})
    assert out["status"] == AgentStatus.ERROR.value
    assert out["error_log"] == ["boom"]


def test_inner_graph_compiles():
    g = _graph()
    g.compile()
    assert g._compiled is not None


def test_inner_invoke_lookup_on_v1_stub():
    """Direct inner invoke (default ANONYMOUS ctx - every inner node is
    ANONYMOUS): validate -> classify -> infer -> call -> confirm."""
    g = _graph({"configurable": {"smaregi": {"base_url": "https://api.smaregi.jp/pos"}}})
    g.compile()
    result = g.invoke(user_input="Look up transaction t-1001 in store s-01 and check the receipt on file.")
    assert result["status"] == AgentStatus.SUCCESS.value
    assert result["record_id"] == "t-1001"
    assert result["record_ref"] == "smaregi://transactions/t-1001"
    assert result["intent"] == "lookup_transaction"
    assert result["confirmation"]
    history = result.get("node_history", [])
    assert history == [
        "ValidateInputNode",
        "ClassifyIntentNode",
        "InferSmaregiFieldsNode",
        "CallSmaregiApiNode",
        "ConfirmNode",
    ]
