"""Regression: an ERROR envelope must not disclose Smaregi POS record evidence.

Molt source review, 2026-09-04 (wave-9 batch, family-level finding over sixteen
sibling repos): "existing-ERROR rebuilds truthy `formatted_output` with record
evidence; and caller-visible `error_log` can carry interpolated exception /
upstream text rather than closed-set labels."

The `errored` branch of PostProcessNode rebuilt `formatted_output` from
`record_id` / `record_ref` read straight back out of state, and returned ONLY
that plus `status` - so every other output-bearing field (`result`,
`record_label`, `confirmation`, `smaregi_payload`, `intent`, and the resolved
POS identifiers) survived in state untouched.

Those identifiers ARE the lookup evidence: this node's own gate
(`_security_gate_output`, is_success=True) REFUSES a SUCCESS that lacks them. An
error envelope carrying them tells a caller who is being informed of a FAILURE
that a POS record was nonetheless resolved, and which receipt / store / product
it was. `AgentBaseGraph.get_output()` projects `formatted_output or result` with
NO status check, so anything left in `result` ships in the error response too.

The containment contract is not "shape a nicer error mapping". It is: no
un-gated caller-facing content or record evidence survives on any error path -
neither in the replacement `formatted_output`, nor in the diagnostics it
carries, nor in the state left behind for a checkpoint or a downstream reader.

Reachability: on the compiled graph this branch is defence-in-depth.
`AgentBaseGraph.route()` sends an ERROR status to `finalize`, bypassing
`post_process`, and `BaseNode.__call__` short-circuits an already-errored state
before `execute()` runs. It is reachable by a direct `execute()` call, which is
how the tests below drive it - and how any future re-wiring would reach it.
"""

import pytest

from framework.schemas.agent_status import AgentStatus

from src.nodes.call_smaregi_api_node import CallSmaregiApiNode
from src.nodes.post_process_node import PostProcessNode
from src.schemas.state import to_json

# Record evidence + the POS content hanging off it.
_RECORD_ID = "t-1001"
_RECORD_REF = "smaregi://transactions/t-1001"
_STORE_ID = "s-01"
_LABEL = "POS transaction #t-1001 at store #s-01"
_PRODUCT_CODE = "p-77"


def _errored_state() -> dict:
    return {
        "status": AgentStatus.ERROR.value,
        "error_log": ["CallSmaregiApiNode: Smaregi API error 500"],
        "record_id": _RECORD_ID,
        "record_ref": _RECORD_REF,
        "record_label": _LABEL,
        "store_id": _STORE_ID,
        "transaction_id": _RECORD_ID,
        "product_code": _PRODUCT_CODE,
        "intent": "lookup_transaction",
        "confirmation": f"Retrieved POS transaction '{_LABEL}' - ref={_RECORD_REF}",
        "smaregi_payload": to_json(
            {"store_id": _STORE_ID, "transaction_id": _RECORD_ID, "product_code": _PRODUCT_CODE}
        ),
        "result": {"record_id": _RECORD_ID, "confirmation": "done"},
    }


def _flatten(value) -> str:
    """Render every reachable string in a returned value - nesting is not cover."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_flatten(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return " ".join(_flatten(v) for v in value)
    return str(value)


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.post_process_node.emit_trace_event", lambda *a, **k: None)


class TestErrorEnvelopeContainment:
    def test_error_envelope_carries_no_record_evidence(self):
        out = PostProcessNode().execute(_errored_state())

        assert out["status"] == AgentStatus.ERROR.value

        # formatted_output must be PRESENT and TRUTHY. The framework projects
        # `formatted_output or result` with no status check, so a falsy value
        # re-opens the fallback onto whatever survived in state.
        assert "formatted_output" in out
        assert out["formatted_output"], "falsy formatted_output re-opens the `or result` fallback"

        shipped = _flatten(out["formatted_output"])
        assert _RECORD_ID not in shipped, "record id shipped in the error envelope"
        assert _RECORD_REF not in shipped, "record ref shipped in the error envelope"
        assert "smaregi://" not in shipped
        assert _STORE_ID not in shipped
        assert _PRODUCT_CODE not in shipped
        assert _LABEL not in shipped
        assert "Retrieved POS transaction" not in shipped

    def test_error_path_clears_output_bearing_state(self):
        """Omitting a field from one envelope is not clearing it from state."""
        out = PostProcessNode().execute(_errored_state())
        for field in (
            "result",
            "confirmation",
            "record_label",
            "smaregi_payload",
            "record_id",
            "record_ref",
            "intent",
            "store_id",
            "transaction_id",
            "product_code",
        ):
            assert field in out, f"{field} not cleared on the error path"
            assert not out[field], f"{field} still carries content on the error path"

    def test_success_path_still_returns_the_answer(self):
        """Control: containment must not empty out the clean path."""
        out = PostProcessNode().execute(
            {
                "status": AgentStatus.SUCCESS.value,
                "record_id": _RECORD_ID,
                "record_ref": _RECORD_REF,
                "record_label": _LABEL,
                "intent": "lookup_transaction",
                "confirmation": f"Retrieved POS transaction - ref={_RECORD_REF}",
                "smaregi_payload": to_json({"store_id": _STORE_ID}),
            }
        )
        assert out["status"] == AgentStatus.SUCCESS.value
        shipped = _flatten(out["formatted_output"])
        assert _RECORD_ID in shipped, "the success path must still return the record evidence"
        assert _RECORD_REF in shipped


class TestErrorLogNamesNoUpstreamText:
    """The diagnostics an API failure writes are a channel of their own.

    `error_log` is the internal channel - post_process never projects it into
    the caller's envelope - but it is the audit trail, so an upstream message
    that interpolates the record or the tenant's error body would store that
    text server-side for every failed run. Reasons must be closed-set labels.
    """

    def _state(self, **overrides) -> dict:
        state = {
            "smaregi_payload": to_json({"store_id": _STORE_ID, "transaction_id": _RECORD_ID}),
            "intent": "lookup_transaction",
            "transaction_id": _RECORD_ID,
            "store_id": _STORE_ID,
            "correlation_id": "pb-error-envelope",
            "session_id": "pb-s1",
            "thread_id": "pb-th1",
            "trace_id": "pb-t1",
            "node_history": [],
            "error_log": [],
            "execution_time": {},
        }
        state.update(overrides)
        return state

    def test_upstream_api_failure_reason_carries_no_upstream_body(self, monkeypatch):
        """A live tenant's error body is unbounded third-party text; only the
        HTTP status - a closed-set signal - belongs in a caller-facing reason."""
        monkeypatch.setattr("src.nodes.call_smaregi_api_node.emit_trace_event", lambda *a, **k: None)
        from src.services.smaregi_client import SmaregiApiError

        class _ApiErrorClient:
            uses_stub_transport = True

            def __init__(self, *args, **kwargs):
                pass

            def get_transaction(self, transaction_id, store_id, api_token):
                raise SmaregiApiError(403, f"denied for receipt {_RECORD_ID} at store {_STORE_ID}")

        monkeypatch.setattr("src.nodes.call_smaregi_api_node.SmaregiClient", _ApiErrorClient)
        out = CallSmaregiApiNode().execute(self._state())
        assert out["status"] == AgentStatus.ERROR.value
        joined = " ".join(out["error_log"])
        assert "403" in joined, "the HTTP status is the actionable signal - keep it"
        assert "denied for receipt" not in joined, "upstream error body reached the caller-facing reason"
        assert _RECORD_ID not in joined
        assert _STORE_ID not in joined

    def test_transport_failure_reason_carries_only_the_exception_type(self, monkeypatch):
        """A transport error string can carry the request URL and the record id."""
        monkeypatch.setattr("src.nodes.call_smaregi_api_node.emit_trace_event", lambda *a, **k: None)

        class _TransportErrorClient:
            uses_stub_transport = True

            def __init__(self, *args, **kwargs):
                pass

            def get_transaction(self, transaction_id, store_id, api_token):
                raise ConnectionError(
                    f"GET https://api.smaregi.jp/pos/transactions/{_RECORD_ID}?store={_STORE_ID} failed"
                )

        monkeypatch.setattr("src.nodes.call_smaregi_api_node.SmaregiClient", _TransportErrorClient)
        out = CallSmaregiApiNode().execute(self._state())
        assert out["status"] == AgentStatus.ERROR.value
        joined = " ".join(out["error_log"])
        assert "ConnectionError" in joined, "the exception TYPE is the actionable signal - keep it"
        assert "api.smaregi.jp" not in joined, "the request URL reached the caller-facing reason"
        assert _RECORD_ID not in joined
        assert _STORE_ID not in joined
