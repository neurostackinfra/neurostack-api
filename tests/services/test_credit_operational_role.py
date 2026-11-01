"""Group 7: credit readiness must not infer production from ENVIRONMENT.

``production-serving`` is derived only from the injected allowlist of exact
(environment, cluster ARN, service ARN) triples (deliverable 9); production
really runs ``ENVIRONMENT=staging`` and ``settings.is_production`` is false
there, so no credit module may read it.  No real account or ARN is pinned
here: the triples below are syntactic examples only.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.config.settings import Settings
from src.services import credit_rollout
from src.services.credit_rollout import PRODUCTION_SERVING, derive_operational_role

ROOT = Path(__file__).resolve().parents[2]
CLUSTER = "arn:aws:ecs:us-east-1:000000000000:cluster/example-cluster"
SERVICE = "arn:aws:ecs:us-east-1:000000000000:service/example-cluster/example-api"
ALLOWLIST = (("staging", CLUSTER, SERVICE),)
SECRET = "credit-role-test-key-0123456789abcdef"


def _credit_modules() -> list[Path]:
    paths = [
        *sorted((ROOT / "src/services").glob("credit_*.py")),
        ROOT / "src/services/content_retention.py",
        *sorted((ROOT / "src/models").glob("credit*.py")),
        ROOT / "src/models/content_retention.py",
        ROOT / "src/models/operational_capability.py",
        *sorted((ROOT / "src/repositories").glob("credit*.py")),
        ROOT / "src/repositories/content_retention.py",
        *sorted((ROOT / "src/storage").glob("credit_*.py")),
        ROOT / "src/api/routes/credits.py",
        ROOT / "src/api/routes/credit_export.py",
        ROOT / "src/api/routes/admin_credits.py",
        ROOT / "src/api/schemas/credits.py",
        ROOT / "src/api/schemas/admin_credits.py",
        # The credit one-off tasks (``python -m src.ops.credit_enforcement``).
        *sorted((ROOT / "src/ops").glob("credit_*.py")),
    ]
    return [path for path in paths if path.exists()]


def test_the_swept_module_set_is_the_declared_one() -> None:
    swept = {path.relative_to(ROOT).as_posix() for path in _credit_modules()}
    assert {
        "src/services/credit_rollout.py",
        "src/services/credit_export.py",
        "src/api/routes/credit_export.py",
        "src/models/operational_capability.py",
        "src/repositories/content_retention.py",
        "src/services/content_retention.py",
        "src/models/content_retention.py",
        "src/api/routes/admin_credits.py",
        "src/ops/credit_enforcement.py",
    } <= swept


def test_credit_modules_never_read_is_production() -> None:
    offenders: list[str] = []
    for path in _credit_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "is_production":
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
            if isinstance(node, ast.Constant) and node.value == "is_production":
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not offenders


def test_credit_settings_do_not_require_appconfig() -> None:
    configured = Settings(environment="staging", secret_key=SECRET)
    assert configured.environment == "staging"
    assert configured.credit_binding_grace_days == 5
    assert configured.credit_production_serving_allowlist == ()


# ---------------------------------------------------------------------------
# The environment + ARN derivation.
# ---------------------------------------------------------------------------


def test_staging_with_an_allowlisted_arn_is_production_serving() -> None:
    assert (
        derive_operational_role("staging", CLUSTER, SERVICE, allowlist=ALLOWLIST)
        == PRODUCTION_SERVING
        == "production-serving"
    )


@pytest.mark.parametrize(
    ("environment", "cluster", "service", "allowlist"),
    [
        # An ARN outside the allowlist.
        ("staging", CLUSTER.replace("example-cluster", "other"), SERVICE, ALLOWLIST),
        ("staging", CLUSTER, SERVICE.replace("example-api", "worker"), ALLOWLIST),
        # The right ARNs under another environment.
        ("production", CLUSTER, SERVICE, ALLOWLIST),
        ("development", CLUSTER, SERVICE, ALLOWLIST),
        # No allowlist injected: nothing derives.
        ("staging", CLUSTER, SERVICE, ()),
        # Missing identity.
        ("staging", None, SERVICE, ALLOWLIST),
        ("staging", CLUSTER, None, ALLOWLIST),
        ("staging", "", "", ALLOWLIST),
        # Not exact: case, whitespace, prefix.
        ("Staging", CLUSTER, SERVICE, ALLOWLIST),
        ("staging", CLUSTER + " ", SERVICE, ALLOWLIST),
        ("staging", CLUSTER[:-1], SERVICE, ALLOWLIST),
    ],
)
def test_anything_but_an_exact_allowlisted_triple_derives_nothing(
    environment: str,
    cluster: str | None,
    service: str | None,
    allowlist: tuple[tuple[str, str, str], ...],
) -> None:
    assert (
        derive_operational_role(environment, cluster, service, allowlist=allowlist)
        is None
    )


@pytest.mark.parametrize(
    "asserted",
    ["production-serving", "production", PRODUCTION_SERVING.upper()],
)
def test_a_self_asserted_role_is_ignored(asserted: str) -> None:
    """A caller-chosen role or target in any argument slot derives nothing,
    and the function has no parameter through which a role could be
    asserted."""

    parameters = set(inspect.signature(derive_operational_role).parameters)
    assert parameters == {"environment", "cluster_arn", "service_arn", "allowlist"}
    assert (
        derive_operational_role(asserted, CLUSTER, SERVICE, allowlist=ALLOWLIST) is None
    )
    assert (
        derive_operational_role("staging", asserted, asserted, allowlist=ALLOWLIST)
        is None
    )
    assert (
        derive_operational_role(
            "staging", CLUSTER, SERVICE, allowlist=((asserted, CLUSTER, SERVICE),)
        )
        is None
    )


def test_the_derivation_reads_no_global_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pure: the configured allowlist is passed in, not read behind the
    caller's back, and ``is_production`` plays no part."""

    monkeypatch.setattr(credit_rollout.settings, "environment", "production")
    monkeypatch.setattr(
        credit_rollout.settings, "credit_production_serving_allowlist", ALLOWLIST
    )
    assert derive_operational_role("staging", CLUSTER, SERVICE, allowlist=()) is None
    assert (
        derive_operational_role("staging", CLUSTER, SERVICE, allowlist=ALLOWLIST)
        == PRODUCTION_SERVING
    )


def test_the_allowlist_is_injected_by_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "CREDIT_PRODUCTION_SERVING_ALLOWLIST",
        f'[["staging", "{CLUSTER}", "{SERVICE}"]]',
    )
    configured = Settings(environment="staging", secret_key=SECRET)
    assert configured.credit_production_serving_allowlist == ALLOWLIST
    assert (
        derive_operational_role(
            configured.environment,
            CLUSTER,
            SERVICE,
            allowlist=configured.credit_production_serving_allowlist,
        )
        == PRODUCTION_SERVING
    )


@pytest.mark.parametrize(
    "entry",
    [
        ("staging", "", SERVICE),
        ("staging", CLUSTER, ""),
        ("qa", CLUSTER, SERVICE),
        ("staging", "not-an-arn", SERVICE),
        ("staging", CLUSTER, "not-an-arn"),
    ],
)
def test_a_malformed_allowlist_entry_is_refused_at_startup(
    entry: tuple[str, str, str],
) -> None:
    with pytest.raises(ValidationError):
        Settings(
            environment="staging",
            secret_key=SECRET,
            credit_production_serving_allowlist=(entry,),
        )
