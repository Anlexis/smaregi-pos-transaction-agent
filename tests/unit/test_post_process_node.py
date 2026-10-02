# CMN-C2-285 - Unit tests: PostProcessNode (outer backbone, external output boundary)
#
# Canon: invoked via node(state) (BaseNode.__call__ -> trust gate -> input gate
# -> execute -> output gate); this backbone formatter declares ANONYMOUS, so
# the state builder sets caller_trust_level = TrustLevel.ANONYMOUS.value. The
# domain gate is the MODULE-LEVEL _security_gate_output() helper (the framework
# gate methods are @final and the SDK auto-wraps _extra_ hooks), so the helper
# and the precision grammar are also unit-tested directly as plain functions.
#
# The errored-state branch is driven through execute() DIRECTLY where noted:
# BaseNode.__call__ short-circuits on an incoming errored state and the
# backbone routes an error straight to finalize, so that branch is only
# reachable from inside - and it must still be a closed set.

import json

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

import src.nodes.post_process_node as ppn
from src.nodes.post_process_node import (
    _NARRATIVE_FIELDS,
    _OUTPUT_BEARING_FIELDS,
    _REASON_OUTPUT_WITHHELD,
    _REASON_WORKFLOW_FAILED,
    _STRUCTURED_FIELDS,
    ERROR_REASONS,
    PostProcessNode,
    _enforce_precision,
    _security_gate_output,
)
from src.schemas.state import to_json

_JWT_LIKE = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9abcdef"


def _bearer(fill: str = "a") -> str:
    # Built at runtime so no credential-shaped literal is committed.
    return "Bearer " + fill * 24


def _sentinel() -> str:
    """An error_log line of the kind an upstream failure produces: a name and a
    credential-shaped token inside an echoed response body. The token is
    assembled at runtime so no credential-shaped literal is committed."""
    token = "sk-" + "live-" + "x" * 3
    return "boom: upstream said {'customer':'A. Tanaka','token':'" + token + "'}"


# Fragments of the sentinel that must survive nowhere in a returned mapping.
_SENTINEL_FRAGMENTS = ("A. Tanaka", "boom: upstream", "sk-" + "live-")


def _leaves(value):
    """Every key and scalar inside `value`, rendered as text, at any depth."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _leaves(item)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _leaves(item)
    else:
        yield str(value)


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.post_process_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "status": AgentStatus.SUCCESS.value,
        "record_id": "t-1001",
        "record_ref": "smaregi://transactions/t-1001",
        "record_label": "POS transaction #t-1001 at store #s-01",
        "intent": "lookup_transaction",
        "confirmation": (
            "Retrieved POS transaction 'POS transaction #t-1001 at store #s-01'" " - ref=smaregi://transactions/t-1001"
        ),
        "smaregi_payload": to_json({"store_id": "s-01", "transaction_id": "t-1001"}),
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "post-process-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestPostProcessNode:
    def setup_method(self):
        self.node = PostProcessNode()

    def test_success_shape(self):
        result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        out = result["formatted_output"]
        assert out["record_id"] == "t-1001"
        assert out["record_ref"] == "smaregi://transactions/t-1001"
        assert out["intent"] == "lookup_transaction"
        # The JSON payload round-trips back to a native mapping for the caller.
        assert out["smaregi_payload"] == {"store_id": "s-01", "transaction_id": "t-1001"}
        # The output schema is stated in the response the caller reads.
        assert "nearest 1,000" in out["schema_note"]

    def test_incoming_error_short_circuits_the_node(self):
        """An already-errored state never reaches execute().

        BaseNode.__call__ returns before calling execute() when the state
        arrives with an error status, so no success shape can be fabricated.
        The backbone router sends an errored state to finalize in any case -
        the branch inside execute() below is defensive, reachable only by a
        direct call.
        """
        result = self.node(_state(status=AgentStatus.ERROR.value, error_log=["CallSmaregiApiNode: boom"]))
        assert result["status"] == AgentStatus.ERROR.value
        assert "Smaregi" in "\n".join(result["error_log"])
        assert "formatted_output" not in result

    def test_error_envelope_shape_on_a_direct_call(self):
        """The inner error is preserved as a status; its text stays internal."""
        result = self.node.execute(_state(status=AgentStatus.ERROR.value, error_log=["CallSmaregiApiNode: boom"]))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["formatted_output"] == {"reason": _REASON_WORKFLOW_FAILED}
        # The inner entries are not re-emitted - the state reducer appends.
        assert "error_log" not in result
        assert "CallSmaregiApiNode: boom" not in json.dumps(result, default=str)

    def test_gate_blocks_success_without_record_evidence(self):
        """A SUCCESS output missing record_id/record_ref is blocked."""
        result = self.node(_state(record_id="", record_ref=""))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("record_id/record_ref" in entry for entry in result["error_log"])

    def test_gate_blocks_credential_in_a_top_level_value(self):
        result = self.node(_state(confirmation=f"Retrieved - Bearer {_JWT_LIKE}"))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("credential-like value" in entry for entry in result["error_log"])
        # The location is named; the matched value never is.
        assert not any(_JWT_LIKE in entry for entry in result["error_log"])

    def test_gate_walks_nested_structures(self):
        """A credential riding one level down must not slip past the scan.

        The nested probe alone cannot distinguish "gate is blind" from "probe
        is wrong", so the top-level control above is what proves the verifier
        itself works.
        """
        result = self.node(_state(smaregi_payload=to_json({"store_id": "s-01", "note": f"token {_JWT_LIKE}"})))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("smaregi_payload" in entry for entry in result["error_log"])

    def test_credential_in_error_log_never_reaches_the_caller(self):
        """A remote 4xx body would arrive in error_log. The error path
        publishes the reason code only, so the body cannot reach the caller
        through it - and the inner entries are not re-emitted either.

        Called directly: __call__ short-circuits an incoming errored state
        before execute() runs, so this branch is only reachable from inside."""
        credential = "sk-" + "a" * 24
        result = self.node.execute(_state(status=AgentStatus.ERROR.value, error_log=[f"failed with {credential}"]))
        assert result["status"] == AgentStatus.ERROR.value
        # The envelope is the reason code and nothing else: truthy on purpose
        # (a falsy value re-opens the framework's `formatted_output or result`
        # projection), and carrying none of the error text.
        assert result["formatted_output"] == {"reason": _REASON_WORKFLOW_FAILED}
        assert credential not in json.dumps(result, default=str)
        assert "error_log" not in result

    def test_violation_clears_every_output_bearing_field(self):
        """Containment, not just refusal.

        The response envelope falls back to state["result"] even on an error
        status, so a gate that merely returned an error would still ship the
        un-gated inner answer inside the error envelope.
        """
        result = self.node(_state(confirmation=f"Bearer {_JWT_LIKE}"))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["result"] == ""
        assert result["confirmation"] == ""
        assert result["record_label"] == ""
        assert result["smaregi_payload"] == ""
        # The record evidence is cleared too - a caller told the operation
        # failed must not learn which POS record was resolved.
        assert result["record_id"] == ""
        assert result["record_ref"] == ""
        assert result["intent"] == ""
        # The replacement envelope is the reason code and nothing else -
        # deliberately truthy, and carrying none of the violation entries.
        assert result["formatted_output"] == {"reason": _REASON_OUTPUT_WITHHELD}
        assert "t-1001" not in repr(result["formatted_output"])
        # Nothing that was blocked survives anywhere in the returned delta.
        assert _JWT_LIKE not in repr(result)

    def test_off_grid_aggregate_is_snapped_onto_the_grid(self):
        label = "daily sales for store #s-01 on 2026-07-21 (total JPY 12,345, 7 transactions)"
        result = self.node(_state(record_label=label, confirmation=label))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "JPY 12,000" in result["formatted_output"]["record_label"]
        assert "12,345" not in result["formatted_output"]["record_label"]
        # The structural count is untouched.
        assert "7 transactions" in result["formatted_output"]["record_label"]

    def test_on_grid_aggregate_is_byte_identical(self):
        label = "daily sales for store #s-01 on 2026-07-21 (total JPY 12,000, 7 transactions)"
        result = self.node(_state(record_label=label))
        assert result["formatted_output"]["record_label"] == label

    def test_grid_reaches_nested_values_inside_a_narrative_field(self):
        label = ["daily total JPY 9999", {"note": "and JPY 8888"}]
        result = self.node(_state(record_label=label))
        assert result["formatted_output"]["record_label"] == [
            "daily total JPY 10,000",
            {"note": "and JPY 9,000"},
        ]

    def test_identifier_fields_are_never_rewritten_by_the_grid(self):
        """The guards protect a value from its NEIGHBOURS, so they cannot
        protect an identifier that IS the whole string. A purely numeric POS id
        must come back byte-identical, or the caller is handed a different
        record."""
        result = self.node(
            _state(
                record_id="1234567",
                record_ref="smaregi://transactions/1234567",
                record_label="POS transaction #1234567 at store #98765",
                smaregi_payload=to_json({"store_id": "98765", "transaction_id": "1234567"}),
            )
        )
        out = result["formatted_output"]
        assert out["record_id"] == "1234567"
        assert out["smaregi_payload"] == {"store_id": "98765", "transaction_id": "1234567"}
        assert out["record_label"] == "POS transaction #1234567 at store #98765"

    def test_output_fields_are_classified(self):
        """Every caller-facing field is either narrative (grid-enforced) or
        structured (identifier/label). A new field must be classified before it
        can ship, or a monetary figure could be rendered where nothing enforces
        the grid."""
        out = self.node(_state())["formatted_output"]
        classified = set(_NARRATIVE_FIELDS) | set(_STRUCTURED_FIELDS)
        assert set(out) <= classified, set(out) - classified


class TestSecurityGateOutputHelper:
    def test_clean_success_output_has_no_violations(self):
        assert _security_gate_output({"record_id": "t-1", "record_ref": "r"}, is_success=True) == []

    def test_missing_evidence_is_a_violation(self):
        assert _security_gate_output({"record_id": "", "record_ref": ""}, is_success=True)

    def test_error_output_needs_no_record_evidence(self):
        assert _security_gate_output({"record_id": "", "record_ref": ""}, is_success=False) == []

    def test_violation_names_the_path_and_never_the_value(self):
        violations = _security_gate_output({"record_id": "t-1", "note": _bearer()}, is_success=True)
        assert violations == ["PostProcess output gate: credential-like value in formatted_output['note']"]

    def test_credential_shaped_mapping_key_is_caught_and_withheld_from_the_label(self):
        """A mapping KEY is caller-facing text too. A credential-shaped key is
        a violation, and the label built from it must not quote it: the label
        travels in error_log, where the framework's own credential scan would
        raise on the node result and discard the cleared fields around it."""
        credential_shaped_key = "sk-" + "b" * 20
        formatted = {"record_id": "t-1", "smaregi_payload": {credential_shaped_key: "x"}}
        violations = _security_gate_output(formatted, is_success=True)
        assert violations
        assert all(credential_shaped_key not in v for v in violations)
        assert any("formatted_output['smaregi_payload']['<withheld>']" in v for v in violations)

    def test_clean_nested_keys_are_named_in_the_label(self):
        formatted = {"record_id": "t-1", "smaregi_payload": {"note": _bearer("c")}}
        violations = _security_gate_output(formatted, is_success=True)
        assert violations == [
            "PostProcess output gate: credential-like value in formatted_output['smaregi_payload']['note']"
        ]


# Every non-success path this node has. `via` says how the path is reached:
# node(state) runs the framework pipeline; execute() is used for an errored
# state because BaseNode.__call__ short-circuits on it (and the backbone routes
# an error straight to finalize), so that branch is only reachable from inside.
# `fault` seeds a defect on the DATA path for the one layer no input reaches:
# the post-grid re-scan only fires if the grid itself writes a credential.
def _post_grid_writes_a_credential(monkeypatch):
    def _inject(formatted_output):
        formatted_output["record_label"] = "snapped " + _bearer("g")
        return 1

    monkeypatch.setattr(ppn, "_apply_precision_grid", _inject)


_ERROR_PATHS = [
    pytest.param(
        {"status": AgentStatus.ERROR.value, "record_id": "", "record_ref": ""},
        "execute",
        None,
        id="inner-workflow-error",
    ),
    pytest.param(
        {
            "status": AgentStatus.ERROR.value,
            "result": {"record_id": "t-1001", "confirmation": "Retrieved POS transaction #t-1001"},
        },
        "execute",
        None,
        id="inner-workflow-error-with-answer-in-result",
    ),
    pytest.param(
        {"status": AgentStatus.ERROR.value, "error_log": [_sentinel(), "CallSmaregiApiNode: rejected " + _bearer()]},
        "execute",
        None,
        id="inner-workflow-error-with-credential-in-error-log",
    ),
    pytest.param({"record_id": "", "record_ref": ""}, "call", None, id="gate-missing-record-evidence"),
    pytest.param(
        {"confirmation": "Retrieved - " + _bearer("d")}, "call", None, id="gate-credential-in-top-level-value"
    ),
    pytest.param(
        {"smaregi_payload": to_json({"store_id": "s-01", "note": "token " + _bearer("e")})},
        "call",
        None,
        id="gate-credential-nested-in-payload",
    ),
    pytest.param(
        {"smaregi_payload": to_json({"store_id": "s-01", "sk-" + "f" * 20: "x"})},
        "call",
        None,
        id="gate-credential-shaped-mapping-key",
    ),
    pytest.param({}, "call", _post_grid_writes_a_credential, id="gate-post-grid-rescan"),
]


class TestErrorEnvelopeIsClosedSet:
    """Whatever the non-success path, the caller-visible envelope is made of
    this module's own constants - never of node-authored text.

    error_log is seeded with a recognisable sentinel on every path: a name and
    a credential-shaped token inside an echoed upstream body, which is exactly
    what an API-call failure can put there. Truncating or redacting such a line
    is not a closed set, so it must appear nowhere in what the node returns.
    """

    def setup_method(self):
        self.node = PostProcessNode()

    def _drive(self, overrides: dict, via: str, fault, monkeypatch) -> dict:
        if fault is not None:
            fault(monkeypatch)
        state = _state(**{"error_log": [_sentinel()], **overrides})
        return self.node.execute(state) if via == "execute" else self.node(state)

    @pytest.mark.parametrize(("overrides", "via", "fault"), _ERROR_PATHS)
    def test_envelope_values_are_declared_constants(self, overrides, via, fault, monkeypatch):
        result = self._drive(overrides, via, fault, monkeypatch)

        assert result["status"] == AgentStatus.ERROR.value
        envelope = result["formatted_output"]
        assert set(envelope) == {"reason"}, envelope
        assert set(envelope.values()) <= ERROR_REASONS, envelope

    @pytest.mark.parametrize(("overrides", "via", "fault"), _ERROR_PATHS)
    def test_envelope_stays_truthy_and_the_answer_is_cleared(self, overrides, via, fault, monkeypatch):
        """`formatted_output or result`: a falsy envelope re-opens the fallback,
        and a surviving `result` is what it would fall back onto."""
        result = self._drive(overrides, via, fault, monkeypatch)

        assert result["formatted_output"], "a falsy formatted_output re-opens the `result` fallback"
        cleared = {field: result[field] for field in _OUTPUT_BEARING_FIELDS}
        assert not any(cleared.values()), cleared

    @pytest.mark.parametrize(("overrides", "via", "fault"), _ERROR_PATHS)
    def test_seeded_error_text_appears_nowhere_in_the_returned_mapping(self, overrides, via, fault, monkeypatch):
        result = self._drive(overrides, via, fault, monkeypatch)

        leaves = list(_leaves(result))
        for fragment in _SENTINEL_FRAGMENTS:
            assert not any(fragment in leaf for leaf in leaves), (fragment, result)
        rendered = json.dumps(result, default=str)
        assert not any(fragment in rendered for fragment in _SENTINEL_FRAGMENTS), rendered

    def test_the_reason_names_the_path_taken(self):
        errored = self.node.execute(_state(status=AgentStatus.ERROR.value, error_log=[_sentinel()]))
        withheld = self.node(_state(record_id="", record_ref="", error_log=[_sentinel()]))

        assert errored["formatted_output"] == {"reason": _REASON_WORKFLOW_FAILED}
        assert withheld["formatted_output"] == {"reason": _REASON_OUTPUT_WITHHELD}

    def test_inner_error_entries_are_not_re_emitted(self):
        """The state reducer appends error_log; re-emitting the inner entries
        would duplicate every line, and the caller never sees them anyway."""
        result = self.node.execute(_state(status=AgentStatus.ERROR.value, error_log=[_sentinel()]))

        assert "error_log" not in result

    def test_gate_violations_travel_in_error_log_only(self):
        result = self.node(_state(record_id="", record_ref="", error_log=[_sentinel()]))

        assert any("output gate" in entry for entry in result["error_log"])
        assert "output gate" not in json.dumps(result["formatted_output"])

    def test_a_credential_shaped_mapping_key_is_withheld_from_the_label(self):
        """A key that is itself credential-shaped is refused AND kept out of
        the label: quoted, it would travel in error_log, where the framework
        credential scan raises on the node result and replaces the cleared
        result - restoring the leak the clearing just closed. The clearing
        surviving the framework pipeline is the proof that nothing raised."""
        credential_shaped_key = "sk-" + "h" * 20
        result = self.node(_state(smaregi_payload=to_json({"store_id": "s-01", credential_shaped_key: "x"})))

        assert result["status"] == AgentStatus.ERROR.value
        assert result["formatted_output"] == {"reason": _REASON_OUTPUT_WITHHELD}
        assert result["result"] == ""
        assert credential_shaped_key not in json.dumps(result, default=str)
        assert any("<withheld>" in entry for entry in result["error_log"])

    def test_audit_event_carries_a_count_and_no_error_text(self, monkeypatch):
        """The audit trail gets outcome signals only: a reason code and a count."""
        events = []
        monkeypatch.setattr(ppn, "emit_trace_event", lambda name, payload, state: events.append((name, payload)))

        self.node(
            _state(record_id="", record_ref="", confirmation="Retrieved - " + _bearer("i"), error_log=[_sentinel()])
        )
        self.node.execute(_state(status=AgentStatus.ERROR.value, error_log=[_sentinel(), "second"]))

        assert events[0] == (
            "post_process_blocked",
            {"reason": _REASON_OUTPUT_WITHHELD, "surface": "response", "violations": 2},
        )
        assert events[1] == ("post_process_error_contained", {"reason": _REASON_WORKFLOW_FAILED, "errors": 2})
        assert not any(fragment in json.dumps(events) for fragment in _SENTINEL_FRAGMENTS)


class TestPrecisionGrammar:
    """Both directions: every leak form snaps, every structural token survives.

    The grammar is group-based (no lookbehinds around the delimiter), so the
    marker/value delimiter can be an arbitrary horizontal whitespace run; the
    identifier guards are exact single-character assertions over this
    template's own render alphabet.
    """

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("JPY 1234", "JPY 1,000"),  # short value in currency context
            ("JPY 1,234", "JPY 1,000"),  # grouped value snaps whole, not as "1"
            ("9999 JPY", "10,000 JPY"),  # marker AFTER the value
            ("9999円", "10,000円"),  # fullwidth marker
            ("¥9999", "¥10,000"),  # symbol marker, attached
            ("JPY +9999", "JPY +10,000"),  # explicit sign preserved
            ("JPY-9999", "JPY-10,000"),  # attached sign, real currency code
            ("JPY\t9999", "JPY\t10,000"),  # tab delimiter
            ("JPY  9999", "JPY  10,000"),  # multi-space delimiter
            ("JPY\n9999", "JPY\n10,000"),  # single newline delimiter
            ("1,234,567", "1,235,000"),  # grouped form, no marker
            ("total 123456", "total 123,000"),  # bare 5+-digit run
            ("JPY 1234.56", "JPY 1,000"),  # decimal absorbed into one token
            ("Receipt totals JPY 9999.", "Receipt totals JPY 10,000."),  # sentence-final
        ],
    )
    def test_leak_forms_snap(self, text, expected):
        assert _enforce_precision(text)[0] == expected

    @pytest.mark.parametrize(
        "text",
        [
            "JPY 1,000",  # already on the grid
            "JPY 12,000",
            "smaregi://transactions/1234567",  # purely numeric id inside a reference
            "POS transaction #1234567 at store #98765",  # purely numeric ids behind '#'
            "ref=smaregi://stores/12345/daily-sales/2026-07-21",
            "daily-s-01-2026-07-21",
            "ABC-1234",  # identifier, not a negative amount
            "SKU-9999",
            "sku_48210",  # underscore alphabet
            "Currency: JPY\n\n3. Cash Position",  # blank line is not a delimiter
            "8.512345",  # decimal fraction survives
            "9999.99999%",
            "ratio 0.123456",
            "JPY 1234.56m",  # no backtracking out of the fraction
            "1234 transactions",  # structural count
            "sales count 9999",
            "STAR 2026",  # embedded acronym, not a marker
            "in 2026",
            "v12",
            "90d",
        ],
    )
    def test_structural_tokens_are_byte_identical(self, text):
        assert _enforce_precision(text)[0] == text

    def test_pattern_shaped_sequences_are_not_mangled(self):
        """The snap must not rewrite digits inside a labelled sequence.

        A three-letter uppercase word followed by digits is read as a currency
        marker by design; if it also rewrote "SSN 123-45-6789" it would destroy
        the very shape a pattern scan looks for. The identifier guards keep such
        sequences intact, and the credential scan runs before AND after the
        snap in any case.
        """
        for text in ("SSN 123-45-6789", "TAX 987-65-4321"):
            assert _enforce_precision(text)[0] == text

    def test_snap_count_is_reported(self):
        _, snaps = _enforce_precision("JPY 1234 and JPY 5678")
        assert snaps == 2
        _, snaps = _enforce_precision("JPY 1,000 and JPY 2,000")
        assert snaps == 0
