# CMN-C2-285 - Unit tests: ConfirmNode (inner Step 5)
#
# Canon: invoked via node(state) (BaseNode.__call__ -> trust gate -> input
# gate -> execute -> output gate); inner domain node, so the state builder
# sets caller_trust_level = TrustLevel.ANONYMOUS.value.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.confirm_node import ConfirmNode


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.confirm_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "record_id": "t-1001",
        "record_ref": "smaregi://transactions/t-1001",
        "record_label": "POS transaction #t-1001 at store #s-01",
        "intent": "lookup_transaction",
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "confirm-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestConfirmNode:
    def setup_method(self):
        self.node = ConfirmNode()

    def test_lookup_confirmation(self):
        result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "Retrieved POS transaction" in result["confirmation"]
        assert "POS transaction #t-1001 at store #s-01" in result["confirmation"]
        assert "ref=smaregi://transactions/t-1001" in result["confirmation"]
        # No bare identifier is ever rendered: at the output boundary a
        # standalone digit run is indistinguishable from an unmarked amount.
        assert "id=t-1001" not in result["confirmation"]
        assert result["result"]["record_id"] == "t-1001"
        assert result["result"]["record_ref"] == "smaregi://transactions/t-1001"

    def test_daily_sales_verb(self):
        result = self.node(
            _state(
                intent="summarize_daily_sales",
                record_id="daily-s-01-2026-07-01",
                record_ref="smaregi://stores/s-01/daily-sales/2026-07-01",
                record_label="daily sales for store #s-01 on 2026-07-01",
            )
        )
        assert "Summarized daily sales" in result["confirmation"]

    def test_product_sales_verb(self):
        result = self.node(
            _state(
                intent="check_product_sales",
                record_id="p-123",
                record_ref="smaregi://stores/s-01/product-sales/p-123",
                record_label="product #p-123 at store #s-01 on latest (sales count 12)",
            )
        )
        assert "Checked product sales count" in result["confirmation"]

    def test_unknown_intent_uses_generic_verb(self):
        result = self.node(_state(intent="mystery"))
        assert "Processed POS request" in result["confirmation"]

    def test_label_only_no_ref(self):
        result = self.node(_state(record_ref=""))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "POS transaction #t-1001" in result["confirmation"]
        assert "ref=" not in result["confirmation"]

    def test_falls_back_to_record_id_when_label_missing(self):
        """Even the fallback renders the id behind a `#`, never bare."""
        result = self.node(_state(record_label=""))
        assert "'#t-1001'" in result["confirmation"]

    def test_missing_record_evidence_errors(self):
        result = self.node(_state(record_id="", record_ref=""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]
