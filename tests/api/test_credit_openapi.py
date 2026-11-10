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
    assert state["pending_reasons"]["items"]["enum"] == [
        "quarantine",
        "standalone",
        "reconciliation",
    ]
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


# ---------------------------------------------------------------------------
# creditos-cortesia: the courtesy routes of the credit admin API.
# ---------------------------------------------------------------------------

_COURTESY = "/v1/admin/credits/courtesy-grants"


def test_the_courtesy_routes_are_documented(schema: dict[str, Any]) -> None:
    grants = schema["paths"][_COURTESY]
    removals = schema["paths"][f"{_COURTESY}/{{lot_id}}/removals"]
    assert set(grants) == {"get", "post"}
    assert set(removals) == {"post"}
    for operation, created, refusals in (
        (grants["post"], "201", {"401", "403", "404", "409", "422", "503"}),
        (removals["post"], "201", {"401", "403", "404", "409", "422", "503"}),
        (grants["get"], "200", {"401", "403", "404", "422", "503"}),
    ):
        assert operation["tags"] == ["Credits Admin"]
        assert operation["summary"] and operation["description"]
        assert set(operation["responses"]) == {created} | refusals
        for status in refusals - {"401", "422"}:
            body = operation["responses"][status]["content"]["application/json"]
            assert body["schema"] == {"$ref": _ref("RefusalResponse")}, status
    for operation in (grants["post"], removals["post"]):
        # The app adds its global headers (X-API-Key, X-Request-ID) to every
        # operation; the courtesy routes add only the Idempotency-Key.
        [header] = [
            p for p in operation["parameters"] if p["name"] == "Idempotency-Key"
        ]
        assert header["in"] == "header"
        assert header["required"] is True
        assert (header["schema"]["minLength"], header["schema"]["maxLength"]) == (
            1,
            200,
        )
    assert "credit_clock_ahead" in grants["post"]["responses"]["503"]["description"]
    [query] = [p for p in grants["get"]["parameters"] if p["in"] == "query"]
    assert (query["name"], query["in"], query["required"]) == (
        "builder_id",
        "query",
        True,
    )


def test_the_courtesy_bodies_are_closed_and_bounded(schema: dict[str, Any]) -> None:
    components = schema["components"]["schemas"]
    grant = components["CourtesyGrantCreate"]
    assert grant["additionalProperties"] is False
    assert set(grant["required"]) == {"builder_id", "credits", "reason"}
    credits = grant["properties"]["credits"]
    assert (credits["type"], credits["minimum"], credits["maximum"]) == (
        "integer",
        1,
        100_000,
    )
    reason = grant["properties"]["reason"]
    assert (reason["minLength"], reason["maxLength"]) == (1, 500)
    removal = components["CourtesyRemovalCreate"]
    assert removal["additionalProperties"] is False
    assert removal["required"] == ["reason"]
    assert set(removal["properties"]) == {"credits", "remove_all", "reason"}
    read = components["CourtesyGrantsResponse"]
    assert set(read["properties"]) == {"as_of", "lots", "events"}
    lot = components["CourtesyLotItem"]
    assert set(lot["properties"]) == {
        "lot_id",
        "kind",
        "status",
        "granted_mc",
        "available_mc",
        "consumed_mc",
        "removed_mc",
        "expired_mc",
        "paid_at",
        "expires_at",
    }
    for field in ("granted_mc", "available_mc", "consumed_mc", "removed_mc"):
        assert lot["properties"][field]["type"] == "integer", field


def test_the_builder_read_documents_courtesy(schema: dict[str, Any]) -> None:
    components = schema["components"]["schemas"]
    for name in ("CreditLot", "CreditExpiry", "CreditNextExpiry"):
        assert "removed_mc" in components[name]["required"], name
        kinds = components[name]["properties"]["kind"]["enum"]
        assert kinds == ["monthly", "topup", "courtesy"], name
    balance = components["CreditBalanceResponse"]
    assert "courtesy_available_mc" in balance["required"]
    binding = components["CreditBindingStatus"]
    assert "usable_courtesy" in binding["required"]
