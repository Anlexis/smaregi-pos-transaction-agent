# CMN-C2-285 - Unit tests: SmaregiClient service (Smaregi POS REST API shape)
# Adapted from the peer template tool-calling golden (kaonavi_client -> smaregi_client).
# Pure service layer (stdlib-only, no framework imports) - plain function tests.
# The v1 tool surface is READ-ONLY: transaction lookup / daily-sales summary /
# product-sales count are all GET operations.

import pytest

from src.services.smaregi_client import SmaregiApiError, SmaregiClient


def test_get_transaction_success_with_injected_get():
    captured = {}

    def get(url, headers, params):
        captured["url"] = url
        captured["headers"] = headers
        captured["params"] = params
        return 200, {"transactionHeadId": "t-1001", "storeId": "s-01", "total": 980}

    client = SmaregiClient("https://smaregi.example.test/pos/", get=get)
    resp = client.get_transaction("t-1001", "s-01", "tok123")
    assert resp["transactionHeadId"] == "t-1001"
    # base_url trailing slash is normalized; the id rides in the path.
    assert captured["url"] == "https://smaregi.example.test/pos/transactions/t-1001"
    # Smaregi POS REST API auth: the per-call token travels as a Bearer header.
    assert captured["headers"]["Authorization"] == "Bearer tok123"
    assert captured["headers"]["Content-Type"] == "application/json"
    assert captured["params"]["transaction_id"] == "t-1001"
    assert captured["params"]["store_id"] == "s-01"


def test_get_daily_sales_success_with_injected_get():
    captured = {}

    def get(url, headers, params):
        captured["url"] = url
        captured["params"] = params
        return 200, {"storeId": "s-01", "sumDate": "2026-07-01", "salesTotal": 4200, "transactionCount": 31}

    client = SmaregiClient("https://smaregi.example.test/pos", get=get)
    resp = client.get_daily_sales("s-01", "2026-07-01", "tok")
    assert resp["salesTotal"] == 4200
    assert captured["url"] == "https://smaregi.example.test/pos/transactions"
    assert captured["params"]["store_id"] == "s-01"
    assert captured["params"]["sum_date"] == "2026-07-01"


def test_get_product_sales_success_with_injected_get():
    captured = {}

    def get(url, headers, params):
        captured["url"] = url
        captured["params"] = params
        return 200, {"productCode": "p-123", "storeId": "s-01", "sumDate": "2026-07-01", "salesCount": 12}

    client = SmaregiClient("https://smaregi.example.test/pos", get=get)
    resp = client.get_product_sales("p-123", "s-01", "2026-07-01", "tok")
    assert resp["salesCount"] == 12
    assert captured["url"] == "https://smaregi.example.test/pos/transactions"
    assert captured["params"]["product_code"] == "p-123"


def test_non_2xx_raises_smaregi_api_error():
    def get(url, headers, params):
        return 404, {"errors": ["transaction not found"]}

    client = SmaregiClient("https://smaregi.example.test/pos", get=get)
    with pytest.raises(SmaregiApiError) as exc:
        client.get_transaction("t-9999", "s-01", "tok")
    assert exc.value.status_code == 404
    assert "transaction not found" in str(exc.value)


def test_non_2xx_message_field_fallback():
    def get(url, headers, params):
        return 500, {"message": "internal error"}

    client = SmaregiClient("https://smaregi.example.test/pos", get=get)
    with pytest.raises(SmaregiApiError) as exc:
        client.get_daily_sales("s-01", "", "tok")
    assert "internal error" in str(exc.value)


def test_default_stub_transport_lookup_shape():
    # No transport injected -> deterministic, network-free v1 stub.
    client = SmaregiClient()
    assert client.uses_stub_transport is True
    resp = client.get_transaction("t-1001", "s-01", "tok")
    assert resp.get("_stub") is True
    assert resp["transactionHeadId"] == "t-1001"
    assert resp["storeId"] == "s-01"
    # Synthetic amount stays small (<= 4 digits) - PII-safe for masked fields.
    assert isinstance(resp["total"], int)
    assert 0 <= resp["total"] <= 9999


def test_default_stub_transport_daily_sales_shape():
    client = SmaregiClient()
    resp = client.get_daily_sales("s-01", "2026-07-01", "tok")
    assert resp.get("_stub") is True
    assert resp["storeId"] == "s-01"
    assert resp["sumDate"] == "2026-07-01"
    assert isinstance(resp["salesTotal"], int)
    assert isinstance(resp["transactionCount"], int)


def test_default_stub_transport_daily_sales_empty_date_is_latest():
    client = SmaregiClient()
    resp = client.get_daily_sales("s-01", "", "tok")
    assert resp["sumDate"] == "latest"


def test_default_stub_transport_product_sales_shape():
    client = SmaregiClient()
    resp = client.get_product_sales("p-123", "", "", "tok")
    assert resp.get("_stub") is True
    assert resp["productCode"] == "p-123"
    assert resp["storeId"] == "all"
    assert isinstance(resp["salesCount"], int)


def test_stub_transport_is_deterministic():
    client = SmaregiClient()
    first = client.get_transaction("t-1001", "s-01", "tok")
    second = client.get_transaction("t-1001", "s-01", "tok")
    assert first == second


def test_injected_transport_disables_stub_flag():
    client = SmaregiClient(get=lambda url, headers, params: (200, {"transactionHeadId": "t-1"}))
    assert client.uses_stub_transport is False
