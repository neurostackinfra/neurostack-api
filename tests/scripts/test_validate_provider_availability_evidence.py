from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import scripts.validate_provider_availability_evidence as checker
from scripts.validate_provider_availability_evidence import EXPECTED_DECISIONS, validate


def _metadata(extra: str = "") -> str:
    return "\n".join(
        (
            "- wish_slug: api-provider-availability",
            "- governing_design_sha: 527a35a82cdf4776004942bf1db99fe9e5e246214e60f1e3b86ff892a0d8ca0a",
            "- status: IN_PROGRESS",
            "- depends_on: api-provider-settings, api-config-clarity",
            "- blocks: api-provider-runtime-shutdown",
            extra,
        )
    ).strip()


def _receipt_row(pair: tuple[str, str], decision: str) -> str:
    return (
        "| "
        + " | ".join(
            (
                pair[0],
                pair[1],
                "not_run",
                "not_run",
                "not_run",
                "not_available",
                "not_available",
                "2026-08-29T00:00:00Z",
                decision,
                "false" if decision == "removed" else "true",
            )
        )
        + " |"
    )


def _write_valid(tmp_path: Path) -> Path:
    receipts = tmp_path / "receipts.md"
    receipt_rows = "\n".join(
        _receipt_row(pair, decision) for pair, decision in EXPECTED_DECISIONS.items()
    )
    receipts.write_text(
        "# Provider availability receipts\n\n"
        + _metadata()
        + "\n\n| capability | provider | test_command | test_exit_code | test_result | validation_kind | validation_authority | validated_at | decision | displayable |\n"
        + "|---|---|---|---|---|---|---|---|---|---|\n"
        + receipt_rows
        + "\n",
        encoding="utf-8",
    )
    return receipts


def test_valid_fixture_passes(tmp_path: Path) -> None:
    assert validate(_write_valid(tmp_path)) == []


@pytest.mark.parametrize(
    "wrong_dependencies",
    (
        "none",
        "api-config-clarity, api-provider-settings",
        "api-provider-settings",
    ),
)
def test_dependencies_must_match_the_wish_in_canonical_order(
    tmp_path: Path, wrong_dependencies: str
) -> None:
    receipts = _write_valid(tmp_path)
    receipts.write_text(
        receipts.read_text(encoding="utf-8").replace(
            "depends_on: api-provider-settings, api-config-clarity",
            f"depends_on: {wrong_dependencies}",
        ),
        encoding="utf-8",
    )
    assert any("metadata depends_on must be" in error for error in validate(receipts))


@pytest.mark.parametrize(
    ("mutation", "expected"),
    (
        (
            lambda text: text.replace(
                "| whatsapp | meta_cloud |", "| whatsapp | absent |", 1
            ),
            "22-pair",
        ),
        (
            lambda text: text.replace(
                "| whatsapp | zapi |", "| whatsapp | meta_cloud |", 1
            ),
            "duplicate",
        ),
        (
            lambda text: text.replace(
                "| whatsapp | evolution | not_run | not_run | not_run | not_available | not_available | 2026-08-29T00:00:00Z | coming_soon | true |",
                "| whatsapp | evolution | not_run | not_run | not_run | not_available | not_available | 2026-08-29T00:00:00Z | active | true |",
            ),
            "0 active",
        ),
        (
            lambda text: text.replace(
                "| multimodal | groq | not_run | not_run | not_run | not_available | not_available | 2026-08-29T00:00:00Z | removed | false |",
                "| multimodal | groq | not_run | not_run | not_run | not_available | not_available | 2026-08-29T00:00:00Z | removed | true |",
            ),
            "displayability",
        ),
        (
            lambda text: text.replace(
                "| not_run | not_run | not_run | not_available | not_available |",
                "| command | not_run | not_run | not_available | not_available |",
                1,
            ),
            "unavailable validation",
        ),
        (
            lambda text: text.replace(
                "| not_run | not_run | not_run | not_available | not_available |",
                "| not_run |  | not_run | not_available | not_available |",
                1,
            ),
            "empty field",
        ),
        (
            lambda text: text.replace("not_available", "Verifier", 1),
            "unavailable validation",
        ),
        (
            lambda text: text.replace("2026-08-29T00:00:00Z", "2026-08-29", 1),
            "invalid timestamp",
        ),
    ),
)
def test_receipt_violations_fail_closed(
    tmp_path: Path, mutation, expected: str
) -> None:
    receipts = _write_valid(tmp_path)
    receipts.write_text(
        mutation(receipts.read_text(encoding="utf-8")), encoding="utf-8"
    )
    assert any(expected in error for error in validate(receipts))


def test_receipt_only_validation_does_not_require_extra_evidence(
    tmp_path: Path,
) -> None:
    receipts = _write_valid(tmp_path)
    assert validate(receipts, "inventory") == []


def test_all_displayable_pairs_are_coming_soon() -> None:
    assert (
        sum(decision == "coming_soon" for decision in EXPECTED_DECISIONS.values()) == 19
    )
    assert sum(decision == "active" for decision in EXPECTED_DECISIONS.values()) == 0
    assert sum(decision == "removed" for decision in EXPECTED_DECISIONS.values()) == 3


@pytest.mark.parametrize(
    "sensitive_value",
    (
        "api_key: example",
        "https://tenant.example.invalid",
        "tenant_id: 1234",
        "clinic record",
        "patient record",
        "alex@example.invalid",
    ),
)
def test_sensitive_data_fails_closed(tmp_path: Path, sensitive_value: str) -> None:
    receipts = _write_valid(tmp_path)
    receipts.write_text(
        receipts.read_text(encoding="utf-8") + f"\n{sensitive_value}\n",
        encoding="utf-8",
    )
    assert any("evidence contains" in error for error in validate(receipts))


def _smoke_artifact(module_bytes: bytes = b"module") -> dict[str, object]:
    return {
        "schema": checker.SMOKE_SCHEMA,
        "result": "pass",
        "result_code": "ok",
        "validation_kind": checker.SMOKE_VALIDATION_KIND,
        "validation_authority": checker.SMOKE_VALIDATION_AUTHORITY,
        "timestamp": "2026-09-08T20:00:00Z",
        "run_id": "protected-run-42",
        "tested_commit": "a" * 40,
        "image_digest": "registry.example/neurostack/api@sha256:" + "b" * 64,
        "smoke_module_sha256": hashlib.sha256(module_bytes).hexdigest(),
        "exit_code": 0,
    }


def _write_gemini_receipt(tmp_path: Path, artifact: dict[str, object]) -> Path:
    embedded = (
        json.dumps(artifact, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    )
    receipt = tmp_path / "gemini-llm-receipt.md"
    receipt.write_text(
        "# Gemini LLM release receipt\n\n"
        f"- smoke_artifact_sha256: {hashlib.sha256(embedded.encode()).hexdigest()}\n\n"
        f"```json\n{embedded}```\n",
        encoding="utf-8",
    )
    return receipt


def _run(*args: str, cwd: Path, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        args, cwd=cwd, env=env, capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def _init_signing_repo(path: Path) -> None:
    path.mkdir()
    _run("git", "init", "-q", cwd=path)
    _run("git", "config", "user.name", "Production Operations Owner", cwd=path)
    _run("git", "config", "user.email", "ops@example.invalid", cwd=path)
    (path / "candidate").write_text("candidate\n")
    _run("git", "add", "candidate", cwd=path)


def test_gemini_receipt_requires_exact_canonical_bytes_and_sha(tmp_path: Path) -> None:
    receipt = _write_gemini_receipt(tmp_path, _smoke_artifact())
    artifact, embedded, errors = checker._extract_gemini_receipt(receipt)
    assert errors == []
    assert artifact == _smoke_artifact()
    assert embedded is not None and embedded.endswith(b"\n")

    receipt.write_text(
        receipt.read_text().replace('"exit_code":0', '"exit_code": 0'),
        encoding="utf-8",
    )
    _artifact, _embedded, errors = checker._extract_gemini_receipt(receipt)
    assert any("SHA-256" in error for error in errors)
    assert any("canonical" in error for error in errors)


@pytest.mark.parametrize(
    "mutation",
    (
        lambda raw: raw.replace(b"\n", b"\r\n"),
        lambda raw: raw.replace(b"\n", b"\r", 1),
        lambda raw: raw[:-1],
        lambda raw: raw + b"extra\n",
    ),
)
def test_gemini_receipt_rejects_noncanonical_raw_bytes(
    tmp_path: Path, mutation
) -> None:
    receipt = _write_gemini_receipt(tmp_path, _smoke_artifact())
    receipt.write_bytes(mutation(receipt.read_bytes()))
    _artifact, _embedded, errors = checker._extract_gemini_receipt(receipt)
    assert any("non-canonical" in error for error in errors)


def test_product_promotion_is_the_sole_active_overlay() -> None:
    assert checker._validate_product_promotion(checker.ROOT) == []


@pytest.mark.parametrize(
    ("relative", "old", "new", "message"),
    (
        (
            "src/services/api_credentials.py",
            '"dropout": "coming_soon"',
            '"renamed": "coming_soon"',
            "backend provider matrix",
        ),
        (
            "src/services/api_credentials.py",
            'PUBLICLY_ACTIVE_LLM_PROVIDERS = frozenset({"gemini"})',
            'PUBLICLY_ACTIVE_LLM_PROVIDERS = frozenset({"gemini", "openai"})',
            "publicly active LLM membership",
        ),
        (
            "frontend/artifacts/neurostack/src/pages/clinic-config.tsx",
            'dropout: "coming_soon"',
            'renamed: "coming_soon"',
            "frontend provider matrix",
        ),
    ),
)
def test_product_promotion_rejects_count_preserving_drift(
    tmp_path: Path, relative: str, old: str, new: str, message: str
) -> None:
    for source in (
        Path("src/services/api_credentials.py"),
        Path("frontend/artifacts/neurostack/src/pages/clinic-config.tsx"),
    ):
        target = tmp_path / source
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((checker.ROOT / source).read_bytes())
    target = tmp_path / relative
    target.write_text(target.read_text().replace(old, new, 1), encoding="utf-8")
    assert any(
        message in error for error in checker._validate_product_promotion(tmp_path)
    )


def test_release_requires_bound_base_bytes_before_git(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    altered_base = _write_valid(tmp_path)
    receipt = _write_gemini_receipt(tmp_path, _smoke_artifact())
    monkeypatch.setattr(
        checker,
        "_validate_git_release",
        lambda *_args: pytest.fail("Git must not run after invalid evidence bytes"),
    )
    errors = validate(
        altered_base,
        "release",
        root=tmp_path,
        gemini_receipt_path=receipt,
    )
    assert any("protected SHA-256" in error for error in errors)


def test_git_release_binds_clean_signed_head_parent_diff_fingerprint_and_blob(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module_bytes = b"candidate smoke bytes"
    artifact = _smoke_artifact(module_bytes)
    head = "c" * 40
    receipt_bytes = b"signed receipt bytes\n"
    receipt_path = tmp_path / checker.GEMINI_RECEIPT_RELATIVE
    receipt_path.parent.mkdir(parents=True)
    receipt_path.write_bytes(receipt_bytes)

    def fake_git(_root: Path, *args: str, text: bool = True):
        key = args[0]
        if key == "rev-parse":
            return subprocess.CompletedProcess([], 0, head + "\n", "")
        if key == "status":
            return subprocess.CompletedProcess([], 0, "", "")
        if key == "ls-files":
            return subprocess.CompletedProcess([], 0, "100644 x 0\tfile\n", "")
        if key == "rev-list":
            return subprocess.CompletedProcess([], 0, f"{head} {'a' * 40}\n", "")
        if key == "diff-tree":
            return subprocess.CompletedProcess(
                [], 0, "A\t" + checker.GEMINI_RECEIPT_RELATIVE.as_posix() + "\n", ""
            )
        if key == "cat-file":
            return subprocess.CompletedProcess([], 1, "", "missing")
        if key == "show":
            if args[1] == "-s":
                return subprocess.CompletedProcess([], 0, "G\n" + "D" * 40 + "\n", "")
            if str(args[1]).endswith(checker.GEMINI_RECEIPT_RELATIVE.as_posix()):
                return subprocess.CompletedProcess([], 0, receipt_bytes, b"")
            return subprocess.CompletedProcess([], 0, module_bytes, b"")
        if key == "verify-commit":
            return subprocess.CompletedProcess([], 0, "", "verified")
        raise AssertionError(args)

    monkeypatch.setattr(checker, "_git", fake_git)
    monkeypatch.setenv("GEMINI_RECEIPT_SIGNER_FINGERPRINT", "D" * 40)
    assert checker._validate_git_release(tmp_path, artifact) == []

    monkeypatch.setenv("GEMINI_RECEIPT_SIGNER_FINGERPRINT", "E" * 40)
    assert any(
        "identity" in error
        for error in checker._validate_git_release(tmp_path, artifact)
    )


@pytest.mark.skipif(shutil.which("gpg") is None, reason="GPG unavailable")
def test_authoritative_gpg_signature_and_exact_fingerprint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "gnupg"
    home.mkdir(mode=0o700)
    env = {**os.environ, "GNUPGHOME": str(home)}
    _run(
        "gpg",
        "--batch",
        "--passphrase",
        "",
        "--quick-generate-key",
        "Production Operations Owner <ops@example.invalid>",
        "ed25519",
        "sign",
        "0",
        cwd=tmp_path,
        env=env,
    )
    listing = _run(
        "gpg", "--batch", "--with-colons", "--list-secret-keys", cwd=tmp_path, env=env
    )
    fingerprint = next(
        line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:")
    )
    repo = tmp_path / "gpg-repo"
    _init_signing_repo(repo)
    _run("git", "config", "user.signingkey", fingerprint, cwd=repo)
    _run("git", "commit", "-q", "-S", "-m", "signed", cwd=repo, env=env)
    monkeypatch.setenv("GNUPGHOME", str(home))
    monkeypatch.setenv("GEMINI_RECEIPT_SIGNER_FINGERPRINT", fingerprint)
    head = _run("git", "rev-parse", "HEAD", cwd=repo)
    assert checker._validate_commit_signature(repo, head) == []


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="OpenSSH unavailable")
def test_authoritative_ssh_signature_allowed_signer_and_exact_fingerprint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    key = tmp_path / "ops_signing_key"
    _run("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key), cwd=tmp_path)
    public = key.with_suffix(".pub").read_text().strip()
    allowed = tmp_path / "allowed_signers"
    allowed.write_text(f"ops@example.invalid {public}\n", encoding="utf-8")
    fingerprint = _run(
        "ssh-keygen",
        "-l",
        "-E",
        "sha256",
        "-f",
        str(key.with_suffix(".pub")),
        cwd=tmp_path,
    ).split()[1]
    repo = tmp_path / "ssh-repo"
    _init_signing_repo(repo)
    _run("git", "config", "gpg.format", "ssh", cwd=repo)
    _run("git", "config", "user.signingkey", str(key), cwd=repo)
    _run("git", "config", "gpg.ssh.allowedSignersFile", str(allowed), cwd=repo)
    _run("git", "commit", "-q", "-S", "-m", "signed", cwd=repo)
    monkeypatch.setenv("GEMINI_RECEIPT_SIGNER_FINGERPRINT", fingerprint)
    head = _run("git", "rev-parse", "HEAD", cwd=repo)
    assert checker._validate_commit_signature(repo, head) == []


@pytest.mark.parametrize(
    ("mode", "message"),
    [
        ("dirty", "clean"),
        ("symlink", "symlink"),
        ("parent", "parent"),
        ("diff", "add only"),
        ("preexisting_receipt", "already exists"),
        ("modified_receipt", "signed HEAD blob"),
        ("blob", "module SHA-256"),
        ("signature", "signature"),
    ],
)
def test_git_release_divergences_fail_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str, message: str
) -> None:
    module_bytes = b"candidate smoke bytes"
    artifact = _smoke_artifact(module_bytes)
    head = "c" * 40
    receipt_bytes = b"signed receipt bytes\n"
    receipt_path = tmp_path / checker.GEMINI_RECEIPT_RELATIVE
    receipt_path.parent.mkdir(parents=True)
    receipt_path.write_bytes(receipt_bytes)

    def fake_git(_root: Path, *args: str, text: bool = True):
        key = args[0]
        if key == "rev-parse":
            return subprocess.CompletedProcess([], 0, head + "\n", "")
        if key == "status":
            return subprocess.CompletedProcess(
                [], 0, " M tracked\n" if mode == "dirty" else "", ""
            )
        if key == "ls-files":
            entry = "120000 x 0\tlink\n" if mode == "symlink" else "100644 x 0\tfile\n"
            return subprocess.CompletedProcess([], 0, entry, "")
        if key == "rev-list":
            parent = "d" * 40 if mode == "parent" else "a" * 40
            return subprocess.CompletedProcess([], 0, f"{head} {parent}\n", "")
        if key == "diff-tree":
            name = (
                "M\tsrc/extra.py"
                if mode == "diff"
                else "A\t" + checker.GEMINI_RECEIPT_RELATIVE.as_posix()
            )
            return subprocess.CompletedProcess([], 0, name + "\n", "")
        if key == "cat-file":
            return subprocess.CompletedProcess(
                [], 0 if mode == "preexisting_receipt" else 1, "", "missing"
            )
        if key == "show":
            if args[1] == "-s":
                return subprocess.CompletedProcess([], 0, "G\n" + "D" * 40 + "\n", "")
            if str(args[1]).endswith(checker.GEMINI_RECEIPT_RELATIVE.as_posix()):
                signed = b"different\n" if mode == "modified_receipt" else receipt_bytes
                return subprocess.CompletedProcess([], 0, signed, b"")
            blob = b"wrong" if mode == "blob" else module_bytes
            return subprocess.CompletedProcess([], 0, blob, b"")
        if key == "verify-commit":
            if mode == "signature":
                return subprocess.CompletedProcess([], 1, "", "")
            return subprocess.CompletedProcess([], 0, "", "verified")
        raise AssertionError(args)

    monkeypatch.setattr(checker, "_git", fake_git)
    monkeypatch.setenv("GEMINI_RECEIPT_SIGNER_FINGERPRINT", "D" * 40)
    assert any(
        message in error for error in checker._validate_git_release(tmp_path, artifact)
    )
