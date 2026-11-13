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

from collections.abc import Iterator
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


def _words(text: str) -> str:
    return " ".join(text.split())


def test_the_state_documents_the_courtesy_rule_with_no_new_value(
    schema: dict[str, Any],
) -> None:
    """debito-cortesia-automatico, wish Decision 4: the contract keeps every
    value (the enumerations above are unchanged; no field is added) and
    documents the rule -- ``active``/``suspended`` with null deadlines,
    ``quarantine`` kept as a reason without ``pending``, never
    ``reconciliation``, and the spendable courtesy as the net available."""

    components = schema["components"]["schemas"]
    state = components["CreditStateResponse"]
    assert set(state["properties"]) == {
        "as_of",
        "mode",
        "enforcement_at",
        "state",
        "pending_reasons",
        "exhausted_at",
        "suspension_due_at",
        "deletion_due_at",
        "export_deadline_at",
    }
    described = _words(state["description"])
    for needle in (
        "Under the courtesy rule",
        "``mode`` is ``enforced`` and there is no episode",
        "``active`` while courtesy is spendable and ``suspended``",
        "``export_deadline_at`` null",
        "``quarantine`` stays in ``pending_reasons`` (a top-up stays refused)",
        "without making the state ``pending``",
        "``reconciliation`` never applies",
    ):
        assert needle in described, needle
    balance = _words(components["CreditBalanceResponse"]["description"])
    assert "``net_available_mc`` is the spendable courtesy" in balance
    route = _words(schema["paths"]["/v1/credits/state"]["get"]["description"])
    for needle in (
        "Under the courtesy rule",
        "suspended with every deadline null",
        "quarantine stays in pending_reasons (the top-up stays refused)",
        "reconciliation never applies",
    ):
        assert needle in route, needle


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


# ---------------------------------------------------------------------------
# cobranca-credito-por-builder Group 5: the read-only ``charging`` field.
# ---------------------------------------------------------------------------

_SUBSCRIPTIONS = "/v1/admin/builders/subscriptions"


def test_the_builder_subscriptions_document_charging(schema: dict[str, Any]) -> None:
    operations = schema["paths"][_SUBSCRIPTIONS]
    assert set(operations) == {"get"}
    get = operations["get"]
    assert "requestBody" not in get
    description = get["description"]
    for needle in (
        "`courtesy`",
        "`charging`",
        "{marked_at}",
        "`unmark`",
        "below 047",
        "No route marks or unmarks a builder",
        "operator command `credit_charge_mark` (a one-off operator task)",
        "has an operator mark, not that it is being charged",
    ):
        assert needle in description, needle


def test_the_charging_shape_is_closed() -> None:
    from src.api.schemas.admin import BuilderChargingResponse

    document = BuilderChargingResponse.model_json_schema()
    assert document["additionalProperties"] is False
    assert set(document["properties"]) == {"marked_at"}
    assert document["required"] == ["marked_at"]
    assert document["properties"]["marked_at"]["format"] == "date-time"


def _mounted_routes(routes: Any, prefix: str = "") -> Iterator[tuple[str, str]]:
    """Every route the app serves, documented or not, as ``(path, methods)``.

    FastAPI 0.142 keeps an included router as an ``_IncludedRouter`` (no
    ``.path`` of its own): walk ``original_router.routes`` under
    ``include_context.prefix``, recursively."""

    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from _mounted_routes(
                inner.routes, prefix + route.include_context.prefix
            )
            continue
        methods = ",".join(sorted(getattr(route, "methods", None) or ()))
        yield prefix + str(getattr(route, "path", "")), methods


def test_no_route_marks_or_unmarks_a_builder(schema: dict[str, Any]) -> None:
    """The mark is written only by the operator command: no HTTP route --
    documented or not -- names a mark, an enforcement or a charging mode."""

    import re

    from src.api.main import app

    pattern = re.compile(r"mark|enforc|charg|cobranc")
    documented = [path for path in schema["paths"] if pattern.search(path.lower())]
    assert documented == []
    mounted = list(_mounted_routes(app.routes))
    # The walk reaches the included routers (it cannot go quiet again): the
    # documented route that carries `charging` is among what it found, and
    # it finds at least every documented path.
    assert (_SUBSCRIPTIONS, "GET") in mounted
    assert {path for path, _ in mounted} >= set(schema["paths"])
    assert [route for route in mounted if pattern.search(route[0].lower())] == []


def test_the_thresholds_document_zero_courtesy_capacity(
    schema: dict[str, Any],
) -> None:
    """debito-cortesia-automatico Group 5a: a blocked builder's thresholds
    (no courtesy capacity in force) read 100 %, described, not computed."""

    described = schema["paths"]["/v1/credits/thresholds"]["get"]["description"]
    assert "used_percent reads 100 and nothing is reached" in described
    component = schema["components"]["schemas"]["CreditThresholdsResponse"]
    assert "``used_percent`` 100 and nothing reached" in component["description"]
