# CMN-C2-285 - Unit tests: CallSmaregiApiNode (inner Step 4, tool call)
#
# Canon: invoked via node(state) (BaseNode.__call__ -> trust gate -> input
# gate -> execute -> output gate); inner domain node, so the state builder
# sets caller_trust_level = TrustLevel.ANONYMOUS.value.
# The ONE documented exception: the config-override call passes a 2nd (config)
# argument, which __call__ cannot forward - that single test stays a DIRECT
# execute(state, config=...) call (ANONYMOUS node, the trust gate is not the
# subject there).
#
# The node builds its client locally (SDK v1 nodes are no-arg), so error-path
# transports are exercised by monkeypatching the module's SmaregiClient symbol
# (our own module attribute - never a sys.modules stub of shared.*).

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets.inmemory_provider import InMemoryProvider

from src.nodes.call_smaregi_api_node import CallSmaregiApiNode
from src.services.smaregi_client import SmaregiApiError
from src.schemas.state import to_json


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.call_smaregi_api_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "smaregi_payload": to_json({"store_id": "s-01", "transaction_id": "t-1001"}),
        "intent": "lookup_transaction",
        "store_id": "s-01",
        "transaction_id": "t-1001",
        "product_code": "",
        "target_date": "",
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "call-smaregi-test",
        "session_id": "s1",
        "thread_id": "th1",
        "trace_id": "t1",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class _FakeErrorClient:
    """Stands in for SmaregiClient: lookup raises the documented API error."""

    def __init__(self, *args, **kwargs):
        pass

    uses_stub_transport = True

    def get_transaction(self, transaction_id, store_id, api_token):
        raise SmaregiApiError(403, "forbidden by integration permissions")


class _FakeLiveClient:
    """Stands in for SmaregiClient with a LIVE (non-stub) transport."""

    captured: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    uses_stub_transport = False

    def get_transaction(self, transaction_id, store_id, api_token):
        _FakeLiveClient.captured = {
            "transaction_id": transaction_id,
            "store_id": store_id,
            "api_token": api_token,
        }
        return {"transactionHeadId": transaction_id, "storeId": store_id, "total": 980}


class TestCallSmaregiApiNode:
    def setup_method(self):
        self.node = CallSmaregiApiNode()

    def test_lookup_success_via_default_transport(self):
        # Default transport = deterministic, network-free stub; no secret
        # provider bound -> the node runs on the documented placeholder.
        result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"] == "t-1001"
        assert result["record_ref"] == "smaregi://transactions/t-1001"
        assert "POS transaction #t-1001" in result["record_label"]
        assert "store #s-01" in result["record_label"]
        # A single receipt's total is a raw line item and is never rendered.
        assert "total" not in result["record_label"]

    def test_daily_sales_success_via_default_transport(self):
        state = _state(
            intent="summarize_daily_sales",
            transaction_id="",
            target_date="2026-07-01",
            smaregi_payload=to_json({"store_id": "s-01", "sum_date": "2026-07-01"}),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"] == "daily-s-01-2026-07-01"
        assert result["record_ref"] == "smaregi://stores/s-01/daily-sales/2026-07-01"
        assert "daily sales for store #s-01" in result["record_label"]

    def test_daily_sales_without_date_defaults_to_latest(self):
        state = _state(
            intent="summarize_daily_sales",
            transaction_id="",
            smaregi_payload=to_json({"store_id": "s-01", "sum_date": ""}),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"] == "daily-s-01-latest"

    def test_product_sales_success_via_default_transport(self):
        state = _state(
            intent="check_product_sales",
            transaction_id="",
            product_code="p-123",
            smaregi_payload=to_json({"store_id": "s-01", "product_code": "p-123", "sum_date": ""}),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"] == "p-123"
        assert result["record_ref"] == "smaregi://stores/s-01/product-sales/p-123"
        assert "product #p-123" in result["record_label"]
        assert "sales count" in result["record_label"]

    def test_product_sales_without_store_aggregates_all(self):
        state = _state(
            intent="check_product_sales",
            store_id="",
            transaction_id="",
            product_code="p-123",
            smaregi_payload=to_json({"store_id": "", "product_code": "p-123", "sum_date": ""}),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_ref"] == "smaregi://stores/all/product-sales/p-123"

    def test_smaregi_config_state_field_sets_base_url(self):
        # The inner graph injects the runtime `smaregi:` section as the JSON
        # smaregi_config state field; the default transport still serves the call.
        state = _state(smaregi_config=to_json({"base_url": "https://smaregi.example.test/pos"}))
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_ref"] == "smaregi://transactions/t-1001"

    def test_config_override_direct_execute_call(self):
        # Documented canon exception: execute(state, config=...) takes a 2nd
        # argument that __call__ cannot forward, so this ONE test calls execute
        # directly (ANONYMOUS node - the trust gate is not the subject here).
        config = {"configurable": {"smaregi": {"base_url": "https://smaregi.example.test/pos"}}}
        result = self.node.execute(_state(), config=config)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"] == "t-1001"

    def test_missing_payload_errors(self):
        result = self.node(_state(smaregi_payload=None))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_lookup_with_unresolved_transaction_errors(self):
        state = _state(transaction_id="", smaregi_payload=to_json({"store_id": "s-01", "transaction_id": ""}))
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unresolved transaction id" in entry for entry in result["error_log"])

    def test_daily_sales_with_unresolved_store_errors(self):
        state = _state(
            intent="summarize_daily_sales",
            store_id="",
            transaction_id="",
            smaregi_payload=to_json({"store_id": "", "sum_date": ""}),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unresolved store id" in entry for entry in result["error_log"])

    def test_product_sales_with_unresolved_code_errors(self):
        state = _state(
            intent="check_product_sales",
            transaction_id="",
            product_code="",
            smaregi_payload=to_json({"store_id": "s-01", "product_code": "", "sum_date": ""}),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unresolved product code" in entry for entry in result["error_log"])

    def test_unknown_intent_errors(self):
        result = self.node(_state(intent="void_transaction"))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unknown intent" in entry for entry in result["error_log"])

    def test_api_error_surfaces_status_error(self, monkeypatch):
        monkeypatch.setattr("src.nodes.call_smaregi_api_node.SmaregiClient", _FakeErrorClient)
        result = self.node(_state())
        assert result["status"] == AgentStatus.ERROR.value
        assert any("403" in entry for entry in result["error_log"])

    def test_live_transport_without_secret_refuses_call(self, monkeypatch):
        # With a LIVE transport a missing SMAREGI_TOKEN is a hard error -
        # a real API is never called unauthenticated.
        monkeypatch.setattr("src.nodes.call_smaregi_api_node.SmaregiClient", _FakeLiveClient)
        result = self.node(_state())
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unauthenticated" in entry for entry in result["error_log"])

    def test_live_transport_reads_token_from_ctx_secrets(self, monkeypatch):
        monkeypatch.setattr("src.nodes.call_smaregi_api_node.SmaregiClient", _FakeLiveClient)
        _FakeLiveClient.captured = {}
        with bound_secrets(InMemoryProvider({"SMAREGI_TOKEN": "mock-token-for-testing"})):
            result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert _FakeLiveClient.captured["api_token"] == "mock-token-for-testing"
        assert _FakeLiveClient.captured["transaction_id"] == "t-1001"

    def test_audit_emits_call_signals_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.call_smaregi_api_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state())
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - presence signals only.
        payload = payloads["call_smaregi_api_complete"]
        assert payload["intent"] == "lookup_transaction"
        assert payload["has_record_id"] is True
        assert payload["caller_supplied_record"] is False
        # No customer-attributable content in the audit payload.
        assert not any(isinstance(v, str) and "t-1001" in v for v in payload.values())
