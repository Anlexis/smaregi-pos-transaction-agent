"""AgentCore Platform v1.0 - inner workflow Step 1: ValidateInput.

Rejects empty / non-request input, refuses instruction-override content, and
flag-and-redacts email addresses / access-token-like strings from the inbound
text before anything is logged.

A POS-transaction request legitimately names stores, products and receipt ids
(the framework PII mask additionally masks emails/phones/names in the request
text), so the sensitive-string handling is flag-and-redact for safe logging,
not a hard reject. The hard rejects are the empty / non-request guard and the
instruction-override screen.

The screen runs here as well as on the outer backbone pre_process. That is not
redundant: this graph is a public class that can be invoked directly, and a
guarantee that only holds when a particular outer node ran in front is not a
guarantee.
"""

import json
from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import to_json
from src.services.security import contains_instruction_override, redact_sensitive

# Minimum signal that the text is a real request rather than noise.
_MIN_LEN = 3


class ValidateInputNode(FunctionNode):
    """Validate and flag-and-redact the inbound POS-transaction request."""

    # Inner domain node - the external trust gate lives on the outer backbone
    # pre_process (VERIFIED_EXTERNAL); the caller context is forwarded unchanged.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: "dict[str, Any]") -> "dict[str, Any]":
        raw = state.get("validated_input") or state.get("user_input") or ""

        # The outer graph serialized the request into a JSON string; accept both
        # the serialized shape and a bare string for direct unit testing.
        text = raw
        store_hint = state.get("store_hint", "")
        if isinstance(raw, str) and raw.strip().startswith("{"):
            try:
                obj = json.loads(raw)
                text = obj.get("text", "")
                store_hint = obj.get("store_hint", store_hint)
            except (ValueError, TypeError):
                text = raw

        if not isinstance(text, str) or len(text.strip()) < _MIN_LEN:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ValidateInputNode: empty or non-request input"],
            }

        # Fail CLOSED on directives aimed at the model. Nothing is carried
        # forward on a refusal - no validated_input, no store_hint.
        if contains_instruction_override(text):
            emit_trace_event(
                "validate_input_refused",
                {"reason": "instruction_override"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ValidateInputNode: request refused - instruction-override content"],
            }

        # Deterministic flag-and-redact (before any logging). The flags list is
        # local per invocation - never a module global (no cross-invoke leak).
        redacted, flags = redact_sensitive(text)

        # Audit the scan outcome - redaction flags only, never the inbound text.
        emit_trace_event(
            "validate_input_complete",
            {"has_store_hint": bool(store_hint), "redaction_flags": flags},
            state,
        )

        return {
            "validated_input": redacted.strip(),
            "store_hint": store_hint,
            "redaction_flags": to_json(flags),
            "status": AgentStatus.SUCCESS.value,
        }
