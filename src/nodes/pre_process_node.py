"""AgentCore Platform v1.0 - outer pre_process node.

Cat 2 outer backbone: validate the caller's request (raw NL text + the
structured caller fields) and serialize it for the inner Smaregi workflow
graph. This node OWNS the caller-data contract: every field a caller can supply
is checked here, against explicit bounds, before any of it reaches the
workflow. Business rules (intent, entity extraction) live in the inner graph.

Caller contract (`input_context`), every field optional:

    store_id / store_hint / store_code   target store: a POS store identifier
    transaction                          a POS transaction head record the
                                         caller already holds:
                                         {transaction_id, store_id, total,
                                          transaction_date_time}
    daily_sales                          a daily-sales aggregate the caller
                                         already holds:
                                         {store_id, sum_date, sales_total,
                                          transaction_count}
    product_sales                        a product-sales aggregate the caller
                                         already holds:
                                         {product_code, store_id, sum_date,
                                          sales_count}

Rules applied to all of them:
  - values must be the declared TYPE. A number, boolean, mapping or list where
    a string is required is refused outright, never coerced: str(float("nan"))
    is "nan", which passes an identifier shape check, so coercion would let a
    non-finite value name the store of a lookup.
  - every NUMBER goes through a finite + bounded parser. NaN and +/-Infinity
    parse fine through float() and arrive intact through raw JSON, and every
    comparison against NaN is False - a silent fail-OPEN on exactly the
    figures this agent reports. They are refused, along with out-of-range
    magnitudes.
  - every string that renders into the response is locked to an inert
    identifier alphabet; free text there would be caller-controlled output.
  - instruction-override text (directives aimed at the MODEL: role
    reassignment, system-prompt manipulation, chat-template control tokens) is
    REFUSED, fail closed, on BOTH caller text channels - the raw request text
    and every decoded string in input_context, keys included, at any depth -
    before anything is carried forward. The screen is the template's own
    (src/services/security.py), never delegated to the platform gate.
  - a refusal names the FIELD and never echoes the offending value; an
    unrecognised field NAME is masked, never echoed either.
  - absent fields are simply absent: the workflow falls back to what it can
    read out of the request text and to the built-in stub records.
"""

import json
import math
import re
from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import to_json
from src.services.security import (
    contains_instruction_override,
    redact_sensitive,
    sanitize_query,
)

# A POS identifier (store / transaction / product). Locked to an inert
# alphabet because these values render into the confirmation the caller reads
# back: anything wider would be caller-controlled output.
_POS_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
# An explicit ISO calendar date. "latest" is expressed by omitting the field.
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Money is reported as an aggregate rounded to the nearest 1,000 (docs/02
# "External output schema"), so the bound is generous; the point of the cap is
# that a magnitude outside it is a malformed figure, not a large store.
_MAX_AMOUNT = 1_000_000_000_000.0
# Counts are per-store, per-day and are rendered VERBATIM (they are structural,
# not monetary, and the precision grid must never touch them). The cap keeps a
# rendered count below five digits, which is what keeps it structurally
# distinguishable from an unmarked monetary run at the output boundary.
_MAX_COUNT = 9_999

_STORE_ALIASES = ("store_id", "store_hint", "store_code")


class CallerFieldError(ValueError):
    """A caller-supplied field failed its contract. Carries the field name only."""


# Contract field names that may be echoed into a refusal message. Any other
# input_context key is caller-controlled text, so its spot in the reported path
# shows a placeholder - an unrecognised field NAME is never echoed either.
# Public: the entry point screens the same channel and masks names the same way.
KNOWN_CONTEXT_FIELDS = frozenset(
    {
        "store_id",
        "store_hint",
        "store_code",
        "transaction",
        "transaction_id",
        "total",
        "transaction_date_time",
        "daily_sales",
        "sum_date",
        "sales_total",
        "transaction_count",
        "product_sales",
        "product_code",
        "sales_count",
    }
)


def _find_instruction_override(value: object, path: str = "input_context") -> "str | None":
    """Depth-first scan of every decoded string in the mapping - keys included.

    Returns the path of the first string carrying an instruction-override
    directive, or None. The walk runs on the PARSED mapping, so JSON \\u
    escaping cannot smuggle a phrase past it, and it covers undeclared keys
    too: the screen must hold on what the caller SENT, not only on what the
    contract keeps. Path components outside the declared contract are masked,
    so the returned path is always safe to name in an error message.
    """
    if isinstance(value, str):
        return path if contains_instruction_override(value) else None
    if isinstance(value, dict):
        for key, item in value.items():
            safe_key = key if isinstance(key, str) and key in KNOWN_CONTEXT_FIELDS else "<unrecognised-field>"
            key_path = f"{path}.{safe_key}"
            if isinstance(key, str) and contains_instruction_override(key):
                return key_path
            found = _find_instruction_override(item, key_path)
            if found is not None:
                return found
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found = _find_instruction_override(item, f"{path}[{index}]")
            if found is not None:
                return found
    return None


def _require_text(value: object, field: str, max_len: int) -> str:
    """Return a bounded string, or raise naming the field (never the value).

    Rejects every non-string type, `bool` included, so numeric input cannot be
    stringified into something that satisfies a downstream shape check.
    """
    if not isinstance(value, str):
        raise CallerFieldError(f"'{field}' must be a string")
    text = value.strip()
    if not text:
        raise CallerFieldError(f"'{field}' must not be empty")
    if len(text) > max_len:
        raise CallerFieldError(f"'{field}' exceeds the {max_len}-character limit")
    return text


def _validate_pos_id(raw: object, field: str) -> str:
    """Validate a POS identifier: inert alphabet, length-bounded."""
    text = _require_text(raw, field, 32).lstrip("#")
    if not _POS_ID_RE.match(text):
        raise CallerFieldError(f"'{field}' is not a valid POS identifier")
    return text


def _validate_iso_date(raw: object, field: str) -> str:
    text = _require_text(raw, field, 10)
    if not _ISO_DATE_RE.match(text):
        raise CallerFieldError(f"'{field}' must be an ISO date (YYYY-MM-DD)")
    return text


def _finite_in_range(raw: object, field: str, minimum: float, maximum: float) -> float:
    """Parse a caller number that must be FINITE and inside an explicit range.

    Fails CLOSED, naming the field. `bool` is rejected before `int`/`float`
    (True is 1 in Python), strings are parsed rather than coerced, and the
    finiteness check is what stops NaN / Infinity - both of which survive
    float() and raw JSON, and both of which make every subsequent comparison
    False rather than raising.
    """
    if isinstance(raw, bool):
        raise CallerFieldError(f"'{field}' must be a number")
    if isinstance(raw, (int, float)):
        value = float(raw)
    elif isinstance(raw, str):
        try:
            value = float(raw.strip())
        except (TypeError, ValueError):
            raise CallerFieldError(f"'{field}' must be a number") from None
    else:
        raise CallerFieldError(f"'{field}' must be a number")
    if not math.isfinite(value):
        raise CallerFieldError(f"'{field}' must be a finite number")
    if not (minimum <= value <= maximum):
        raise CallerFieldError(f"'{field}' is outside the permitted range")
    return value


def _validate_record(raw: object, field: str, spec: "dict[str, str]") -> "dict[str, Any]":
    """Validate one caller-supplied POS record against its field spec.

    `spec` maps a record key to its kind: "id", "date", "amount", "count" or
    "text". Unknown keys are dropped rather than carried - the record that
    reaches the workflow contains only fields this contract validated.
    """
    if not isinstance(raw, dict):
        raise CallerFieldError(f"'{field}' must be a mapping")
    record: dict[str, Any] = {}
    for key, kind in spec.items():
        value = raw.get(key)
        if value is None:
            continue
        qualified = f"{field}.{key}"
        if kind == "id":
            record[key] = _validate_pos_id(value, qualified)
        elif kind == "date":
            record[key] = _validate_iso_date(value, qualified)
        elif kind == "amount":
            record[key] = _finite_in_range(value, qualified, 0.0, _MAX_AMOUNT)
        elif kind == "count":
            count = _finite_in_range(value, qualified, 0.0, float(_MAX_COUNT))
            if count != int(count):
                raise CallerFieldError(f"'{qualified}' must be a whole number")
            record[key] = int(count)
        else:  # "text" - bounded and locked to the inert identifier alphabet
            record[key] = _validate_pos_id(value, qualified)
    return record


_TRANSACTION_SPEC = {
    "transaction_id": "id",
    "store_id": "id",
    "total": "amount",
}
_DAILY_SALES_SPEC = {
    "store_id": "id",
    "sum_date": "date",
    "sales_total": "amount",
    "transaction_count": "count",
}
_PRODUCT_SALES_SPEC = {
    "product_code": "id",
    "store_id": "id",
    "sum_date": "date",
    "sales_count": "count",
}


def validate_caller_fields(input_context: object) -> "dict[str, Any]":
    """Validate the caller contract. Raises CallerFieldError on any breach."""
    if input_context in (None, {}):
        return {}
    if not isinstance(input_context, dict):
        raise CallerFieldError("'input_context' must be a mapping")

    fields: dict[str, Any] = {}

    supplied = [k for k in _STORE_ALIASES if input_context.get(k) is not None]
    if supplied:
        fields["store_hint"] = _validate_pos_id(input_context[supplied[0]], supplied[0])

    for field, spec in (
        ("transaction", _TRANSACTION_SPEC),
        ("daily_sales", _DAILY_SALES_SPEC),
        ("product_sales", _PRODUCT_SALES_SPEC),
    ):
        if input_context.get(field) is not None:
            record = _validate_record(input_context[field], field, spec)
            if record:
                fields[field] = record

    return fields


class PreProcessNode(FunctionNode):
    """Validate the caller contract and shape the request for the inner graph."""

    # The outer backbone's SINGLE external trust gate. A real caller enters at
    # VERIFIED_EXTERNAL and the inner Smaregi call runs under this same
    # (unelevated) context, so the external gate lives HERE, not on the inner
    # API node. An under-trusted (ANONYMOUS) caller is denied at this gate
    # before any call.
    required_trust_level = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: "dict[str, Any]") -> "dict[str, Any]":
        user_input = state.get("user_input", "")
        input_context = state.get("input_context", {})  # read-only

        if not isinstance(user_input, str) or not user_input.strip():
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: user_input is empty or missing"],
            }

        # ── Instruction-override screen (template-owned, fail CLOSED) ────────
        # Runs on BOTH caller text channels before anything is carried
        # forward: a refusal leaves no validated_input, no store_hint and no
        # caller_fields for any downstream node. The screen lives in this
        # node's own execute() path - calling execute() directly still
        # refuses, so the guarantee does not depend on any platform gate being
        # present or configured on.
        if contains_instruction_override(user_input):
            emit_trace_event(
                "pre_process_validation_failed",
                {"reason": "instruction_override", "where": "user_input"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: request refused - instruction-override content in user_input"],
            }

        override_path = _find_instruction_override(input_context) if isinstance(input_context, (dict, list)) else None
        if override_path is not None:
            emit_trace_event(
                "pre_process_validation_failed",
                {"reason": "instruction_override", "where": override_path},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"PreProcessNode: request refused - instruction-override content in {override_path}"],
            }

        try:
            caller_fields = validate_caller_fields(input_context)
        except CallerFieldError as exc:
            # Fail closed, naming the field only - the rejected value is never
            # echoed into the log.
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"PreProcessNode: rejected caller input - {exc}"],
            }

        # Strip markup + cap length, then flag-and-redact before serialization.
        sanitized_input, _ = redact_sensitive(sanitize_query(user_input.strip()))

        store_hint = str(caller_fields.get("store_hint", ""))
        validated_input = json.dumps({"text": sanitized_input, "store_hint": store_hint})

        # Audit the shaped request - field presence only, never the text.
        emit_trace_event(
            "pre_process_complete",
            {
                "has_store_hint": bool(store_hint),
                "caller_fields": sorted(caller_fields),
            },
            state,
        )

        return {
            "validated_input": validated_input,
            "store_hint": store_hint,
            # Stored as a JSON string (the state contract keeps every value
            # msgpack-safe); the graph node reads it back at the boundary.
            "caller_fields": to_json(caller_fields),
            "status": AgentStatus.SUCCESS.value,
        }
