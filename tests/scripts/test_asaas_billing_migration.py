"""File-boundary tests for the local ASAAS billing migration client."""

from __future__ import annotations

import hashlib
import importlib.util
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import src.services.billing_export as billing_export
from src.services.billing_export import (
    BillingExportRejected,
    discard_expired_export,
    discard_expired_export_with_receipt,
    encode_migration_export,
    validate_migration_export,
)

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "asaas-billing-migration.py"


def _client_module() -> object:
    spec = importlib.util.spec_from_file_location("asaas_billing_migration", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_http_operation_map_supports_post_and_delete_204(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _client_module()
    calls: list[tuple[str, str, object]] = []

    class Response:
        def __init__(self, status_code: int, payload: object = None) -> None:
            self.status_code = status_code
            self._payload = payload

        def json(self) -> object:
            return self._payload

    class Client:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> "Client":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def post(self, url: str, *, headers: dict[str, str], json: object) -> Response:
            calls.append(("POST", url, json))
            assert headers["Authorization"] == "Bearer jwt-value"
            return Response(201, {"ok": True})

        def delete(self, url: str, *, headers: dict[str, str]) -> Response:
            calls.append(("DELETE", url, None))
            assert headers["Authorization"] == "Bearer jwt-value"
            return Response(204)

    monkeypatch.setattr(module.httpx, "Client", Client)
    assert module._post_json(
        "http://127.0.0.1:8000",
        "jwt-value",
        "/v1/admin/billing/operations/receipts",
        {"safe": True},
    ) == {"ok": True}
    assignment_id = "00000000-0000-0000-0000-000000000001"
    assert (
        module._post_json(
            "http://127.0.0.1:8000",
            "jwt-value",
            "/v1/admin/billing/operations/assignments/{assignment_id}",
            {"assignment_id": assignment_id},
        )
        is None
    )
    assert calls == [
        (
            "POST",
            "http://127.0.0.1:8000/v1/admin/billing/operations/receipts",
            {"safe": True},
        ),
        (
            "DELETE",
            "http://127.0.0.1:8000/v1/admin/billing/operations/assignments/"
            + assignment_id,
            None,
        ),
    ]


def test_http_operation_map_rejects_unexpected_status_without_token_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _client_module()

    class Response:
        status_code = 202

    class Client:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> "Client":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def post(self, *_args: object, **_kwargs: object) -> Response:
            return Response()

    monkeypatch.setattr(module.httpx, "Client", Client)
    with pytest.raises(module.MigrationClientRejected) as captured:
        module._post_json(
            "http://127.0.0.1:8000",
            "jwt-not-for-logs",
            "/v1/admin/billing/operations/receipts",
            {},
        )
    assert captured.value.code == "billing_migration_api_rejected"
    assert "jwt-not-for-logs" not in str(captured.value)


def test_client_only_accepts_loopback_api() -> None:
    module = _client_module()
    assert module._is_loopback_api("http://127.0.0.1:8000") is True
    assert module._is_loopback_api("https://localhost") is True
    assert module._is_loopback_api("https://api.asaas.com") is False
    assert module._is_loopback_api("http://127.0.0.1:8000?token=no") is False


def test_export_rejects_link_mode_freshness_count_and_digest(tmp_path: Path) -> None:
    content = encode_migration_export(
        [
            {
                "migration_id": "00000000-0000-0000-0000-000000000001",
                "subscription_id": "00000000-0000-0000-0000-000000000002",
                "state": "pending",
                "attempt_count": 0,
                "source_roster_revision": 1,
                "renewal_date": "2026-10-12",
                "open_by_date": "2026-10-05",
                "cutover_by_date": "2026-10-10",
            }
        ]
    )
    export = tmp_path / "roster.jsonl"
    export.write_bytes(content)
    export.chmod(0o600)
    digest = hashlib.sha256(content).hexdigest()

    validated = validate_migration_export(
        export, expected_digest=digest, expected_count=1
    )
    assert validated.row_count == 1

    with pytest.raises(BillingExportRejected, match="count_invalid"):
        validate_migration_export(export, expected_digest=digest, expected_count=2)
    with pytest.raises(BillingExportRejected, match="digest_invalid"):
        validate_migration_export(export, expected_digest="a" * 64, expected_count=1)

    export.chmod(0o644)
    with pytest.raises(BillingExportRejected, match="file_invalid"):
        validate_migration_export(export, expected_digest=digest, expected_count=1)

    export.chmod(0o600)
    old = datetime.now(timezone.utc) - timedelta(hours=25)
    os.utime(export, (old.timestamp(), old.timestamp()))
    with pytest.raises(BillingExportRejected, match="stale"):
        validate_migration_export(export, expected_digest=digest, expected_count=1)


def test_export_rejects_a_symbolic_link_without_echoing_its_path(
    tmp_path: Path,
) -> None:
    content = encode_migration_export(
        [
            {
                "migration_id": "00000000-0000-0000-0000-000000000001",
                "subscription_id": "00000000-0000-0000-0000-000000000002",
                "state": "pending",
            }
        ]
    )
    target = tmp_path / "secure.jsonl"
    target.write_bytes(content)
    target.chmod(0o600)
    linked = tmp_path / "linked.jsonl"
    linked.symlink_to(target)

    with pytest.raises(BillingExportRejected, match="file_invalid") as captured:
        validate_migration_export(
            linked,
            expected_digest=hashlib.sha256(content).hexdigest(),
            expected_count=1,
        )

    assert str(linked) not in str(captured.value)


def test_expired_export_is_unlinked_only_after_persisting_receipt(
    tmp_path: Path,
) -> None:
    export = tmp_path / "roster.jsonl"
    export.write_bytes(
        encode_migration_export(
            [
                {
                    "migration_id": "00000000-0000-0000-0000-000000000001",
                    "subscription_id": "00000000-0000-0000-0000-000000000002",
                    "state": "pending",
                }
            ]
        )
    )
    export.chmod(0o600)
    old = datetime.now(timezone.utc) - timedelta(hours=25)
    os.utime(export, (old.timestamp(), old.timestamp()))

    assert discard_expired_export(export, now=datetime.now(timezone.utc)) is True
    assert not export.exists()
    receipt = export.with_name("roster.jsonl.discard.receipts.jsonl")
    assert receipt.stat().st_mode & 0o777 == 0o600
    receipt_value = receipt.read_text(encoding="ascii")
    assert "export_sha256" in receipt_value
    assert '"actor":"billing-migration-client"' in receipt_value
    assert '"bytes":' in receipt_value
    assert '"count":1' in receipt_value
    assert '"device":' in receipt_value
    assert '"inode":' in receipt_value
    assert '"parent_device":' in receipt_value
    assert '"parent_inode":' in receipt_value
    assert '"path_disposition":"unlinked"' in receipt_value
    assert '"receipt_sha256":' in receipt_value


def test_discard_refuses_path_replacement_before_success_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    export = tmp_path / "roster.jsonl"
    original = encode_migration_export(
        [{"migration_id": "m", "subscription_id": "s", "state": "pending"}]
    )
    export.write_bytes(original)
    export.chmod(0o600)
    old = datetime.now(timezone.utc) - timedelta(hours=25)
    os.utime(export, (old.timestamp(), old.timestamp()))

    original_dispose = billing_export._quarantine_and_dispose

    def replace_during_disposal(*args: object, **kwargs: object) -> None:
        export.unlink()
        export.write_bytes(b"attacker replacement\n")
        export.chmod(0o600)
        original_dispose(*args, **kwargs)

    monkeypatch.setattr(
        billing_export, "_quarantine_and_dispose", replace_during_disposal
    )
    receipts: list[object] = []

    with pytest.raises(BillingExportRejected, match="file_replaced"):
        discard_expired_export_with_receipt(
            export,
            receipt_writer=receipts.append,
            now=datetime.now(timezone.utc),
        )
    quarantined = list(tmp_path.glob(".roster.jsonl.discarding.*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == b"attacker replacement\n"
    assert len(receipts) == 1
    assert receipts[0].path_disposition == "not_disposed"


def test_discard_quarantine_race_never_unlinks_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    export = tmp_path / "roster.jsonl"
    export.write_bytes(
        encode_migration_export(
            [{"migration_id": "m", "subscription_id": "s", "state": "pending"}]
        )
    )
    export.chmod(0o600)
    old = datetime.now(timezone.utc) - timedelta(hours=25)
    os.utime(export, (old.timestamp(), old.timestamp()))
    replacement = b"replacement survives quarantine\n"
    original_rename = billing_export.os.rename

    def replace_during_atomic_move(
        source: object, destination: object, **kwargs: object
    ) -> None:
        # The rename is directory-descriptor relative (src_dir_fd/dst_dir_fd).
        export.unlink()
        export.write_bytes(replacement)
        export.chmod(0o600)
        original_rename(source, destination, **kwargs)

    monkeypatch.setattr(billing_export.os, "rename", replace_during_atomic_move)
    with pytest.raises(BillingExportRejected, match="file_replaced"):
        discard_expired_export(export, now=datetime.now(timezone.utc))

    quarantined = list(tmp_path.glob(".roster.jsonl.discarding.*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == replacement


def test_discard_rejects_hardlink_without_touching_target(tmp_path: Path) -> None:
    export = tmp_path / "roster.jsonl"
    linked = tmp_path / "linked.jsonl"
    export.write_bytes(
        encode_migration_export(
            [{"migration_id": "m", "subscription_id": "s", "state": "pending"}]
        )
    )
    export.chmod(0o600)
    linked.hardlink_to(export)
    with pytest.raises(BillingExportRejected, match="file_invalid"):
        discard_expired_export(export, now=datetime.now(timezone.utc))
    assert linked.exists()


def test_operation_payload_rejects_pii_secret_and_permissive_mode(
    tmp_path: Path,
) -> None:
    module = _client_module()
    payload = tmp_path / "operation.json"
    payload.write_text('{"email":"not-allowed@example.test"}', encoding="utf-8")
    payload.chmod(0o600)

    with pytest.raises(module.MigrationClientRejected, match="payload_invalid"):
        module._read_operation_payload(payload)

    payload.write_text("{}", encoding="utf-8")
    payload.chmod(0o644)
    with pytest.raises(module.MigrationClientRejected, match="file_invalid"):
        module._read_operation_payload(payload)


def test_cli_export_refuses_repository_symlinked_and_shared_parents(
    tmp_path: Path,
) -> None:
    module = _client_module()
    data = encode_migration_export(
        [{"migration_id": "m", "subscription_id": "s", "state": "pending"}]
    )
    inside = ROOT / f"g5-cli-never-created-{os.getpid()}.jsonl"
    with pytest.raises(
        BillingExportRejected, match="^billing_export_path_inside_repository$"
    ):
        module._write_export(inside, data)
    assert not inside.exists()

    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    (tmp_path / "alias").symlink_to(private, target_is_directory=True)
    with pytest.raises(BillingExportRejected, match="^billing_export_path_invalid$"):
        module._write_export(tmp_path / "alias" / "roster.jsonl", data)
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    with pytest.raises(
        BillingExportRejected, match="^billing_export_directory_invalid$"
    ):
        module._write_export(shared / "roster.jsonl", data)
    assert list(private.iterdir()) == [] and list(shared.iterdir()) == []

    digest, count = module._write_export(private / "roster.jsonl", data)
    assert (digest, count) == (hashlib.sha256(data).hexdigest(), 1)
    assert (
        validate_migration_export(
            private / "roster.jsonl", expected_digest=digest, expected_count=1
        ).row_count
        == 1
    )
