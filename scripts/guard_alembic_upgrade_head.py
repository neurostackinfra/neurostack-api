#!/usr/bin/env python3
"""Refuse ``alembic upgrade head`` (and the other developer database operations)
unless the target is a local development database.

The production migration window is pinned to one exact revision (see
docs/runbooks/asaas-deployment.md and scripts/deploy-vps.sh). The developer
paths that migrate to the head, roll back or reset (``make db-migrate``,
``make db-rollback``, ``make db-reset`` and the ``migrate`` service of
docker-compose.yml) run this guard first, chained with ``&&`` on the same
shell line, and stop when it exits non-zero. ``--operation`` (``upgrade-head``,
the default, ``downgrade`` or ``reset``) only names the operation in the
messages; the rules are the same. Pure standard library: it runs before Poetry dependencies,
inside the dev image and on a bare host alike.

Inputs are resolved the way the migration itself resolves them:
``DATABASE_URL`` and ``ENVIRONMENT`` follow src/config/settings.py (process
environment, names case-insensitive, then the ``.env`` file in the working
directory, then the Settings default); ``POSTGRES_USER``, ``POSTGRES_DB`` and
``POSTGRES_PASSWORD`` follow Compose interpolation (process environment, then
``.env``). ``--database-url`` replaces the resolved ``DATABASE_URL``. The
``.env`` file is accepted only in the subset where this reader and
python-dotenv provably agree (see ``_parse_dotenv``); anything else, and any
value that needs interpolation or escapes, refuses (not overridable).

The operation is allowed only when every rule holds:

1. ``DATABASE_URL`` is valid UTF-8 and parses as one PostgreSQL URL
   (``postgres``/``postgresql`` with an optional ``+driver``) with exactly
   one host, no whitespace or control characters, no fragment, exactly one
   ``@`` at most and none after the host, and a valid port. Unix-socket
   forms, an empty host and several hosts fail closed.
2. The host is loopback (``localhost``, ``127.0.0.1``, ``::1``) or
   ``postgres``, the database service of docker-compose.yml.
3. The user and the database name are development identifiers: exactly
   ``neurostack`` (the .env.example / Settings default) or containing ``dev``
   or ``test``, and never containing ``prod``, ``prd``, ``stag``, ``stg`` or
   ``live``. scripts/deploy-vps.sh refuses every such identifier for the VPS
   database, so no VPS identifier is ever a development identifier. Variants
   such as ``neurostack_core`` are not development identifiers, and neither is
   ``postgres`` (the superuser and the RDS default master user; it also
   contains the ``stg`` marker).
4. The password, when present, is not production-grade: scripts/deploy-vps.sh
   accepts a secret with 24+ characters, 3 character classes, 10 distinct
   characters and no weak marker; such a password means a real database.
5. Query parameters are limited to connection tuning (``ssl``, ``sslmode``,
   ``connect_timeout``, ``application_name``, ``command_timeout``,
   ``timeout``, ``prepared_statement_cache_size``); anything else, notably
   ``host``/``hostaddr``/``service``/``user``/``dbname``, is refused.
6. ``ENVIRONMENT`` is unset, ``development`` or ``test`` (exact, lowercase,
   like the Settings literal). ``staging`` is NOT development: real production
   runs with ``ENVIRONMENT=staging``. ``NEUROSTACK_CHECKOUT_ENVIRONMENT``,
   which docker-compose.yml fills from the checkout's ``ENVIRONMENT``, must
   be empty or one of those values too.
7. ``POSTGRES_USER``/``POSTGRES_DB`` (when set) are development identifiers
   and ``POSTGRES_PASSWORD`` (when set) is not production-grade: these name
   the Compose database of this checkout, the one ``make db-reset`` drops.

The only override is ``ALLOW_ALEMBIC_UPGRADE_HEAD=I_KNOW_THIS_IS_NOT_PRODUCTION``
in the process environment (never read from ``.env``; any other non-empty
value is refused). It waives rules 2-5 and 7, printing a loud warning, and
never rules 1 or 6: under ``staging``, ``production`` or any other
environment it is refused. docker-compose.yml does not forward it.

The URL itself is never printed: only a printable host (``unparsable`` when
the URL is malformed) and a short SHA-256 fingerprint of the URL without its
password.

Exit status: 0 allowed, 1 refused, 2 usage error.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import os
import re
import sys
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit

PROG = "guard_alembic_upgrade_head"

OVERRIDE_VARIABLE = "ALLOW_ALEMBIC_UPGRADE_HEAD"
OVERRIDE_VALUE = "I_KNOW_THIS_IS_NOT_PRODUCTION"
CHECKOUT_ENVIRONMENT_VARIABLE = "NEUROSTACK_CHECKOUT_ENVIRONMENT"
# What the caller runs after the guard (``--operation``), for the messages.
OPERATIONS = {
    "upgrade-head": "alembic upgrade to the head",
    "downgrade": "alembic downgrade",
    "reset": "database reset (DROP SCHEMA public, then upgrade to the head)",
}

# Equal to Settings.model_fields["database_url"].default (a test pins it).
SETTINGS_DEFAULT_DATABASE_URL = (
    "postgresql+asyncpg://neurostack:neurostack@localhost:5434/neurostack"
)
DEVELOPMENT_ENVIRONMENTS = frozenset({"development", "test"})
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
# The database service of docker-compose.yml.
COMPOSE_DATABASE_HOSTS = frozenset({"postgres"})
DEVELOPMENT_IDENTIFIERS = frozenset({"neurostack"})
DEVELOPMENT_MARKERS = ("dev", "test")
PRODUCTION_MARKERS = ("prod", "prd", "stag", "stg", "live")
ALLOWED_QUERY_KEYS = frozenset(
    {
        "ssl",
        "sslmode",
        "connect_timeout",
        "application_name",
        "command_timeout",
        "timeout",
        "prepared_statement_cache_size",
    }
)
COMPOSE_IDENTIFIER_VARIABLES = ("POSTGRES_USER", "POSTGRES_DB")
COMPOSE_PASSWORD_VARIABLE = "POSTGRES_PASSWORD"
# scripts/deploy-vps.sh reject_weak_secret: exact values and substrings.
WEAK_SECRET_VALUES = frozenset(
    {"postgres", "neurostack", "password", "secret", "admin", "root"}
)
WEAK_SECRET_MARKERS = (
    "dev",
    "test",
    "default",
    "changeme",
    "change-me",
    "example",
    "password",
)

_SCHEME = re.compile(r"postgres(?:ql)?(?:\+[a-z0-9_]+)?")
_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]+")
_PRINTABLE_HOST = re.compile(r"[a-z0-9.:-]{1,253}")
_PRINTABLE_KEY = re.compile(r"[A-Za-z0-9_]{1,64}")
# python-dotenv's line endings, quoted-value and comment grammar (dotenv/parser.py)
# restricted to plain keys and to space/tab as the only in-line whitespace.
_DOTENV_NEWLINE = re.compile(r"\r\n|\r|\n")
_DOTENV_EXPORT = re.compile(r"export[ \t]+")
_DOTENV_BINDING = re.compile(
    r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)[ \t]*=[ \t]*"
    r"(?:'(?P<single>(?:\\'|[^'])*)'[ \t]*(?:#.*)?"
    r'|"(?P<double>(?:\\"|[^"])*)"[ \t]*(?:#.*)?'
    r"|(?P<unquoted>[^'\"].*)?)"
)


@dataclass(frozen=True)
class Finding:
    """One reason to refuse; ``overridable`` findings yield to the override."""

    code: str
    message: str
    overridable: bool


@dataclass(frozen=True)
class Decision:
    allowed: bool
    findings: tuple[Finding, ...]
    override_used: bool
    host: str | None
    fingerprint: str | None


class _DotenvError(ValueError):
    pass


def _unsupported_character(ch: str) -> bool:
    """Characters python-dotenv treats as whitespace or line breaks.

    Its regexes use ``\\s`` (VT, FF, 0x1c-0x1f, NEL, U+2028, U+2029, NBSP, ...)
    while a line-based reader would split or keep them, so the two could see
    different keys. Only space and tab are accepted inside a line.
    """
    if ch in " \t\r\n":
        return False
    return ch.isspace() or unicodedata.category(ch) == "Cc"


def _parse_dotenv(text: str) -> list[tuple[str, str]]:
    """Parse ``.env`` exactly as python-dotenv (what Settings uses) does, or refuse.

    The accepted subset is: blank lines, ``#`` comments and ``[export ]KEY=value``
    with a plain key, a single- or double-quoted value closed on its line
    (optionally followed by a comment) or an unquoted value whose `` #`` starts
    a comment. Lines end at CRLF, CR or LF only. Every other line (a quoted
    key, a key without ``=``, a quote that does not close on its line, ...)
    and any other whitespace or control character fail the whole file,
    because python-dotenv would read such a file differently. Escapes and
    interpolation inside values are kept raw and refused where the guard
    needs the value (see ``_lookup``).
    """
    if any(_unsupported_character(ch) for ch in text):
        raise _DotenvError(
            "it contains whitespace or control characters other than space, tab "
            "and line breaks"
        )
    entries: list[tuple[str, str]] = []
    for number, line in enumerate(_DOTENV_NEWLINE.split(text), start=1):
        body = line.lstrip(" \t")
        if not body or body.startswith("#"):
            continue
        export = _DOTENV_EXPORT.match(body)
        if export is not None:
            body = body[export.end() :]
        match = _DOTENV_BINDING.fullmatch(body)
        if match is None:
            raise _DotenvError(f"line {number} is not a plain KEY=value binding")
        quoted = match.group("single")
        if quoted is None:
            quoted = match.group("double")
        if quoted is not None:
            # python-dotenv reads a closing quote right after a backslash as an
            # escaped quote and keeps reading into the following lines.
            if quoted.endswith("\\"):
                raise _DotenvError(
                    f"line {number} has a quoted value ending in a backslash"
                )
            value = quoted
        else:
            value = re.sub(r"\s+#.*", "", match.group("unquoted") or "").rstrip()
        entries.append((match.group("key"), value))
    return entries


def _lookup(
    name: str,
    environ: Mapping[str, str],
    dotenv: list[tuple[str, str]],
    *,
    case_insensitive: bool,
    findings: list[Finding],
) -> str | None:
    """Resolve one variable: process environment first, then ``.env``."""

    def matches(key: str) -> bool:
        return key.lower() == name.lower() if case_insensitive else key == name

    for source, items in (
        ("environment", list(environ.items())),
        (".env", dotenv),
    ):
        values = [value for key, value in items if matches(key)]
        if not values:
            continue
        if source == ".env" and len(values) > 1:
            findings.append(
                Finding("ambiguous", f"{name} is set more than once in .env", False)
            )
            return None
        if len(set(values)) > 1:
            findings.append(
                Finding(
                    "ambiguous",
                    f"{name} is set with conflicting values in the {source}",
                    False,
                )
            )
            return None
        value = values[0]
        if source == ".env" and ("$" in value or "\\" in value):
            findings.append(
                Finding(
                    "dotenv",
                    f"{name} in .env uses interpolation or escapes the guard "
                    "cannot resolve",
                    False,
                )
            )
            return None
        return value
    return None


def is_development_identifier(value: str) -> bool:
    """True only for an identifier shaped like a development database's."""
    if not _IDENTIFIER.fullmatch(value):
        return False
    lowered = value.lower()
    if any(marker in lowered for marker in PRODUCTION_MARKERS):
        return False
    return lowered in DEVELOPMENT_IDENTIFIERS or any(
        marker in lowered for marker in DEVELOPMENT_MARKERS
    )


def is_production_grade_secret(value: str) -> bool:
    """True when scripts/deploy-vps.sh would accept ``value`` as a secret."""
    lowered = value.lower()
    if len(value) < 24 or lowered in WEAK_SECRET_VALUES:
        return False
    if any(marker in lowered for marker in WEAK_SECRET_MARKERS):
        return False
    classes = sum(
        bool(re.search(pattern, value))
        for pattern in (r"[a-z]", r"[A-Z]", r"[0-9]", r"[^a-zA-Z0-9]")
    )
    return classes >= 3 and len(set(value)) >= 10


def _printable_host(host: str | None) -> str | None:
    if host is None:
        return None
    return host if _PRINTABLE_HOST.fullmatch(host) else "<unprintable host>"


def _fingerprint(url: str) -> str | None:
    """SHA-256 prefix of the URL without its password."""
    try:
        parts = urlsplit(url)
        netloc = parts.netloc
        if "@" in netloc:
            userinfo, _, hostinfo = netloc.rpartition("@")
            netloc = f"{userinfo.partition(':')[0]}@{hostinfo}"
        stripped = urlunsplit(parts._replace(netloc=netloc))
        data = stripped.encode("utf-8", errors="replace")
    except (ValueError, UnicodeError):
        return None
    return hashlib.sha256(data).hexdigest()[:12]


def _check_url(url: str, findings: list[Finding]) -> str | None:
    """Append URL findings; return the parsed host (or None)."""

    def structural(message: str) -> None:
        findings.append(Finding("url", message, False))

    if not url.strip():
        structural("DATABASE_URL is empty")
        return None
    try:
        url.encode("utf-8")
    except UnicodeEncodeError:
        structural("DATABASE_URL is not valid UTF-8")
        return None
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in url):
        structural("DATABASE_URL contains whitespace or control characters")
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        structural("DATABASE_URL does not parse as a URL")
        return None
    if not _SCHEME.fullmatch(parts.scheme):
        structural("DATABASE_URL is not a PostgreSQL URL")
        return None
    if parts.fragment or url.endswith("#"):
        structural("DATABASE_URL has a fragment")
        return None
    if parts.netloc.count("@") > 1 or "@" in parts.path + parts.query:
        # An unencoded '/', '?' or '@' in the credentials moves the authority:
        # urllib would then report the user name or the password as the host.
        structural("DATABASE_URL has an unencoded '@' (malformed credentials)")
        return None
    hostinfo = parts.netloc.rpartition("@")[2]
    if "," in hostinfo:
        structural("DATABASE_URL names several hosts")
        return None
    try:
        host = parts.hostname
        _ = parts.port  # raises on an invalid port
    except ValueError:
        structural("DATABASE_URL has an invalid host or port")
        return None
    if not host:
        structural("DATABASE_URL has no host (Unix-socket or default host form)")
        return None
    if "%" in host:
        structural("DATABASE_URL has a percent-encoded host (Unix-socket path form)")
        return None
    if hostinfo.startswith("[") or ":" in host:
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            structural("DATABASE_URL has an invalid IPv6 host")
            return None

    if host not in LOOPBACK_HOSTS | COMPOSE_DATABASE_HOSTS:
        findings.append(
            Finding(
                "host",
                f"host {_printable_host(host)!r} is neither loopback "
                f"({', '.join(sorted(LOOPBACK_HOSTS))}) nor the dev Compose "
                f"database service ({', '.join(sorted(COMPOSE_DATABASE_HOSTS))})",
                True,
            )
        )

    user = unquote(parts.username) if parts.username else ""
    if not user:
        findings.append(Finding("user", "DATABASE_URL names no user", True))
    elif not is_development_identifier(user):
        findings.append(
            Finding("user", "the database user is not a development identifier", True)
        )

    database = unquote(parts.path[1:]) if parts.path.startswith("/") else ""
    if not database or not is_development_identifier(database):
        findings.append(
            Finding(
                "database",
                "the database name is missing or not a development identifier",
                True,
            )
        )

    password = unquote(parts.password) if parts.password else ""
    if password and is_production_grade_secret(password):
        findings.append(
            Finding(
                "password",
                "the database password is production-grade "
                "(as scripts/deploy-vps.sh defines it)",
                True,
            )
        )

    for key, _ in parse_qsl(parts.query, keep_blank_values=True):
        if key.lower() not in ALLOWED_QUERY_KEYS:
            shown = key if _PRINTABLE_KEY.fullmatch(key) else "<unprintable>"
            findings.append(
                Finding("query", f"unsupported query parameter {shown!r}", True)
            )
    return host


def _check_environment(
    name: str, value: str | None, findings: list[Finding], *, empty_is_unset: bool
) -> None:
    if value is None or (empty_is_unset and value == ""):
        return
    if value not in DEVELOPMENT_ENVIRONMENTS:
        shown = value if _PRINTABLE_KEY.fullmatch(value) else "<unprintable>"
        findings.append(
            Finding(
                "environment",
                f"{name}={shown!r} is not one of "
                f"{', '.join(sorted(DEVELOPMENT_ENVIRONMENTS))} (or unset)",
                False,
            )
        )


@dataclass(frozen=True)
class SettingsInputs:
    """What the guard resolved for Settings; ``None`` values when unresolvable."""

    database_url: str | None
    environment: str | None
    findings: tuple[Finding, ...]


def _resolve(
    environ: Mapping[str, str],
    dotenv_text: str | None,
    database_url: str | None,
    findings: list[Finding],
) -> tuple[list[tuple[str, str]], str | None, str | None]:
    try:
        dotenv = _parse_dotenv(dotenv_text) if dotenv_text is not None else []
    except _DotenvError as error:
        findings.append(Finding("dotenv", f".env is unsupported: {error}", False))
        dotenv = []

    if database_url is None:
        database_url = _lookup(
            "DATABASE_URL",
            environ,
            dotenv,
            case_insensitive=True,
            findings=findings,
        )
        if database_url is None and not findings:
            database_url = SETTINGS_DEFAULT_DATABASE_URL

    environment = _lookup(
        "ENVIRONMENT", environ, dotenv, case_insensitive=True, findings=findings
    )
    return dotenv, database_url, environment


def resolve_settings_inputs(
    environ: Mapping[str, str], dotenv_text: str | None
) -> SettingsInputs:
    """Resolve DATABASE_URL and ENVIRONMENT the way Settings does, or say why not.

    Exposed for the differential test against pydantic-settings/python-dotenv.
    """
    findings: list[Finding] = []
    _, database_url, environment = _resolve(environ, dotenv_text, None, findings)
    return SettingsInputs(database_url, environment, tuple(findings))


def evaluate(
    environ: Mapping[str, str],
    *,
    database_url: str | None = None,
    dotenv_text: str | None = None,
) -> Decision:
    """Decide whether a guarded development operation may run.

    ``environ`` is the process environment, ``dotenv_text`` the content of
    the project ``.env`` (None when there is none) and ``database_url`` an
    explicit URL that replaces the resolved one.
    """
    findings: list[Finding] = []
    dotenv, database_url, environment = _resolve(
        environ, dotenv_text, database_url, findings
    )

    host = _check_url(database_url, findings) if database_url is not None else None
    fingerprint = _fingerprint(database_url) if database_url else None

    _check_environment("ENVIRONMENT", environment, findings, empty_is_unset=False)
    _check_environment(
        CHECKOUT_ENVIRONMENT_VARIABLE,
        environ.get(CHECKOUT_ENVIRONMENT_VARIABLE),
        findings,
        empty_is_unset=True,
    )

    for name in COMPOSE_IDENTIFIER_VARIABLES:
        value = _lookup(
            name, environ, dotenv, case_insensitive=False, findings=findings
        )
        if value and not is_development_identifier(value):
            findings.append(
                Finding(
                    "compose",
                    f"{name} (this checkout's Compose database) is not a "
                    "development identifier",
                    True,
                )
            )
    password = _lookup(
        COMPOSE_PASSWORD_VARIABLE,
        environ,
        dotenv,
        case_insensitive=False,
        findings=findings,
    )
    if password and is_production_grade_secret(password):
        findings.append(
            Finding(
                "compose",
                f"{COMPOSE_PASSWORD_VARIABLE} (this checkout's Compose database) "
                "is production-grade",
                True,
            )
        )

    override = environ.get(OVERRIDE_VARIABLE, "")
    override_requested = override == OVERRIDE_VALUE
    if override and not override_requested:
        findings.append(
            Finding(
                "override",
                f"{OVERRIDE_VARIABLE} is set but not to the exact acknowledgement "
                f"value {OVERRIDE_VALUE}",
                False,
            )
        )

    blocking = [finding for finding in findings if not finding.overridable]
    override_used = override_requested and bool(findings) and not blocking
    allowed = not findings or override_used
    return Decision(
        allowed=allowed,
        findings=tuple(findings),
        override_used=override_used,
        host=_printable_host(host),
        fingerprint=fingerprint,
    )


def _report(decision: Decision, override_requested: bool, operation: str) -> None:
    action = OPERATIONS[operation]
    target = f"host {decision.host or 'unparsable'!r}"
    if decision.fingerprint:
        target += f" (url fingerprint {decision.fingerprint})"
    err = sys.stderr
    if decision.allowed and not decision.override_used:
        print(
            f"{PROG}: allowed {action} on the development database at {target}",
            file=err,
        )
        if override_requested:
            print(
                f"{PROG}: WARNING: {OVERRIDE_VARIABLE} is set but was not needed",
                file=err,
            )
        return
    if decision.override_used:
        banner = "!" * 72
        print(banner, file=err)
        print(
            f"{PROG}: WARNING: {OVERRIDE_VARIABLE}={OVERRIDE_VALUE} is in effect.",
            file=err,
        )
        print(
            f"{PROG}: WARNING: running {action} on {target} although it was "
            "NOT recognised as a development database:",
            file=err,
        )
        for finding in decision.findings:
            print(f"{PROG}: WARNING:   - {finding.message}", file=err)
        print(
            f"{PROG}: WARNING: if this is production, interrupt now; production "
            "is migrated only to its approved revision, never by this path.",
            file=err,
        )
        print(banner, file=err)
        return
    print(f"{PROG}: REFUSED {action} on {target}:", file=err)
    for finding in decision.findings:
        print(f"{PROG}:   - {finding.message}", file=err)
    print(
        f"{PROG}: this development path touches only a local development "
        "database. Migrate any other environment to the revision approved for "
        "it (`alembic upgrade <revision>`, see docs/runbooks/asaas-deployment.md).",
        file=err,
    )
    if any(finding.overridable for finding in decision.findings) and not any(
        not finding.overridable for finding in decision.findings
    ):
        print(
            f"{PROG}: for a development database this guard does not recognise, "
            f"set {OVERRIDE_VARIABLE}={OVERRIDE_VALUE} in the shell (refused "
            "under staging and production).",
            file=err,
        )


def main(
    argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None
) -> int:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "Exit 0 only when the guarded Alembic/reset operation targets a "
            "local development database."
        ),
    )
    parser.add_argument(
        "--operation",
        choices=sorted(OPERATIONS),
        default="upgrade-head",
        help="what the caller runs next, named in the messages (default: "
        "upgrade-head)",
    )
    parser.add_argument(
        "--database-url",
        help="URL to check instead of DATABASE_URL (never printed)",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="project .env read like Settings (default: ./.env)",
    )
    args = parser.parse_args(argv)
    environ = os.environ if environ is None else environ

    dotenv_text: str | None = None
    env_file = Path(args.env_file)
    findings: list[Finding] = []
    try:
        if env_file.is_file():
            dotenv_text = env_file.read_text(encoding="utf-8")
        elif env_file.exists():
            findings.append(Finding("dotenv", f"{env_file} is not a file", False))
    except (OSError, UnicodeDecodeError):
        findings.append(Finding("dotenv", f"{env_file} cannot be read", False))

    decision = evaluate(
        environ, database_url=args.database_url, dotenv_text=dotenv_text
    )
    if findings:
        decision = Decision(
            allowed=False,
            findings=tuple(findings) + decision.findings,
            override_used=False,
            host=decision.host,
            fingerprint=decision.fingerprint,
        )
    _report(
        decision,
        environ.get(OVERRIDE_VARIABLE, "") == OVERRIDE_VALUE,
        args.operation,
    )
    return 0 if decision.allowed else 1


if __name__ == "__main__":
    sys.exit(main())
