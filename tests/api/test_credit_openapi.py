"""OpenAPI contract of the credit read API (credit Group 6).

Every ``/v1/credits/*`` route of this group is documented: GET only, no
request body, the ``tenant_id`` scope parameter, its 200 schema, and the
refusals (401, 403 with the indistinguishable body, 503
``credit_unavailable``).  Amounts are integers of millicredits (no
``number`` anywhere in the credit schemas), and the ASAAS checkout the UI
calls accepts only ``offer_id``, ``method`` and an optional subscription
``coupon_code`` (no price, credits, quantity, builder or validity).
"""

from __future__ import annotations

from typing import Any

import pytest

ROUTES = {
    "/v1/credits/catalog": "CreditCatalogResponse",
    "/v1/credits/summary": "CreditSummaryResponse",
    "/v1/credits/balance": "CreditBalanceResponse",
    "/v1/credits/lots": "CreditLotsResponse",
    "/v1/credits/expiries": "CreditExpiriesResponse",
    "/v1/credits/debt": "CreditDebtResponse",
    "/v1/credits/reservations": "CreditReservationsResponse",
    "/v1/credits/thresholds": "CreditThresholdsResponse",
    "/v1/credits/state": "CreditStateResponse",
    "/v1/credits/consumption": "CreditConsumptionResponse",
}


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    from src.api.main import app

    document: dict[str, Any] = app.openapi()
    return document


def _ref(name: str) -> str:
    return f"#/components/schemas/{name}"


def _credit_schema_names(schema: dict[str, Any]) -> set[str]:
    """Every component reachable from the credit responses."""

    components = schema["components"]["schemas"]
    seen: set[str] = set()
    pending = list(ROUTES.values())
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                ref = node.get("$ref")
                if isinstance(ref, str):
                    pending.append(ref.rsplit("/", 1)[-1])
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(components[name])
    return seen


@pytest.mark.parametrize(("path", "model"), sorted(ROUTES.items()))
def test_every_credit_route_is_a_documented_get(
    schema: dict[str, Any], path: str, model: str
) -> None:
    operations = schema["paths"][path]
    assert set(operations) == {"get"}, path
    get = operations["get"]
    assert "requestBody" not in get
    assert get["tags"] == ["Credits"]
    assert get["summary"] and get["description"]
    ok = get["responses"]["200"]["content"]["application/json"]["schema"]
    assert ok == {"$ref": _ref(model)}
    for status in ("401", "403", "503"):
        assert status in get["responses"], (path, status)
    refused = get["responses"]["403"]["content"]["application/json"]["schema"]
    assert refused == {"$ref": _ref("CreditRefusal")}
    unavailable = get["responses"]["503"]["content"]["application/json"]["schema"]
    assert unavailable == {"$ref": _ref("CreditUnavailable")}
    parameters = {p["name"]: p for p in get.get("parameters", [])}
    assert parameters["tenant_id"]["in"] == "query"
    assert parameters["tenant_id"]["required"] is False
    # The scope is the only client input; the builder is never one.
    assert "builder_id" not in parameters


def test_no_credit_path_accepts_a_body_or_a_write(schema: dict[str, Any]) -> None:
    credit_paths = {
        path: operations
        for path, operations in schema["paths"].items()
        if path.startswith("/v1/credits")
    }
    assert set(ROUTES) <= set(credit_paths)
    for path, operations in credit_paths.items():
        assert set(operations) == {"get"}, path
        assert "requestBody" not in operations["get"], path


def test_credit_amounts_are_integer_millicredits(schema: dict[str, Any]) -> None:
    components = schema["components"]["schemas"]
    names = _credit_schema_names(schema)
    assert {"CreditBalanceResponse", "CreditLot", "CreditDebtItem"} <= names
    amounts = 0
    for name in names:
        for field, spec in components[name].get("properties", {}).items():
            types = {spec.get("type")} | {
                option.get("type") for option in spec.get("anyOf", [])
            }
            assert "number" not in types, (name, field)
            if field.endswith("_mc"):
                assert "integer" in types, (name, field)
                amounts += 1
    assert amounts >= 20


def test_the_state_and_mode_are_closed_enumerations(schema: dict[str, Any]) -> None:
    state = schema["components"]["schemas"]["CreditStateResponse"]["properties"]
    assert state["state"]["enum"] == [
        "active",
        "pending",
        "grace",
        "suspended",
        "deletion_due",
    ]
    assert state["mode"]["enum"] == ["observational", "enforced"]
    assert "export_deadline_at" in state


def test_the_checkout_the_ui_calls_accepts_no_price_quantity_or_builder(
    schema: dict[str, Any],
) -> None:
    request = schema["components"]["schemas"]["CheckoutRequest"]
    assert set(request["properties"]) == {"offer_id", "method", "coupon_code"}
    assert request["additionalProperties"] is False
    assert request["properties"]["method"]["enum"] == ["credit_card", "pix"]
    catalog = schema["components"]["schemas"]["CreditCatalogResponse"]["properties"]
    items = catalog["subscription_payment_methods"]["items"]
    assert items.get("enum", [items.get("const")]) == ["credit_card"]
