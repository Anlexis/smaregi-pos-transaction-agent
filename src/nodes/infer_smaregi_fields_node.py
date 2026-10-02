"""AgentCore Platform v1.0 - inner workflow Step 3: InferSmaregiFields.

Extracts the store id, transaction (receipt) id, product code, and ISO target
date from the (redacted) request and assembles validated Smaregi POS REST API
request parameters for the classified intent. Every id is taken only from an
explicit mention in the text, the caller-supplied store_hint, or a "Key:
value" request line - an unresolved id is left empty rather than invented
(never query the wrong store/transaction; the executor surfaces the miss as
status=error). Deterministic - no language model (docs/02_design.md,
"Deterministic classification and extraction").
"""

import re
from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import to_json

# A Smaregi id (store / transaction / product): short alphanumeric identifier
# (no spaces). Extracted candidates must also contain at least one digit so a
# following plain word ("store transactions" -> "transactions") never
# false-matches as an id.
_ID_SHAPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,19}$")
# Explicit id mentions in the request text, EN or JA
# ("transaction t-1001" / "receipt no: 42" / "取引番号 t-1001").
_TXN_IN_TEXT_RE = re.compile(
    r"(?:transaction|txn|receipt)s?\s+(?:id|number|no\.?)?\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9_-]{0,19})"
    r"|(?:取引|レシート)(?:番号|ID)?\s*[:：#]?\s*([A-Za-z0-9][A-Za-z0-9_-]{0,19})",
    re.IGNORECASE,
)
# "store s-01" / "shop code: 12" / "店舗コード s-01".
_STORE_IN_TEXT_RE = re.compile(
    r"(?:store|shop|branch)s?\s+(?:id|code|number|no\.?)?\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9_-]{0,19})"
    r"|(?:店舗|店)(?:コード|番号|ID)?\s*[:：#]?\s*([A-Za-z0-9][A-Za-z0-9_-]{0,19})",
    re.IGNORECASE,
)
# "product p-123" / "item code: 9001" / "商品コード p-123".
_PRODUCT_IN_TEXT_RE = re.compile(
    r"(?:product|item|sku)s?\s+(?:code|id|number|no\.?)?\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9_-]{0,19})"
    r"|(?:商品)(?:コード|番号|ID)?\s*[:：#]?\s*([A-Za-z0-9][A-Za-z0-9_-]{0,19})",
    re.IGNORECASE,
)
# Explicit ISO date only - no relative-date parsing.
_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
# "Key: value" request lines (ASCII or full-width colon). CJK ranges:
# hiragana/katakana + CJK unified ideographs.
_KV_RE = re.compile(r"^\s*([A-Za-z぀-ヿ一-鿿][\w \-぀-ヿ一-鿿]{0,40})[:：]\s*(.+?)\s*$")
_STORE_KEYS = ("store", "store id", "store code", "shop", "店舗")
_TXN_KEYS = ("transaction", "transaction id", "receipt", "取引")
_PRODUCT_KEYS = ("product", "product code", "item", "sku", "商品")
_DATE_KEYS = ("date", "day", "日付")


class InferSmaregiFieldsNode(FunctionNode):
    """Extract entities and assemble the Smaregi POS REST API request parameters."""

    # Inner domain node - derives fields from already-validated text; the
    # external trust gate lives on the outer backbone pre_process.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: "dict[str, Any]") -> "dict[str, Any]":
        text = state.get("validated_input", "") or ""
        intent = state.get("intent", "lookup_transaction") or "lookup_transaction"
        store_hint = state.get("store_hint", "") or ""

        if not text.strip():
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["InferSmaregiFieldsNode: missing validated_input"],
            }

        fields = self._parse_fields(text)
        store_id = self._resolve_store(text, store_hint, fields)
        transaction_id = self._resolve_id(_TXN_IN_TEXT_RE, text, fields, _TXN_KEYS)
        product_code = self._resolve_id(_PRODUCT_IN_TEXT_RE, text, fields, _PRODUCT_KEYS)
        target_date = self._resolve_date(text, fields)

        if intent == "summarize_daily_sales":
            payload = {"store_id": store_id, "sum_date": target_date}
        elif intent == "check_product_sales":
            payload = {"store_id": store_id, "product_code": product_code, "sum_date": target_date}
        else:  # lookup_transaction (read-only default)
            payload = {"store_id": store_id, "transaction_id": transaction_id}

        # Audit the assembled request shape - field signals only, not content.
        emit_trace_event(
            "infer_smaregi_fields_complete",
            {
                "intent": intent,
                "has_store_id": bool(store_id),
                "has_transaction_id": bool(transaction_id),
                "has_product_code": bool(product_code),
                "has_date": bool(target_date),
            },
            state,
        )

        return {
            "store_id": store_id,
            "transaction_id": transaction_id,
            "product_code": product_code,
            "target_date": target_date,
            "smaregi_payload": to_json(payload),
            "status": AgentStatus.SUCCESS.value,
        }

    # -- extraction -----------------------------------------------------------

    @staticmethod
    def _first_id_match(pattern: "re.Pattern[str]", text: str) -> str:
        """First regex candidate that contains a digit (id shape, never a plain word)."""
        for m in pattern.finditer(text):
            candidate = (m.group(1) or m.group(2) or "").strip()
            if candidate and any(ch.isdigit() for ch in candidate):
                return candidate
        return ""

    def _resolve_store(self, text: str, store_hint: str, fields: "list[tuple[str, str]]") -> str:
        """Explicit store only: text mention > id-shaped hint > 'Store:' field. Never invented."""
        found = self._first_id_match(_STORE_IN_TEXT_RE, text)
        if found:
            return found
        hint = store_hint.strip()
        if hint and _ID_SHAPE_RE.match(hint):
            return hint
        return self._field_id(fields, _STORE_KEYS)

    def _resolve_id(
        self, pattern: "re.Pattern[str]", text: str, fields: "list[tuple[str, str]]", keys: "tuple[str, ...]"
    ) -> str:
        found = self._first_id_match(pattern, text)
        if found:
            return found
        return self._field_id(fields, keys)

    @staticmethod
    def _field_id(fields: "list[tuple[str, str]]", keys: "tuple[str, ...]") -> str:
        for key, value in fields:
            if key.strip().lower() in keys and _ID_SHAPE_RE.match(value.strip()):
                return value.strip()
        return ""  # unresolved - left empty, never invented

    def _resolve_date(self, text: str, fields: "list[tuple[str, str]]") -> str:
        m = _DATE_RE.search(text)
        if m:
            return m.group(1)
        for key, value in fields:
            if key.strip().lower() in _DATE_KEYS:
                dm = _DATE_RE.search(value)
                if dm:
                    return dm.group(1)
        return ""  # no explicit date - executor treats as latest business day

    def _parse_fields(self, text: str) -> "list[tuple[str, str]]":
        """Return the [(key, value), ...] fields parsed from the request lines."""
        fields: list[tuple[str, str]] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            m = _KV_RE.match(stripped)
            if m:
                fields.append((m.group(1).strip(), m.group(2).strip()))
        return fields
