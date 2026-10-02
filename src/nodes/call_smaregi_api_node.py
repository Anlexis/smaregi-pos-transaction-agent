"""AgentCore Platform v1.0 - inner workflow Step 4: CallSmaregiApi (tool call).

Performs the read-only lookup/summary/count call against the Smaregi POS REST
API via src/services/smaregi_client.py, and renders the record label the caller
reads back.

Where the data comes from:
  - when the caller supplied the matching POS record on the request's
    structured channel (`input_context.transaction` / `.daily_sales` /
    `.product_sales`, validated and bounded by the outer PreProcessNode), the
    pipeline computes the response from THAT data - real figures, the caller's
    own;
  - otherwise it falls back to the built-in network-free transport, which
    returns the documented Smaregi response shapes so the pipeline is runnable
    end to end before a live contract exists (docs/02, "Transport").

External output schema (docs/02, "External output schema"):
  - monetary figures are AGGREGATES, rendered rounded to the nearest 1,000;
  - a single transaction's exact total is a raw line item and is NEVER
    rendered - the lookup returns the record's identity, store and timestamp;
  - counts are structural and render verbatim (bounded upstream so they cannot
    be mistaken for an unmarked monetary run at the output boundary);
  - every identifier renders behind a `#` or inside a `smaregi://` reference,
    so the output boundary can tell an identifier from an amount.

Security posture:
  Trust: required_trust_level = ANONYMOUS. The single external trust gate
       lives on the OUTER backbone pre_process (VERIFIED_EXTERNAL), not on this
       inner node. GraphNode.execute() passes the caller's InvocationContext
       into the inner subgraph UNCHANGED (no trust elevation), so a real
       external caller runs this call under its own VERIFIED_EXTERNAL context;
       declaring INTERNAL here would deny that already-gated external caller
       before the call ever runs. The node therefore stays ANONYMOUS.
  Credentials: the integration token is read via
       ctx.secrets.get("SMAREGI_TOKEN") (InvocationContext.from_state(state)) -
       never os.environ, never stored in state. While the network-free built-in
       transport is active a missing token is tolerated (a sentinel placeholder
       is used - it is never sent anywhere because no request leaves the
       process); with a LIVE transport injected, a missing token is a hard
       status=error - a real API is never called unauthenticated.
  Audit: emit_trace_event() is called on the success path - a call against an
       external POS system; HTTP 4xx/5xx surfaces as status=error + error_log
       (no silent pass).

Configuration: this node takes NO constructor arguments (SDK v1 nodes are
no-arg). Smaregi settings (base_url) arrive as the JSON `smaregi_config` state
field - injected by the inner graph's _extra_initial_state() from the runtime
section forwarded by SmaregiWorkflowGraphNode._parent_config() - or via the
optional `config["configurable"]["smaregi"]` argument for direct invocation.
The client is constructed locally per call (no module-global mutation).
"""

from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json
from src.services.smaregi_client import SmaregiApiError, SmaregiClient

_SECRET_KEY = "SMAREGI_TOKEN"
# Placeholder handed to the network-free built-in transport when no secret is
# provisioned. Never sent over any network (that transport performs no I/O) and
# never written to state or logs.
_STUB_PLACEHOLDER = "no-credential-network-free-transport"

# Reporting currency of the rendered aggregates. Rendering the marker is what
# lets the output boundary tell a monetary figure from a bare number.
_CURRENCY = "JPY"
# Approved external precision: monetary figures are reported in units of 1,000
# (docs/02 "External output schema"). The renderer ROUNDS onto this grid; the
# output gate in post_process independently ENFORCES it.
_EXTERNAL_ROUND_UNIT = 1000


def _render_amount(value: Any) -> str:
    """Render a monetary aggregate on the approved 1,000 grid, with its marker.

    Returns "" when the figure is absent or not a real number - a missing
    aggregate is omitted from the label rather than rendered as a zero the
    source never reported.
    """
    if isinstance(value, bool) or value is None:
        return ""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return ""
    if amount != amount or amount in (float("inf"), float("-inf")):  # non-finite
        return ""
    snapped = round(amount / _EXTERNAL_ROUND_UNIT) * _EXTERNAL_ROUND_UNIT
    return f"{_CURRENCY} {snapped:,d}"


class CallSmaregiApiNode(FunctionNode):
    """Look up a transaction / summarize daily sales / check a product's sales count."""

    # The external trust gate is enforced UPSTREAM on the outer backbone
    # pre_process (VERIFIED_EXTERNAL). This inner node runs under the caller's
    # UNELEVATED context (GraphNode does not elevate trust for the subgraph), so
    # it must stay ANONYMOUS - declaring INTERNAL would deny a real external
    # caller before the call runs.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: "dict[str, Any]", config: "dict[str, Any] | None" = None) -> "dict[str, Any]":
        payload = from_json(state.get("smaregi_payload"), None)
        if not payload:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["CallSmaregiApiNode: missing smaregi_payload"],
            }

        intent = state.get("intent", "lookup_transaction") or "lookup_transaction"
        # The validated caller contract, bridged in from the outer graph.
        caller = state.get("input_context") or {}
        if not isinstance(caller, dict):
            caller = {}

        # Settings: runtime section from state (graph-injected), overridable via
        # an explicit config["configurable"]["smaregi"] for direct invocation.
        # Merged into a LOCAL dict - module globals are never mutated.
        settings = dict(from_json(state.get("smaregi_config"), {}) or {})
        override = ((config or {}).get("configurable") or {}).get("smaregi") or {}
        settings.update(override)

        # Client built locally per call; with no injected transport it uses the
        # deterministic NETWORK-FREE built-in default (documented, docs/02).
        base_url = str(settings.get("base_url", "") or "").strip()
        client = SmaregiClient(base_url=base_url) if base_url else SmaregiClient()

        # Token from the bound secret provider - never os.environ / state.
        ctx = InvocationContext.from_state(state)
        api_token = ctx.secrets.get(_SECRET_KEY)
        if api_token is None:
            if client.uses_stub_transport:
                # No request leaves the process, so run with a non-credential
                # placeholder (see the module docstring).
                api_token = _STUB_PLACEHOLDER
            else:
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": [
                        f"CallSmaregiApiNode: secret {_SECRET_KEY} unavailable - "
                        "refusing to call a live transport unauthenticated"
                    ],
                }

        store_id = state.get("store_id", "") or str(payload.get("store_id", "") or "")
        transaction_id = state.get("transaction_id", "") or str(payload.get("transaction_id", "") or "")
        product_code = state.get("product_code", "") or str(payload.get("product_code", "") or "")
        sum_date = state.get("target_date", "") or str(payload.get("sum_date", "") or "")

        try:
            if intent == "lookup_transaction":
                outcome = self._lookup_transaction(client, caller, transaction_id, store_id, api_token)
            elif intent == "summarize_daily_sales":
                outcome = self._summarize_daily_sales(client, caller, store_id, sum_date, api_token)
            elif intent == "check_product_sales":
                outcome = self._check_product_sales(client, caller, product_code, store_id, sum_date, api_token)
            else:
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": [f"CallSmaregiApiNode: unknown intent '{intent}'"],
                }
        except SmaregiApiError as exc:
            # HTTP status only. A live tenant's error body is unbounded
            # third-party text that can echo the record it refused (receipt,
            # store, product), and error_log rides the caller-facing error
            # envelope in post_process - so the closed-set signal travels, the
            # body does not.
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"CallSmaregiApiNode: Smaregi API error {exc.status_code}"],
            }
        except Exception as exc:  # transport failure - no silent pass
            # Exception TYPE only, for the same reason: a transport error
            # string can carry the request URL and the record id.
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"CallSmaregiApiNode: Smaregi call failed ({type(exc).__name__})"],
            }

        if "error_log" in outcome:
            return {"status": AgentStatus.ERROR.value, "error_log": outcome["error_log"]}

        # Audit the tool call - intent + presence signals only, never
        # customer-attributable sales content or credentials.
        emit_trace_event(
            "call_smaregi_api_complete",
            {
                "intent": intent,
                "has_record_id": bool(outcome["record_id"]),
                "caller_supplied_record": bool(outcome["caller_supplied"]),
            },
            state,
        )

        return {
            "record_id": outcome["record_id"],
            "record_ref": outcome["record_ref"],
            "record_label": outcome["record_label"],
            "store_id": outcome.get("store_id", store_id),
            "status": AgentStatus.SUCCESS.value,
        }

    # -- per-intent handlers --------------------------------------------------

    def _lookup_transaction(
        self, client: SmaregiClient, caller: "dict[str, Any]", transaction_id: str, store_id: str, api_token: str
    ) -> "dict[str, Any]":
        raw_record = caller.get("transaction")
        record: dict[str, Any] = raw_record if isinstance(raw_record, dict) else {}
        transaction_id = transaction_id or str(record.get("transaction_id", "") or "")
        if not transaction_id:
            return {"error_log": ["CallSmaregiApiNode: unresolved transaction id - cannot look up transaction"]}
        if record:
            resp: dict[str, Any] = dict(record)
            resp.setdefault("transactionHeadId", record.get("transaction_id", transaction_id))
            caller_supplied = True
        else:
            resp = client.get_transaction(transaction_id, store_id, api_token) or {}
            caller_supplied = False
        record_id = str(resp.get("transactionHeadId", "")) or transaction_id
        resolved_store = store_id or str(resp.get("store_id", resp.get("storeId", "")) or "")
        record_ref = f"smaregi://transactions/{record_id}"
        # The receipt's exact total is a raw line item: reported to the caller
        # only as the identity of the record it belongs to, never as a figure.
        label = f"POS transaction #{record_id}"
        if resolved_store:
            label += f" at store #{resolved_store}"
        return {
            "record_id": record_id,
            "record_ref": record_ref,
            "record_label": label,
            "store_id": resolved_store,
            "caller_supplied": caller_supplied,
        }

    def _summarize_daily_sales(
        self, client: SmaregiClient, caller: "dict[str, Any]", store_id: str, sum_date: str, api_token: str
    ) -> "dict[str, Any]":
        raw_record = caller.get("daily_sales")
        record: dict[str, Any] = raw_record if isinstance(raw_record, dict) else {}
        store_id = store_id or str(record.get("store_id", "") or "")
        if not store_id:
            return {"error_log": ["CallSmaregiApiNode: unresolved store id - cannot summarize daily sales"]}
        if record:
            resp: dict[str, Any] = {
                "sumDate": record.get("sum_date", sum_date),
                "salesTotal": record.get("sales_total"),
                "transactionCount": record.get("transaction_count"),
            }
            caller_supplied = True
        else:
            resp = client.get_daily_sales(store_id, sum_date, api_token) or {}
            caller_supplied = False
        sum_label = str(resp.get("sumDate", "") or "") or sum_date or "latest"
        record_id = f"daily-{store_id}-{sum_label}"
        record_ref = f"smaregi://stores/{store_id}/daily-sales/{sum_label}"
        label = f"daily sales for store #{store_id} on {sum_label}"
        # Aggregate figures only, on the approved grid.
        total = _render_amount(resp.get("salesTotal"))
        count = resp.get("transactionCount")
        parts = []
        if total:
            parts.append(f"total {total}")
        if isinstance(count, int) and not isinstance(count, bool):
            parts.append(f"{count} transactions")
        if parts:
            label += " (" + ", ".join(parts) + ")"
        return {
            "record_id": record_id,
            "record_ref": record_ref,
            "record_label": label,
            "store_id": store_id,
            "caller_supplied": caller_supplied,
        }

    def _check_product_sales(
        self,
        client: SmaregiClient,
        caller: "dict[str, Any]",
        product_code: str,
        store_id: str,
        sum_date: str,
        api_token: str,
    ) -> "dict[str, Any]":
        raw_record = caller.get("product_sales")
        record: dict[str, Any] = raw_record if isinstance(raw_record, dict) else {}
        product_code = product_code or str(record.get("product_code", "") or "")
        if not product_code:
            return {"error_log": ["CallSmaregiApiNode: unresolved product code - cannot check product sales"]}
        store_scope = store_id or str(record.get("store_id", "") or "") or "all"
        if record:
            resp: dict[str, Any] = {
                "productCode": record.get("product_code", product_code),
                "sumDate": record.get("sum_date", sum_date),
                "salesCount": record.get("sales_count"),
            }
            caller_supplied = True
        else:
            resp = client.get_product_sales(product_code, store_id, sum_date, api_token) or {}
            caller_supplied = False
        sum_label = str(resp.get("sumDate", "") or "") or sum_date or "latest"
        record_id = str(resp.get("productCode", "") or "") or product_code
        record_ref = f"smaregi://stores/{store_scope}/product-sales/{record_id}"
        label = f"product #{record_id} at store #{store_scope} on {sum_label}"
        count = resp.get("salesCount")
        if isinstance(count, int) and not isinstance(count, bool):
            label += f" (sales count {count})"
        return {
            "record_id": record_id,
            "record_ref": record_ref,
            "record_label": label,
            "store_id": store_id,
            "caller_supplied": caller_supplied,
        }
