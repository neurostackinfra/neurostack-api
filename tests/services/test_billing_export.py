"""Race and truthfulness boundaries for migration export disposal."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import src.services.billing_export as export
from src.services.billing_export import BillingExportRejected


def _write_expired(path: Path) -> None:
    path.write_bytes(
        b'{"migration_id":"m-1","subscription_id":"s-1","state":"pending"}\n'
    )
    os.chmod(path, 0o600)
    old = (datetime.now(timezone.utc) - timedelta(days=2)).timestamp()
    os.utime(path, (old, old))


def test_discard_records_unlinked_only_after_successful_disposal(tmp_path: Path) -> None:
    path = tmp_path / "roster.jsonl"
    _write_expired(path)
    receipts: list[export.ExportDiscardReceipt] = []

    assert export.discard_expired_export_with_receipt(
        path,
        receipt_writer=receipts.append,
        now=datetime.now(timezone.utc),
        actor="operator-1",
    ) is True

    assert not path.exists()
    assert len(receipts) == 1
    assert receipts[0].path_disposition == "unlinked"
    assert receipts[0].disposal_path == "quarantine"
    assert receipts[0].bytes > 0
    assert receipts[0].count == 1
    assert receipts[0].actor == "operator-1"


def test_disposal_failure_records_truthful_non_disposed_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "roster.jsonl"
    _write_expired(path)
    receipts: list[export.ExportDiscardReceipt] = []

    def fail_disposal(*_args: object, **_kwargs: object) -> None:
        raise BillingExportRejected("billing_export_discard_failed")

    monkeypatch.setattr(export, "_quarantine_and_dispose", fail_disposal)
    with pytest.raises(BillingExportRejected, match="discard_failed"):
        export.discard_expired_export_with_receipt(
            path,
            receipt_writer=receipts.append,
            now=datetime.now(timezone.utc),
        )

    assert path.exists()
    assert [receipt.path_disposition for receipt in receipts] == ["not_disposed"]
    assert receipts[0].disposal_path == "source"
    assert receipts[0].bytes > 0
    assert receipts[0].count == 1


def test_replacement_failure_never_claims_unlinked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "roster.jsonl"
    _write_expired(path)
    receipts: list[export.ExportDiscardReceipt] = []
    original = export._quarantine_and_dispose

    def replace_then_dispose(*args: object, **kwargs: object) -> None:
        path.unlink()
        _write_expired(path)
        original(*args, **kwargs)

    monkeypatch.setattr(export, "_quarantine_and_dispose", replace_then_dispose)
    with pytest.raises(BillingExportRejected, match="file_replaced"):
        export.discard_expired_export_with_receipt(
            path,
            receipt_writer=receipts.append,
            now=datetime.now(timezone.utc),
        )

    assert receipts and receipts[0].path_disposition == "not_disposed"
    assert all(receipt.path_disposition != "unlinked" for receipt in receipts)


# ---------------------------------------------------------------------------
# Fix cycle 7 (HIGH-2): one descriptor, verified parent, contained output.
# ---------------------------------------------------------------------------

import hashlib  # noqa: E402
import stat  # noqa: E402

_ROOT = Path(__file__).resolve().parents[2]


def _rows(marker: str) -> bytes:
    return export.encode_migration_export(
        [{"migration_id": marker, "subscription_id": marker, "state": "pending"}]
    )


def _fresh(path: Path, content: bytes) -> str:
    path.write_bytes(content)
    os.chmod(path, 0o600)
    return hashlib.sha256(content).hexdigest()


@pytest.mark.parametrize("expected", ["original", "attacker"])
@pytest.mark.parametrize("swap", ["symlink", "other_inode"])
def test_validation_never_reads_through_a_swapped_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, swap: str, expected: str
) -> None:
    path = tmp_path / "roster.jsonl"
    original_digest = _fresh(path, _rows("original"))
    attacker = tmp_path / "attacker.jsonl"
    attacker_content = _rows("attacker")
    attacker_digest = _fresh(attacker, attacker_content)
    real_read = export._read_descriptor
    swapped: list[bool] = []

    def swap_then_read(fd: int, size: int) -> bytes:
        # Runs after fstat() validated the descriptor, before the bytes are read.
        path.unlink()
        if swap == "symlink":
            path.symlink_to(attacker)
        else:
            _fresh(path, attacker_content)
        swapped.append(True)
        return real_read(fd, size)

    monkeypatch.setattr(export, "_read_descriptor", swap_then_read)
    # The bytes come from the validated descriptor, whose inode lost its name
    # mid-validation: rejected, and the attacker's content is never accepted
    # (a second path lookup would return attacker bytes matching its digest).
    with pytest.raises(BillingExportRejected, match="^billing_export_file_invalid$"):
        export.validate_migration_export(
            path,
            expected_digest=original_digest if expected == "original" else attacker_digest,
            expected_count=1,
        )
    assert swapped == [True]


def test_validation_rejects_hardlink_fifo_owner_mode_and_symlink(tmp_path: Path) -> None:
    content = _rows("m")
    path = tmp_path / "roster.jsonl"
    digest = _fresh(path, content)
    linked = tmp_path / "second-name.jsonl"
    linked.hardlink_to(path)
    with pytest.raises(BillingExportRejected, match="^billing_export_file_invalid$"):
        export.validate_migration_export(path, expected_digest=digest, expected_count=1)
    linked.unlink()
    os.chmod(path, 0o640)
    with pytest.raises(BillingExportRejected, match="^billing_export_file_invalid$"):
        export.validate_migration_export(path, expected_digest=digest, expected_count=1)
    fifo = tmp_path / "fifo.jsonl"
    os.mkfifo(fifo, 0o600)
    with pytest.raises(BillingExportRejected, match="^billing_export_file_invalid$"):
        export.validate_migration_export(fifo, expected_digest=digest, expected_count=1)


def test_parent_must_be_private_unlinked_and_outside_the_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = _rows("m")
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    digest = _fresh(real / "roster.jsonl", content)
    (tmp_path / "link").symlink_to(real, target_is_directory=True)

    # Symlinked parent (and symlinked grandparent).
    for path in (tmp_path / "link" / "roster.jsonl",):
        with pytest.raises(BillingExportRejected, match="^billing_export_path_invalid$"):
            export.validate_migration_export(path, expected_digest=digest, expected_count=1)
        with pytest.raises(BillingExportRejected, match="^billing_export_path_invalid$"):
            export.create_migration_export(tmp_path / "link" / "new.jsonl", content)
    nested = real / "nested"
    nested.mkdir(mode=0o700)
    with pytest.raises(BillingExportRejected, match="^billing_export_path_invalid$"):
        export.create_migration_export(tmp_path / "link" / "nested" / "new.jsonl", content)

    # Group/world-accessible parents.
    for mode in (0o777, 0o770, 0o750):
        os.chmod(real, mode)
        with pytest.raises(
            BillingExportRejected, match="^billing_export_directory_invalid$"
        ):
            export.create_migration_export(real / "new.jsonl", content)
        with pytest.raises(
            BillingExportRejected, match="^billing_export_directory_invalid$"
        ):
            export.validate_migration_export(
                real / "roster.jsonl", expected_digest=digest, expected_count=1
            )
    os.chmod(real, 0o700)
    assert not (real / "new.jsonl").exists()

    # A directory treated as the repository is refused even when private.
    monkeypatch.setattr(export, "_repository_roots", lambda: (tmp_path.resolve(),))
    with pytest.raises(
        BillingExportRejected, match="^billing_export_path_inside_repository$"
    ):
        export.create_migration_export(real / "new.jsonl", content)
    assert not (real / "new.jsonl").exists()


def test_real_checkout_and_main_repository_are_forbidden() -> None:
    roots = export._repository_roots()
    assert _ROOT.resolve() in roots
    git_marker = _ROOT / ".git"
    if git_marker.is_file():
        gitdir = Path(
            git_marker.read_text(encoding="utf-8").split("gitdir:", 1)[1].strip()
        )
        if not gitdir.is_absolute():
            gitdir = _ROOT / gitdir
        assert gitdir.resolve().parent.parent.parent in roots
    target = _ROOT / f"g5-never-created-{os.getpid()}.jsonl"
    with pytest.raises(
        BillingExportRejected, match="^billing_export_path_inside_repository$"
    ):
        export.create_migration_export(target, _rows("m"))
    assert not target.exists()
    with pytest.raises(
        BillingExportRejected, match="^billing_export_path_inside_repository$"
    ):
        export.validate_migration_export(
            _ROOT / "tests" / "roster.jsonl", expected_digest="a" * 64, expected_count=1
        )


def test_create_is_exclusive_private_and_leaves_nothing_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = _rows("m")
    target = tmp_path / "roster.jsonl"
    digest, count = export.create_migration_export(target, content)
    assert (digest, count) == (hashlib.sha256(content).hexdigest(), 1)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    with pytest.raises(BillingExportRejected, match="^billing_export_path_invalid$"):
        export.create_migration_export(target, content)
    with pytest.raises(BillingExportRejected, match="^billing_export_path_invalid$"):
        export.create_migration_export(tmp_path / "roster.csv", content)

    real_write = os.write

    def short_write(
        fd: int, payload: bytes, *, failure_code: str = "x"
    ) -> None:
        real_write(fd, payload[: len(payload) // 2])
        raise BillingExportRejected(failure_code)

    monkeypatch.setattr(export, "_write_all", short_write)
    partial = tmp_path / "partial.jsonl"
    with pytest.raises(BillingExportRejected, match="^billing_export_write_failed$"):
        export.create_migration_export(partial, content)
    assert not partial.exists()
    monkeypatch.undo()

    def failing_fsync(fd: int) -> None:
        raise OSError("fsync failed")

    monkeypatch.setattr(export, "_fsync", failing_fsync)
    unsynced = tmp_path / "unsynced.jsonl"
    with pytest.raises(BillingExportRejected, match="^billing_export_write_failed$"):
        export.create_migration_export(unsynced, content)
    assert not unsynced.exists()


def test_discard_symlink_swap_before_unlink_retains_it_and_reports_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "roster.jsonl"
    _write_expired(path)
    victim = tmp_path / "victim.jsonl"
    victim.write_bytes(b"must survive\n")
    receipts: list[export.ExportDiscardReceipt] = []
    original = export._quarantine_and_dispose

    def swap_to_symlink(*args: object, **kwargs: object) -> None:
        path.unlink()
        path.symlink_to(victim)
        original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(export, "_quarantine_and_dispose", swap_to_symlink)
    with pytest.raises(BillingExportRejected, match="^billing_export_file_replaced$"):
        export.discard_expired_export_with_receipt(
            path, receipt_writer=receipts.append, now=datetime.now(timezone.utc)
        )
    assert victim.read_bytes() == b"must survive\n"
    quarantined = list(tmp_path.glob(".roster.jsonl.discarding.*"))
    assert len(quarantined) == 1 and quarantined[0].is_symlink()
    assert [r.path_disposition for r in receipts] == ["not_disposed"]
    assert receipts[0].failure_reason == "billing_export_file_replaced"
    assert receipts[0].bytes > 0 and receipts[0].count == 1


def test_discard_rejects_wrong_mode_without_touching_it(tmp_path: Path) -> None:
    path = tmp_path / "roster.jsonl"
    _write_expired(path)
    os.chmod(path, 0o644)
    with pytest.raises(BillingExportRejected, match="^billing_export_file_invalid$"):
        export.discard_expired_export(path, now=datetime.now(timezone.utc))
    assert path.exists()


@pytest.mark.parametrize("failure", ["short_write", "fsync"])
def test_receipt_write_failure_leaves_no_partial_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    path = tmp_path / "roster.jsonl"
    _write_expired(path)
    receipt_log = tmp_path / "roster.jsonl.discard.receipts.jsonl"
    receipt_log.write_bytes(b'{"previous":"complete"}\n')
    os.chmod(receipt_log, 0o600)
    real_write = os.write
    if failure == "short_write":

        def broken_write(fd: int, payload: bytes, **_kwargs: object) -> None:
            real_write(fd, payload[: len(payload) // 3])
            raise BillingExportRejected("billing_export_discard_receipt_failed")

        monkeypatch.setattr(export, "_write_all", broken_write)
    else:

        def broken_fsync(fd: int) -> None:
            raise OSError("fsync failed")

        monkeypatch.setattr(export, "_fsync", broken_fsync)
    with pytest.raises(
        BillingExportRejected, match="^billing_export_discard_receipt_failed$"
    ):
        export.discard_expired_export(path, now=datetime.now(timezone.utc))
    # Disposal happened (the failure is reported), but the log never holds a
    # partial or false record.
    assert not path.exists()
    assert receipt_log.read_bytes() == b'{"previous":"complete"}\n'
