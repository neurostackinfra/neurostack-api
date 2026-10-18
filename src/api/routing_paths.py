"""The one request path every path-based decision is taken on: the router's.

Starlette's router matches ``get_route_path(scope)`` (the decoded
``scope["path"]`` with ``root_path`` removed) against each route's compiled
regex.  ``request.url.path`` is a different string: it is rebuilt through
``urllib.parse`` from the Host header and the path, which deletes tab, CR and
LF anywhere, cuts at the first decoded ``?`` or ``#``, prefixes a missing
leading ``/`` and keeps ``root_path``.  A decision taken on it (the auth
bypass, ingress authentication, cache, rate limit) judges a different path
than the one that is routed: ``GET /health%3Fx`` was public for auth and 404
for the router, and ``POST /v1/billing/webhook/asaas%0a`` skipped the
ingress token check yet still reached the receiver.

* ``route_path`` is the exact string the router matches.  Use it where every
  distinct routed string must stay distinct (cache keys) and where the
  consumer re-validates that same string (the SPA registry lookups).
* ``decision_path`` is that string folded exactly as the router's literal
  matcher folds it: Starlette compiles a literal route to
  ``^<escaped path>$`` and ``re.match`` lets ``$`` also match before one
  final newline, so ``/x`` and ``/x\\n`` reach the same literal route.  Exact
  and prefix decisions (public paths, rehearsal targets, guarded ingress
  endpoints, rate-limit exemptions, cache TTLs) use it, so an exact
  restriction cannot be skipped by a trailing ``%0a`` that still routes.

``tests/api/test_route_path_decisions.py`` pins ``decision_path`` to
Starlette's own ``compile_path`` matcher, and
``tests/api/test_route_table_auth_invariants.py`` checks the real route table
against the auth middleware's public paths.
"""

from __future__ import annotations

from starlette._utils import get_route_path
from starlette.types import Scope


def route_path(scope: Scope) -> str:
    """The exact path string Starlette's router matches for ``scope``."""
    return get_route_path(scope)


def decision_path(scope: Scope) -> str:
    """``route_path`` with the one trailing newline a literal route ignores."""
    path = get_route_path(scope)
    return path[:-1] if path.endswith("\n") else path
