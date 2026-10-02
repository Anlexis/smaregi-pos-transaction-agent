# CMN-C2-285 - Unit tests: ClassifyIntentNode (inner Step 2)
# Adapted from the peer template tool-calling golden (Kaonavi -> Smaregi).
# Intents: lookup_transaction / summarize_daily_sales / check_product_sales
# (deterministic keyword heuristic, v1 - no LLM; unknown falls back to the
# read-only single-record lookup).
#
# Canon: invoked via node(state) (BaseNode.__call__ -> trust gate -> input
# gate -> execute -> output gate); inner domain node, so the state builder
# sets caller_trust_level = TrustLevel.ANONYMOUS.value.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.classify_intent_node import ClassifyIntentNode


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.classify_intent_node.emit_trace_event", lambda *a, **k: None)


def _state(text: str) -> dict:
    return {
        "validated_input": text,
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "classify-intent-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }


class TestClassifyIntentNode:
    def setup_method(self):
        self.node = ClassifyIntentNode()

    def test_keyword_lookup_transaction(self):
        result = self.node(_state("Look up transaction t-1001 in store s-01."))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "lookup_transaction"

    def test_keyword_summarize_daily_sales(self):
        result = self.node(_state("Summarize daily sales for store s-01 on 2026-07-01"))
        assert result["intent"] == "summarize_daily_sales"

    def test_keyword_check_product_sales(self):
        result = self.node(_state("How many units of product p-123 were sold at store s-01?"))
        assert result["intent"] == "check_product_sales"

    def test_ja_keyword_lookup_transaction(self):
        result = self.node(_state("取引 t-1001 を照会してください"))
        assert result["intent"] == "lookup_transaction"

    def test_product_wins_over_daily_and_lookup(self):
        # Priority order is most-specific-first (product > daily > lookup): a
        # product-count request that also says "sales"/"show" classifies as the
        # product check, never the broader summary or lookup.
        result = self.node(_state("Check the sales count for product p-123 and show the daily summary"))
        assert result["intent"] == "check_product_sales"

    def test_daily_wins_over_lookup(self):
        result = self.node(_state("Show the sales summary for store s-01"))
        assert result["intent"] == "summarize_daily_sales"

    def test_no_signal_defaults_to_readonly_lookup(self):
        result = self.node(_state("please handle this for the team"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "lookup_transaction"
        # Non-fatal low-confidence note travels in error_log; status stays SUCCESS.
        assert any("defaulted to lookup_transaction" in entry for entry in result.get("error_log", []))

    def test_empty_input_errors(self):
        result = self.node(_state(""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_audit_emits_intent_label_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.classify_intent_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state("Look up transaction t-1001 in store s-01."))
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - the label, never the text.
        assert payloads["classify_intent_complete"]["intent"] == "lookup_transaction"
        assert payloads["classify_intent_complete"]["defaulted"] is False
