"""Safe local export checks for migration roster evidence.

Exports are intentionally limited to opaque internal IDs, state, counters and
São Paulo cutoffs.  This module rejects links, permissive modes, stale files,
unexpected rows, and digests that do not bind the exact bytes.

Every file operation is descriptor-relative: the parent directory is opened
component by component with ``O_NOFOLLOW`` (no symlinked ancestor), checked
(owned by the effective UID, mode ``0700``, outside the source repository),
and the export is then opened, created, renamed and unlinked only by name
relative to that verified directory descriptor.  Validation reads the bytes
from the same descriptor it ``fstat``-ed; there is no second path lookup.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Final, Mapping, Sequence
from uuid import uuid4

_MAX_EXPORT_BYTES: Final = 2_000_000
_MAX_EXPORT_AGE: Final = timedelta(hours=24)
_ALLOWED_ROW_KEYS: Final = frozenset(
    {
        "attempt_count",
        "cutover_by_date",
        "migration_id",
        "open_by_date",
        "renewal_date",
        "source_roster_revision",
        "state",
        "subscription_id",
    }
)
_FORBIDDEN_EXPORT_KEYS: Final = frozenset(
    {
        "address",
        "api_key",
        "customer",
        "document",
        "email",
        "name",
        "password",
        "phone",
        "secret",
        "signature",
        "token",
    }
)


class BillingExportRejected(RuntimeError):
    """Fixed-code export failure that never echoes a filesystem path."""

    code = "billing_export_rejected"

    def __init__(self, code: str | None = None) -> None:
        self.code = code or self.code
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class ValidatedMigrationExport:
    digest: str
    row_count: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ExportDiscardReceipt:
    """Append-only evidence for one descriptor-verified disposal."""

    export_sha256: str
    bytes: int
    count: int
    actor: str
    path_disposition: str
    disposal_path: str
    reason: str
    observed_at: datetime
    device: int
    inode: int
    mode: int
    nlink: int
    uid: int
    gid: int
    mtime_ns: int
    ctime_ns: int
    parent_device: int
    parent_inode: int
    failure_reason: str | None = None


def _validate_row(value: object) -> None:
    if not isinstance(value, Mapping):
        raise BillingExportRejected("billing_export_row_invalid")
    keys = set(value)
    if not keys <= _ALLOWED_ROW_KEYS or keys & _FORBIDDEN_EXPORT_KEYS:
        raise BillingExportRejected("billing_export_fields_invalid")
    if not {"migration_id", "subscription_id", "state"} <= keys:
        raise BillingExportRejected("billing_export_row_invalid")
    if any(not isinstance(key, str) for key in keys):
        raise BillingExportRejected("billing_export_row_invalid")


def encode_migration_export(rows: Sequence[Mapping[str, object]]) -> bytes:
    """Encode the closed JSONL roster format after rejecting unexpected data."""

    if not rows:
        raise BillingExportRejected("billing_export_roster_empty")
    encoded_rows: list[bytes] = []
    for row in rows:
        _validate_row(row)
        try:
            encoded_rows.append(
                json.dumps(
                    dict(row), sort_keys=True, separators=(",", ":"), ensure_ascii=True
                ).encode("ascii")
            )
        except (TypeError, ValueError) as exc:
            raise BillingExportRejected("billing_export_row_invalid") from exc
    return b"\n".join(encoded_rows) + b"\n"


def _repository_roots() -> tuple[Path, ...]:
    """Directories an export may never live in: this checkout and its repo.

    Derived from this module's own location (never from the process working
    directory).  A linked git worktree's ``.git`` file names the main
    repository, which is forbidden too.  Without git metadata (for example an
    installed image) the source tree root is still forbidden.
    """

    here = Path(__file__).resolve()
    roots: list[Path] = [here.parents[2]]
    for candidate in here.parents:
        marker = candidate / ".git"
        if marker.is_dir():
            roots.append(candidate)
            break
        if marker.is_file():
            roots.append(candidate)
            try:
                lines = marker.read_text(encoding="utf-8").splitlines()
            except OSError:
                break
            for line in lines:
                if not line.startswith("gitdir:"):
                    continue
                gitdir = Path(line.split(":", 1)[1].strip())
                if not gitdir.is_absolute():
                    gitdir = candidate / gitdir
                gitdir = gitdir.resolve()
                if gitdir.parent.name == "worktrees" and gitdir.parent.parent.name == ".git":
                    roots.append(gitdir.parent.parent.parent)
            break
    return tuple(dict.fromkeys(root.resolve() for root in roots))


@dataclass(frozen=True, slots=True)
class _ExportLocation:
    directory_fd: int
    directory: os.stat_result
    name: str


def _open_export_location(path: Path) -> _ExportLocation:
    """Open and verify the parent directory without following any link.

    The caller owns ``directory_fd`` and must close it.
    """

    raw = Path(os.fspath(path))
    if any(part == ".." for part in raw.parts):
        raise BillingExportRejected("billing_export_path_invalid")
    absolute = Path(os.path.abspath(raw))
    name = absolute.name
    if not name or name in {".", ".."}:
        raise BillingExportRejected("billing_export_path_invalid")
    parent = absolute.parent
    try:
        directory_fd = os.open(
            parent.anchor or "/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
        )
    except OSError as exc:
        raise BillingExportRejected("billing_export_unavailable") from exc
    try:
        for component in parent.parts[1:]:
            try:
                next_fd = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=directory_fd,
                )
            except FileNotFoundError as exc:
                raise BillingExportRejected("billing_export_unavailable") from exc
            except OSError as exc:
                # ELOOP/ENOTDIR: a symlinked or non-directory ancestor.
                raise BillingExportRejected("billing_export_path_invalid") from exc
            os.close(directory_fd)
            directory_fd = next_fd
        # Every component was opened without following links, so the lexical
        # parent is the real parent.
        for root in _repository_roots():
            if parent == root or root in parent.parents:
                raise BillingExportRejected("billing_export_path_inside_repository")
        directory = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(directory.st_mode)
            or directory.st_uid != os.geteuid()
            or stat.S_IMODE(directory.st_mode) & 0o077
        ):
            raise BillingExportRejected("billing_export_directory_invalid")
    except BaseException:
        os.close(directory_fd)
        raise
    return _ExportLocation(directory_fd=directory_fd, directory=directory, name=name)


def _open_export_file(location: _ExportLocation) -> int:
    """Open the export itself relative to the verified directory, never a link."""

    return os.open(
        location.name,
        os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
        dir_fd=location.directory_fd,
    )


def _owned_secure_export(metadata: os.stat_result) -> bool:
    return _secure_export_metadata(metadata) and metadata.st_uid == os.geteuid()


def validate_migration_export(
    path: Path,
    *,
    expected_digest: str,
    expected_count: int,
    now: datetime | None = None,
) -> ValidatedMigrationExport:
    """Validate one existing secure export from a single verified descriptor."""

    if (
        not isinstance(expected_count, int)
        or expected_count <= 0
        or len(expected_digest) != 64
        or any(character not in "0123456789abcdef" for character in expected_digest)
    ):
        raise BillingExportRejected("billing_export_expectation_invalid")
    location = _open_export_location(path)
    try:
        try:
            fd = _open_export_file(location)
        except FileNotFoundError as exc:
            raise BillingExportRejected("billing_export_unavailable") from exc
        except OSError as exc:
            # ELOOP: the final component is a symbolic link.
            raise BillingExportRejected("billing_export_file_invalid") from exc
        try:
            metadata = os.fstat(fd)
            if not _owned_secure_export(metadata):
                raise BillingExportRejected("billing_export_file_invalid")
            current = now or datetime.now(timezone.utc)
            created_at = datetime.fromtimestamp(metadata.st_mtime, tz=timezone.utc)
            if created_at > current or current - created_at > _MAX_EXPORT_AGE:
                raise BillingExportRejected("billing_export_stale")
            raw = _read_descriptor(fd, metadata.st_size)
            if not _same_export_metadata(metadata, os.fstat(fd)):
                raise BillingExportRejected("billing_export_file_invalid")
        finally:
            os.close(fd)
    finally:
        os.close(location.directory_fd)
    try:
        decoded = raw.decode("ascii")
    except UnicodeError as exc:
        raise BillingExportRejected("billing_export_file_invalid") from exc
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_digest:
        raise BillingExportRejected("billing_export_digest_invalid")
    lines = [line for line in decoded.splitlines() if line]
    if len(lines) != expected_count:
        raise BillingExportRejected("billing_export_count_invalid")
    try:
        for line in lines:
            _validate_row(json.loads(line))
    except (json.JSONDecodeError, TypeError) as exc:
        raise BillingExportRejected("billing_export_row_invalid") from exc
    return ValidatedMigrationExport(
        digest=digest, row_count=len(lines), created_at=created_at
    )


def create_migration_export(path: Path, data: bytes) -> tuple[str, int]:
    """Create one new private ``.jsonl`` export; never overwrite or follow a link.

    The parent must be a ``0700`` directory owned by the effective UID,
    reached without symbolic links and outside this repository.  A short
    write or a failed ``fsync`` removes the partial file (only the inode this
    call created) and fails; no partial export is ever left behind.
    """

    if Path(os.fspath(path)).suffix != ".jsonl" or not data:
        raise BillingExportRejected("billing_export_path_invalid")
    location = _open_export_location(path)
    try:
        try:
            fd = os.open(
                location.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=location.directory_fd,
            )
        except FileExistsError as exc:
            raise BillingExportRejected("billing_export_path_invalid") from exc
        except OSError as exc:
            raise BillingExportRejected("billing_export_write_failed") from exc
        created: os.stat_result | None = None
        try:
            created = os.fstat(fd)
            os.fchmod(fd, 0o600)
            _write_all(fd, data, failure_code="billing_export_write_failed")
            _fsync(fd)
            _fsync(location.directory_fd)
        except (OSError, BillingExportRejected) as exc:
            if created is not None:
                _unlink_if_same(location, created)
            raise BillingExportRejected("billing_export_write_failed") from exc
        finally:
            os.close(fd)
    finally:
        os.close(location.directory_fd)
    return hashlib.sha256(data).hexdigest(), len(data.splitlines())


def _unlink_if_same(location: _ExportLocation, expected: os.stat_result) -> None:
    try:
        current = os.stat(
            location.name, dir_fd=location.directory_fd, follow_symlinks=False
        )
        if _same_path_identity(expected, current):
            os.unlink(location.name, dir_fd=location.directory_fd)
    except OSError:
        pass


def _append_discard_receipt(path: Path, receipt: ExportDiscardReceipt) -> None:
    """Append one complete receipt line beside the export, or nothing at all.

    A short write or ``fsync`` failure truncates the receipt log back to its
    previous length before failing, so a partial record never survives.
    """

    payload = {
        "actor": receipt.actor,
        "bytes": receipt.bytes,
        "count": receipt.count,
        "ctime_ns": receipt.ctime_ns,
        "device": receipt.device,
        "export_sha256": receipt.export_sha256,
        "failure_reason": receipt.failure_reason,
        "gid": receipt.gid,
        "inode": receipt.inode,
        "mode": receipt.mode,
        "mtime_ns": receipt.mtime_ns,
        "nlink": receipt.nlink,
        "observed_at": receipt.observed_at.isoformat(),
        "parent_device": receipt.parent_device,
        "parent_inode": receipt.parent_inode,
        "path_disposition": receipt.path_disposition,
        "disposal_path": receipt.disposal_path,
        "reason": receipt.reason,
        "uid": receipt.uid,
    }
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
        "ascii"
    )
    payload["receipt_sha256"] = hashlib.sha256(payload_bytes).hexdigest()
    record = (
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")
        + b"\n"
    )
    location = _open_export_location(
        Path(os.fspath(path)).with_name(f"{Path(os.fspath(path)).name}.discard.receipts.jsonl")
    )
    try:
        try:
            fd = os.open(
                location.name,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=location.directory_fd,
            )
        except OSError as exc:
            raise BillingExportRejected("billing_export_discard_receipt_failed") from exc
        try:
            metadata = os.fstat(fd)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_uid != os.geteuid()
            ):
                raise BillingExportRejected("billing_export_receipt_invalid")
            previous_size = metadata.st_size
            try:
                _write_all(fd, record)
                _fsync(fd)
                _fsync(location.directory_fd)
            except (OSError, BillingExportRejected) as exc:
                try:
                    os.ftruncate(fd, previous_size)
                    _fsync(fd)
                except OSError:
                    pass
                raise BillingExportRejected(
                    "billing_export_discard_receipt_failed"
                ) from exc
        finally:
            os.close(fd)
    finally:
        os.close(location.directory_fd)


def discard_expired_export(
    path: Path,
    *,
    now: datetime | None = None,
    actor: str = "billing-migration-client",
) -> bool:
    """Append a local receipt and delete only an expired secure export."""

    return discard_expired_export_with_receipt(
        path,
        receipt_writer=lambda receipt: _append_discard_receipt(path, receipt),
        now=now,
        actor=actor,
    )


def discard_expired_export_with_receipt(
    path: Path,
    *,
    receipt_writer: Callable[[ExportDiscardReceipt], None],
    now: datetime | None = None,
    actor: str = "billing-migration-client",
) -> bool:
    """Dispose only the exact, descriptor-verified file that was read.

    The export is opened relative to the verified directory descriptor; its
    bytes and row count come from that descriptor, and disposal renames and
    unlinks by name relative to the same directory only after the
    ``(st_dev, st_ino)`` identity is confirmed.  A successful ``unlinked``
    receipt is written only after disposal; a replacement or disposal failure
    gets a truthful ``not_disposed`` record carrying the failure code.
    """

    if not callable(receipt_writer):
        raise BillingExportRejected("billing_export_discard_receipt_required")
    try:
        location = _open_export_location(path)
    except BillingExportRejected as exc:
        if exc.code == "billing_export_unavailable":
            return False
        raise
    try:
        try:
            fd = _open_export_file(location)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise BillingExportRejected("billing_export_file_invalid") from exc
        try:
            checked = os.fstat(fd)
            if not _owned_secure_export(checked):
                raise BillingExportRejected("billing_export_file_invalid")
            current = now or datetime.now(timezone.utc)
            created_at = datetime.fromtimestamp(checked.st_mtime, tz=timezone.utc)
            if current - created_at <= _MAX_EXPORT_AGE:
                return False
            raw = _read_descriptor(fd, checked.st_size)
            if not _same_export_metadata(checked, os.fstat(fd)):
                raise BillingExportRejected("billing_export_file_invalid")
            try:
                decoded = raw.decode("ascii")
                lines = [line for line in decoded.splitlines() if line]
                for line in lines:
                    _validate_row(json.loads(line))
            except (UnicodeError, json.JSONDecodeError, TypeError) as exc:
                raise BillingExportRejected("billing_export_row_invalid") from exc
            receipt = ExportDiscardReceipt(
                export_sha256=hashlib.sha256(raw).hexdigest(),
                bytes=len(raw),
                count=len(lines),
                actor=_validate_actor(actor),
                path_disposition="not_disposed",
                disposal_path="source",
                reason="expired",
                observed_at=current,
                device=checked.st_dev,
                inode=checked.st_ino,
                mode=stat.S_IMODE(checked.st_mode),
                nlink=checked.st_nlink,
                uid=checked.st_uid,
                gid=checked.st_gid,
                mtime_ns=checked.st_mtime_ns,
                ctime_ns=checked.st_ctime_ns,
                parent_device=location.directory.st_dev,
                parent_inode=location.directory.st_ino,
            )
            try:
                _quarantine_and_dispose(
                    path,
                    expected=checked,
                    expected_parent=location.directory,
                    location=location,
                    descriptor=fd,
                )
            except BillingExportRejected as exc:
                try:
                    receipt_writer(replace(receipt, failure_reason=exc.code))
                except BillingExportRejected:
                    raise
                except Exception as receipt_exc:
                    raise BillingExportRejected(
                        "billing_export_discard_receipt_failed"
                    ) from receipt_exc
                raise exc
            receipt_writer(
                replace(receipt, path_disposition="unlinked", disposal_path="quarantine")
            )
        finally:
            os.close(fd)
    finally:
        os.close(location.directory_fd)
    return True


def _secure_export_metadata(metadata: os.stat_result) -> bool:
    return bool(
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_nlink == 1
        and stat.S_IMODE(metadata.st_mode) == 0o600
        and 0 < metadata.st_size <= _MAX_EXPORT_BYTES
    )


def _same_export_metadata(left: os.stat_result, right: os.stat_result) -> bool:
    """Compare every file attribute that matters to safe disposal."""

    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and left.st_mode == right.st_mode
        and left.st_nlink == right.st_nlink
        and left.st_uid == right.st_uid
        and left.st_gid == right.st_gid
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
        and left.st_ctime_ns == right.st_ctime_ns
    )


def _same_path_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _quarantine_and_dispose(
    path: Path,
    *,
    expected: os.stat_result,
    expected_parent: os.stat_result,
    location: _ExportLocation,
    descriptor: int,
) -> None:
    """Atomically move and unlink only the descriptor-verified inode.

    Rename and unlink are by name relative to the verified directory
    descriptor.  If the name no longer refers to the validated
    ``(st_dev, st_ino)`` the quarantined entry is retained (it is the only
    name proven to belong to the replacement) and nothing is unlinked.
    """

    del path  # every lookup below is relative to ``location.directory_fd``
    directory_fd = location.directory_fd
    quarantine = f".{location.name}.discarding.{uuid4().hex}"
    if not _same_path_identity(expected_parent, os.fstat(directory_fd)):
        raise BillingExportRejected("billing_export_file_replaced")
    try:
        os.rename(
            location.name,
            quarantine,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
    except OSError as exc:
        raise BillingExportRejected("billing_export_file_replaced") from exc
    try:
        quarantined = os.stat(quarantine, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as exc:
        raise BillingExportRejected("billing_export_file_replaced") from exc
    if not _same_export_metadata_after_rename(expected, quarantined):
        raise BillingExportRejected("billing_export_file_replaced")
    try:
        os.unlink(quarantine, dir_fd=directory_fd)
    except OSError as exc:
        raise BillingExportRejected("billing_export_discard_failed") from exc
    # The inode we validated and read is the one that lost its last name.
    if os.fstat(descriptor).st_nlink != 0:
        raise BillingExportRejected("billing_export_discard_failed")


def _same_export_metadata_after_rename(
    left: os.stat_result, right: os.stat_result
) -> bool:
    """Compare inode and content identity while allowing rename ctime churn."""

    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and left.st_mode == right.st_mode
        and left.st_nlink == right.st_nlink
        and left.st_uid == right.st_uid
        and left.st_gid == right.st_gid
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
    )


def _read_descriptor(fd: int, size: int) -> bytes:
    if size <= 0 or size > _MAX_EXPORT_BYTES:
        raise BillingExportRejected("billing_export_file_invalid")
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            chunk = os.read(fd, min(remaining, 131_072))
            if not chunk:
                raise BillingExportRejected("billing_export_file_invalid")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    except OSError as exc:
        raise BillingExportRejected("billing_export_file_invalid") from exc


def _fsync(fd: int) -> None:
    """Indirection kept so a failed ``fsync`` can be simulated in tests."""

    os.fsync(fd)


def _write_all(
    fd: int,
    payload: bytes,
    *,
    failure_code: str = "billing_export_discard_receipt_failed",
) -> None:
    """Persist every byte; a short write is never reported as success."""

    offset = 0
    try:
        while offset < len(payload):
            written = os.write(fd, payload[offset:])
            if written <= 0:
                raise BillingExportRejected(failure_code)
            offset += written
    except BillingExportRejected:
        raise
    except OSError as exc:
        raise BillingExportRejected(failure_code) from exc


def _validate_actor(actor: str) -> str:
    if (
        not isinstance(actor, str)
        or not actor
        or len(actor) > 80
        or not actor.isascii()
        or any(character.isspace() for character in actor)
    ):
        raise BillingExportRejected("billing_export_actor_invalid")
    return actor
