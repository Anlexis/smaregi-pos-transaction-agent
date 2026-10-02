# PB (containment): what the caller-facing envelope carries on a non-success
# path - projected through get_output(), and driven through the real ASGI
# /invoke the deployment exposes.
#
# The envelope resolves its payload as `formatted_output or result` and never
# looks at the status; by the time post_process runs, `result` already holds
# the inner workflow's answer. So an error return that merely sets the status,
# or that replaces formatted_output with something falsy, ships the answer it
# refused inside an envelope that calls itself an error. And whatever the
# envelope DOES carry must be this module's own labels: node-authored text can
# embed an upstream API error body, identifiers or caller-derived fragments,
# and truncating or redacting that is not a closed set.
#
# Two levels, because each sees a failure the other cannot:
#
#   * post_process is driven through its real call path where the framework
#     pipeline allows (node(state) - trust gate, input gate, execute,
#     credential scan) and its returned partial state is merged the way the
#     state reducer merges it (error_log / node_history append); an errored
#     state is driven through execute(), because BaseNode.__call__
#     short-circuits on it and the backbone routes an error straight to
#     finalize, so that branch is only reachable from inside. The merged state
#     is then projected through the agent's own get_output(), which is exactly
#     what the caller reads.
#   * the same properties over the wire, through the ASGI app: a real run,
#     with the sentinel seeded into error_log by an inner node (a non-fatal
#     note is an ordinary shape here - classify_intent writes one) and a
#     violation forced at the output boundary.

import asyncio
import importlib
import json
import re

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

import src.nodes.post_process_node as ppn
from src.graph.graph import SmaregiPOSTransactionAgent
from src.nodes.post_process_node import (
    _REASON_OUTPUT_WITHHELD,
    _REASON_WORKFLOW_FAILED,
    ERROR_REASONS,
    PostProcessNode,
)
from src.schemas.state import to_json

try:
    import httpx

    _CLIENT_ERROR = None
except Exception as exc:  # pragma: no cover - only in stripped-down envs
    _CLIENT_ERROR = exc

_RECORD_ID = "t-1001"
_RECORD_REF = "smaregi://transactions/t-1001"
_LABEL = "POS transaction #t-1001 at store #s-01"
_STORE_ID = "s-01"
_CONFIRMATION = f"Retrieved POS transaction '{_LABEL}' - ref={_RECORD_REF}"

# Fields the state reducer accumulates rather than replaces.
_ACCUMULATED = ("error_log", "node_history")


def _bearer(fill: str = "a") -> str:
    # Assembled at runtime so no credential-shaped literal is committed.
    return "Bearer " + fill * 24


def _sentinel() -> str:
    """An error_log line of the kind an upstream failure produces: a name and a
    credential-shaped token inside an echoed response body. The token is
    assembled at runtime so no credential-shaped literal is committed."""
    token = "sk-" + "live-" + "x" * 3
    return "boom: upstream said {'customer':'A. Tanaka','token':'" + token + "'}"


# Fragments of the sentinel that must survive nowhere in the caller's envelope.
_SENTINEL_FRAGMENTS = ("A. Tanaka", "boom: upstream", "sk-" + "live-")


def _leaves(value):
    """Every key and scalar inside `value`, rendered as text, at any depth.

    Keys as well as values: a mapping key is caller-facing text too, and a
    top-level-only or values-only walk is how a leak stays invisible.
    """
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _leaves(item)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _leaves(item)
    else:
        yield str(value)


def _assert_sentinel_absent(payload) -> None:
    leaves = list(_leaves(payload))
    for fragment in _SENTINEL_FRAGMENTS:
        assert not any(fragment in leaf for leaf in leaves), (fragment, payload)
    rendered = json.dumps(payload, default=str)
    assert not any(fragment in rendered for fragment in _SENTINEL_FRAGMENTS), rendered


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.post_process_node.emit_trace_event", lambda *a, **k: None)


def _state_after_a_successful_inner_run(**overrides) -> dict:
    """The outer state as it stands when post_process runs after a clean inner
    workflow: the answer is already merged into `result` and every domain
    field is populated."""
    state = {
        "status": AgentStatus.SUCCESS.value,
        "result": {"record_id": _RECORD_ID, "record_ref": _RECORD_REF, "confirmation": _CONFIRMATION},
        "record_id": _RECORD_ID,
        "record_ref": _RECORD_REF,
        "record_label": _LABEL,
        "store_id": _STORE_ID,
        "transaction_id": _RECORD_ID,
        "product_code": "",
        "intent": "lookup_transaction",
        "confirmation": _CONFIRMATION,
        "smaregi_payload": to_json({"store_id": _STORE_ID, "transaction_id": _RECORD_ID}),
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "pb-envelope",
        "trace_id": "pb-envelope-trace",
        "node_history": ["InitializeNode", "PreProcessNode", "SmaregiWorkflowGraphNode"],
        "error_log": [_sentinel()],
        "execution_time": {},
    }
    state.update(overrides)
    return state


def _state_after_a_failed_inner_run(**overrides) -> dict:
    """The outer state when the inner workflow reported an error after the
    Smaregi call had resolved a record: the answer is still merged into
    `result`, and error_log carries the upstream failure text."""
    return _state_after_a_successful_inner_run(status=AgentStatus.ERROR.value, **overrides)


def _merge(state: dict, partial: dict) -> dict:
    merged = dict(state)
    for key, value in partial.items():
        if key in _ACCUMULATED:
            merged[key] = list(merged.get(key, [])) + list(value)
        else:
            merged[key] = value
    return merged


def _run(state: dict) -> "tuple[dict, dict]":
    """Run post_process over `state`; return (merged state, caller envelope)."""
    node = PostProcessNode()
    errored = state.get("status") == AgentStatus.ERROR.value
    partial = node.execute(state) if errored else node(state)
    merged = _merge(state, partial)
    return merged, SmaregiPOSTransactionAgent().get_output(merged)


def _envelope(state: dict) -> dict:
    return _run(state)[1]


_REFUSED_STATES = [
    pytest.param(
        _state_after_a_successful_inner_run(record_id="", record_ref=""),
        id="success-without-record-evidence",
    ),
    pytest.param(
        _state_after_a_successful_inner_run(confirmation="Retrieved - " + _bearer("b")),
        id="credential-in-a-top-level-value",
    ),
    pytest.param(
        _state_after_a_successful_inner_run(
            smaregi_payload=to_json({"store_id": _STORE_ID, "note": "token " + _bearer("c")})
        ),
        id="credential-nested-in-payload",
    ),
    pytest.param(
        _state_after_a_successful_inner_run(smaregi_payload=to_json({"store_id": _STORE_ID, "sk-" + "d" * 20: "x"})),
        id="credential-shaped-mapping-key",
    ),
]

_ALL_NON_SUCCESS = [*_REFUSED_STATES, pytest.param(_state_after_a_failed_inner_run(), id="inner-workflow-error")]


class TestErrorEnvelopeIsClosedSet:
    """On every non-success path the caller receives closed-set labels only."""

    @pytest.mark.parametrize("state", _ALL_NON_SUCCESS)
    def test_every_envelope_value_is_a_declared_constant(self, state):
        envelope = _envelope(state)

        assert envelope["status"] == AgentStatus.ERROR.value
        assert set(envelope["output"]) == {"reason"}, envelope["output"]
        assert set(envelope["output"].values()) <= ERROR_REASONS, envelope["output"]

    @pytest.mark.parametrize("state", _ALL_NON_SUCCESS)
    def test_payload_stays_truthy_so_the_result_fallback_never_fires(self, state):
        """The payload must be non-empty: the envelope falls through to
        `result` on any falsy payload, so an empty payload would silently
        restore the answer this refusal exists to withhold - a failure mode
        that looks identical to no fix at all."""
        envelope = _envelope(state)

        assert envelope["output"], "a falsy payload re-opens the `result` fallback"

    @pytest.mark.parametrize("state", _ALL_NON_SUCCESS)
    def test_seeded_error_text_reaches_no_part_of_the_envelope(self, state):
        _assert_sentinel_absent(_envelope(state))

    @pytest.mark.parametrize("state", _ALL_NON_SUCCESS)
    def test_envelope_carries_none_of_the_refused_response(self, state):
        rendered = json.dumps(_envelope(state), default=str)

        assert _RECORD_ID not in rendered
        assert _RECORD_REF not in rendered
        assert "smaregi://" not in rendered
        assert _LABEL not in rendered
        assert "Retrieved POS transaction" not in rendered
        assert "Traceback" not in rendered
        assert "/src/nodes/" not in rendered

    def test_inner_error_envelope_carries_the_reason_code_only(self):
        """The inner workflow's error_log can carry upstream response text -
        names, identifiers, tokens. None of it, none of the record evidence,
        and none of the answer merged into `result`, reaches the caller; and
        the internal channel keeps the line exactly once."""
        merged, envelope = _run(_state_after_a_failed_inner_run())

        assert envelope["output"] == {"reason": _REASON_WORKFLOW_FAILED}
        _assert_sentinel_absent(envelope)
        assert merged["error_log"].count(_sentinel()) == 1

    def test_refusal_names_the_location_in_error_log_and_never_in_the_envelope(self):
        """The violation entry names a place, not a value, and it stays
        internal: it is written to error_log and never enters the envelope."""
        state = _state_after_a_successful_inner_run(
            smaregi_payload=to_json({"store_id": _STORE_ID, "note": "token " + _bearer("e")})
        )
        merged, envelope = _run(state)
        reported = " ".join(merged["error_log"])
        rendered = json.dumps(envelope, default=str)

        assert _bearer("e") not in reported
        assert "formatted_output['smaregi_payload']['note']" in reported
        assert envelope["output"] == {"reason": _REASON_OUTPUT_WITHHELD}
        assert "output gate" not in rendered
        assert "smaregi_payload" not in rendered

    def test_credential_shaped_key_is_withheld_and_the_clearing_survives(self):
        """A key that is itself credential-shaped is refused AND kept out of
        the label: quoted, it would travel in error_log, where the framework's
        credential scan raises on the node result and replaces the cleared
        result - restoring the leak the clearing just closed. The clearing
        surviving the framework pipeline is the proof that nothing raised."""
        credential_shaped_key = "sk-" + "f" * 20
        state = _state_after_a_successful_inner_run(
            smaregi_payload=to_json({"store_id": _STORE_ID, credential_shaped_key: "x"})
        )
        merged, envelope = _run(state)

        assert envelope["output"] == {"reason": _REASON_OUTPUT_WITHHELD}
        assert credential_shaped_key not in json.dumps(merged, default=str)
        assert any("<withheld>" in entry for entry in merged["error_log"])


class TestUnrefusedResponseStillShips:
    """CONTROL: a clean response is delivered intact - otherwise every
    containment assertion above would pass vacuously."""

    def test_clean_response_carries_the_answer(self):
        envelope = _envelope(_state_after_a_successful_inner_run())

        assert envelope["status"] == AgentStatus.SUCCESS.value
        out = envelope["output"]
        assert out["record_id"] == _RECORD_ID
        assert out["record_ref"] == _RECORD_REF
        assert out["record_label"] == _LABEL
        assert out["confirmation"] == _CONFIRMATION

    def test_clean_response_carries_no_reason_code(self):
        rendered = json.dumps(_envelope(_state_after_a_successful_inner_run()), default=str)

        assert "reason" not in _envelope(_state_after_a_successful_inner_run())["output"]
        assert not any(reason in rendered for reason in ERROR_REASONS)


# ---------------------------------------------------------------------------
# The same properties over the wire, through the deployed ASGI surface.
# ---------------------------------------------------------------------------

_TOKEN = "invoke-token-for-testing"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}
_REQUEST = {"input": "Look up transaction t-1001 in store s-01.", "session_id": "pb-containment"}


class _AsgiClient:
    def __init__(self, app):
        self._app = app

    def post(self, path, json=None, headers=None):
        async def _run_request():
            transport = httpx.ASGITransport(app=self._app)
            async with httpx.AsyncClient(transport=transport, base_url="http://agent") as http:
                return await http.post(path, json=json, headers=headers)

        return asyncio.run(_run_request())


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", _TOKEN)
    server = importlib.import_module("src.api.server")
    return _AsgiClient(server.app)


@pytest.fixture()
def sentinel_in_error_log(monkeypatch):
    """Seed the sentinel into error_log from inside a real run.

    A node returning an error_log note on a SUCCESS result is an ordinary
    shape here (classify_intent writes exactly such a non-fatal note), so the
    line reaches post_process the way an upstream diagnostic would.
    """
    from src.nodes.infer_smaregi_fields_node import InferSmaregiFieldsNode

    original = InferSmaregiFieldsNode.execute

    def _with_note(self, state):
        result = original(self, state)
        result["error_log"] = [_sentinel()]
        return result

    monkeypatch.setattr(InferSmaregiFieldsNode, "execute", _with_note)


@pytest.mark.skipif(_CLIENT_ERROR is not None, reason=f"http client unavailable: {_CLIENT_ERROR}")
class TestEnvelopeOverTheWire:
    def test_refused_response_publishes_the_reason_code_only(self, client, monkeypatch, sentinel_in_error_log):
        """A real run, refused at the output boundary with an upstream line
        sitting in error_log: the body the caller receives is the reason code
        and nothing else - not the violation the gate raised, not the log.

        The violation is forced by widening what counts as credential-shaped,
        NOT by planting a credential: the framework runs its own credential
        scan over whatever the node returns, so a violation label quoting a
        match would make that scan raise and the containment delta would never
        be applied.
        """
        monkeypatch.setattr(ppn, "_CREDENTIAL_LIKE_RE", re.compile(r"smaregi://transactions"))
        body = client.post("/invoke", json=_REQUEST, headers=_AUTH).json()

        assert body["status"] == "error"
        assert body["output"] == {"reason": _REASON_OUTPUT_WITHHELD}
        _assert_sentinel_absent(body)
        rendered = json.dumps(body, default=str)
        assert "output gate" not in rendered
        assert "credential-like" not in rendered
        assert "Retrieved POS transaction" not in rendered
        assert "smaregi://" not in rendered
        assert "Traceback" not in rendered
        assert "/src/nodes/" not in rendered

    def test_inner_workflow_failure_publishes_no_text_at_all(self, client, monkeypatch):
        """When the inner workflow errors, the backbone routes straight to
        finalize - post_process never runs - so the caller's envelope must
        carry no payload and, in particular, no error_log key: the framework's
        get_output() is not overridden here, and it projects neither."""
        from src.nodes.call_smaregi_api_node import CallSmaregiApiNode

        def _fails(self, state, config=None):
            return {"status": AgentStatus.ERROR.value, "error_log": [_sentinel()]}

        monkeypatch.setattr(CallSmaregiApiNode, "execute", _fails)
        body = client.post("/invoke", json=_REQUEST, headers=_AUTH).json()

        assert body["status"] == "error"
        assert not body["output"]
        assert "error_log" not in body
        _assert_sentinel_absent(body)

    def test_clean_run_over_the_wire_still_answers(self, client, sentinel_in_error_log):
        """CONTROL: an error_log line does not by itself refuse the response -
        the clean path still ships the answer, and still ships none of the log."""
        body = client.post("/invoke", json=_REQUEST, headers=_AUTH).json()

        assert body["status"] == "success"
        assert body["output"]["record_ref"] == _RECORD_REF
        _assert_sentinel_absent(body)
