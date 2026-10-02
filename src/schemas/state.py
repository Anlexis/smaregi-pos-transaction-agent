"""AgentCore Platform v1.0 - CMN-C2-285 Smaregi POS Transaction Agent state."""

# State must be a flat TypedDict - never a Pydantic BaseModel. Graph
# checkpoints use msgpack serialization; Pydantic objects (and nested
# dict/list containers) are not msgpack-safe. Extend AgentState with
# agent-specific fields only, and declare every domain field NotRequired[...]
# (fields are absent until their producer node writes them). smaregi_payload /
# smaregi_config / redaction_flags are dicts/lists at the point of use but are
# stored in State as JSON strings via to_json/from_json below. Do NOT add
# credentials, secrets, or Pydantic models. The Smaregi access
# token is NEVER stored here - it is read via ctx.secrets in
# CallSmaregiApiNode.

from __future__ import annotations

import json
from typing import Any, NotRequired, Optional

from framework.schemas.agent_state import AgentState


def to_json(value: Any) -> Optional[str]:
    """Serialize a list/dict State value to a compact JSON string (msgpack-safe).

    Returns None for None so the field stays a true Optional[str].
    """
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def from_json(value: Any, default: Any) -> Any:
    """Deserialize a JSON-string State value back to its list/dict form.

    Tolerant by design: None/empty -> default; an already-native list/dict (e.g. a value
    supplied directly in a unit test) passes through unchanged; a malformed string -> default.
    """
    if value is None or value == "":
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class State(AgentState):
    """Smaregi POS Transaction agent state.

    Shared fields (user_input, validated_input, intent, result, status,
    formatted_output, session_id, node_history, error_log, correlation_id,
    trace_id, hitl_*, etc.) are inherited from AgentState and NOT re-declared.
    Only Smaregi-workflow fields are added below, all NotRequired (the state
    contract). All values are JSON/msgpack-serializable primitives -
    the Smaregi access token is NEVER stored here (accessed via ctx.secrets).
    """

    # Caller-supplied target hint (store id/code from input_context / the
    # request envelope). Never inferred; resolution to a Smaregi store id is
    # explicit-only (pass-through when the hint or request text already
    # carries an id).
    store_hint: NotRequired[str]
    store_id: NotRequired[str]  # resolved Smaregi store id

    # JSON - the VALIDATED caller contract produced by PreProcessNode
    # (bounded ids, ISO dates, finite amounts and counts). Carried across
    # the outer/inner graph boundary by the caller-context bridge; never
    # the raw request body.
    caller_fields: NotRequired[Optional[str]]

    # ValidateInput (deterministic sensitive-string scan)
    # JSON list[str] of patterns redacted from the text before logging
    # (stored as a JSON string; (de)serialize via to_json/from_json).
    redaction_flags: NotRequired[Optional[str]]

    # InferSmaregiFields (explicit-only extraction - never invented)
    transaction_id: NotRequired[str]  # transaction (receipt) head id
    product_code: NotRequired[str]  # product code for sales-count checks
    target_date: NotRequired[str]  # ISO date (YYYY-MM-DD); empty -> latest
    # JSON - assembled Smaregi POS REST API request parameters (stored as a
    # JSON string, not a native dict; (de)serialize via to_json/from_json).
    smaregi_payload: NotRequired[Optional[str]]

    # Runtime `smaregi:` section forwarded by _parent_config() and injected
    # by the inner graph's _extra_initial_state() (JSON string).
    smaregi_config: NotRequired[Optional[str]]

    # CallSmaregiApi
    record_id: NotRequired[str]  # transaction head id / summary id
    record_ref: NotRequired[str]  # human-readable reference (smaregi://transactions/<id>)
    record_label: NotRequired[str]  # human-readable label of the affected record

    # Confirm
    confirmation: NotRequired[str]  # human-readable confirmation message
