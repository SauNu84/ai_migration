"""Codegen stage (SAU-305): canonical SQL once → SQLGlot transpile.

For each entity we generate the medallion transform **once** as a single canonical
SQL statement (authored in the Snowflake dialect), then let **SQLGlot** transpile it
deterministically to:
  * **Snowflake** — the production target and the byte-identical-regeneration artifact,
  * **DuckDB** — the executable proxy the verification loop runs the sample through.

The LLM is never in this path: the canonical SQL is a pure function of the approved
mappings, and SQLGlot — not an LLM — does every dialect translation. That is the
reliability lever from the board brief.

Medallion layers:
  * **Bronze** — raw landing; every column is text (the lossless wire form bulk-load
    uses), so nothing is coerced before the typing decision is made.
  * **Silver** — typed: each column CAST to its mapped Snowflake type (semi-structured
    / array columns parsed via PARSE_JSON, which SQLGlot maps to DuckDB's JSON()).
  * **Gold** — conformed, business-ready projection the reconciliation runs against.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import sqlglot

from . import mapper as mp

# Columns whose canonical transform is a JSON parse rather than a scalar CAST.
_JSON_RECONCILE = {mp.RC_JSON_CANONICAL, mp.RC_ARRAY_CANONICAL}


@dataclass
class EntityCode:
    entity: str
    bronze_ddl: dict[str, str]          # dialect -> CREATE TABLE (all text)
    silver_sql: dict[str, str]          # dialect -> CREATE TABLE AS SELECT (typed)
    gold_sql: dict[str, str]            # dialect -> CREATE TABLE AS SELECT (conformed)
    canonical_silver: str               # the single authored canonical statement
    text_columns: list[str] = field(default_factory=list)


def _quote(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def _silver_select_expr(m: mp.Mapping) -> str:
    """Canonical (Snowflake-dialect) projection expression for one column."""
    col = _quote(m.column)
    if m.reconcile in _JSON_RECONCILE:
        # Bronze lands these as canonical JSON text → PARSE_JSON re-types them.
        # SQLGlot transpiles PARSE_JSON → JSON() for DuckDB execution.
        return f"PARSE_JSON({col}) AS {col}"
    return f"CAST({col} AS {m.snowflake_type}) AS {col}"


def build_canonical_silver(entity: str, mappings: list[mp.Mapping]) -> str:
    """The single canonical SQL statement for an entity's Silver transform.

    Deterministic: identical mappings → identical bytes. This is the artifact the §8
    byte-identical-regeneration check compares.
    """
    cols = sorted(mappings, key=lambda m: m.column)  # stable, name-ordered
    projections = ",\n  ".join(_silver_select_expr(m) for m in cols)
    src = f"bronze_{entity}"
    tgt = f"silver_{entity}"
    return (
        f"CREATE TABLE {_quote(tgt)} AS\n"
        f"SELECT\n  {projections}\nFROM {_quote(src)}"
    )


def _bronze_ddl(entity: str, mappings: list[mp.Mapping], dialect: str) -> str:
    cols = sorted(mappings, key=lambda m: m.column)
    text_type = "VARCHAR" if dialect == "snowflake" else "VARCHAR"
    lines = ",\n  ".join(f"{_quote(m.column)} {text_type}" for m in cols)
    return f"CREATE TABLE {_quote('bronze_' + entity)} (\n  {lines}\n)"


def _transpile(sql: str, dialect: str) -> str:
    """Deterministic SQLGlot transpile from the canonical (Snowflake) form."""
    return sqlglot.transpile(sql, read="snowflake", write=dialect, pretty=True)[0]


def build_entity(entity: str, mappings: list[mp.Mapping]) -> EntityCode:
    canonical = build_canonical_silver(entity, mappings)
    silver = {d: _transpile(canonical, d) for d in ("snowflake", "duckdb")}

    # Gold: conformed projection over Silver (Phase 0 = typed pass-through, PK-ordered
    # at read time). Authored canonically once, transpiled the same way.
    cols = sorted(mappings, key=lambda m: m.column)
    gold_cols = ",\n  ".join(_quote(m.column) for m in cols)
    canonical_gold = (
        f"CREATE TABLE {_quote('gold_' + entity)} AS\n"
        f"SELECT\n  {gold_cols}\nFROM {_quote('silver_' + entity)}"
    )
    gold = {d: _transpile(canonical_gold, d) for d in ("snowflake", "duckdb")}

    bronze = {d: _bronze_ddl(entity, mappings, d) for d in ("snowflake", "duckdb")}

    return EntityCode(
        entity=entity,
        bronze_ddl=bronze,
        silver_sql=silver,
        gold_sql=gold,
        canonical_silver=canonical,
        text_columns=[m.column for m in cols],
    )


def generate(mappings: list[mp.Mapping]) -> dict[str, EntityCode]:
    """Generate medallion code for every entity. Deterministic & order-stable."""
    by_entity: dict[str, list[mp.Mapping]] = {}
    for m in mappings:
        by_entity.setdefault(m.entity, []).append(m)
    return {e: build_entity(e, ms) for e, ms in sorted(by_entity.items())}


# ── Compile check (Snowflake dialect) ───────────────────────────────────────

def compile_check(code: dict[str, EntityCode]) -> list[str]:
    """Parse every generated Snowflake statement in the Snowflake dialect.

    A parse failure is a compile failure. (No live Snowflake in Phase 0; SQLGlot's
    Snowflake parser is the dialect gate.) Returns a list of errors (empty == ok).
    """
    errors: list[str] = []
    for entity, ec in code.items():
        for label, sql in (
            (f"{entity}.bronze", ec.bronze_ddl["snowflake"]),
            (f"{entity}.silver", ec.silver_sql["snowflake"]),
            (f"{entity}.gold", ec.gold_sql["snowflake"]),
        ):
            try:
                parsed = sqlglot.parse(sql, dialect="snowflake")
                if not parsed or parsed[0] is None:
                    errors.append(f"{label}: produced empty parse")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{label}: {type(exc).__name__}: {exc}")
    return errors
