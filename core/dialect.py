"""Source dialect seam (SAU-322): the pluggable source-half of the migration loop.

Everything that is *specific to the source database* lives behind one interface,
`SourceDialect`, so a new source lane (SQL Server, Oracle, …) is a ~400-LOC plugin
rather than a fork of the chassis. The four source-coupled seams the CTO build note
calls out are exactly the methods of this interface:

  1. **profiler-SQL template** — the in-database profiling routine + its invocation
     (the `information_schema` queries that build the ontology IR).
  2. **physical-type token table** — `physical_token()` maps a source physical type to
     a *canonical* `PT_*` token, plus `is_user_defined()` for enum domains. The lift /
     inference pass switches on the canonical token, never on raw source type strings.
  3. **extraction-expr builder** — `extraction_expr()` (and `extract_rows()` that uses
     it) define how each physical column is read into the lossless text wire form.
  4. **tz/float canonicalization hooks** — `fix_tz()` (source tz wire format) and
     `is_narrow_float()` (the float-udt token), the two narrowly-coupled hooks the
     reconcile comparison core delegates to.

The *canonical* layers downstream of the token — semantic inference, mapper, codegen,
the reconcile comparison core, the §8 gate — are dialect-agnostic and untouched.

`PostgresDialect` is the first implementation; it reproduces today's hard-wired
Postgres behavior byte-for-byte (the §8 gate stays GREEN through the new seam).
"""
from __future__ import annotations

import json
import subprocess
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

# ── Canonical physical-type tokens (dialect-agnostic) ───────────────────────
# A source dialect maps its physical types onto this fixed vocabulary; the inference
# pass branches on these tokens, so adding a source means writing one mapping table,
# not editing the inference logic. (The reconcile *strategy* tokens live in mapper.py;
# these are the upstream *physical-kind* tokens the inference pass consumes.)
PT_ARRAY = "array"
PT_ARRAY_NUMERIC = "array_numeric"
PT_JSON = "json"
PT_NETWORK = "network"
PT_TIMESTAMP_TZ = "timestamp_tz"
PT_TIMESTAMP_NTZ = "timestamp_ntz"
PT_DATE = "date"
PT_BOOLEAN = "boolean"
PT_FIXED_CHAR = "fixed_char"          # char(N) — fixed-width, leading zeros significant
PT_FLOAT = "float"                    # binary float, double width (float8 / double)
PT_FLOAT_NARROW = "float_narrow"      # binary float, single width (float4 / real) — lossier
PT_NUMERIC = "numeric"                # exact decimal
PT_INTEGER = "integer"
PT_TEXT = "text"                      # varchar / text
PT_UNKNOWN = "unknown"


class SourceDialect(ABC):
    """The source-half of one migration lane. Implementations are stateless."""

    #: short identifier, e.g. "postgres", "sqlserver".
    name: str = "abstract"

    # ── Seam 1: profiler-SQL template ───────────────────────────────────────
    @abstractmethod
    def profiler_install_sql(self) -> str:
        """SQL that (idempotently) installs the in-database profiling routine."""

    @abstractmethod
    def profile_query(self, schema: str) -> str:
        """SQL that invokes the profiler for `schema` and returns the aggregate JSON."""

    # ── Seam 2: extraction-expr builder ─────────────────────────────────────
    @abstractmethod
    def extraction_expr(self, column: str, reconcile: str) -> str:
        """Per-column SQL expression producing the lossless text wire value."""

    @abstractmethod
    def extract_rows(self, entity: str, mappings: list[Any]) -> list[dict[str, Any]]:
        """Read `entity` from the source as a list of {column: text|None} rows."""

    # ── Seam 3: physical-type token table ───────────────────────────────────
    @abstractmethod
    def physical_token(self, data_type: str, udt: str) -> str:
        """Map a source physical type (data_type, udt) to a canonical PT_* token."""

    @abstractmethod
    def is_user_defined(self, data_type: str) -> bool:
        """Whether `data_type` denotes a user-defined (enum) domain in this source."""

    # ── Seam 4: tz/float canonicalization hooks ─────────────────────────────
    @abstractmethod
    def fix_tz(self, s: str) -> str:
        """Normalize this source's tz wire format to ISO-8601 (parseable by datetime)."""

    @abstractmethod
    def is_narrow_float(self, source_udt: str) -> bool:
        """Whether `source_udt` is a single-width binary float (lossier reconcile epsilon)."""


# ── PostgresDialect — the first implementation (SAU-303/305 behavior, verbatim) ──

_REPO = Path(__file__).resolve().parent.parent

# Postgres physical-type recognition tables (was hard-wired in ontology.infer_property).
_PG_ARRAY_NUMERIC_HINT = ("numeric", "int", "float", "numer")
_PG_JSON = ("jsonb", "json")
_PG_NETWORK = ("inet", "cidr")
_PG_FLOAT_NARROW_UDT = ("float4",)
_PG_FLOAT_NARROW_DT = ("real",)
_PG_FLOAT_WIDE_DT = ("double precision",)
_PG_FLOAT_WIDE_UDT = ("float8",)
_PG_CHAR_DT = ("character", "char")
_PG_INT_DT = ("bigint", "integer", "smallint")
_PG_INT_UDT = ("int8", "int4", "int2")
_PG_TEXT_DT = ("character varying", "varchar", "text")
_PG_TEXT_UDT = ("varchar", "text")


class PostgresDialect(SourceDialect):
    """Postgres source lane. Reproduces the pre-SAU-322 hard-wired behavior exactly."""

    name = "postgres"

    # ── Seam 1 ──
    def profiler_install_sql(self) -> str:
        return (_REPO / "profile" / "profile.sql").read_text()

    def profile_query(self, schema: str) -> str:
        return f"SELECT phase0_profile('{schema}')"

    def _psql(self, sql: str, *, context: str = "psql") -> str:
        """Run SQL via psql (tuples-only, unaligned) and return stdout."""
        cmd = ["psql", "-X", "-tA", "-v", "ON_ERROR_STOP=1", "-c", sql]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr)
            raise SystemExit(f"{context} failed (exit {proc.returncode})")
        return proc.stdout

    def run_profiler(self, schema: str) -> str:
        """Install the profiler then invoke it; return the raw aggregate JSON string."""
        self._psql(self.profiler_install_sql(), context="psql install")
        return self._psql(self.profile_query(schema), context="psql profile").strip()

    # ── Seam 2 ──
    def extraction_expr(self, column: str, reconcile: str) -> str:
        # Imported lazily to avoid a circular import (mapper has no source coupling).
        from . import mapper as mp
        col = f'"{column}"'
        if reconcile == mp.RC_ARRAY_CANONICAL:
            # JSON array of element TEXT, order-preserving; NULL array → JSON null.
            return (
                f"CASE WHEN {col} IS NULL THEN NULL ELSE "
                f"(SELECT COALESCE(json_agg(e ORDER BY ord), '[]'::json)::text "
                f" FROM unnest({col}) WITH ORDINALITY AS u(e, ord)) END"
            )
        if reconcile == mp.RC_JSON_CANONICAL:
            return f"{col}::text"
        return f"{col}::text"

    def extract_rows(self, entity: str, mappings: list[Any]) -> list[dict[str, Any]]:
        cols = sorted(mappings, key=lambda m: m.column)
        pairs = ",\n      ".join(
            f"'{m.column}', ({self.extraction_expr(m.column, m.reconcile)})" for m in cols
        )
        has_id = any(m.column == "id" for m in cols)
        order = 'ORDER BY "id"' if has_id else ""
        sql = (
            f"SELECT json_build_object(\n      {pairs}\n    )::text "
            f'FROM "{entity}" {order}'
        )
        out = self._psql(sql, context=f"psql extract {entity}")
        rows: list[dict[str, Any]] = []
        for line in out.splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
        return rows

    # ── Seam 3 ──
    def physical_token(self, data_type: str, udt: str) -> str:
        dt = (data_type or "").lower()
        ut = (udt or "").lower()
        # Order mirrors the original infer_property if-chain precedence exactly.
        if dt.upper() == "ARRAY" or ut.startswith("_"):
            if any(k in ut for k in _PG_ARRAY_NUMERIC_HINT):
                return PT_ARRAY_NUMERIC
            return PT_ARRAY
        if dt in _PG_JSON or ut in _PG_JSON:
            return PT_JSON
        if ut in _PG_NETWORK or dt in _PG_NETWORK:
            return PT_NETWORK
        if "timestamp with time zone" in dt or ut == "timestamptz":
            return PT_TIMESTAMP_TZ
        if "timestamp without time zone" in dt or ut == "timestamp":
            return PT_TIMESTAMP_NTZ
        if dt == "date":
            return PT_DATE
        if dt == "boolean" or ut == "bool":
            return PT_BOOLEAN
        if dt in _PG_CHAR_DT or ut == "bpchar":
            return PT_FIXED_CHAR
        if dt in _PG_FLOAT_NARROW_DT or ut in _PG_FLOAT_NARROW_UDT:
            return PT_FLOAT_NARROW
        if dt in _PG_FLOAT_WIDE_DT or ut in _PG_FLOAT_WIDE_UDT:
            return PT_FLOAT
        if dt == "numeric" or ut == "numeric":
            return PT_NUMERIC
        if dt in _PG_INT_DT or ut in _PG_INT_UDT:
            return PT_INTEGER
        if dt in _PG_TEXT_DT or ut in _PG_TEXT_UDT:
            return PT_TEXT
        return PT_UNKNOWN

    def is_user_defined(self, data_type: str) -> bool:
        return (data_type or "").upper() == "USER-DEFINED"

    # ── Seam 4 ──
    def fix_tz(self, s: str) -> str:
        # Postgres emits '+00' / '+02'; datetime.fromisoformat wants '+00:00'.
        for i in range(len(s) - 1, max(len(s) - 7, 0), -1):
            if s[i] in "+-" and (len(s) - i) == 3:
                return s + ":00"
        return s

    def is_narrow_float(self, source_udt: str) -> bool:
        return (source_udt or "").lower() in ("float4", "real")


# Default lane until a run selects another. The whole loop is Postgres today; this keeps
# every existing call site (and the test suite) working without threading a dialect.
DEFAULT_DIALECT: SourceDialect = PostgresDialect()
