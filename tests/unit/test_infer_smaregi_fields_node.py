# CMN-C2-285 - Unit tests: InferSmaregiFieldsNode (inner Step 3)
# Adapted from the peer template tool-calling golden (Kaonavi -> Smaregi).
#
# Canon: invoked via node(state) (BaseNode.__call__ -> trust gate -> input
# gate -> execute -> output gate); inner domain node, so the state builder
# sets caller_trust_level = TrustLevel.ANONYMOUS.value.
# Positive payloads are PII-free: the framework PII mask rewrites Title-Case
# bigrams / '@' / digit runs in validated_input, so ids stay short (t-1001 /
# s-01 / p-123) and dates ISO.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.infer_smaregi_fields_node import InferSmaregiFieldsNode
from src.schemas.state import from_json


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.infer_smaregi_fields_node.emit_trace_event", lambda *a, **k: None)


def _state(text: str, intent: str = "lookup_transaction", store_hint: str = "", **overrides) -> dict:
    state = {
        "validated_input": text,
        "intent": intent,
        "store_hint": store_hint,
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "infer-fields-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestInferSmaregiFieldsNode:
    def setup_method(self):
        self.node = InferSmaregiFieldsNode()

    def test_lookup_extracts_ids_from_text(self):
        result = self.node(_state("Look up transaction t-1001 in store s-01 and check the receipt on file."))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["transaction_id"] == "t-1001"
        assert result["store_id"] == "s-01"
        # smaregi_payload is stored as a JSON string, not a native dict.
        assert isinstance(result["smaregi_payload"], str)
        assert from_json(result["smaregi_payload"], {}) == {"store_id": "s-01", "transaction_id": "t-1001"}

    def test_id_shaped_hint_used_when_text_has_no_store(self):
        result = self.node(_state("Look up transaction t-1001 and check the receipt", store_hint="s-02"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["store_id"] == "s-02"
        assert from_json(result["smaregi_payload"], {}) == {"store_id": "s-02", "transaction_id": "t-1001"}

    def test_daily_sales_builds_sum_date_payload(self):
        result = self.node(_state("Summarize daily sales for store s-01 on 2026-07-01", intent="summarize_daily_sales"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["store_id"] == "s-01"
        assert result["target_date"] == "2026-07-01"
        assert from_json(result["smaregi_payload"], {}) == {"store_id": "s-01", "sum_date": "2026-07-01"}

    def test_daily_sales_without_date_leaves_sum_date_empty(self):
        # No explicit ISO date -> left empty (executor treats as latest business
        # day); deterministic v1 does no relative-date parsing.
        result = self.node(_state("Summarize daily sales for store s-01", intent="summarize_daily_sales"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert from_json(result["smaregi_payload"], {}) == {"store_id": "s-01", "sum_date": ""}

    def test_product_sales_builds_product_payload(self):
        result = self.node(
            _state(
                "How many units of product p-123 were sold at store s-01 on 2026-07-01?",
                intent="check_product_sales",
            )
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["product_code"] == "p-123"
        assert from_json(result["smaregi_payload"], {}) == {
            "store_id": "s-01",
            "product_code": "p-123",
            "sum_date": "2026-07-01",
        }

    def test_key_value_request_lines_resolve_ids(self):
        # "Key: value" lines (no inline "transaction t-1001" phrasing) resolve
        # via the line-structure parse - mirrors the peer template field parse.
        text = "Check the receipt on file\nStore: s-01\nTransaction: t-1001"
        result = self.node(_state(text))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["store_id"] == "s-01"
        assert result["transaction_id"] == "t-1001"

    def test_unresolved_ids_left_empty_never_invented(self):
        # "store transactions" must NOT false-match as ids (id candidates must
        # contain a digit); unresolved ids stay "" - never invented.
        result = self.node(_state("Check the flagged store transactions on file"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["store_id"] == ""
        assert result["transaction_id"] == ""
        assert from_json(result["smaregi_payload"], {}) == {"store_id": "", "transaction_id": ""}

    def test_non_id_shaped_hint_left_unresolved(self):
        result = self.node(_state("Check the receipt on file", store_hint="not a valid id!"))
        assert result["store_id"] == ""

    def test_missing_input_errors(self):
        result = self.node(_state(""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_audit_emits_field_signals_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.infer_smaregi_fields_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state("Look up transaction t-1001 in store s-01."))
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - presence signals, no content.
        payload = payloads["infer_smaregi_fields_complete"]
        assert payload["intent"] == "lookup_transaction"
        assert payload["has_store_id"] is True
        assert payload["has_transaction_id"] is True
        assert "store_id" not in payload
