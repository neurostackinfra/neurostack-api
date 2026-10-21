"""The development paths may migrate to the head only on a development database.

``make db-migrate``, ``make db-reset`` and the dev Compose ``migrate`` service
run ``alembic upgrade head``. The production migration window is pinned to an
exact revision (tests/scripts/test_production_migration_window.py), so none of
these developer paths may reach a production database: each one runs
``scripts/guard_alembic_upgrade_head.py`` first and stops when it refuses.
"""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped,unused-ignore]
from pydantic_settings import BaseSettings, SettingsConfigDict

from scripts import guard_alembic_upgrade_head as guard
from src.config.settings import Settings

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "guard_alembic_upgrade_head.py"
MAKEFILE = ROOT / "Makefile"
DEV_COMPOSE = ROOT / "docker-compose.yml"
PRODUCTION_COMPOSE = ROOT / "docker-compose.production.yml"
DEPLOY_VPS = ROOT / "scripts" / "deploy-vps.sh"
SYNTHETIC_PRODUCTION_ENV = (
    ROOT / "tests" / "fixtures" / "compose" / "production-synthetic.env"
)

GUARD_COMMAND = "poetry run python scripts/guard_alembic_upgrade_head.py"
OVERRIDE = {"ALLOW_ALEMBIC_UPGRADE_HEAD": "I_KNOW_THIS_IS_NOT_PRODUCTION"}
DEV_URL = "postgresql+asyncpg://neurostack:neurostack@localhost:5434/neurostack"
SETTINGS_DEFAULT: str = Settings.model_fields["database_url"].default
RDS_URL = (
    "postgresql+asyncpg://neurostack:neurostack@"
    "neurostack-prod.c1abcdefghij.us-east-1.rds.amazonaws.com:5432/neurostack"
)
# 24+ characters, three classes, ten distinct characters and no weak marker:
# what scripts/deploy-vps.sh accepts as a production secret.
STRONG_PASSWORD = "Xk9vQ2mL7wR4tZ8bN1pY6cJ3"
# Identifiers scripts/deploy-vps.sh accepts for the VPS database.
VPS_USER = "clinicas_owner"
VPS_DATABASE = "clinicas_core"


def _decide(
    environ: dict[str, str],
    dotenv_text: str | None = None,
    *,
    database_url: str | None = None,
) -> guard.Decision:
    return guard.evaluate(environ, dotenv_text=dotenv_text, database_url=database_url)


def _codes(decision: guard.Decision) -> set[str]:
    return {finding.code for finding in decision.findings}


# ---------------------------------------------------------------------------
# Database URL
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        DEV_URL,
        "postgresql://neurostack:neurostack@127.0.0.1:5434/neurostack",
        "postgresql+asyncpg://neurostack:neurostack@[::1]:5434/neurostack",
        "postgresql+asyncpg://neurostack:neurostack@postgres:5432/neurostack",
        "postgresql+psycopg://neurostack:neurostack@LOCALHOST/neurostack",
        "postgresql+psycopg2://neurostack:neurostack@localhost/neurostack_dev",
        "postgres://neurostack@localhost/neurostack_test",
        "postgresql://dev_user:short@localhost/app_development",
        "postgresql://neurostack:p%40ss%3Aword@localhost/neurostack",
        "postgresql+asyncpg://neurostack:neurostack@localhost/neurostack?ssl=disable",
        "postgresql://neurostack:neurostack@localhost/neurostack?sslmode=disable"
        "&connect_timeout=5&application_name=dev",
    ],
)
def test_development_urls_are_allowed(url: str) -> None:
    decision = _decide({"DATABASE_URL": url})
    assert decision.allowed, decision.findings
    assert decision.findings == ()
    assert not decision.override_used


@pytest.mark.parametrize(
    ("url", "code"),
    [
        # Hosts that are not loopback or the dev Compose database service.
        (RDS_URL, "host"),
        (DEV_URL.replace("localhost", "localhost.evil.example"), "host"),
        (DEV_URL.replace("localhost", "evil-localhost"), "host"),
        (DEV_URL.replace("localhost", "127.0.0.1.nip.io"), "host"),
        (DEV_URL.replace("localhost", "localhost."), "host"),
        (DEV_URL.replace("localhost", "127.0.0.2"), "host"),
        (DEV_URL.replace("localhost", "0.0.0.0"), "host"),
        (DEV_URL.replace("localhost", "10.0.0.5"), "host"),
        (DEV_URL.replace("localhost", "host.docker.internal"), "host"),
        (DEV_URL.replace("localhost", "postgres.internal"), "host"),
        (DEV_URL.replace("localhost", "postgres-prod"), "host"),
        (DEV_URL.replace("localhost", "neurostack-postgres"), "host"),
        (DEV_URL.replace("localhost", "[::2]"), "host"),
        # Production-looking or unrecognised identifiers on a loopback host.
        ("postgresql://prod_user:x@localhost/neurostack", "user"),
        ("postgresql://neurostack:x@localhost/prod_db", "database"),
        ("postgresql://neurostack:x@localhost/neurostack_production", "database"),
        ("postgresql://neurostack:x@localhost/neurostack_staging", "database"),
        ("postgresql://dev_user:x@localhost/devlive", "database"),
        (f"postgresql://{VPS_USER}:x@localhost/neurostack", "user"),
        (f"postgresql://neurostack:x@postgres/{VPS_DATABASE}", "database"),
        ("postgresql://neurostack:x@postgres/neurostack_core", "database"),
        ("postgresql://postgres:x@localhost/neurostack", "user"),
        ("postgresql://neurostack:x@localhost/postgres", "database"),
        ("postgresql://neurostack:x@localhost/neuro%20stack", "database"),
        ("postgresql://neurostack:x@localhost/neurostack;x", "database"),
        ("postgresql://localhost/neurostack", "user"),
        ("postgresql://neurostack@localhost/", "database"),
        ("postgresql://neurostack@localhost", "database"),
        ("postgresql://neurostack@localhost/neurostack/extra", "database"),
        # A production-strength password: the AWS database seen through a tunnel.
        (f"postgresql://neurostack:{STRONG_PASSWORD}@localhost/neurostack", "password"),
        # Query parameters that redirect the connection, or are unknown.
        (DEV_URL + "?host=/var/run/postgresql", "query"),
        (DEV_URL + "?hostaddr=10.0.0.5", "query"),
        (DEV_URL + "?service=prod", "query"),
        (DEV_URL + "?dbname=prod_db", "query"),
        (DEV_URL + "?user=prod_user", "query"),
        (DEV_URL + "?options=-csearch_path%3Dx", "query"),
        (DEV_URL + "?HOST=db.example", "query"),
    ],
)
def test_non_development_targets_are_refused(url: str, code: str) -> None:
    decision = _decide({"DATABASE_URL": url})
    assert not decision.allowed
    assert code in _codes(decision), decision.findings


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "not a url",
        "localhost:5434/neurostack",
        "sqlite:///./dev.db",
        "mysql://neurostack:neurostack@localhost/neurostack",
        "http://neurostack:neurostack@localhost/neurostack",
        "postgresql+asyncpg://neurostack:neurostack@localhost:notaport/neurostack",
        "postgresql+asyncpg://neurostack:neurostack@localhost:99999/neurostack",
        "postgresql://neurostack:neurostack@local\nhost/neurostack",
        "postgresql://neurostack:neurostack@local\thost/neurostack",
        " postgresql://neurostack:neurostack@localhost/neurostack",
        "postgresql://neurostack:neurostack@[::1/neurostack",
        "postgresql://neurostack:neurostack@::1/neurostack",
        "postgresql://neurostack:neurostack@localhost/neurostack#frag",
        "postgresql://neurostack:p@ss@localhost/neurostack",
        DEV_URL.replace("localhost", "%6cocalhost"),
        # Unix-socket forms: no host the guard can prove is local.
        "postgresql:///neurostack?host=/var/run/postgresql",
        "postgresql://neurostack@/neurostack?host=/tmp",
        "postgresql://neurostack:neurostack@%2Fvar%2Frun%2Fpostgresql/neurostack",
        # Several hosts.
        "postgresql://neurostack:neurostack@localhost,127.0.0.1/neurostack",
        "postgresql://neurostack:neurostack@localhost:5432,db.example:5432/neurostack",
        "postgresql://neurostack:neurostack@localhost,/neurostack",
    ],
)
def test_unparsable_or_ambiguous_urls_fail_closed_even_with_the_override(
    url: str,
) -> None:
    decision = _decide({"DATABASE_URL": url})
    assert not decision.allowed
    assert any(not finding.overridable for finding in decision.findings)
    overridden = _decide({"DATABASE_URL": url, **OVERRIDE})
    assert not overridden.allowed
    assert not overridden.override_used


def test_conflicting_case_variants_of_database_url_fail_closed() -> None:
    # Settings reads environment names case-insensitively.
    decision = _decide({"DATABASE_URL": DEV_URL, "database_url": RDS_URL})
    assert not decision.allowed
    assert "ambiguous" in _codes(decision)
    same = _decide({"DATABASE_URL": DEV_URL, "database_url": DEV_URL})
    assert same.allowed


def test_explicit_database_url_replaces_the_environment() -> None:
    assert _decide({"DATABASE_URL": RDS_URL}, database_url=DEV_URL).allowed
    assert not _decide({"DATABASE_URL": DEV_URL}, database_url=RDS_URL).allowed
    assert not _decide({}, database_url="").allowed


def test_missing_url_and_env_file_uses_the_settings_default() -> None:
    default = Settings.model_fields["database_url"].default
    assert guard.SETTINGS_DEFAULT_DATABASE_URL == default
    decision = _decide({})
    assert decision.allowed
    assert decision.host == "localhost"


# ---------------------------------------------------------------------------
# ENVIRONMENT
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("environment", [None, "development", "test"])
def test_development_environments_are_allowed(environment: str | None) -> None:
    environ = {"DATABASE_URL": DEV_URL}
    if environment is not None:
        environ["ENVIRONMENT"] = environment
    assert _decide(environ).allowed


@pytest.mark.parametrize(
    "environment",
    [
        "staging",
        "production",
        "Development",
        "DEVELOPMENT",
        " development",
        "development ",
        "",
        "qa",
        "prod",
        "dev",
    ],
)
def test_other_environments_are_refused_even_with_the_override(
    environment: str,
) -> None:
    environ = {"DATABASE_URL": DEV_URL, "ENVIRONMENT": environment}
    decision = _decide(environ)
    assert not decision.allowed
    assert "environment" in _codes(decision)
    assert not _decide({**environ, **OVERRIDE}).allowed


def test_lowercase_environment_name_is_read_like_settings() -> None:
    assert not _decide({"DATABASE_URL": DEV_URL, "environment": "staging"}).allowed
    conflicting = {"ENVIRONMENT": "development", "environment": "production"}
    decision = _decide({"DATABASE_URL": DEV_URL, **conflicting})
    assert not decision.allowed


@pytest.mark.parametrize("checkout", ["staging", "production", "Development", "qa"])
def test_checkout_environment_passed_by_compose_must_be_development(
    checkout: str,
) -> None:
    environ = {
        "DATABASE_URL": DEV_URL,
        "ENVIRONMENT": "development",
        "NEUROSTACK_CHECKOUT_ENVIRONMENT": checkout,
    }
    assert not _decide(environ).allowed
    assert not _decide({**environ, **OVERRIDE}).allowed


@pytest.mark.parametrize("checkout", ["", "development", "test"])
def test_unset_or_development_checkout_environment_is_allowed(checkout: str) -> None:
    environ = {
        "DATABASE_URL": DEV_URL,
        "ENVIRONMENT": "development",
        "NEUROSTACK_CHECKOUT_ENVIRONMENT": checkout,
    }
    assert _decide(environ).allowed


# ---------------------------------------------------------------------------
# Compose interpolation variables of this checkout
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("POSTGRES_USER", VPS_USER),
        ("POSTGRES_USER", "prod_user"),
        ("POSTGRES_DB", VPS_DATABASE),
        ("POSTGRES_DB", "prod_db"),
        ("POSTGRES_PASSWORD", STRONG_PASSWORD),
    ],
)
def test_production_shaped_compose_variables_are_refused(name: str, value: str) -> None:
    decision = _decide({"DATABASE_URL": DEV_URL, name: value})
    assert not decision.allowed
    assert "compose" in _codes(decision)


def test_development_compose_variables_are_allowed() -> None:
    environ = {
        "DATABASE_URL": DEV_URL,
        "POSTGRES_USER": "neurostack",
        "POSTGRES_PASSWORD": "neurostack",
        "POSTGRES_DB": "neurostack",
    }
    assert _decide(environ).allowed


# ---------------------------------------------------------------------------
# The single override
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        RDS_URL,
        "postgresql://neurostack:neurostack@192.168.1.20/neurostack",
        f"postgresql://{VPS_USER}:{STRONG_PASSWORD}@localhost/{VPS_DATABASE}",
        DEV_URL + "?options=-cx",
    ],
)
def test_override_allows_unrecognised_targets_under_development(url: str) -> None:
    assert not _decide({"DATABASE_URL": url}).allowed
    for environment in (None, "development", "test"):
        environ = {"DATABASE_URL": url, **OVERRIDE}
        if environment:
            environ["ENVIRONMENT"] = environment
        decision = _decide(environ)
        assert decision.allowed
        assert decision.override_used
        assert decision.findings


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_override_is_refused_under_staging_and_production(environment: str) -> None:
    environ = {"DATABASE_URL": RDS_URL, "ENVIRONMENT": environment, **OVERRIDE}
    decision = _decide(environ)
    assert not decision.allowed
    assert not decision.override_used


@pytest.mark.parametrize(
    "value",
    [
        "1",
        "true",
        "yes",
        "i_know_this_is_not_production",
        "I_KNOW_THIS_IS_NOT_PRODUCTION ",
        " I_KNOW_THIS_IS_NOT_PRODUCTION",
        "I_KNOW_THIS_IS_PRODUCTION",
    ],
)
def test_any_other_override_value_is_refused(value: str) -> None:
    for url in (DEV_URL, RDS_URL):
        decision = _decide({"DATABASE_URL": url, "ALLOW_ALEMBIC_UPGRADE_HEAD": value})
        assert not decision.allowed
        assert "override" in _codes(decision)


def test_empty_override_is_the_same_as_unset() -> None:
    assert _decide({"DATABASE_URL": DEV_URL, "ALLOW_ALEMBIC_UPGRADE_HEAD": ""}).allowed
    assert not _decide(
        {"DATABASE_URL": RDS_URL, "ALLOW_ALEMBIC_UPGRADE_HEAD": ""}
    ).allowed


def test_override_is_never_read_from_the_env_file() -> None:
    dotenv = "ALLOW_ALEMBIC_UPGRADE_HEAD=I_KNOW_THIS_IS_NOT_PRODUCTION\n"
    assert not _decide({"DATABASE_URL": RDS_URL}, dotenv).allowed


def test_override_with_the_variable_in_another_case_is_not_honoured() -> None:
    environ = {
        "DATABASE_URL": RDS_URL,
        "allow_alembic_upgrade_head": "I_KNOW_THIS_IS_NOT_PRODUCTION",
    }
    assert not _decide(environ).allowed


# ---------------------------------------------------------------------------
# The project .env, read the way Settings and Compose read it
# ---------------------------------------------------------------------------


# A development URL that is NOT the Settings default, so a skipped .env line
# cannot hide behind the default.
DEV_URL_2 = "postgresql+asyncpg://neurostack_dev:short@127.0.0.1:5434/neurostack_test"

# Forms python-dotenv and the guard read identically; ``{url}`` is the value.
ACCEPTED_DOTENV_FORMS = [
    "DATABASE_URL={url}\n",
    "export DATABASE_URL={url}\n",
    "export\tDATABASE_URL={url}\n",
    'DATABASE_URL="{url}"\n',
    "DATABASE_URL='{url}'\n",
    'export DATABASE_URL="{url}" # quoted with comment\n',
    "DATABASE_URL={url}  # local stack\n",
    "DATABASE_URL = {url}\n",
    "  DATABASE_URL={url}\n",
    "DATABASE_URL\t=\t{url}\t\n",
    "database_url={url}\n",
    "Database_Url={url}\n",
    "# DATABASE_URL=postgresql://x@db.example/x\nDATABASE_URL={url}\n",
    "DATABASE_URL={url}\r\n",
    "A=1\rDATABASE_URL={url}\r",
    "DATABASE_URL={url}",
    "UNRELATED=${{HOME}}\nQUOTED='a \\' b'\nDATABASE_URL={url}\n",
]


@pytest.mark.parametrize("form", ACCEPTED_DOTENV_FORMS)
def test_every_accepted_env_file_form_is_read_not_skipped(form: str) -> None:
    development = form.format(url=DEV_URL_2)
    inputs = guard.resolve_settings_inputs({}, development)
    assert inputs.findings == ()
    assert inputs.database_url == DEV_URL_2
    assert _decide({}, development).allowed
    assert not _decide({}, form.format(url=RDS_URL)).allowed


# Shapes python-dotenv reads differently from a naive line reader: the guard
# must refuse each one (not overridable), whatever URL the file carries.
_EXOTIC_WHITESPACE = [
    "\x0b",
    "\x0c",
    "\x1c",
    "\x1d",
    "\x1e",
    "\x1f",
    "\x85",
    "\xa0",
    "\u2028",
    "\u2029",
]
REFUSED_DOTENV_FORMS = [
    f"'DATABASE_URL'={RDS_URL}\n",
    f"export 'DATABASE_URL'={RDS_URL}\n",
    f"DATABASE_URL={DEV_URL_2}\n'DATABASE_URL'={RDS_URL}\n",
    *(f"DATABASE_URL{ch}={RDS_URL}\n" for ch in _EXOTIC_WHITESPACE),
    *(
        f"DATABASE_URL={DEV_URL_2}{ch}DATABASE_URL={RDS_URL}\n"
        for ch in _EXOTIC_WHITESPACE
    ),
    f"\ufeffDATABASE_URL={RDS_URL}\n",
    f"DATABASE_URL={DEV_URL_2}\nDATABASE_URL\n",
    f'NOTE="multi\nDATABASE_URL={RDS_URL}"\n',
    f'NOTE="never closes\nDATABASE_URL={RDS_URL}\n',
    f'DATABASE_URL="{DEV_URL_2}\n',
    f'DATABASE_URL="{RDS_URL}" trailing\n',
    # python-dotenv drops this binding (Settings falls back to the default);
    # a reader that kept DEV_URL_2 would disagree with Settings.
    f'DATABASE_URL="{DEV_URL_2}" trailing\n',
    f"DATABASE_URL='{DEV_URL_2}'trailing\n",
    f"DATABASE-URL={RDS_URL}\n",
    f"DATABASE_URL={DEV_URL_2}\nDATABASE_URL={RDS_URL}\n",
    f"DATABASE_URL={DEV_URL_2}\ndatabase_url={DEV_URL_2}\n",
    # Interpolation and escapes are resolved by python-dotenv, never by the guard.
    "P=x@prod-db.example:5432/neurostack?\n"
    "DATABASE_URL=postgresql+asyncpg://neurostack:${P}@localhost/neurostack\n",
    f"DB={RDS_URL}\nDATABASE_URL=${{DB}}\n",
    f"DATABASE_URL='{DEV_URL_2}${{X:-}}'\n",
    'DATABASE_URL="postgresql://neurostack:a\\"b@localhost/neurostack"\n',
    "DATABASE_URL='postgresql://neurostack:a\\'b@localhost/neurostack'\n",
    f"ENVIRONMENT\x0b=production\nDATABASE_URL={DEV_URL_2}\n",
    f"'ENVIRONMENT'=production\nDATABASE_URL={DEV_URL_2}\n",
    "DATABASE_URL=\n",
    # A quoted value ending in a backslash: python-dotenv reads the closing
    # quote as escaped and swallows the following lines (Settings then falls
    # back to its default) while a line reader would keep DEV_URL_2.
    f"X='a\\'\nDATABASE_URL={DEV_URL_2}\nY=b'\n",
    f"X='a\\\\'\nDATABASE_URL={DEV_URL_2}\nY=b'\n",
    f'X="a\\"\nDATABASE_URL={DEV_URL_2}\nY=b"\n',
    f"X='a\\' # c\nDATABASE_URL={DEV_URL_2}\nY=b'\n",
    f"DATABASE_URL='{DEV_URL_2}\\'\n",
]


@pytest.mark.parametrize("character", _EXOTIC_WHITESPACE)
def test_exotic_whitespace_is_named_in_the_refusal(character: str) -> None:
    for dotenv in (
        f"DATABASE_URL{character}={RDS_URL}\n",
        f"DATABASE_URL={DEV_URL_2}{character}\n",
        f"UNRELATED=a{character}b\nDATABASE_URL={DEV_URL_2}\n",
    ):
        decision = _decide({}, dotenv)
        assert not decision.allowed
        assert any(
            "whitespace or control characters" in finding.message
            for finding in decision.findings
        ), dotenv


@pytest.mark.parametrize("dotenv", REFUSED_DOTENV_FORMS)
def test_env_file_shapes_python_dotenv_reads_differently_are_refused(
    dotenv: str,
) -> None:
    decision = _decide({}, dotenv)
    assert not decision.allowed
    assert any(not finding.overridable for finding in decision.findings)
    assert not _decide({}, dotenv, database_url=None).override_used
    assert not _decide(dict(OVERRIDE), dotenv).allowed


@pytest.mark.parametrize(
    ("dotenv", "allowed"),
    [
        (f"DATABASE_URL={DEV_URL_2}\nENVIRONMENT=development\n", True),
        (f"DATABASE_URL={DEV_URL_2}\nENVIRONMENT=staging\n", False),
        (f"DATABASE_URL={DEV_URL_2}\nexport ENVIRONMENT=production\n", False),
        (f"DATABASE_URL={DEV_URL_2}\nenvironment='production'\n", False),
        (f"DATABASE_URL={DEV_URL_2}\nPOSTGRES_USER={VPS_USER}\n", False),
        (f"DATABASE_URL={DEV_URL_2}\nexport POSTGRES_DB={VPS_DATABASE}\n", False),
        (f"DATABASE_URL={DEV_URL_2}\nPOSTGRES_PASSWORD='{STRONG_PASSWORD}'\n", False),
        ("POSTGRES_USER=neurostack\nPOSTGRES_DB=neurostack\n", True),
        ("", True),
    ],
)
def test_env_file_environment_and_compose_values(dotenv: str, allowed: bool) -> None:
    assert _decide({}, dotenv).allowed is allowed


class _SettingsProbe(BaseSettings):
    """Settings' own .env machinery (same model_config) for the two fields."""

    model_config = SettingsConfigDict(**{**Settings.model_config, "env_file": None})
    database_url: str = SETTINGS_DEFAULT
    environment: str | None = None


_DIFFERENTIAL_CASES = [
    # ``export`` needs whitespace: this key is ``exportDATABASE_URL``, which
    # Settings ignores (it falls back to its default).
    f"exportDATABASE_URL={DEV_URL_2}\n",
    f"export\tDATABASE_URL={DEV_URL_2}\nexportENVIRONMENT=staging\n",
    # Whitespace inside quotes is part of the value for python-dotenv.
    f'DATABASE_URL=" {DEV_URL_2} "\n',
    f"DATABASE_URL=' {DEV_URL_2}'\n",
    "ENVIRONMENT=' test '\n",
    'ENVIRONMENT="development "\n',
    *(
        form.format(url=url)
        for form in ACCEPTED_DOTENV_FORMS
        for url in (DEV_URL_2, RDS_URL)
    ),
    *REFUSED_DOTENV_FORMS,
    f"DATABASE_URL={DEV_URL_2}\nENVIRONMENT=staging\n",
    f"export ENVIRONMENT='test'\nDATABASE_URL={DEV_URL_2}\n",
    "ENVIRONMENT=production # c\n",
    "",
]


@pytest.mark.parametrize("dotenv", _DIFFERENTIAL_CASES)
def test_guard_resolution_matches_settings_or_refuses(
    dotenv: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Differential: what the guard resolves is what Settings resolves.

    Whenever the guard's DATABASE_URL/ENVIRONMENT would differ from what
    pydantic-settings (python-dotenv) reads from the same file, the guard must
    have refused instead.
    """
    for name in list(os.environ):
        if name.lower() in {"database_url", "environment", "p", "db", "x", "home"}:
            monkeypatch.delenv(name)
    env_file = tmp_path / ".env"
    env_file.write_text(dotenv, encoding="utf-8", newline="")
    probe = _SettingsProbe(_env_file=env_file)  # type: ignore[call-arg]
    text = env_file.read_text(encoding="utf-8")
    inputs = guard.resolve_settings_inputs({}, text)
    if inputs.findings:
        assert not _decide({}, text).allowed
        return
    assert (inputs.database_url, inputs.environment) == (
        probe.database_url,
        probe.environment,
    )


def test_process_environment_wins_over_the_env_file() -> None:
    assert _decide({"DATABASE_URL": DEV_URL}, f"DATABASE_URL={RDS_URL}\n").allowed
    assert not _decide({"DATABASE_URL": RDS_URL}, f"DATABASE_URL={DEV_URL}\n").allowed
    assert not _decide({"ENVIRONMENT": "staging"}, "ENVIRONMENT=development\n").allowed


def test_the_vps_env_file_is_refused() -> None:
    """The VPS .env must name identifiers deploy-vps.sh accepts, never dev ones."""
    dotenv = (
        "ENVIRONMENT=production\n"
        f"POSTGRES_USER={VPS_USER}\n"
        f"POSTGRES_PASSWORD={STRONG_PASSWORD}\n"
        f"POSTGRES_DB={VPS_DATABASE}\n"
    )
    assert not _decide({}, dotenv).allowed
    without_environment = "\n".join(dotenv.splitlines()[1:]) + "\n"
    assert not _decide({}, without_environment).allowed


# ---------------------------------------------------------------------------
# Identifier rule versus scripts/deploy-vps.sh
# ---------------------------------------------------------------------------


def _deploy_vps_weak_identifier_patterns() -> list[str]:
    text = DEPLOY_VPS.read_text(encoding="utf-8")
    body = text[text.index("reject_weak_identifier() {") :]
    body = body[: body.index("\n}\n")]
    match = re.search(r'case "\$normalized" in\n\s*([^\n]+)\)\n', body)
    assert match, "reject_weak_identifier case pattern moved"
    return match.group(1).split("|")


@pytest.mark.parametrize(
    "identifier",
    ["neurostack", "neurostack_dev", "neurostack_test", "dev_user", "app_development"],
)
def test_every_identifier_the_guard_accepts_is_rejected_by_deploy_vps(
    identifier: str,
) -> None:
    assert guard.is_development_identifier(identifier)
    patterns = _deploy_vps_weak_identifier_patterns()
    assert any(fnmatch.fnmatchcase(identifier.lower(), p) for p in patterns)


@pytest.mark.parametrize(
    "identifier",
    [
        VPS_USER,
        VPS_DATABASE,
        "nsapp_owner_9f3",
        "neurostack_core",
        "neurostack2",
        "NeuroStack_App",
        "postgres",
        "Neuro",
    ],
)
def test_identifiers_deploy_vps_accepts_or_superusers_are_not_development(
    identifier: str,
) -> None:
    assert not guard.is_development_identifier(identifier)


def test_development_identifier_rule_is_a_subset_of_deploy_vps_rejections() -> None:
    patterns = _deploy_vps_weak_identifier_patterns()
    for marker in guard.DEVELOPMENT_MARKERS:
        assert f"*{marker}*" in patterns, marker
    for identifier in guard.DEVELOPMENT_IDENTIFIERS:
        assert identifier in patterns, identifier


# ---------------------------------------------------------------------------
# The script, run the way the Makefile runs it
# ---------------------------------------------------------------------------

CANARY_PASSWORD = "CanaryPw7f3a"
CANARY_USER = "canary_dev_user"
CANARY_DATABASE = "canary_dev_db"


def _run(
    cwd: Path,
    environ: dict[str, str],
    *args: str,
    script: str | Path = SCRIPT,
) -> subprocess.CompletedProcess[str]:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **environ}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, str(script), *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _assert_no_secret(result: subprocess.CompletedProcess[str]) -> None:
    output = result.stdout + result.stderr
    for canary in (CANARY_PASSWORD, CANARY_USER, CANARY_DATABASE):
        assert canary not in output
    assert "postgresql" not in output


def _canary_url(host: str) -> str:
    return (
        f"postgresql+asyncpg://{CANARY_USER}:{CANARY_PASSWORD}"
        f"@{host}:5432/{CANARY_DATABASE}"
    )


def test_script_allows_a_development_url_without_printing_it(tmp_path: Path) -> None:
    result = _run(tmp_path, {"DATABASE_URL": _canary_url("localhost")})
    assert result.returncode == 0, result.stderr
    assert "allowed" in result.stderr
    assert "localhost" in result.stderr
    _assert_no_secret(result)


def test_script_refuses_a_production_url_without_printing_it(tmp_path: Path) -> None:
    host = "neurostack-prod.c1abcdefghij.us-east-1.rds.amazonaws.com"
    result = _run(tmp_path, {"DATABASE_URL": _canary_url(host)})
    assert result.returncode == 1
    assert "REFUSED" in result.stderr
    assert host in result.stderr
    assert "alembic upgrade <revision>" in result.stderr
    _assert_no_secret(result)


def test_script_refuses_a_garbled_url_without_printing_it(tmp_path: Path) -> None:
    garbled = f"postgresql://{CANARY_USER}:{CANARY_PASSWORD}@x@y,z/{CANARY_DATABASE}"
    result = _run(tmp_path, {"DATABASE_URL": garbled})
    assert result.returncode == 1
    _assert_no_secret(result)


def test_script_reads_the_env_file_in_its_working_directory(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        f"DATABASE_URL={_canary_url('localhost')}\nPOSTGRES_DB={VPS_DATABASE}\n",
        encoding="utf-8",
    )
    result = _run(tmp_path, {})
    assert result.returncode == 1
    assert "POSTGRES_DB" in result.stderr
    assert VPS_DATABASE not in result.stderr
    _assert_no_secret(result)


def test_script_explicit_database_url_argument(tmp_path: Path) -> None:
    allowed = _run(tmp_path, {}, "--database-url", _canary_url("127.0.0.1"))
    assert allowed.returncode == 0, allowed.stderr
    _assert_no_secret(allowed)
    refused = _run(tmp_path, {}, "--database-url", _canary_url("db.example"))
    assert refused.returncode == 1
    _assert_no_secret(refused)


def test_script_override_prints_a_loud_warning(tmp_path: Path) -> None:
    environ = {"DATABASE_URL": _canary_url("db.example"), **OVERRIDE}
    result = _run(tmp_path, environ)
    assert result.returncode == 0, result.stderr
    assert "WARNING" in result.stderr
    assert "ALLOW_ALEMBIC_UPGRADE_HEAD" in result.stderr
    assert "db.example" in result.stderr
    _assert_no_secret(result)
    refused = _run(tmp_path, {**environ, "ENVIRONMENT": "staging"})
    assert refused.returncode == 1
    _assert_no_secret(refused)


def test_script_runs_from_the_repository_root_with_a_relative_path(
    tmp_path: Path,
) -> None:
    (tmp_path / "scripts").mkdir()
    shutil.copy2(SCRIPT, tmp_path / "scripts" / SCRIPT.name)
    result = _run(
        tmp_path,
        {"DATABASE_URL": DEV_URL},
        script="scripts/guard_alembic_upgrade_head.py",
    )
    assert result.returncode == 0, result.stderr


def test_script_unreadable_env_file_fails_closed(tmp_path: Path) -> None:
    (tmp_path / ".env").write_bytes(b"DATABASE_URL=\xff\xfe\n")
    result = _run(tmp_path, {"DATABASE_URL": DEV_URL})
    assert result.returncode == 1


@pytest.mark.parametrize(
    "url",
    [
        b"postgresql://canaryuser:" + CANARY_PASSWORD.encode() + b"\xff@localhost/x",
        b"postgresql://canary\xffuser:" + CANARY_PASSWORD.encode() + b"@localhost/x",
        b"postgresql://canaryuser:" + CANARY_PASSWORD.encode() + b"@local\xffhost/x",
        b"postgresql://canaryuser:" + CANARY_PASSWORD.encode() + b"@localhost/x\xff",
    ],
    ids=["password", "user", "host", "database"],
)
def test_script_refuses_a_non_utf8_url_without_a_traceback(
    tmp_path: Path, url: bytes
) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=tmp_path,
        env={b"PATH": b"/usr/bin:/bin", b"DATABASE_URL": url},
        capture_output=True,
        timeout=60,
        check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 1
    assert b"Traceback" not in output
    assert b"not valid UTF-8" in output
    assert CANARY_PASSWORD.encode() not in output
    assert b"canaryuser" not in output


def test_non_utf8_url_is_a_structural_refusal() -> None:
    url = "postgresql://neurostack:\udcff@localhost/neurostack"
    decision = _decide({"DATABASE_URL": url, **OVERRIDE})
    assert not decision.allowed
    assert "url" in _codes(decision)


@pytest.mark.parametrize(
    "url",
    [
        f"postgresql://{CANARY_USER}:12345/{CANARY_PASSWORD}@localhost/{CANARY_DATABASE}",
        f"postgresql://{CANARY_USER}:{CANARY_PASSWORD}/x@localhost/{CANARY_DATABASE}",
        f"postgresql://{CANARY_USER}:pa?{CANARY_PASSWORD}@localhost/{CANARY_DATABASE}",
    ],
)
def test_malformed_credentials_print_unparsable_not_the_user(
    tmp_path: Path, url: str
) -> None:
    result = _run(tmp_path, {"DATABASE_URL": url})
    assert result.returncode == 1
    assert "host 'unparsable'" in result.stderr
    _assert_no_secret(result)
    assert "canary" not in result.stderr.lower()


@pytest.mark.parametrize(
    ("operation", "action"),
    [
        (None, "alembic upgrade to the head"),
        ("upgrade-head", "alembic upgrade to the head"),
        ("downgrade", "alembic downgrade"),
        ("reset", "database reset (DROP SCHEMA public"),
    ],
)
def test_script_names_the_guarded_operation(
    tmp_path: Path, operation: str | None, action: str
) -> None:
    args = [] if operation is None else ["--operation", operation]
    refused = _run(tmp_path, {"DATABASE_URL": RDS_URL}, *args)
    assert refused.returncode == 1
    assert f"REFUSED {action}" in refused.stderr
    allowed = _run(tmp_path, {"DATABASE_URL": DEV_URL}, *args)
    assert allowed.returncode == 0
    assert f"allowed {action}" in allowed.stderr


def test_script_rejects_an_unknown_operation(tmp_path: Path) -> None:
    result = _run(tmp_path, {"DATABASE_URL": DEV_URL}, "--operation", "upgrade")
    assert result.returncode == 2


@pytest.mark.parametrize("marker", ["prod", "prd", "stag", "stg", "live"])
def test_every_production_marker_beats_a_development_marker(marker: str) -> None:
    for identifier in (f"dev_{marker}", f"{marker}_test", f"neurostack_dev{marker}"):
        assert not guard.is_development_identifier(identifier), identifier
        url = f"postgresql://neurostack:x@localhost/{identifier}"
        assert not _decide({"DATABASE_URL": url}).allowed


@pytest.mark.parametrize(
    ("password", "production_grade"),
    [
        ("aA1bB2cC3" * 3, False),  # 27 chars, 3 classes, 9 distinct
        ("aA1bB2cC3d" * 3, True),  # 30 chars, 3 classes, 10 distinct
        ("aA1bB2cC3dE4fF5gG6hH7iJ", False),  # 23 chars
        ("aA1bB2cC3dE4fF5gG6hH7iJ8", True),  # 24 chars
        ("abcdefghijklmnopqrstuvwxyz", False),  # 1 class
        ("abcdefghijklmABCDEFGHIJKLM", False),  # 2 classes
        ("abcdefghijklmABCDEFGHIJKL1", True),  # 3 classes
        ("abcdefghijklm-nopqrstuvwxyz1", True),  # lower, digit, symbol
        ("aA1bB2cC3dE4fF5gG6hH7iJ8dev", False),  # weak marker
        ("aA1bB2cC3dE4fF5gG6hH7iJ8Example", False),  # weak marker, any case
    ],
)
def test_production_grade_password_boundaries(
    password: str, production_grade: bool
) -> None:
    assert guard.is_production_grade_secret(password) is production_grade
    url = f"postgresql://neurostack:{password}@localhost/neurostack"
    assert _decide({"DATABASE_URL": url}).allowed is not production_grade


def test_fingerprint_ignores_the_password_only() -> None:
    first = _decide({"DATABASE_URL": DEV_URL.replace(":neurostack@", ":one@")})
    second = _decide({"DATABASE_URL": DEV_URL.replace(":neurostack@", ":two@")})
    no_password = _decide({"DATABASE_URL": DEV_URL.replace(":neurostack@", "@")})
    other_host = _decide({"DATABASE_URL": DEV_URL.replace("localhost", "127.0.0.1")})
    assert first.fingerprint is not None
    assert first.fingerprint == second.fingerprint == no_password.fingerprint
    assert first.fingerprint != other_host.fingerprint


def test_script_has_no_third_party_imports() -> None:
    tree = SCRIPT.read_text(encoding="utf-8")
    imported = set(re.findall(r"^(?:from|import) ([a-zA-Z_][\w]*)", tree, re.M))
    assert imported <= set(sys.stdlib_module_names) | {"__future__"}, imported


# ---------------------------------------------------------------------------
# Makefile
# ---------------------------------------------------------------------------


def _make_targets() -> dict[str, list[str]]:
    """Recipe LOGICAL lines per target (backslash continuations joined).

    Each logical line is one shell invocation: ``make -i``/``-k`` ignores a
    failing line and runs the next one, so a guard on its own line would not
    stop the destructive command.
    """
    targets: dict[str, list[str]] = {}
    current: list[str] | None = None
    pending = ""
    for line in MAKEFILE.read_text(encoding="utf-8").splitlines():
        if pending or line.startswith("\t"):
            if current is None:
                continue
            text = pending + line.strip()
            if text.endswith("\\"):
                pending = text[:-1].rstrip() + " "
                continue
            pending = ""
            current.append(" ".join(text.split()))
            continue
        match = re.match(r"^([A-Za-z0-9_.-]+):(?!=)", line)
        current = targets.setdefault(match.group(1), []) if match else None
    return targets


GUARDED_RECIPES = {
    "db-migrate": [
        f"{GUARD_COMMAND} --operation upgrade-head",
        "poetry run alembic upgrade head",
    ],
    "db-rollback": [
        f"{GUARD_COMMAND} --operation downgrade",
        "poetry run alembic downgrade -1",
    ],
    "db-reset": [
        f"{GUARD_COMMAND} --operation reset",
        'docker compose exec postgres psql -U neurostack -d neurostack -c "DROP '
        'SCHEMA public CASCADE; CREATE SCHEMA public;"',
        "poetry run alembic upgrade head",
    ],
}
DESTRUCTIVE = re.compile(r"alembic\s+(?:upgrade|downgrade|stamp)|DROP\s+SCHEMA", re.I)


@pytest.mark.parametrize("target", sorted(GUARDED_RECIPES))
def test_guarded_targets_chain_the_guard_on_one_shell_line(target: str) -> None:
    recipe = _make_targets()[target]
    assert recipe == [" && ".join(GUARDED_RECIPES[target])]


def test_no_destructive_command_runs_outside_a_guarded_chain() -> None:
    for target, recipe in _make_targets().items():
        for line in recipe:
            if not DESTRUCTIVE.search(line):
                continue
            assert target in GUARDED_RECIPES, target
            assert line.startswith(GUARD_COMMAND + " "), line
            assert not line.startswith(("-", "@", "+")), line
            for separator in (";", "||", "|", "\n"):
                assert separator not in line.replace(
                    "DROP SCHEMA public CASCADE; CREATE SCHEMA public;", ""
                ), (target, separator)
            assert "& " not in line.replace("&& ", ""), line


def _fake_tools(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "ran.log"
    poetry = bin_dir / "poetry"
    poetry.write_text(
        "#!/bin/sh\n"
        "shift\n"
        f'if [ "$1" = python ]; then shift; exec "{sys.executable}" "$@"; fi\n'
        f'echo "poetry $*" >> "{log}"\n',
        encoding="utf-8",
    )
    docker = bin_dir / "docker"
    docker.write_text(f'#!/bin/sh\necho "docker $*" >> "{log}"\n', encoding="utf-8")
    for tool in (poetry, docker):
        tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    return bin_dir, log


@pytest.mark.skipif(shutil.which("make") is None, reason="make is not installed")
@pytest.mark.parametrize("target", sorted(GUARDED_RECIPES))
@pytest.mark.parametrize("flags", [[], ["-i"], ["-k"], ["-i", "-k"]])
def test_make_stops_at_the_guard_even_when_ignoring_errors(
    tmp_path: Path, target: str, flags: list[str]
) -> None:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy2(MAKEFILE, repo / "Makefile")
    shutil.copy2(SCRIPT, repo / "scripts" / SCRIPT.name)
    bin_dir, log = _fake_tools(tmp_path)
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "DATABASE_URL": RDS_URL,
    }
    result = subprocess.run(
        ["make", *flags, target],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert "REFUSED" in result.stderr
    assert not log.exists(), log.read_text(encoding="utf-8")
    allowed = subprocess.run(
        ["make", *flags, target],
        cwd=repo,
        env={**env, "DATABASE_URL": DEV_URL},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert allowed.returncode == 0, allowed.stderr
    assert log.read_text(encoding="utf-8").strip()


# ---------------------------------------------------------------------------
# Compose files
# ---------------------------------------------------------------------------

GUARD_ENTRYPOINT = [
    "sh",
    "-c",
    'python scripts/guard_alembic_upgrade_head.py && exec "$$@"',
    "guard_alembic_upgrade_head",
]
GUARD_MOUNT = (
    "./scripts/guard_alembic_upgrade_head.py:"
    "/app/scripts/guard_alembic_upgrade_head.py:ro"
)
# On the VPS checkout both files share the Compose project name (the directory)
# and the same `.env`, so these services mount the same Docker volume
# `<project>_postgres_data`. A plain `docker compose up -d` there starts the
# dev file against the production data.
SERVICES_SHARING_POSTGRES_DATA = {
    "docker-compose.yml": {"postgres"},
    "docker-compose.production.yml": {"postgres"},
}


def _compose(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _argv(service: dict[str, Any]) -> list[str]:
    parts: list[str] = []
    for key in ("entrypoint", "command"):
        value = service.get(key)
        if value is None:
            continue
        parts.extend(value if isinstance(value, list) else [value])
    return [str(part) for part in parts]


def test_dev_migrate_service_cannot_run_alembic_without_the_guard() -> None:
    migrate = _compose(DEV_COMPOSE)["services"]["migrate"]
    assert migrate["entrypoint"] == GUARD_ENTRYPOINT
    assert migrate["command"] == ["alembic", "upgrade", "head"]
    assert GUARD_MOUNT in migrate["volumes"]
    environment = migrate["environment"]
    assert "NEUROSTACK_CHECKOUT_ENVIRONMENT=${ENVIRONMENT:-}" in environment
    assert not any("ALLOW_ALEMBIC_UPGRADE_HEAD" in item for item in environment)
    assert "env_file" not in migrate


def test_only_the_dev_migrate_service_runs_alembic() -> None:
    for name, service in _compose(DEV_COMPOSE)["services"].items():
        if any("alembic" in part for part in _argv(service)):
            assert name == "migrate", name


def test_production_compose_never_runs_upgrade_head() -> None:
    text = PRODUCTION_COMPOSE.read_text(encoding="utf-8")
    assert re.search(r"upgrade\W+heads?\b", text, re.I) is None
    for name, service in _compose(PRODUCTION_COMPOSE)["services"].items():
        argv = " ".join(_argv(service))
        assert "alembic" not in argv, name


@pytest.mark.parametrize("path", [DEV_COMPOSE, PRODUCTION_COMPOSE])
def test_services_sharing_the_postgres_volume_are_listed(path: Path) -> None:
    compose = _compose(path)
    assert "postgres_data" in compose["volumes"]
    sharing = {
        name
        for name, service in compose["services"].items()
        if any(
            str(volume).split(":", 1)[0] == "postgres_data"
            for volume in service.get("volumes", [])
        )
    }
    assert sharing == SERVICES_SHARING_POSTGRES_DATA[path.name]


def _interpolate(value: str, variables: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        name, operator, default = match.group(1), match.group(2), match.group(3)
        current = variables.get(name, "")
        if operator == ":-" and not current:
            return default or ""
        return current

    return re.sub(r"\$\{([A-Z0-9_]+)(?:(:-|:\?|-|\?)([^}]*))?\}", replace, value)


def _env_lines(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key] = value
    return values


def _run_dev_migrate_entrypoint(
    tmp_path: Path, checkout_env: dict[str, str]
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run the migrate entrypoint and command as the container would, offline.

    The service environment is the dev compose list interpolated with the
    checkout's .env values; ``alembic`` is a fake that records it ran.
    """
    migrate = _compose(DEV_COMPOSE)["services"]["migrate"]
    app = tmp_path / "app"
    (app / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPT, app / "scripts" / SCRIPT.name)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "alembic-ran"
    fake = bin_dir / "alembic"
    fake.write_text(f'#!/bin/sh\necho "$@" > "{marker}"\n', encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    python = bin_dir / "python"
    python.symlink_to(sys.executable)
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"}
    for item in migrate["environment"]:
        key, _, value = item.partition("=")
        env[key] = _interpolate(value, checkout_env)
    entrypoint = [part.replace("$$", "$") for part in migrate["entrypoint"]]
    result = subprocess.run(
        [*entrypoint, *migrate["command"]],
        cwd=app,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return result, marker


def test_dev_migrate_service_runs_alembic_for_the_local_dev_env(
    tmp_path: Path,
) -> None:
    checkout = _env_lines(ROOT / ".env.example")
    result, marker = _run_dev_migrate_entrypoint(tmp_path, checkout)
    assert result.returncode == 0, result.stderr
    assert marker.read_text(encoding="utf-8").split() == ["upgrade", "head"]
    assert "postgres" in result.stderr


@pytest.mark.parametrize(
    "checkout",
    [
        _env_lines(SYNTHETIC_PRODUCTION_ENV),
        {
            "POSTGRES_USER": VPS_USER,
            "POSTGRES_PASSWORD": STRONG_PASSWORD,
            "POSTGRES_DB": VPS_DATABASE,
        },
        {
            "ENVIRONMENT": "production",
            "POSTGRES_USER": "neurostack",
            "POSTGRES_PASSWORD": "neurostack",
            "POSTGRES_DB": "neurostack",
        },
    ],
    ids=["synthetic-production-env", "vps-shaped-env", "production-environment"],
)
def test_dev_migrate_service_refuses_on_the_vps_checkout(
    tmp_path: Path, checkout: dict[str, str]
) -> None:
    result, marker = _run_dev_migrate_entrypoint(tmp_path, checkout)
    assert result.returncode == 1
    assert not marker.exists()
    assert "REFUSED" in result.stderr
    for name in ("POSTGRES_PASSWORD", "POSTGRES_USER", "POSTGRES_DB"):
        value = checkout.get(name, "")
        if value and value != "neurostack":
            assert value not in result.stdout + result.stderr


# ---------------------------------------------------------------------------
# The dev compose file refuses to load without NEUROSTACK_DEV_STACK
# ---------------------------------------------------------------------------

DEV_STACK_VARIABLE = "NEUROSTACK_DEV_STACK"
DEV_STACK_FIELD = "x-neurostack-dev-stack"
REQUIRED_DEV_STACK = re.compile(r"\$\{NEUROSTACK_DEV_STACK:\?([^}]+)\}")
EXAMPLE_ENV_FILES = (ROOT / ".env.example", ROOT / "env.example")


def test_dev_compose_requires_the_dev_stack_variable_at_top_level() -> None:
    """A top-level ``x-`` field is interpolated whenever Compose loads the file.

    So every subcommand (up, down -v, run, exec, logs, config) aborts; a
    per-service label would also recreate the owner's containers.
    """
    text = DEV_COMPOSE.read_text(encoding="utf-8")
    compose = _compose(DEV_COMPOSE)
    assert list(compose)[0] == DEV_STACK_FIELD
    match = REQUIRED_DEV_STACK.fullmatch(compose[DEV_STACK_FIELD])
    assert match, compose[DEV_STACK_FIELD]
    message = match.group(1)
    assert "DEVELOPMENT" in message
    assert "never run on the production checkout" in message
    assert f"{DEV_STACK_VARIABLE}=1" in message
    assert "any non-empty value enables it, even 0" in message
    # No other reference may supply a default or make the variable optional.
    references = re.findall(r"\$\{NEUROSTACK_DEV_STACK[^}]*\}", text)
    assert references == [compose[DEV_STACK_FIELD]]
    assert "env_file" not in text


def test_dev_stack_field_leaves_project_services_and_volumes_alone() -> None:
    compose = _compose(DEV_COMPOSE)
    assert "name" not in compose  # the project name stays the directory name
    assert set(compose["volumes"]) == {
        "postgres_data",
        "redis_data",
        "qdrant_data",
        "grafana_data",
        "prometheus_data",
        "alertmanager_data",
        "loki_data",
    }
    for service in compose["services"].values():
        assert DEV_STACK_VARIABLE not in yaml.safe_dump(service)


def _docker_compose_config(
    tmp_path: Path, extra_env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """``docker compose config`` parses the file client-side; no daemon call."""
    work = tmp_path / "compose"
    work.mkdir(parents=True)
    shutil.copy2(DEV_COMPOSE, work / "docker-compose.yml")
    (work / "dev.env").write_text(
        "POSTGRES_USER=neurostack\nPOSTGRES_PASSWORD=neurostack\n"
        "POSTGRES_DB=neurostack\n",
        encoding="utf-8",
    )
    env = {"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/tmp")}
    return subprocess.run(
        [
            "/usr/bin/docker",
            "compose",
            "--env-file",
            "dev.env",
            "-f",
            "docker-compose.yml",
            "config",
            "--quiet",
        ],
        cwd=work,
        env={**env, **extra_env},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.mark.skipif(
    not Path("/usr/bin/docker").is_file(),
    reason="authoritative Compose renderer is unavailable",
)
def test_docker_compose_refuses_to_load_the_dev_file_without_the_variable(
    tmp_path: Path,
) -> None:
    unset = _docker_compose_config(tmp_path / "unset", {})
    assert unset.returncode != 0
    assert "never run on the production checkout" in unset.stderr
    empty = _docker_compose_config(tmp_path / "empty", {DEV_STACK_VARIABLE: ""})
    assert empty.returncode != 0
    loaded = _docker_compose_config(tmp_path / "set", {DEV_STACK_VARIABLE: "1"})
    assert loaded.returncode == 0, loaded.stderr


@pytest.mark.parametrize("path", EXAMPLE_ENV_FILES, ids=lambda path: path.name)
def test_example_env_files_enable_the_dev_stack(path: Path) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines.count(f"{DEV_STACK_VARIABLE}=1") == 1
    index = lines.index(f"{DEV_STACK_VARIABLE}=1")
    comment = " ".join(lines[max(0, index - 3) : index])
    assert "Development" in comment and "NEVER set it on a production" in comment
    assert "ANY non-empty value (even 0) enables it" in comment


@pytest.mark.parametrize(
    "path",
    [PRODUCTION_COMPOSE, SYNTHETIC_PRODUCTION_ENV, DEPLOY_VPS],
    ids=lambda path: path.name,
)
def test_production_inputs_never_carry_the_dev_stack_variable(path: Path) -> None:
    assert DEV_STACK_VARIABLE not in path.read_text(encoding="utf-8")


def _deploy_vps_env_allowlist() -> set[str]:
    text = DEPLOY_VPS.read_text(encoding="utf-8")
    body = text[text.index("load_runtime_environment() {") :]
    match = re.search(r'case "\$key" in\n\s*([A-Z0-9_|]+)\) ;;\n', body)
    assert match, "the deploy-vps .env key allowlist moved"
    return set(match.group(1).split("|"))


def test_deploy_vps_env_allowlist_excludes_the_dev_stack_variable() -> None:
    allowlist = _deploy_vps_env_allowlist()
    assert {"ENVIRONMENT", "POSTGRES_USER", "POSTGRES_DB"} <= allowlist
    assert DEV_STACK_VARIABLE not in allowlist


def test_deploy_vps_refuses_a_production_env_with_the_dev_stack_variable(
    tmp_path: Path,
) -> None:
    root = tmp_path / "checkout"
    (root / "scripts").mkdir(parents=True)
    copied = root / "scripts" / "deploy-vps.sh"
    shutil.copy2(DEPLOY_VPS, copied)
    digest = "registry.example/neurostack/api@sha256:" + "a" * 64
    (root / "scripts" / "validate_provider_availability_evidence.py").write_text(
        f"#!/usr/bin/env python3\nprint({digest!r})\n", encoding="utf-8"
    )
    (root / "docker-compose.production.yml").write_text("services: {}\n")
    (root / ".env").write_text(
        "SECRET_KEY=Sk9!Unique-Application-Secret-2026xx\n"
        f"POSTGRES_USER={VPS_USER}\nPOSTGRES_PASSWORD=Pg9!Unique-Database-Secret-2026\n"
        f"POSTGRES_DB={VPS_DATABASE}\nREDIS_PASSWORD=Rd8!Unique-Cache-Secret-2026xx\n"
        "QDRANT_API_KEY=Qd7!Unique-Vector-Secret-2026xxx\n"
        'ALLOWED_ORIGINS=["https://app.production.invalid"]\n'
        'ALLOWED_HOSTS=["api.production.invalid"]\n'
        f"{DEV_STACK_VARIABLE}=1\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        ["/bin/bash", str(copied)],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode != 0
    assert "unknown key" in completed.stderr
