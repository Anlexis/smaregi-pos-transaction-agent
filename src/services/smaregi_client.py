"""AgentCore Platform v1.0 - Smaregi POS REST API client.

Service layer: a thin wrapper around the Smaregi (cloud POS SaaS) REST API
transaction endpoints. Contains NO business logic, NO routing, and NO
credentials - the access token is passed in per call by the node (which reads
it via ctx.secrets). This module imports no framework internals - pure stdlib.

The tool surface is READ-ONLY by design: transaction lookup, daily-sales
summary, and product-sales count are all GET operations - no create/update/
void of POS transactions.

DEFAULT TRANSPORT (deliberate, documented):
    The DEFAULT transport is a deterministic, NETWORK-FREE stub. It returns the
    documented Smaregi POS response shapes (a transaction head record for
    lookups; daily-sales / product-sales aggregation shapes with synthetic
    totals derived from the request) so the pipeline is runnable and testable
    without a live Smaregi contract or an HTTP client library - it does NOT
    perform a live Smaregi call. The template never fakes a live call; the
    limitation is stated instead.

    To perform real Smaregi calls, inject a live ``get`` transport at
    construction time; the method contracts and parameter shapes follow the
    Smaregi POS REST API, so no business-logic change is needed to go live. A
    live transport also requires a real access token (see CallSmaregiApiNode -
    the stub runs without one because no request ever leaves the process).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

# A transport callable: (url, headers, params) -> (status_code, response_dict)
Transport = Callable[[str, "dict[str, str]", "dict[str, Any]"], "tuple[int, dict[str, Any]]"]

_BASE_URL = "https://api.smaregi.jp/pos"


class SmaregiApiError(Exception):
    """Raised when the Smaregi REST API returns a non-2xx status."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(f"Smaregi API error {status_code}: {message}")


class SmaregiClient:
    """Smaregi POS transaction read-only client.

    Args:
        base_url: Smaregi POS API base URL (default https://api.smaregi.jp/pos).
        get: optional injected GET transport (tests or a live client).
            When none is injected, a deterministic NETWORK-FREE stub is used
            (see the module docstring - it returns the documented shape without
            a live Smaregi call).
    """

    def __init__(
        self,
        base_url: str = _BASE_URL,
        *,
        get: Transport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._get = get

    # -- transport mode --------------------------------------------------------

    @property
    def uses_stub_transport(self) -> bool:
        """True when NO live transport is injected (the network-free default)."""
        return self._get is None

    # -- auth ----------------------------------------------------------------

    def _headers(self, api_token: str) -> "dict[str, str]":
        """Build the Smaregi POS REST API auth headers.

        api_token is supplied per-call by the node (from ctx.secrets); it is
        never persisted on the instance or logged.
        """
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_token}",
        }

    # -- deterministic stub transport (default; NO network) -------------------

    def _stub_transport(
        self, url: str, headers: "dict[str, str]", params: "dict[str, Any]"
    ) -> "tuple[int, dict[str, Any]]":
        """Deterministic, network-free stub - returns the documented Smaregi shape.

        NOT a live call. Synthetic ids/amounts are derived from the request so
        the response is stable and inspectable. See the module docstring for how
        to inject a live transport.
        """
        seed = url + "|" + json.dumps(params, sort_keys=True, ensure_ascii=False, default=str)
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        op = params.get("_smaregi_op")
        if op == "transaction":
            txn_id = str(params.get("transaction_id", "")) or f"t-{digest[:8]}"
            # Documented GET /transactions/{id} head shape (subset).
            return 200, {
                "transactionHeadId": txn_id,
                "storeId": str(params.get("store_id", "")) or f"s-{digest[8:10]}",
                "transactionDateTime": "1970-01-01T00:00:00",
                "total": int(digest[:3], 16),  # synthetic, <= 4 digits
                "details": [],
                "_stub": True,  # marks the network-free stub response
            }
        if op == "daily_sales":
            # Documented daily-sales aggregation shape (subset).
            return (
                200,
                {
                    "storeId": str(params.get("store_id", "")),
                    "sumDate": str(params.get("sum_date", "")) or "latest",
                    "salesTotal": int(digest[:3], 16),  # synthetic, <= 4 digits
                    "transactionCount": int(digest[3:5], 16),  # synthetic, <= 3 digits
                    "_stub": True,  # marks the network-free stub response
                },
            )
        # product_sales - documented product-sales aggregation shape (subset).
        return 200, {
            "productCode": str(params.get("product_code", "")) or f"p-{digest[:6]}",
            "storeId": str(params.get("store_id", "")) or "all",
            "sumDate": str(params.get("sum_date", "")) or "latest",
            "salesCount": int(digest[:2], 16),  # synthetic, <= 3 digits
            "_stub": True,  # marks the network-free stub response
        }

    def _resolve(self, injected: Transport | None) -> Transport:
        return injected or self._stub_transport

    # -- public API (read-only) ----------------------------------------------

    def get_transaction(self, transaction_id: str, store_id: str, api_token: str) -> "dict[str, Any]":
        """GET /transactions/{id} - look up one POS transaction head record.

        Returns the parsed transaction head dict (``transactionHeadId``,
        ``storeId``, ``total``, ...). Raises SmaregiApiError on non-2xx.
        """
        url = f"{self._base_url}/transactions/{transaction_id}"
        transport = self._resolve(self._get)
        status, body = transport(
            url,
            self._headers(api_token),
            {"_smaregi_op": "transaction", "transaction_id": transaction_id, "store_id": store_id},
        )
        if not (200 <= status < 300):
            raise SmaregiApiError(status, _err_message(body))
        return body

    def get_daily_sales(self, store_id: str, sum_date: str, api_token: str) -> "dict[str, Any]":
        """GET /transactions daily aggregation - summarize one store's daily sales.

        ``sum_date`` is an ISO date (YYYY-MM-DD) or empty for the latest
        business day. Returns the parsed aggregation dict (``salesTotal``,
        ``transactionCount``, ...). Raises SmaregiApiError on a non-2xx status.
        """
        url = f"{self._base_url}/transactions"
        transport = self._resolve(self._get)
        status, body = transport(
            url,
            self._headers(api_token),
            {"_smaregi_op": "daily_sales", "store_id": store_id, "sum_date": sum_date},
        )
        if not (200 <= status < 300):
            raise SmaregiApiError(status, _err_message(body))
        return body

    def get_product_sales(self, product_code: str, store_id: str, sum_date: str, api_token: str) -> "dict[str, Any]":
        """GET /transactions product aggregation - a product's sales count.

        ``store_id`` empty aggregates across stores; ``sum_date`` empty means
        the latest business day. Returns the parsed aggregation dict
        (``salesCount``, ...). Raises SmaregiApiError on a non-2xx status.
        """
        url = f"{self._base_url}/transactions"
        transport = self._resolve(self._get)
        status, body = transport(
            url,
            self._headers(api_token),
            {"_smaregi_op": "product_sales", "product_code": product_code, "store_id": store_id, "sum_date": sum_date},
        )
        if not (200 <= status < 300):
            raise SmaregiApiError(status, _err_message(body))
        return body


def _err_message(body: Any) -> str:
    """Extract a human-readable error message from a Smaregi error body."""
    if isinstance(body, dict):
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            return "; ".join(str(e) for e in errors)
        msg = body.get("message")
        if msg:
            return str(msg)
    return str(body)
