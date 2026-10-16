#!/usr/bin/env python3
"""Verify the one byte-addressed, closed ``billing.fact.v1`` contract bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

_SCHEMA_VERSION = "billing.fact.v1"

_LINE = re.compile(r"^([0-9a-f]{64})  ([^\n]+)\n$")
_EVENT_TYPES = {
    "subscription.cycle_settled.v1",
    "subscription.cycle_reversed.v1",
    "subscription.cycle_restored.v1",
    "topup.payment_settled.v1",
    "topup.payment_reversed.v1",
    "topup.payment_restored.v1",
}
_ROOT_FIELDS = {
    "schema_version",
    "event_id",
    "event_type",
    "producer",
    "occurred_at",
    "produced_at",
    "builder_id",
    "aggregate_kind",
    "aggregate_id",
    "subscription_id",
    "order_id",
    "payment_id",
    "paid_at",
    "catalog_version_id",
    "offer_id",
    "currency",
    "financial_revision",
    "total_amount_cents",
    "settled_cents",
    "reversed_cents",
    "restored_cents",
    "credit_grant_mc",
    "lines",
}
_LINE_FIELDS = {
    "line_id",
    "offer_id",
    "kind",
    "quantity",
    "unit_amount_cents",
    "line_amount_cents",
    "credit_grant_mc",
    "settled_cents",
    "reversed_cents",
    "restored_cents",
    "financial_state",
}
_SUBSCRIPTION_EVENTS = {
    "subscription.cycle_settled.v1",
    "subscription.cycle_reversed.v1",
    "subscription.cycle_restored.v1",
}
_TOPUP_EVENTS = {
    "topup.payment_settled.v1",
    "topup.payment_reversed.v1",
    "topup.payment_restored.v1",
}


def _fail(message: str) -> None:
    raise ValueError(message)


def _root_for(paths: list[Path]) -> Path:
    candidates = [Path.cwd().resolve(), *[path.resolve().parent for path in paths]]
    for candidate in candidates:
        if (candidate / "pyproject.toml").is_file():
            return candidate
        for parent in candidate.parents:
            if (parent / "pyproject.toml").is_file():
                return parent
    _fail("repository root is unavailable")


def _relative(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError(f"bundle path is outside repository: {path}") from exc


def _verify_schema(value: Any) -> None:
    if not isinstance(value, dict):
        _fail("schema is not an object")
    if (
        value.get("title") != _SCHEMA_VERSION
        or value.get("additionalProperties") is not False
    ):
        _fail("schema root is divergent")
    if set(value.get("required", ())) != _ROOT_FIELDS:
        _fail("schema required fields are divergent")
    properties = value.get("properties")
    if not isinstance(properties, dict) or set(properties) != _ROOT_FIELDS | {
        "subscription_cycle"
    }:
        _fail("schema root properties are divergent")
    if properties.get("schema_version", {}).get("const") != _SCHEMA_VERSION:
        _fail("schema version is divergent")
    if properties.get("producer", {}).get("const") != "asaas-payment-gateway":
        _fail("schema producer is divergent")
    if properties.get("currency", {}).get("const") != "BRL":
        _fail("schema currency is divergent")
    if properties.get("paid_at") != {
        "type": ["string", "null"],
        "format": "date",
    }:
        _fail("schema paid_at property is divergent")
    paid_at_rule = {
        "if": {"properties": {"settled_cents": {"minimum": 1}}},
        "then": {"properties": {"paid_at": {"type": "string", "format": "date"}}},
        "else": {"properties": {"paid_at": {"type": "null"}}},
    }
    if paid_at_rule not in value.get("allOf", []):
        _fail("schema paid_at settlement condition is divergent")
    if set(properties.get("aggregate_kind", {}).get("enum", ())) != {
        "subscription_cycle",
        "topup_order",
    }:
        _fail("schema aggregate kinds are divergent")
    for field, minimum in (
        ("financial_revision", 1),
        ("total_amount_cents", 1),
        ("settled_cents", 0),
        ("reversed_cents", 0),
        ("restored_cents", 0),
        ("credit_grant_mc", 0),
    ):
        field_schema = properties.get(field, {})
        if (
            field_schema.get("type") != "integer"
            or field_schema.get("minimum") != minimum
        ):
            _fail(f"schema {field} is divergent")
    lines = properties.get("lines", {})
    if (
        lines.get("type") != "array"
        or lines.get("minItems") != 1
        or lines.get("items") != {"$ref": "#/$defs/line"}
    ):
        _fail("schema lines are divergent")
    cycle = properties.get("subscription_cycle")
    if not isinstance(cycle, dict) or (
        cycle.get("type") != "object"
        or cycle.get("additionalProperties") is not False
        or set(cycle.get("required", ())) != {"cycle_start", "cycle_end"}
        or cycle.get("properties", {}).get("cycle_start", {}).get("format") != "date"
        or cycle.get("properties", {}).get("cycle_end", {}).get("format") != "date"
    ):
        _fail("schema subscription cycle is divergent")
    definitions = value.get("$defs")
    if not isinstance(definitions, dict):
        _fail("schema definitions are missing")
    line = definitions.get("line")
    if not isinstance(line, dict) or line.get("additionalProperties") is not False:
        _fail("line schema is divergent")
    if set(line.get("required", ())) != _LINE_FIELDS:
        _fail("line required fields are divergent")
    if set(line.get("properties", ())) != _LINE_FIELDS:
        _fail("line properties are divergent")
    root_events = properties.get("event_type", {}).get("enum")
    if set(root_events or ()) != _EVENT_TYPES:
        _fail("schema event types are divergent")
    if line.get("allOf") != [
        {
            "if": {"properties": {"kind": {"enum": ["additional_clinic", "tax"]}}},
            "then": {"properties": {"credit_grant_mc": {"const": 0}}},
        }
    ]:
        _fail("line grant rule is divergent")
    one_of = value.get("oneOf")
    if not isinstance(one_of, list) or len(one_of) != 2:
        _fail("schema aggregate xor is divergent")
    variants = {
        variant.get("properties", {}).get("aggregate_kind", {}).get("const"): variant
        for variant in one_of
        if isinstance(variant, dict)
    }
    subscription = variants.get("subscription_cycle")
    topup = variants.get("topup_order")
    if (
        not isinstance(subscription, dict)
        or set(subscription.get("properties", {}).get("event_type", {}).get("enum", ()))
        != _SUBSCRIPTION_EVENTS
        or subscription.get("required") != ["subscription_cycle"]
        or subscription.get("properties", {}).get("subscription_id", {}).get("type")
        != "string"
        or subscription.get("properties", {}).get("order_id", {}).get("type") != "null"
        or not isinstance(topup, dict)
        or set(topup.get("properties", {}).get("event_type", {}).get("enum", ()))
        != _TOPUP_EVENTS
        or topup.get("not") != {"required": ["subscription_cycle"]}
        or topup.get("properties", {}).get("subscription_id", {}).get("type") != "null"
        or topup.get("properties", {}).get("order_id", {}).get("type") != "string"
    ):
        _fail("schema aggregate xor is divergent")


def _manifest_entries(raw: bytes) -> list[tuple[str, str]]:
    if not raw or not raw.endswith(b"\n"):
        _fail("manifest must end with exactly one newline")
    if b"\r" in raw:
        _fail("manifest must use LF newlines")
    entries: list[tuple[str, str]] = []
    for line in raw.decode("utf-8").splitlines(keepends=True):
        match = _LINE.fullmatch(line)
        if match is None:
            _fail("manifest line is not '<lowercase sha256><two spaces><path>\\n'")
        entries.append((match.group(1), match.group(2)))
    if len(entries) != len(set(entries)):
        _fail("manifest repeats an entry")
    paths = [path for _digest, path in entries]
    if paths != sorted(paths, key=lambda item: item.encode("utf-8")):
        _fail("manifest paths are not LC_ALL=C bytewise ordered")
    return entries


def _fixture_integer(value: object, field: str, *, minimum: int = 0) -> int:
    if type(value) is not int or int(value) < minimum:
        _fail(f"fixture has invalid {field}")
    return int(value)


def _verify_fixture(value: dict[str, object]) -> None:
    """Independently enforce the closed schema equations for bundle fixtures."""

    kind = value.get("aggregate_kind")
    expected = set(_ROOT_FIELDS)
    if kind == "subscription_cycle":
        expected.add("subscription_cycle")
    if kind not in {"subscription_cycle", "topup_order"} or set(value) != expected:
        _fail("fixture root is not the closed envelope")
    if kind == "subscription_cycle":
        if (
            not isinstance(value.get("subscription_id"), str)
            or value.get("order_id") is not None
        ):
            _fail("fixture subscription identity xor is invalid")
        if value.get("subscription_id") == value.get("aggregate_id"):
            _fail("fixture cycle aggregate aliases its subscription")
    elif (
        value.get("subscription_id") is not None
        or not isinstance(value.get("order_id"), str)
        or value.get("order_id") != value.get("aggregate_id")
    ):
        _fail("fixture top-up identity xor is invalid")
    if value.get("schema_version") != _SCHEMA_VERSION:
        _fail("fixture has wrong schema version")
    event_type = value.get("event_type")
    prefix = "subscription.cycle_" if kind == "subscription_cycle" else "topup.payment_"
    if (
        not isinstance(event_type, str)
        or event_type not in _EVENT_TYPES
        or not event_type.startswith(prefix)
    ):
        _fail("fixture event type does not match aggregate")
    amount = _fixture_integer(
        value.get("total_amount_cents"), "total amount", minimum=1
    )
    settled = _fixture_integer(value.get("settled_cents"), "settled cents")
    reversed_cents = _fixture_integer(value.get("reversed_cents"), "reversed cents")
    restored = _fixture_integer(value.get("restored_cents"), "restored cents")
    grant = _fixture_integer(value.get("credit_grant_mc"), "credit amount")
    paid_at = value.get("paid_at")
    if settled > 0:
        if not isinstance(paid_at, str) or len(paid_at) != 10:
            _fail("fixture paid_at is missing or invalid")
        try:
            parsed_paid_at = date.fromisoformat(paid_at)
        except ValueError:
            _fail("fixture paid_at is invalid")
        if parsed_paid_at.isoformat() != paid_at:
            _fail("fixture paid_at is not canonical")
    elif paid_at is not None:
        _fail("fixture without settlement has paid_at")
    if not 0 <= restored <= reversed_cents <= settled <= amount:
        _fail("fixture cumulative totals are invalid")
    lines = value.get("lines")
    if not isinstance(lines, list) or not lines:
        _fail("fixture has no lines")
    line_ids: list[str] = []
    sums = [0, 0, 0, 0, 0]
    for raw_line in lines:
        if not isinstance(raw_line, dict) or set(raw_line) != _LINE_FIELDS:
            _fail("fixture line is not closed")
        line_id = raw_line.get("line_id")
        if not isinstance(line_id, str) or not line_id:
            _fail("fixture line id is invalid")
        line_ids.append(line_id)
        if raw_line.get("offer_id") != value.get("offer_id"):
            _fail("fixture line offer differs from fact offer")
        line_kind = raw_line.get("kind")
        if line_kind not in {"plan", "additional_clinic", "topup", "tax"}:
            _fail("fixture line kind is invalid")
        quantity = _fixture_integer(
            raw_line.get("quantity"), "line quantity", minimum=1
        )
        unit = _fixture_integer(raw_line.get("unit_amount_cents"), "line unit")
        line_amount = _fixture_integer(raw_line.get("line_amount_cents"), "line amount")
        line_grant = _fixture_integer(raw_line.get("credit_grant_mc"), "line credit")
        line_settled = _fixture_integer(raw_line.get("settled_cents"), "line settled")
        line_reversed = _fixture_integer(
            raw_line.get("reversed_cents"), "line reversed"
        )
        line_restored = _fixture_integer(
            raw_line.get("restored_cents"), "line restored"
        )
        if (
            line_amount != quantity * unit
            or not 0 <= line_restored <= line_reversed <= line_settled <= line_amount
        ):
            _fail("fixture line equation is invalid")
        if line_kind in {"additional_clinic", "tax"} and line_grant != 0:
            _fail("fixture non-credit line has grant")
        expected_state = (
            "pending"
            if line_settled == 0
            else (
                "restored"
                if line_restored > 0
                else "reversed" if line_reversed > 0 else "settled"
            )
        )
        if raw_line.get("financial_state") != expected_state:
            _fail("fixture line state disagrees with totals")
        if (
            line_grant > 0
            and line_settled > 0
            and (paid_at is None or line_settled != line_amount)
        ):
            _fail("fixture credit grant is not fully paid")
        sums[0] += line_amount
        sums[1] += line_settled
        sums[2] += line_reversed
        sums[3] += line_restored
        sums[4] += line_grant
    if line_ids != sorted(line_ids) or len(line_ids) != len(set(line_ids)):
        _fail("fixture lines are not deterministically ordered")
    if tuple(sums) != (amount, settled, reversed_cents, restored, grant):
        _fail("fixture totals do not equal lines")


def verify(*, schema: Path, fixtures: Path, manifest: Path) -> str:
    root = _root_for([schema, fixtures, manifest])
    if not schema.is_file() or not fixtures.is_dir() or not manifest.is_file():
        _fail("schema, fixtures, and manifest must exist")
    if manifest.resolve().parent != fixtures.resolve() or manifest.name != "SHA256SUMS":
        _fail("manifest must be the fixture directory SHA256SUMS")
    contained = {path.resolve() for path in fixtures.rglob("*") if path.is_file()}
    fixture_paths = sorted(fixtures.glob("*.json"), key=lambda path: path.name.encode())
    if not fixture_paths:
        _fail("contract bundle has no JSON fixtures")
    allowed = {manifest.resolve(), *(path.resolve() for path in fixture_paths)}
    if contained != allowed:
        _fail("fixture directory has an extra or missing bundle path")
    expected = {
        _relative(root, schema),
        *(_relative(root, path) for path in fixture_paths),
    }
    entries = _manifest_entries(manifest.read_bytes())
    paths = [path for _digest, path in entries]
    if set(paths) != expected or len(paths) != len(expected):
        _fail("manifest has an extra or missing bundle path")
    for expected_digest, relative_path in entries:
        candidate = root / relative_path
        if not candidate.is_file():
            _fail(f"manifest path is missing: {relative_path}")
        actual_digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if actual_digest != expected_digest:
            _fail(f"manifest hash mismatch: {relative_path}")
    try:
        schema_value = json.loads(schema.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("schema is invalid JSON") from exc
    _verify_schema(schema_value)
    fixture_events: set[str] = set()
    for fixture in fixture_paths:
        try:
            value = json.loads(fixture.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"fixture is invalid JSON: {fixture.name}") from exc
        if not isinstance(value, dict):
            _fail(f"fixture is not an object: {fixture.name}")
        _verify_fixture(value)
        fixture_events.add(str(value["event_type"]))
    if fixture_events != _EVENT_TYPES:
        _fail("fixtures do not cover exactly the six event types")
    return hashlib.sha256(manifest.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--schema", required=True, type=Path)
    parser.add_argument("--fixtures", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--emit-digest", action="store_true")
    arguments = parser.parse_args()
    try:
        digest = verify(
            schema=arguments.schema,
            fixtures=arguments.fixtures,
            manifest=arguments.manifest,
        )
    except (OSError, ValueError) as exc:
        print(f"billing fact bundle invalid: {exc}", file=sys.stderr)
        return 1
    if arguments.emit_digest:
        print(digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
