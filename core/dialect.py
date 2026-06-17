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
PT_MONEY = "money"                    # fixed scale-4 currency (SQL Server MONEY/SMALLMONEY)
PT_INTEGER = "integer"
PT_TEXT = "text"                      # varchar / text
PT_BINARY = "binary"                  # raw bytes / row-version counter (NOT a timestamp)
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


# ── SqlServerDialect — the second lane (SAU-324, against the SAU-323 seed) ───────
#
# A ~source-only swap: the Snowflake target, codegen, the §8 gate, and the reconcile
# comparison core are all reused verbatim. Everything SQL-Server-specific lives here —
# the profiler-SQL template (sys.*/INFORMATION_SCHEMA), the lossless extraction
# wire-form, the physical-type → canonical-token table, and the tz/float hooks.
#
# Two type traps the §8 gate exists to catch are engineered deliberately in the
# extraction wire-form (see `_wire_expr`):
#   * MONEY / SMALLMONEY — the default `CONVERT(varchar, money)` emits only 2 dp; we
#     cast through `DECIMAL(38,4)` first so the full scale-4 survives. No silent drop.
#   * DATETIME2(7) — 100-ns resolution exceeds the default `121` style's 3 fractional
#     digits; we emit ISO-8601 style `126`, which preserves all 7 digits. No truncation.

# SQL Server physical-type recognition tables. Keyed on INFORMATION_SCHEMA.COLUMNS
# DATA_TYPE (lower-cased). NB: DATA_TYPE 'timestamp' is the legacy synonym for
# ROWVERSION — an 8-byte BINARY counter, NOT a datetime; that is the rowversion trap.
_MS_MONEY = ("money", "smallmoney")
_MS_TS_NTZ = ("datetime", "datetime2", "smalldatetime")          # no time zone
_MS_TS_TZ = ("datetimeoffset",)                                  # tz-aware instant
_MS_BINARY = ("binary", "varbinary", "image", "timestamp", "rowversion")
_MS_FLOAT_WIDE = ("float",)                                      # FLOAT(53) ≈ double
_MS_FLOAT_NARROW = ("real",)                                     # REAL ≈ float4
_MS_NUMERIC = ("decimal", "numeric")
_MS_INT = ("bigint", "int", "smallint", "tinyint")
_MS_FIXED_CHAR = ("char", "nchar")
_MS_TEXT = ("varchar", "nvarchar", "text", "ntext", "uniqueidentifier", "sysname", "xml")


class SqlServerDialect(SourceDialect):
    """SQL Server source lane (SAU-324). Lifts the SAU-323 seed to the same ontology IR
    as the Postgres path; stateless."""

    name = "sqlserver"

    # ── Seam 1: profiler-SQL template ───────────────────────────────────────
    def profiler_install_sql(self) -> str:
        return (_REPO / "sqlserver" / "profile" / "profile.sqlserver.sql").read_text()

    def profile_query(self, schema: str) -> str:
        # The profiler proc prints one JSON document (FOR JSON) for `schema`.
        return f"EXEC dbo.phase0_profile @target_schema = N'{schema}';"

    def _sqlcmd(self, sql: str, *, context: str = "sqlcmd",
                input_file: str | None = None) -> str:
        """Run T-SQL via sqlcmd and return stdout.

        Connection comes from the standard env the SQL Server rig uses (mirrors
        sqlserver/scripts/setup.sh): MSSQL_HOST / MSSQL_HOST_PORT / MSSQL_SA_PASSWORD /
        MSSQL_DATABASE, with $SQLCMD overriding the binary. `-h -1` strips headers and
        `-y 0`/`-Y 0` keep wide values un-truncated so the JSON/wire form is lossless.
        """
        import os
        host = os.environ.get("MSSQL_HOST", "localhost")
        port = os.environ.get("MSSQL_HOST_PORT", "14333")
        pwd = os.environ.get("MSSQL_SA_PASSWORD", "")
        user = os.environ.get("MSSQL_USER", "sa")
        db = os.environ.get("MSSQL_DATABASE", "phase1")
        binary = os.environ.get("SQLCMD", "sqlcmd")
        # Output flags: `-y 0` = unbounded variable-width columns (no 256-char truncation,
        # no line-wrap), the one lossless mode common to both the classic ODBC sqlcmd and
        # the go/ODBC-18 sqlcmd bundled in the MS SQL 2022 image. (That newer sqlcmd
        # rejects the classic `-h -1 -y 0 -Y 0 -W` combo: `-h`+`-y 0` and `-W`+`-y` are
        # mutually exclusive there.) `-h -1` is omitted because it conflicts with `-y 0`;
        # any header line a flavour still prints is dropped by the `{`-prefixed row filter
        # in extract_rows, so the wire form stays losslessly parseable on either tool.
        cmd = [binary, "-S", f"{host},{port}", "-U", user, "-P", pwd, "-d", db,
               "-C", "-b", "-y", "0"]
        if input_file is not None:
            cmd += ["-i", input_file]
        else:
            cmd += ["-Q", sql]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr)
            raise SystemExit(f"{context} failed (exit {proc.returncode})")
        return proc.stdout

    def run_profiler(self, schema: str = "dbo") -> str:
        """Install the profiler proc then invoke it; return the raw aggregate JSON."""
        self._sqlcmd(self.profiler_install_sql(), context="sqlcmd install")
        return self._sqlcmd(self.profile_query(schema), context="sqlcmd profile").strip()

    # ── Seam 2: extraction-expr builder (the lossless wire form) ─────────────
    def _wire_expr(self, column: str, source_type: str, reconcile: str) -> str:
        """Per-column T-SQL expression that reads the physical column into the lossless
        text wire form — the same shape a bulk load to Snowflake uses.

        `source_type` is the SQL Server DATA_TYPE (e.g. 'money', 'datetime2'); the
        wire-form cast is chosen by physical type because several distinct SQL Server
        types share one reconcile strategy (RC_EXACT_TEXT covers money, date, GUID,
        decimal and binary — each needs a different lossless cast).
        """
        from . import mapper as mp
        col = f"[{column}]"
        st = (source_type or "").split("(")[0].strip().lower()

        # JSON / array land their text as-is; reconcile canonicalizes the structure.
        if reconcile in (mp.RC_JSON_CANONICAL, mp.RC_ARRAY_CANONICAL):
            return f"CONVERT(nvarchar(max), {col})"
        # MONEY / SMALLMONEY: cast through DECIMAL(38,4) so the 4th dp is NOT dropped
        # (the default CONVERT(varchar, money) emits only 2 dp — the silent-drop trap).
        if st in _MS_MONEY:
            return f"CONVERT(varchar(64), CAST({col} AS decimal(38,4)))"
        # DATETIME / DATETIME2 / SMALLDATETIME: ISO-8601 style 126 keeps the FULL
        # fractional precision (DATETIME2(7)'s 7 digits; the legacy DATETIME tick) —
        # style 121 would truncate to 3 digits at the boundary.
        if st in _MS_TS_NTZ:
            return f"CONVERT(varchar(27), {col}, 126)"
        # DATETIMEOFFSET: style 126 keeps the offset (+HH:MM) so the instant round-trips.
        if st in _MS_TS_TZ:
            return f"CONVERT(varchar(34), {col}, 126)"
        if st == "date":
            return f"CONVERT(varchar(10), {col}, 23)"
        if st == "time":
            return f"CONVERT(varchar(16), {col}, 114)"
        # UNIQUEIDENTIFIER: canonical lower-case 36-char text (SQL Server emits upper).
        if st == "uniqueidentifier":
            return f"LOWER(CONVERT(varchar(36), {col}))"
        # ROWVERSION / BINARY: lossless '0x…' hex; identity preserved, NOT read as time.
        # The inner CONVERT to varbinary(max) is load-bearing: FOR JSON serializes a
        # rowversion-typed value as its raw bytes and silently ignores the style-1 hex
        # request, so we first re-type it to plain varbinary, where style 1 produces the
        # '0x…' hex text that round-trips. Without it the 8-byte counter leaks as raw
        # bytes (a silent identity corruption the §8 gate must never allow).
        if st in _MS_BINARY:
            return f"CONVERT(varchar(max), CONVERT(varbinary(max), {col}), 1)"
        # FLOAT / REAL: style 3 emits up to 17 significant digits (round-trippable).
        if st in _MS_FLOAT_WIDE or st in _MS_FLOAT_NARROW:
            return f"CONVERT(varchar(64), {col}, 3)"
        # Exact DECIMAL/NUMERIC: varchar conversion preserves the full declared scale.
        if st in _MS_NUMERIC:
            return f"CONVERT(varchar(64), {col})"
        if st == "bit":
            return f"CONVERT(varchar(1), {col})"
        # char / nchar / varchar / nvarchar / text — verbatim (leading zeros, unicode).
        return f"CONVERT(nvarchar(max), {col})"

    def extraction_expr(self, column: str, reconcile: str) -> str:
        # Interface seam: when only the reconcile strategy is known (no physical type),
        # fall back to the strategy-driven wire form. extract_rows uses the richer,
        # type-aware `_wire_expr` (Mapping carries the source type) for full fidelity.
        return self._wire_expr(column, "", reconcile)

    def extract_rows(self, entity: str, mappings: list[Any]) -> list[dict[str, Any]]:
        cols = sorted(mappings, key=lambda m: m.column)
        # One JSON object per row (FOR JSON keys off the column alias), so each output
        # line is a parseable row in the same {column: text|None} shape Postgres returns.
        # INCLUDE_NULL_VALUES keeps NULL keys present (rather than FOR JSON's default of
        # dropping them), so a NULL is a real None in the row, not a missing column.
        pairs = ",\n      ".join(
            f"({self._wire_expr(m.column, m.pg_type, m.reconcile)}) AS [{m.column}]"
            for m in cols
        )
        has_id = any(m.column == "id" for m in cols)
        order = 'ORDER BY [id]' if has_id else ""
        sql = (
            "SET NOCOUNT ON;\n"
            f"SELECT (SELECT {pairs} "
            f"FOR JSON PATH, WITHOUT_ARRAY_WRAPPER, INCLUDE_NULL_VALUES) AS row_json\n"
            f"FROM [{entity}] {order}"
        )
        out = self._sqlcmd(sql, context=f"sqlcmd extract {entity}")
        rows: list[dict[str, Any]] = []
        for line in out.splitlines():
            line = line.strip()
            # Each row is one FOR JSON object → one line. Skip any header / separator /
            # warning lines a sqlcmd flavour may interpose: a data line starts with '{'.
            if line.startswith("{"):
                rows.append(json.loads(line))
        return rows

    # ── Seam 3: physical-type token table ───────────────────────────────────
    def physical_token(self, data_type: str, udt: str) -> str:
        dt = (data_type or "").lower()
        ut = (udt or "").lower()
        # JSON has no native SQL Server type: it lives in NVARCHAR(MAX) guarded by an
        # ISJSON CHECK; the profiler tags those columns with udt='json' so the canonical
        # token (and thus VARIANT mapping) is driven by a real signal, not the name.
        if ut == "json":
            return PT_JSON
        if dt in _MS_BINARY:
            return PT_BINARY
        if dt in _MS_MONEY:
            return PT_MONEY
        if dt in _MS_TS_TZ:
            return PT_TIMESTAMP_TZ
        if dt in _MS_TS_NTZ:
            return PT_TIMESTAMP_NTZ
        if dt == "date":
            return PT_DATE
        if dt == "bit":
            return PT_BOOLEAN
        if dt in _MS_FIXED_CHAR:
            return PT_FIXED_CHAR
        if dt in _MS_FLOAT_NARROW:
            return PT_FLOAT_NARROW
        if dt in _MS_FLOAT_WIDE:
            return PT_FLOAT
        if dt in _MS_NUMERIC:
            return PT_NUMERIC
        if dt in _MS_INT:
            return PT_INTEGER
        if dt in _MS_TEXT:
            return PT_TEXT
        return PT_UNKNOWN

    def is_user_defined(self, data_type: str) -> bool:
        # SQL Server has no native ENUM; the seed encodes domains as VARCHAR + CHECK
        # (account_type, status, kind), so there are no UDT enum domains to lift.
        return False

    # ── Seam 4: tz/float canonicalization hooks ─────────────────────────────
    def fix_tz(self, s: str) -> str:
        # The extraction wire form (style 126) already emits ISO-8601 with a colon
        # offset (…+02:00), which datetime.fromisoformat parses directly. Only normalize
        # a colon-less offset (…+0200) defensively, so other producers still round-trip.
        if len(s) >= 5 and s[-5] in "+-" and s[-3] != ":" and s[-4:].isdigit():
            return s[:-2] + ":" + s[-2:]
        return s

    def is_narrow_float(self, source_udt: str) -> bool:
        return (source_udt or "").lower() in ("real", "float4")


# Default lane until a run selects another. The whole loop is Postgres today; this keeps
# every existing call site (and the test suite) working without threading a dialect.
DEFAULT_DIALECT: SourceDialect = PostgresDialect()
