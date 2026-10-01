#!/usr/bin/env python3
"""Inventory Brazilian Portuguese string literals in Python and preset TOML."""

from __future__ import annotations

import argparse
import ast
import re
import runpy
import sys
import tomllib
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST_PATH = ROOT / "scripts" / "pt-br-protocol-ids.txt"
PROTOCOL_PATH = (
    ROOT / "packages" / "neurostack-core" / "neurostack_core" / "protocol.py"
)

ACCENT_RE = re.compile(r"[áàâãäçéèêëíìîïóòôõöúùûüÁÀÂÃÄÇÉÈÊËÍÌÎÏÓÒÔÕÖÚÙÛÜ]")

# Deliberately lexical and reviewable.  Underscores and hyphens are treated as
# separators so an unlisted identifier such as ``agendar_consulta`` cannot use
# identifier spelling to bypass this vocabulary gate.
PT_BR_VOCABULARY = frozenset(
    {
        "acolhedor",
        "administrativo",
        "agendar",
        "agenda",
        "agendamento",
        "agendado",
        "agendei",
        "ajuda",
        "alguem",
        "alto",
        "amanha",
        "assistente",
        "atendente",
        "atendimento",
        "atendimentos",
        "ativar",
        "atual",
        "avaliacao",
        "beleza",
        "bombeiros",
        "botox",
        "bruxismo",
        "cancelamento",
        "cancelar",
        "canal",
        "calmo",
        "carteirinha",
        "clareamento",
        "clinica",
        "clinico",
        "cobranca",
        "conhecimento",
        "consulta",
        "consultas",
        "consultar",
        "conduta",
        "contexto",
        "contato",
        "continuar",
        "convenio",
        "conversa",
        "cortesia",
        "cvv",
        "dados",
        "desconto",
        "definir",
        "diagnostico",
        "dia",
        "diferenciais",
        "disque",
        "documentos",
        "domingo",
        "dosagens",
        "equipe",
        "encaminhar",
        "encaminhamento",
        "esclarecer",
        "especialidades",
        "especialista",
        "especialistas",
        "estacionamento",
        "etapa",
        "faceta",
        "faltar",
        "fidelizacao",
        "financeiro",
        "fluxo",
        "fontes",
        "fora do horario",
        "funil",
        "gerencia",
        "gestante",
        "gratuita",
        "harmonizacao",
        "historia",
        "hora",
        "horario",
        "horarios",
        "imediato",
        "inadequada",
        "informacoes",
        "juridico",
        "medicacao",
        "medicamento",
        "medicamentos",
        "medico",
        "modo",
        "nome",
        "objetivo",
        "noite",
        "obrigatorio",
        "paciente",
        "pacientes",
        "pagamento",
        "particular",
        "pergunta",
        "perguntar",
        "preco",
        "prioritario",
        "processar",
        "profissionais",
        "protocolo",
        "presenca",
        "procedimento",
        "procedimentos",
        "profissional",
        "recursos",
        "qualificacao",
        "reagendar",
        "reagendamento",
        "reclamacao",
        "recusar",
        "remedio",
        "resposta",
        "responder",
        "resumo",
        "rotina",
        "sabado",
        "samu",
        "sangramento",
        "saudacao",
        "saude",
        "segunda",
        "seguranca",
        "semana",
        "servico",
        "servicos",
        "sintomas",
        "sugira",
        "tarde",
        "tratamento",
        "tratamentos",
        "triagem",
        "turnos",
        "urgencia",
        "usuario",
        "valor",
        "valores",
        "voces",
    }
)

VOCAB_RE = re.compile(
    r"(?<!\w)(?:"
    + "|".join(
        sorted((re.escape(word) for word in PT_BR_VOCABULARY), key=len, reverse=True)
    )
    + r")(?!\w)",
    re.IGNORECASE,
)
PT_BR_DOMAIN_RE = re.compile(r"R\$")
PT_BR_DATETIME_FORMAT_RE = re.compile(r"(?:%d/%m/%Y %H:%M|%H:%M)")


class InventoryError(Exception):
    """Raised for malformed inputs or an invalid protocol allowlist."""


def _load_allowlist() -> frozenset[str]:
    try:
        lines = ALLOWLIST_PATH.read_text(encoding="utf-8").splitlines()
        values = frozenset(
            line.strip()
            for line in lines
            if line.strip() and not line.lstrip().startswith("#")
        )
        protocol = runpy.run_path(str(PROTOCOL_PATH))
        canonical = frozenset(protocol["PROTOCOL_IDENTIFIERS"])
    except (OSError, KeyError, RuntimeError) as exc:
        raise InventoryError(f"cannot load stable protocol allowlist: {exc}") from exc

    missing = canonical - values
    extra = values - canonical
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing={sorted(missing)!r}")
        if extra:
            details.append(f"extra={sorted(extra)!r}")
        raise InventoryError(
            "scripts/pt-br-protocol-ids.txt must exactly equal "
            "neurostack_core.protocol.PROTOCOL_IDENTIFIERS; " + "; ".join(details)
        )
    return values


def is_pt_br_literal(value: str, allowlist: frozenset[str]) -> bool:
    """Return whether *value* is localized content rather than an exact protocol id."""

    if value in allowlist:
        return False
    if (
        ACCENT_RE.search(value)
        or PT_BR_DOMAIN_RE.search(value)
        or PT_BR_DATETIME_FORMAT_RE.fullmatch(value)
    ):
        return True
    identifier_transparent = value.casefold().replace("_", " ").replace("-", " ")
    return bool(VOCAB_RE.search(identifier_transparent))


def _docstring_nodes(tree: ast.AST) -> set[int]:
    result: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        if not node.body:
            continue
        first = node.body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            result.add(id(first.value))
    return result


def python_strings(path: Path) -> Iterator[str]:
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (OSError, SyntaxError, UnicodeError) as exc:
        raise InventoryError(f"cannot inspect {path}: {exc}") from exc
    docstrings = _docstring_nodes(tree)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            yield node.value


def _walk_toml_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for nested in value.values():
            yield from _walk_toml_strings(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_toml_strings(nested)


def toml_strings(path: Path) -> Iterator[str]:
    try:
        with path.open("rb") as handle:
            value = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise InventoryError(f"cannot inspect {path}: {exc}") from exc
    yield from _walk_toml_strings(value)


def _candidate_files(paths: Iterable[Path], include_toml: bool) -> list[Path]:
    candidates: set[Path] = set()
    suffixes = {".py", ".toml"} if include_toml else {".py"}
    for path in paths:
        if path.is_dir():
            for child in path.rglob("*"):
                if child.is_file() and child.suffix in suffixes:
                    candidates.add(child)
        elif path.is_file() and path.suffix in suffixes:
            candidates.add(path)
        elif not path.exists():
            raise InventoryError(f"inventory path does not exist: {path}")
    return sorted(candidates, key=lambda item: item.as_posix())


def _display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--include-toml", action="store_true", help="also inspect parsed TOML values"
    )
    parser.add_argument(
        "--max", type=int, default=None, dest="maximum", help="maximum allowed count"
    )
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args(argv)
    if args.maximum is not None and args.maximum < 0:
        parser.error("--max must be non-negative")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        allowlist = _load_allowlist()
        files = _candidate_files(args.paths, args.include_toml)
        rows: list[tuple[str, int, int]] = []
        total = 0
        counted_files = 0
        exempt_total = 0
        for path in files:
            values = list(
                toml_strings(path) if path.suffix == ".toml" else python_strings(path)
            )
            count = sum(is_pt_br_literal(value, allowlist) for value in values)
            exempt = sum(value in allowlist for value in values)
            if count:
                counted_files += 1
            total += count
            exempt_total += exempt
            if count or exempt:
                rows.append((_display_path(path), count, exempt))
    except InventoryError as exc:
        print(f"inventory error: {exc}", file=sys.stderr)
        return 2

    print(f"PROTOCOL ALLOWLIST: {len(allowlist)} exact identifiers (verified)")
    print("FILE\tLOCALIZED\tALLOWLISTED")
    for filename, count, exempt in rows:
        print(f"{filename}\t{count}\t{exempt}")
    print(f"ALLOWLIST OCCURRENCES: {exempt_total} exact literals")
    print(f"CLOSED INVENTORY: {total} literals across {counted_files} files")

    if args.maximum is not None and total > args.maximum:
        print(f"inventory exceeds --max {args.maximum}: found {total}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
