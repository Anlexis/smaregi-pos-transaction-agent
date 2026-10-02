"""AgentCore Platform v1.0 - inner workflow Step 2: ClassifyIntent.

Classifies the (redacted) request into one of lookup_transaction /
summarize_daily_sales / check_product_sales using a deterministic keyword
heuristic, so the template is testable and runnable without a language model
(docs/02_design.md, "Deterministic classification and extraction"). Low-confidence /
unknown falls back to the "lookup_transaction" default with a note - the
narrowest single-record read (the whole tool surface is read-only).
"""

from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

_VALID_INTENTS = ("lookup_transaction", "summarize_daily_sales", "check_product_sales")

# Deterministic keyword signals (checked in priority order, most specific
# first: a product-count request usually also mentions "sales", and a daily
# summary usually also says "show" - so product > daily > lookup).
_KEYWORDS = (
    (
        "check_product_sales",
        (
            "product",
            "item",
            "sku",
            "units sold",
            "sales count",
            "how many",
            "sold today",
            "sell count",
            "商品",
            "販売数",
            "個数",
            "売れた",
        ),
    ),
    (
        "summarize_daily_sales",
        (
            "daily sales",
            "day's sales",
            "sales summary",
            "summarize",
            "summary",
            "total sales",
            "revenue",
            "aggregate",
            "sales total",
            "売上",
            "日次",
            "集計",
            "日計",
        ),
    ),
    (
        "lookup_transaction",
        (
            "look up",
            "lookup",
            "find",
            "show",
            "get",
            "fetch",
            "retrieve",
            "search",
            "transaction",
            "receipt",
            "取引",
            "レシート",
            "照会",
            "検索",
            "参照",
            "確認",
        ),
    ),
)


class ClassifyIntentNode(FunctionNode):
    """Classify the request into a Smaregi POS-transaction operation intent."""

    # Inner domain node, read-only classification of already-redacted text -
    # the external trust gate lives on the outer backbone pre_process.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: "dict[str, Any]") -> "dict[str, Any]":
        text = state.get("validated_input", "") or ""
        if not text:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ClassifyIntentNode: missing validated_input"],
            }

        intent = self._classify_via_keywords(text)

        note: list[str] = []
        if intent not in _VALID_INTENTS:
            note = [
                "ClassifyIntentNode: low-confidence classification, "
                "defaulted to lookup_transaction (single-record read)"
            ]
            intent = "lookup_transaction"

        # Audit the classification decision - intent label only, never the text.
        emit_trace_event(
            "classify_intent_complete",
            {"intent": intent, "defaulted": bool(note)},
            state,
        )

        result = {"intent": intent, "status": AgentStatus.SUCCESS.value}
        if note:
            result["error_log"] = note  # non-fatal note; status stays SUCCESS
        return result

    # -- classification -------------------------------------------------------

    def _classify_via_keywords(self, text: str) -> str:
        low = text.lower()
        for intent, words in _KEYWORDS:
            if any(w in low for w in words):
                return intent
        # No signal at all: fall through to the read-only default via the
        # _VALID_INTENTS guard in execute() (returns a sentinel outside the set).
        return "unknown"
