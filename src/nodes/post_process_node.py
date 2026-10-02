"""AgentCore Platform v1.0 - outer post_process node.

Cat 2 outer backbone: finalize the response after the inner Smaregi workflow
graph has run. GraphNode.merge_output() maps the inner result into the outer
state; this node shapes the caller-facing `formatted_output` and is the
external output boundary.

Three independent layers, in order:

  (1) credential scan - API keys, bearer tokens and JWT-shaped strings
      ANYWHERE in the caller-facing output withhold the response entirely. It
      runs BEFORE the numeric layer: the precision grammar deliberately treats
      any standalone 3-letter uppercase word as a currency marker, so snapping
      first could rewrite digits inside a labelled sequence and hide it from
      this scan;
  (2) monetary precision grid - the documented external schema reports
      monetary figures in units of 1,000; every monetary-form token is snapped
      onto that grid, with an audit event. Identifiers, counts, dates and
      decimal ratios are structural and stay byte-identical;
  (3) the credential scan is repeated afterwards, so no rewrite performed
      inside this gate can ever produce an unscanned surface.

The gate walks the WHOLE output structure, not just its top-level string
values: `smaregi_payload` is a nested mapping, so a scan that only looked at
top-level strings would step straight past a credential sitting one level down.
Mapping keys are scanned as well as values.

EVERY non-success return - a gate violation AND a pre-existing inner-workflow
error - goes through the one module-level `_contain()` helper. It does more
than fail: `AgentBaseGraph.get_output()` projects `formatted_output or result`
with NO status check, so the content has to be CLEARED from state or it would
still ship inside the error envelope. Every error return therefore overwrites
every output-bearing field, and the replacement `formatted_output` is a truthy
mapping - a falsy value would re-open the `or result` projection the
containment exists to prevent.

What the ERROR envelope may say: closed-set labels only. It carries a constant
reason code chosen by this module (one of `ERROR_REASONS`) and nothing else -
never `error_log`, never the gate's violation entries, never any other
node-authored text. Those lines can embed upstream response text (an API error
body), identifiers or caller-derived fragments, and truncating or redacting
them is not a closed set. `error_log` stays the INTERNAL channel: the state
reducer appends to it and the audit trail needs it; it is simply never
projected to the caller. Gate violations are written to `error_log` naming the
offending PATH (fixed keys and indices, never the value), and the audit event
carries a count.

No error envelope carries Smaregi record evidence either. `record_id` /
`record_ref` are this agent's LOOKUP EVIDENCE - the SUCCESS branch of the gate
below REFUSES an output that lacks them - so returning them under an ERROR
status would tell a caller being informed of failure that a POS record was
nonetheless resolved, and which receipt, store or product it was.

The domain output gate is implemented as MODULE-LEVEL functions called from
inside execute() - not instance methods, and not the framework
`_extra_security_gate_output` hook: the framework gate methods are @final on
FunctionNode and the SDK auto-wraps `_extra_` hooks, so domain checks live in
module-level helpers invoked inline.
"""

import re
from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json

# Credential-shaped strings that must never reach the caller (defence in depth -
# the framework credential scan in FunctionNode also runs on every result).
_CREDENTIAL_LIKE_RE = re.compile(
    r"eyJ[A-Za-z0-9._-]{10,}"
    r"|sk-[A-Za-z0-9]{20,}"
    r"|secret_[A-Za-z0-9]{10,}"
    r"|Bearer\s+[A-Za-z0-9._-]{16,}"
    r"|\bAKIA[A-Z0-9]{16}\b",
    re.IGNORECASE,
)

# Stand-in for a mapping key that cannot itself be written into a path label.
_UNNAMEABLE_KEY = "<withheld>"

# Approved external precision: monetary figures are reported in units of 1,000
# (must match the schema note appended below and the aggregates rendered by
# src/nodes/call_smaregi_api_node.py - the pipeline RENDERS on this grid, this
# gate ENFORCES it).
_EXTERNAL_ROUND_UNIT = 1000

# EXPLICIT output schema - monetary values are identified by FORM and by
# CURRENCY CONTEXT, never by magnitude:
#   form:    comma-grouped numbers (9,999 / 1,234,567) and unformatted runs of
#            5+ digits (a rendering-regression leak);
#   context: any bare 1-4 digit number associated with a currency marker is
#            monetary even though short - SYMMETRICALLY: a 3-letter uppercase
#            code or a currency symbol (incl. fullwidth ￥ and 円/₩), before or
#            after the value, attached or separated by ANY horizontal
#            whitespace run, signed or unsigned. Any standalone 3-letter
#            uppercase word counts as a code on purpose: a false snap fails
#            SAFE while a missed leak does not.
#
# IDENTIFIER GUARDS. This agent's output is dense with identifiers - store,
# transaction and product ids, `smaregi://` references, ISO dates - and a POS
# identifier is very often PURELY NUMERIC, which has no letters to protect it.
# A monetary token never starts or ends INSIDE such an identifier, so the whole
# grammar is wrapped in a pair of single-character, fixed-width guards: a match
# may not be immediately preceded or followed by an identifier character. The
# class is this template's OWN render alphabet, read off the renderer rather
# than assumed: `[A-Za-z0-9_-]` for the identifiers themselves, plus `/` and
# `:` (they surround an id inside `smaregi://stores/<id>/...`) and `#` (every
# bare identifier renders behind one - see ConfirmNode). The LEADING guard also
# carries `.`, so no alternative can enter a number part-way through and treat
# the tail of a decimal fraction as a value of its own; `.` is deliberately
# absent from the TRAILING guard, or an amount ending a sentence would escape
# the grid.
_IDENT_CHAR = r"A-Za-z0-9_\-/:#"
_LEAD_GUARD = rf"(?<![{_IDENT_CHAR}.])"
_TRAIL_GUARD = rf"(?![{_IDENT_CHAR}])"

_CURRENCY_MARKER = r"(?:\b[A-Z]{3}|[¥￥$€£円₩])"

# Delimiter between a currency marker and its value: horizontal whitespace and
# at most ONE newline - never a paragraph break. A plain `\s*` spans blank
# lines, so a 3-letter uppercase word ending a line would bind to the number
# that opens the next block and rewrite it ("Currency: JPY\n\n3. Cash Position"
# -> "0. Cash Position"). Every enumerated leak form (spaces, tabs, single
# newline, signed, symmetric, comma-grouped) still matches.
_GATE_DELIM = r"[ \t]*(?:\n[ \t]*)?"

# A monetary amount may carry a DECIMAL part, and every value alternative
# absorbs it into the SAME token. Without that, the fraction of "9999.99999" is
# a standalone 5+-digit run in its own right and gets rewritten into a number
# the output never contained; and in currency context the integer part snaps
# while the fraction dangles ("JPY 1234.56" -> "JPY 1,000.56").
#
# The `(?!\.\d)` arm is what makes absorption stick. A plain `(?:\.\d+)?` lets
# the engine backtrack out of the fraction and re-match the integer part alone
# whenever the text right after the fraction fails the trailing guard, and the
# dangling-fraction bug returns. Either the fraction is taken whole, or there
# is none there.
_VAL_FRACTION = r"(?:\.\d+|(?!\.\d))"

_NUM_TOKEN_RE = re.compile(
    _LEAD_GUARD
    # marker THEN value: "JPY 9999", "JPY  -9999", "JPY\t9999", "¥9999".
    # The value alternatives accept a comma-grouped form FIRST: the regex is
    # leftmost-first, so without it "JPY 1,234" would match as marker + "1"
    # (mangling the number on the snap) instead of as the whole grouped value.
    + rf"(?:(?P<pre>{_CURRENCY_MARKER}{_GATE_DELIM})"
    rf"(?P<val_after>[+-]?\d{{1,3}}(?:,\d{{3}})+{_VAL_FRACTION}|[+-]?\d{{1,4}}{_VAL_FRACTION})"
    # value THEN marker: "9999 JPY", "-9999\tJPY", "9999円", "+9999  $"
    rf"|(?P<val_before>[+-]?\d{{1,4}}{_VAL_FRACTION})(?P<post>{_GATE_DELIM}(?:[A-Z]{{3}}\b|[¥￥$€£円₩]))"
    # form-based, standalone at any magnitude: comma-grouped or 5+-digit runs
    rf"|(?P<val_form>[+-]?\d{{1,3}}(?:,\d{{3}})+{_VAL_FRACTION}|[+-]?\d{{5,}}{_VAL_FRACTION}))" + _TRAIL_GUARD
)

# ISO 4217 alphabetic codes. Used for ONE decision only: whether
# "<three uppercase letters>-<digits>" is a negative amount or an identifier.
# The two are lexically identical - "JPY-9999" (a signed amount, a real leak
# form) and "SKF-6205" (a part number) have the same shape - so no amount of
# guard-widening can separate them; something has to know which three-letter
# words are currencies. ISO 4217 is a CLOSED, standardised vocabulary, unlike
# the open set of identifiers, which is why the knowledge sits on this side.
# Everywhere else the grammar still treats ANY standalone three-letter
# uppercase word as a marker, because there a false snap fails safe. Here it
# does not: this agent renders caller-supplied store, transaction and product
# ids of exactly that shape, and rewriting one names a different record.
_ISO_CURRENCY_CODES = frozenset(
    "AED AUD BRL CAD CHF CNY DKK EUR GBP HKD IDR ILS INR JPY KRW MXN MYR NOK NZD "
    "PHP PLN RUB SAR SEK SGD THB TRY TWD USD VND ZAR".split()
)


def _is_identifier_hyphen(match: "re.Match[str]") -> bool:
    """True when this match is "<letters>-<digits>", i.e. an identifier.

    Only the marker-then-value branch with an ALPHABETIC marker, an EMPTY
    delimiter and a signed value can be ambiguous; every other form is
    unambiguous and never reaches this test.
    """
    pre = match.group("pre") or ""
    token = match.group("val_after") or ""
    if not pre or not token.startswith(("-", "+")):
        return False
    marker = pre.strip()
    if not marker.isalpha():  # a currency SYMBOL is never an identifier prefix
        return False
    return pre == marker and marker.upper() not in _ISO_CURRENCY_CODES


_SCHEMA_NOTE = (
    "Monetary figures are reported as aggregates rounded to the nearest 1,000; "
    "individual transaction amounts are not reported."
)


def _enforce_precision(text: str) -> "tuple[str, int]":
    """Snap every monetary-form token onto the approved external grid.

    Returns (sanitised_text, snap_count). A snap means a full-precision
    monetary figure reached the external surface - the gate rounds it onto the
    approved grid. The currency marker, the original delimiter whitespace and
    the explicit sign of the original token are all preserved.
    """
    snaps = 0

    def _snap(match: "re.Match[str]") -> str:
        nonlocal snaps
        pre = match.group("pre") or ""
        post = match.group("post") or ""
        if _is_identifier_hyphen(match):
            return match.group(0)
        token = match.group("val_after") or match.group("val_before") or match.group("val_form")
        # float(), not int(): the token may carry a decimal fraction, and the
        # whole amount - not just its integer part - is what sits on the grid.
        value = float(token.replace(",", ""))  # float() understands leading +/-
        if value % _EXTERNAL_ROUND_UNIT == 0:
            return match.group(0)
        snaps += 1
        snapped = round(value / _EXTERNAL_ROUND_UNIT) * _EXTERNAL_ROUND_UNIT
        plus = "+" if token.startswith("+") and snapped >= 0 else ""
        return f"{pre}{plus}{snapped:,d}{post}"

    return _NUM_TOKEN_RE.sub(_snap, text), snaps


def _walk_strings(value: object, path: str) -> "list[tuple[str, str]]":
    """Yield every (path, string) in the output, however deeply it is nested.

    Mappings, sequences and bare strings are all reachable representations of a
    caller-facing value, so all three are walked - and mapping KEYS are walked
    too, since a key is caller-facing text like any value.

    A credential-shaped KEY is reported as a violation by the caller of this
    walk, so the path label must not repeat it: the label travels in
    error_log, where the framework's own credential scan would raise on it
    and replace the cleared result around it with a bare error - restoring
    the very leak the clearing closed.
    """
    found: list[tuple[str, str]] = []
    if isinstance(value, str):
        found.append((path, value))
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                label = _UNNAMEABLE_KEY if _CREDENTIAL_LIKE_RE.search(key) else key
                key_path = f"{path}['{label}']"
                found.append((key_path, key))
            else:
                key_path = f"{path}[<key>]"
            found.extend(_walk_strings(item, key_path))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found.extend(_walk_strings(item, f"{path}[{index}]"))
    return found


def _scan_credentials(formatted_output: "dict[str, Any]") -> "list[str]":
    """Report every location carrying a credential-shaped string (never the value).

    The report is an error_log entry (internal); it never reaches the caller.
    """
    return [
        f"PostProcess output gate: credential-like value in {path}"
        for path, text in _walk_strings(formatted_output, "formatted_output")
        if _CREDENTIAL_LIKE_RE.search(text)
    ]


# The caller-facing fields that carry NARRATIVE text — the only place a
# monetary figure is ever rendered, and therefore the exact scope of the
# precision grid.
#
# Scope matters as much as the grammar. The identifier guards protect a value
# from its NEIGHBOURS, so they cannot protect a standalone identifier that IS
# the whole string: `record_id = "1234567"` has no neighbouring characters, and
# a grid applied to it rewrites a POS record id into "1,235,000" — naming a
# different receipt. Every other caller-facing field is an identifier, a
# reference, an ISO date or an intent label, all of them validated to an inert
# alphabet upstream, so none of them can carry an amount in the first place.
#
# A new caller-facing free-text field MUST be added here, and
# test_output_fields_are_classified fails until it is.
_NARRATIVE_FIELDS = ("record_label", "confirmation")

# Fields whose string values are identifiers, references, dates or labels.
# Listed explicitly so the classification is asserted rather than assumed.
_STRUCTURED_FIELDS = ("record_id", "record_ref", "intent", "smaregi_payload", "schema_note")


def _apply_precision_grid(formatted_output: "dict[str, Any]") -> int:
    """Snap every monetary-form token in the NARRATIVE fields onto the grid.

    Returns the number of off-grid figures corrected. Rewrites string leaves at
    any depth within those fields; every other field is left untouched (see
    _NARRATIVE_FIELDS for why the scope is a field list, not the whole output).
    """
    snaps = 0

    def _walk(value: object) -> object:
        nonlocal snaps
        if isinstance(value, str):
            snapped, count = _enforce_precision(value)
            snaps += count
            return snapped
        if isinstance(value, dict):
            return {key: _walk(item) for key, item in value.items()}
        if isinstance(value, list):
            return [_walk(item) for item in value]
        return value

    for key in _NARRATIVE_FIELDS:
        if key in formatted_output:
            formatted_output[key] = _walk(formatted_output[key])
    return snaps


def _security_gate_output(formatted_output: "dict[str, Any]", is_success: bool) -> "list[str]":
    """Domain output gate (module-level; called from PostProcessNode.execute()).

    Blocks (returns violations for):
      - a SUCCESS response with no record evidence (record_id/record_ref),
        which would misrepresent the Smaregi lookup outcome to the caller;
      - any credential-shaped string ANYWHERE in the caller-facing output,
        including inside nested payload mappings and lists, keys included.

    A violation names the offending PATH, never the value. Violations are
    error_log entries (internal); they never reach the caller.
    """
    problems: list[str] = []
    if is_success and not (formatted_output.get("record_id") or formatted_output.get("record_ref")):
        problems.append("PostProcess output gate: SUCCESS output missing record_id/record_ref evidence")
    problems.extend(_scan_credentials(formatted_output))
    return problems


# Every state field that can carry released content or record evidence to the
# caller. On EVERY error return - a gate violation and a pre-existing
# inner-workflow error alike - all of them are overwritten, because the response
# envelope reads formatted_output/result even on an error status, and because
# omitting a field from one envelope is not clearing it: a checkpoint or a
# downstream reader picks it straight back up out of state.
#
# record_id / record_ref / store_id / transaction_id / product_code are the
# LOOKUP EVIDENCE, not content, and they are cleared for that reason: a caller
# told the operation failed must not learn which POS record was resolved.
_OUTPUT_BEARING_FIELDS = (
    "result",
    "confirmation",
    "record_label",
    "smaregi_payload",
    "intent",
    "record_id",
    "record_ref",
    "store_id",
    "transaction_id",
    "product_code",
)

# Reason codes - the ONLY values the caller-visible ERROR envelope may carry.
# Chosen here, never derived from state, so the envelope is a closed set: it
# says WHAT happened, never to which record and never in whose words.
_REASON_WORKFLOW_FAILED = "smaregi_workflow_failed"  # the inner workflow reported an error
_REASON_OUTPUT_WITHHELD = "output_withheld_by_gate"  # the output gate refused the response
ERROR_REASONS = frozenset({_REASON_WORKFLOW_FAILED, _REASON_OUTPUT_WITHHELD})


def _contain(reason: str, new_errors: "list[str] | None" = None) -> "dict[str, Any]":
    """The node result for ANY non-success outcome - the single error shape.

    Error status, every output-bearing field cleared (_OUTPUT_BEARING_FIELDS),
    and an envelope made of closed-set labels only: `reason` is one of
    ERROR_REASONS. `new_errors` (gate violations - path labels only) are
    appended to `error_log`, the internal channel the state reducer
    accumulates, and never enter the envelope. Nothing is read out of state:
    not the record, not `error_log`.

    The constant `reason` key keeps the mapping TRUTHY, so the framework's
    `formatted_output or result` projection (AgentBaseGraph.get_output()
    applies no status check) serves this envelope and never whatever survived
    in `result`.
    """
    contained: dict[str, Any] = {field: "" for field in _OUTPUT_BEARING_FIELDS}
    contained["formatted_output"] = {"reason": reason}
    contained["status"] = AgentStatus.ERROR.value
    if new_errors:
        contained["error_log"] = list(new_errors)
    return contained


class PostProcessNode(FunctionNode):
    """Format, gate and release the final agent output."""

    # Read-only formatting of the already-produced result - default permissive.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: "dict[str, Any]") -> "dict[str, Any]":
        # If the inner workflow errored, preserve the error status (do not mask
        # it) and publish NOTHING of it: error_log already carries the inner
        # entries (the state reducer appends, so re-emitting them here would
        # duplicate every line) and the caller receives the reason code only.
        # The delta clears every output-bearing field, so the identifiers and
        # the rendered label cannot be recovered from the checkpoint or by a
        # downstream reader either.
        if state.get("status") == AgentStatus.ERROR.value:
            # Outcome signals only - a closed-set reason code and a count. The
            # audit log is not a store for POS record content or error text.
            emit_trace_event(
                "post_process_error_contained",
                {"reason": _REASON_WORKFLOW_FAILED, "errors": len(state.get("error_log") or [])},
                state,
            )
            return _contain(_REASON_WORKFLOW_FAILED)

        formatted_output: dict[str, Any] = {
            "record_id": state.get("record_id", ""),
            "record_ref": state.get("record_ref", ""),
            "record_label": state.get("record_label", ""),
            "intent": state.get("intent", ""),
            "confirmation": state.get("confirmation", ""),
            "smaregi_payload": from_json(state.get("smaregi_payload"), {}),
            "schema_note": _SCHEMA_NOTE,
        }

        # Layer 1 - credential scan, before any numeric rewrite. A refusal is
        # contained the same way as an inner error: the violations go to
        # error_log only, the caller receives the reason code only.
        violations = _security_gate_output(formatted_output, is_success=True)
        if violations:
            emit_trace_event(
                "post_process_blocked",
                {"reason": _REASON_OUTPUT_WITHHELD, "surface": "response", "violations": len(violations)},
                state,
            )
            return _contain(_REASON_OUTPUT_WITHHELD, violations)

        # Layer 2 - monetary precision grid.
        snaps = _apply_precision_grid(formatted_output)

        # Layer 3 - re-scan: no rewrite this gate performed may produce an
        # unscanned surface.
        violations = _scan_credentials(formatted_output)
        if violations:
            emit_trace_event(
                "post_process_blocked",
                {"reason": _REASON_OUTPUT_WITHHELD, "surface": "post_grid", "violations": len(violations)},
                state,
            )
            return _contain(_REASON_OUTPUT_WITHHELD, violations)

        # Audit the final response shaping - outcome signals only, no content.
        emit_trace_event(
            "post_process_complete",
            {
                "intent": state.get("intent", ""),
                "has_record_id": bool(state.get("record_id")),
                "precision_snaps": snaps,
            },
            state,
        )

        return {
            "formatted_output": formatted_output,
            "status": AgentStatus.SUCCESS.value,
        }
