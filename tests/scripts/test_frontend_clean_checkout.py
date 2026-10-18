"""Clean-checkout contract for the frontend built into the production image.

The image builds the SPA from a clean git checkout, so every file the build
reads must be tracked and none may be shadowed by stale generated output.
Static analysis only: it does not run pnpm, Vite or Docker.
"""

from __future__ import annotations

import json
import posixpath
import re
import subprocess
from functools import cache
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ARTIFACT = "frontend/artifacts/neurostack"
APP_TSX = ROOT / ARTIFACT / "src" / "App.tsx"
SPA_ROUTES = ROOT / "tests" / "fixtures" / "spa_client_routes.json"
ALIASES = {
    "@/": f"{ARTIFACT}/src/",
    "@assets/": "frontend/attached_assets/",
}
# Every extension Vite may try for an extensionless specifier.
RESOLVE_EXTENSIONS = (".mjs", ".js", ".mts", ".ts", ".jsx", ".tsx", ".json")
SOURCE_SUFFIXES = (".ts", ".tsx")
SPECIFIER = re.compile(
    r"""(?:\bfrom\s+|\bimport\s+|\bimport\(\s*|\bvi\.mock\(\s*)["']([^"'\n]+)["']"""
)
# The start of a <Route ...> element; its attributes are tokenised by
# _route_tags (any order, across lines, nested braces inside {...} values).
ROUTE_OPEN = re.compile(r"<Route\b")
ATTRIBUTE_NAME = re.compile(r"[A-Za-z_$][\w$:-]*")
# A path the route list can be derived from: "x", 'x', {"x"}, {'x'} or a
# template literal without substitutions ({`x`}).
STATIC_PATH = re.compile(
    r"""^(?:"([^"\\]*)"|'([^'\\]*)'"""
    r"""|\{\s*(?:"([^"\\]*)"|'([^'\\]*)'|`([^`\\$]*)`)\s*\})$"""
)
# The path syntax both route lists can express (allow-list): "/" or segments
# that are literals or a single `:param`. Wildcards (`/billing/*`, `:rest*`),
# optional params (`:id?`) and regexps match more than the list would serve.
ROUTE_PATH = re.compile(r"^/$|^(?:/(?:[A-Za-z0-9._~-]+|:[A-Za-z_][A-Za-z0-9_]*))+$")
WOUTER_IMPORT = re.compile(r"""\bimport\s+([^;]*?)\s*\bfrom\s*["']wouter["']\s*;?""")


@cache
def _tracked_files() -> frozenset[str]:
    """Tracked paths present in the working tree (identical to a clean checkout
    once committed; deletions not yet committed are already applied)."""

    def listing(*flags: str) -> set[str]:
        completed = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "-z", *flags],
            capture_output=True,
            check=True,
        )
        return {item for item in completed.stdout.decode().split("\0") if item}

    return frozenset(listing("--cached") - listing("--deleted"))


def _tracked_frontend() -> list[str]:
    return sorted(path for path in _tracked_files() if path.startswith("frontend/"))


def _source_files() -> list[str]:
    roots = (f"{ARTIFACT}/src/", "frontend/lib/")
    return [
        path
        for path in _tracked_frontend()
        if path.startswith(roots) and path.endswith(SOURCE_SUFFIXES)
    ]


def _candidates(target: str) -> list[str]:
    tracked = _tracked_files()
    if target in tracked:
        return [target]
    options = [target + ext for ext in RESOLVE_EXTENSIONS]
    options += [f"{target}/index{ext}" for ext in RESOLVE_EXTENSIONS]
    return [option for option in options if option in tracked]


def _local_target(importer: str, specifier: str) -> str | None:
    specifier = specifier.split("?", 1)[0]
    for alias, base in ALIASES.items():
        if specifier.startswith(alias):
            return posixpath.normpath(base + specifier[len(alias) :])
    if specifier.startswith(("./", "../")):
        return posixpath.normpath(
            posixpath.join(posixpath.dirname(importer), specifier)
        )
    return None


def test_import_closure_resolves_unambiguously_against_tracked_files() -> None:
    edges = 0
    failures: list[str] = []
    for importer in _source_files():
        text = (ROOT / importer).read_text(encoding="utf-8")
        for specifier in SPECIFIER.findall(text):
            target = _local_target(importer, specifier)
            if target is None:
                continue
            edges += 1
            candidates = _candidates(target)
            if len(candidates) != 1:
                failures.append(f"{importer}: {specifier!r} -> {candidates}")
    assert not failures, "\n".join(failures)
    # Guard against a vacuous pass (the parser silently matching nothing).
    assert edges > 100, edges


def test_index_html_entry_is_tracked() -> None:
    html = (ROOT / ARTIFACT / "index.html").read_text(encoding="utf-8")
    entries = re.findall(r"""<script[^>]+src=["']/([^"']+)["']""", html)
    assert entries == ["src/main.tsx"]
    assert f"{ARTIFACT}/{entries[0]}" in _tracked_files()


def test_workspace_dependencies_export_tracked_typescript_sources() -> None:
    manifest = json.loads((ROOT / ARTIFACT / "package.json").read_text())
    dependencies = {
        **manifest.get("dependencies", {}),
        **manifest.get("devDependencies", {}),
    }
    workspace = sorted(
        name for name, spec in dependencies.items() if spec.startswith("workspace:")
    )
    assert workspace == ["@workspace/api-client-react"]
    packages = {
        json.loads((ROOT / path).read_text())["name"]: posixpath.dirname(path)
        for path in _tracked_frontend()
        if path.endswith("/package.json") and "/lib/" in path
    }
    for name in workspace:
        directory = packages[name]
        package = json.loads((ROOT / directory / "package.json").read_text())
        exports = package["exports"]["."]
        for entry in (package["main"], package["types"], exports):
            target = posixpath.normpath(posixpath.join(directory, entry))
            assert target.endswith(".ts"), (name, entry)
            assert target in _tracked_files(), (name, entry)


def test_no_tracked_frontend_file_is_ignored_by_git() -> None:
    paths = _tracked_frontend()
    assert paths
    completed = subprocess.run(
        ["git", "-C", str(ROOT), "check-ignore", "--no-index", "-z", "--stdin"],
        input="\0".join(paths).encode(),
        capture_output=True,
        check=False,
    )
    ignored = [item for item in completed.stdout.decode().split("\0") if item]
    assert completed.returncode == 1 and not ignored, (completed.returncode, ignored)


def test_no_compiled_javascript_shadows_typescript_sources() -> None:
    tracked = set(_tracked_frontend())
    shadows = [
        path
        for path in sorted(tracked)
        if path.endswith((".js", ".mjs", ".cjs", ".jsx"))
        and any(
            posixpath.splitext(path)[0] + suffix in tracked
            for suffix in (".ts", ".tsx", ".mts", ".cts")
        )
    ]
    assert not shadows, shadows
    assert not [path for path in tracked if path.endswith(".tsbuildinfo")]


JS_SUFFIXES = (".js", ".mjs", ".cjs", ".jsx")
TS_SUFFIXES = (".ts", ".tsx", ".mts", ".cts")


def _shadowing_javascript(paths: set[str]) -> list[str]:
    return sorted(
        path
        for path in paths
        if path.endswith(JS_SUFFIXES)
        and any(posixpath.splitext(path)[0] + suffix in paths for suffix in TS_SUFFIXES)
    )


def _frontend_lib_working_tree() -> set[str]:
    """Every file under frontend/lib on disk (tracked, untracked or ignored),
    node_modules excluded: local images and Vite see the working tree."""

    lib = ROOT / "frontend" / "lib"
    found = {
        path.relative_to(ROOT).as_posix()
        for path in lib.rglob("*")
        if path.is_file() and "node_modules" not in path.relative_to(lib).parts
    }
    completed = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z", "-o", "--", "frontend/lib"],
        capture_output=True,
        check=True,
    )
    others = {
        item
        for item in completed.stdout.decode().split("\0")
        if item and "/node_modules/" not in item
    }
    assert others <= found, others - found
    return found


def test_shadow_detector_flags_javascript_next_to_typescript() -> None:
    paths = {"lib/a/src/index.ts", "lib/a/src/index.js", "lib/a/src/b.js"}
    assert _shadowing_javascript(paths) == ["lib/a/src/index.js"]


def test_no_untracked_javascript_shadows_typescript_in_frontend_lib() -> None:
    # `tsc --build` used to write src/*.js next to the .ts sources; Vite
    # resolves .js before .ts, so a stray file silently replaces the source.
    found = _frontend_lib_working_tree()
    assert any(path.endswith(".ts") for path in found)
    shadows = _shadowing_javascript(found)
    assert not shadows, shadows


def test_frontend_lib_tsconfigs_never_emit_javascript() -> None:
    configs = [
        path
        for path in _tracked_frontend()
        if path.startswith("frontend/lib/")
        and posixpath.basename(path).startswith("tsconfig")
        and path.endswith(".json")
    ]
    assert configs
    for path in configs:
        config = json.loads((ROOT / path).read_text(encoding="utf-8"))
        # An `extends` could re-enable emit behind our back.
        assert "extends" not in config, path
        options = config["compilerOptions"]
        assert options.get("noEmit") is True, path
        for emitting in ("composite", "declaration", "emitDeclarationOnly", "outDir"):
            assert emitting not in options, (path, emitting)


def _braced_end(text: str, index: int) -> int:
    """Index just past the ``}`` closing the ``{`` at ``index`` (strings and
    template literals may contain braces)."""

    depth = 0
    quote = ""
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\":
                index += 1
            elif char == quote:
                quote = ""
            elif char == "\n" and quote != "`":
                # JSX text such as <p>Isn't</p> is not a string: reading it as
                # one would swallow the rest of the tag (and its path).
                raise AssertionError(
                    f"string literal crosses a line break in a <Route> value "
                    f"(JSX text apostrophe?): {text[index - 40 : index]!r}"
                )
        elif char in "\"'`":
            previous = text[index - 1 : index]
            if char != "`" and (previous.isalnum() or previous in {"_", "$"}):
                raise AssertionError(
                    f"quote after an identifier in a <Route> value "
                    f"(JSX text apostrophe?): {text[index - 40 : index + 1]!r}"
                )
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    raise AssertionError("unbalanced {...} in a <Route> tag")


def _route_tags(text: str) -> list[dict[str, str]]:
    """The attributes (name -> raw value) of every <Route ...> opening tag."""

    tags: list[dict[str, str]] = []
    for match in ROUTE_OPEN.finditer(text):
        attributes: dict[str, str] = {}
        index = match.end()
        while True:
            while index < len(text) and text[index].isspace():
                index += 1
            context = text[match.start() : index + 40]
            assert index < len(text), f"unterminated <Route> tag: {context!r}"
            if text.startswith(("/>", ">"), index):
                break
            assert text[index] != "{", f"spread attribute in <Route>: {context!r}"
            name = ATTRIBUTE_NAME.match(text, index)
            assert name, f"unparseable <Route> tag: {context!r}"
            index = name.end()
            value = ""
            if text.startswith("=", index):
                index += 1
                opener = text[index : index + 1]
                if opener in {'"', "'"}:
                    end = text.find(opener, index + 1)
                    assert end != -1, f"unterminated attribute: {context!r}"
                    end += 1
                elif opener == "{":
                    end = _braced_end(text, index)
                else:
                    raise AssertionError(f"unparseable attribute: {context!r}")
                value = text[index:end]
                index = end
            assert name.group() not in attributes, f"duplicate attribute: {context!r}"
            attributes[name.group()] = value
        tags.append(attributes)
    return tags


def _static_path(value: str) -> str:
    match = STATIC_PATH.match(value)
    assert match, (
        f"<Route path={value}> is not a static string: the SPA route list "
        f"({SPA_ROUTES.name}) cannot be derived from it"
    )
    return next(group for group in match.groups() if group is not None)


def _routes_in(text: str) -> set[str]:
    routes: set[str] = set()
    for attributes in _route_tags(text):
        # wouter `nest` makes every inner path relative: the list would lie.
        assert "nest" not in attributes, f"nested <Route>: {attributes}"
        assert "base" not in attributes, f"base attribute on <Route>: {attributes}"
        # A pathless <Route component={X} /> is the catch-all: no SPA route.
        if "path" in attributes:
            path = _static_path(attributes["path"])
            assert ROUTE_PATH.match(path), (
                f"unsupported route path syntax {path!r} (wildcard, optional "
                f"param or regexp): the SPA route list cannot express it"
            )
            routes.add(path)
    return routes


def _wouter_imports(text: str) -> list[tuple[str, str]]:
    """(imported, local) for every binding imported from wouter."""

    bindings: list[tuple[str, str]] = []
    for clause in WOUTER_IMPORT.findall(text):
        clause = clause.strip()
        if clause.startswith("type "):
            continue
        named = clause.startswith("{") and clause.endswith("}")
        assert named, f"non-named wouter import (default or namespace): {clause!r}"
        for spec in clause[1:-1].split(","):
            words = spec.split()
            if not words or words[0] == "type":
                continue
            assert len(words) in {1, 3} and words[1:2] in ([], ["as"]), spec
            bindings.append((words[0], words[-1]))
    return bindings


def _assert_jsx_only_references(text: str, name: str) -> None:
    """Every reference to ``name`` outside the wouter import is a JSX tag."""

    body = WOUTER_IMPORT.sub("", text)
    for match in re.finditer(rf"(?<![\w$.]){re.escape(name)}(?![\w$])", body):
        before = body[max(0, match.start() - 2) : match.start()]
        assert before.endswith("<") or before == "</", (
            f"{name} used other than as a <{name}> tag (createElement, "
            f"aliasing, `< {name}`): {body[match.start() - 30 : match.end() + 30]!r}"
        )


def _assert_route_bindings(text: str) -> None:
    """wouter's Route is only rendered as a literal <Route> tag (no alias,
    createElement/jsx call or rebinding), and its Router (which takes `base`,
    making every inner path relative) only once."""

    bindings = _wouter_imports(text)
    routes = [local for imported, local in bindings if imported == "Route"]
    assert routes == ["Route"], f"Route imported as {routes} (aliased Route import)"
    _assert_jsx_only_references(text, "Route")
    routers = [local for imported, local in bindings if imported == "Router"]
    assert len(routers) <= 1, f"Router imported as {routers}"
    for local in routers:
        _assert_jsx_only_references(text, local)
        count = len(re.findall(rf"<{re.escape(local)}(?![\w$])", text))
        assert count == 1, f"nested <Router> (<{local}> rendered {count} times)"


def _declared_routes(text: str) -> set[str]:
    _assert_route_bindings(text)
    return _routes_in(text)


def test_route_parser_ignores_attribute_order_and_line_breaks() -> None:
    snippet = """
        <Switch>
          <Route path="/a" component={A} />
          <Route component={B} path="/b" />
          <Route
            component={C}
            path='/c/:id'
          />
          <Route path="/d">{() => <D />}</Route>
          {flag && <Route component={() => <E />} path="/e" />}
          <Route component={NotFound} />
          <Router base="/ignored">
        </Switch>
    """
    assert _routes_in(snippet) == {"/a", "/b", "/c/:id", "/d", "/e"}


def test_route_parser_reads_braced_and_template_literal_paths() -> None:
    snippet = """
        <Route path={"/x"} component={X} />
        <Route path={ '/y/:id' } component={Y} />
        <Route path={`/z`} component={Z} />
        <Route
          component={() => {
            const label = "a } b";
            return <Inner label={`${label}`} />;
          }}
          path="/w"
        />
        <Router base="/ignored" />
        <Routes />
    """
    assert _routes_in(snippet) == {"/x", "/y/:id", "/z", "/w"}


@pytest.mark.parametrize(
    ("snippet", "message"),
    [
        ("<Route path={base} component={A} />", "not a static string"),
        ("<Route path={`/a/${id}`} component={A} />", "not a static string"),
        ('<Route path={"/a" + suffix} component={A} />', "not a static string"),
        ('<Route path={"/a\\u0062"} component={A} />', "not a static string"),
        ("<Route {...props} />", "spread attribute"),
        ('<Route path="/a" nest>{() => <A />}</Route>', "nested <Route>"),
        ('<Route path="/a" component={() => {}', "unbalanced"),
        ('<Route path="/billing/*" component={A} />', "unsupported route path"),
        ('<Route path="/admin/:rest*" component={A} />', "unsupported route path"),
        ('<Route path="/t/:id?" component={A} />', "unsupported route path"),
        ('<Route path="*" component={A} />', "unsupported route path"),
        ('<Route path="/a" base="/b" component={A} />', "base attribute"),
        (
            # T08: the apostrophe opened a "string" that closed in a later,
            # non-Route tag; the /x route was silently read as pathless.
            '<Route component={() => <p>Isn\'t ready</p>} path="/x" />\n'
            "<Toaster label={<span>Don't</span>} />",
            "quote after an identifier",
        ),
        (
            '<Route component={() => <p>Say \'hi</p>} path="/x" />\n'
            "<Toaster label={<span>bye'</span>} />",
            "crosses a line break",
        ),
        (
            "<Route\n  component={() => {\n    // it's lazy\n    return <L />;\n"
            '  }}\n  path="/lazy"\n/>',
            "quote after an identifier",
        ),
    ],
)
def test_route_parser_fails_loudly_on_paths_it_cannot_derive(
    snippet: str, message: str
) -> None:
    with pytest.raises(AssertionError, match=re.escape(message)):
        _routes_in(snippet)


WOUTER_IMPORT_LINE = 'import { Switch, Route, Router as WouterRouter } from "wouter";\n'
APP_ROUTER = '<WouterRouter base="/">{children}</WouterRouter>\n'


def test_route_bindings_accept_the_app_tsx_shape() -> None:
    snippet = WOUTER_IMPORT_LINE + APP_ROUTER + '<Route path="/a" component={A} />'
    assert _declared_routes(snippet) == {"/a"}


@pytest.mark.parametrize(
    ("snippet", "message"),
    [
        (
            # T07: an aliased Route is invisible to the <Route scan.
            WOUTER_IMPORT_LINE.replace("Route,", "Route, Route as Page,")
            + APP_ROUTER
            + '<Page path="/aliased" component={A} />',
            "aliased Route import",
        ),
        (
            WOUTER_IMPORT_LINE.replace("Route,", "Route as Page,")
            + APP_ROUTER
            + '<Page path="/aliased" component={A} />',
            "aliased Route import",
        ),
        (
            # T15
            WOUTER_IMPORT_LINE
            + APP_ROUTER
            + '{React.createElement(Route, { path: "/ce", component: A })}',
            "used other than as a <Route> tag",
        ),
        (
            WOUTER_IMPORT_LINE + APP_ROUTER + '{jsx(Route, { path: "/j" })}',
            "used other than as a <Route> tag",
        ),
        (
            WOUTER_IMPORT_LINE + APP_ROUTER + "const Page = Route;",
            "used other than as a <Route> tag",
        ),
        (
            WOUTER_IMPORT_LINE + APP_ROUTER + '< Route path="/spaced" />',
            "used other than as a <Route> tag",
        ),
        (
            # T12: a nested Router makes every inner path relative to `base`.
            WOUTER_IMPORT_LINE
            + APP_ROUTER
            + '<Route path="/admin/x">{() => <WouterRouter base="/admin">'
            '<Route path="/users" component={U} /></WouterRouter>}</Route>',
            "nested <Router>",
        ),
        (
            WOUTER_IMPORT_LINE + "<WouterRouter>{children}</WouterRouter>\n"
            "{createElement(WouterRouter, { base: '/admin' })}",
            "used other than as a <WouterRouter> tag",
        ),
        (
            'import * as wouter from "wouter";\n<wouter.Route path="/ns" />',
            "non-named wouter import",
        ),
        (
            'import { Switch } from "wouter";\n<Route path="/a" />',
            "Route imported as []",
        ),
    ],
    ids=[
        "alias-plus-route",
        "alias-only",
        "create-element",
        "jsx-runtime-call",
        "rebinding",
        "space-after-angle",
        "nested-router-base",
        "router-create-element",
        "namespace-import",
        "route-not-imported",
    ],
)
def test_route_bindings_fail_loudly_on_routes_outside_literal_tags(
    snippet: str, message: str
) -> None:
    with pytest.raises(AssertionError, match=re.escape(message)):
        _declared_routes(snippet)


def _declares_routes(text: str) -> bool:
    if ROUTE_OPEN.search(text):
        return True
    # An aliased or namespaced wouter Route declares routes without a literal
    # <Route tag. A bare Router (tests wrap pages in a memory-location Router)
    # adds no route by itself.
    try:
        bindings = _wouter_imports(text)
    except AssertionError:
        return True
    return any(imported == "Route" for imported, _ in bindings)


def _route_declarers(sources: dict[str, str]) -> list[str]:
    return sorted(
        path
        for path, text in sources.items()
        if path != APP_TSX.relative_to(ROOT).as_posix() and _declares_routes(text)
    )


def test_route_declarer_scan_flags_routes_outside_app_tsx() -> None:
    sources = {
        f"{ARTIFACT}/src/App.tsx": '<Route path="/a" component={A} />',
        f"{ARTIFACT}/src/pages/nested.tsx": '<Route path="/a/b" component={B} />',
        f"{ARTIFACT}/src/pages/other.tsx": "<Router hook={hook}><Routes /></Router>",
        f"{ARTIFACT}/src/pages/links.tsx": 'import { Link, useLocation } from "wouter";',
        f"{ARTIFACT}/src/pages/aliased.tsx": (
            'import { Route as Page } from "wouter";\n<Page path="/p" />'
        ),
        f"{ARTIFACT}/src/pages/router.tsx": (
            'import { Router as R } from "wouter";\n<R base="/x">{c}</R>'
        ),
        f"{ARTIFACT}/src/pages/namespace.tsx": 'import * as w from "wouter";',
    }
    assert _route_declarers(sources) == [
        f"{ARTIFACT}/src/pages/aliased.tsx",
        f"{ARTIFACT}/src/pages/namespace.tsx",
        f"{ARTIFACT}/src/pages/nested.tsx",
    ]


def test_no_source_file_other_than_app_tsx_declares_a_route() -> None:
    # The route list is derived from App.tsx alone: a <Route> anywhere else
    # (a nested router in a page) would add client routes nobody listed.
    sources = {
        path: (ROOT / path).read_text(encoding="utf-8")
        for path in _tracked_frontend()
        if path.startswith(f"{ARTIFACT}/src/")
        and path.endswith((*JS_SUFFIXES, *TS_SUFFIXES))
    }
    assert APP_TSX.relative_to(ROOT).as_posix() in sources
    assert _route_declarers(sources) == []


def _app_routes() -> set[str]:
    return _declared_routes(APP_TSX.read_text(encoding="utf-8"))


def test_spa_route_list_matches_app_tsx_routes() -> None:
    routes = json.loads(SPA_ROUTES.read_text(encoding="utf-8"))
    excluded = set(routes["excluded"])
    assert excluded == {"/privacidade/:clinicId"}
    app_routes = _app_routes()
    assert excluded <= app_routes
    static = routes["static"]
    dynamic = {entry["route"]: entry["pattern"] for entry in routes["dynamic"]}
    assert len(static) == len(set(static))
    assert not set(static) & set(dynamic)
    assert set(static) | set(dynamic) == app_routes - excluded
    assert all(":" not in route and "*" not in route for route in static)


def test_spa_dynamic_patterns_match_only_their_route_shape() -> None:
    routes = json.loads(SPA_ROUTES.read_text(encoding="utf-8"))
    for entry in routes["dynamic"]:
        prefix = entry["route"].rsplit(":", 1)[0]
        pattern = re.compile(entry["pattern"])
        assert prefix.endswith("/") and entry["pattern"].startswith(prefix)
        assert pattern.fullmatch(prefix + "abc_DEF-123")
        for value in ("", "..", "a/b", "x\n", "%2e%2e", "a" * 129):
            assert not pattern.fullmatch(prefix + value), value
