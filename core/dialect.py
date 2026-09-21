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
from collections.abc import Iterator
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


# ── Named type equivalences (SAU-516, decision 1) ───────────────────────────
# A *documented* equivalence between the form a seed literal is written in and the form
# the source type canonically stores, declared by the dialect that owns the type.
#
# The line between one of these and an answer key is a single question, and it is the
# reason each one carries a name:
#
#   **Would the rule have been written the same way BEFORE we saw this failure?**
#
# `inet` passes it: "an omitted mask means the full host mask" is documented PostgreSQL
# type semantics, derivable from the type alone without ever looking at our corpus. A
# rule that can only be justified by pointing at an observed output fails it, and is a
# seed edit wearing a lab coat — reject it on sight.
#
# Every application is reported with BOTH raw forms and the rule's name (gate bullet 0's
# readout). An equivalence that hid its own application would be the invisible-
# normalization defect one layer up — exactly what bullet 0 was written to catch.
EQ_INET_HOST_MASK = "inet_host_mask"


class SourceDialect(ABC):
    """The source-half of one migration lane. Implementations are stateless."""

    #: short identifier, e.g. "postgres", "sqlserver".
    name: str = "abstract"

    #: sqlglot dialect for parsing this lane's seed file (§8 gate bullet 0, SAU-513).
    #: Deliberately unset on the base: a lane that has not declared how its seed parses
    #: must fail the corpus audit loudly rather than be parsed under a wrong grammar.
    sql_read: str = ""

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

    def iter_rows(self, entity: str, mappings: list[Any]) -> "Iterator[dict[str, Any]]":
        """Stream `entity` as {column: text|None} rows, PK-ordered (SAU-511).

        Same wire form and same order as `extract_rows`, yielded one row at a time so the
        caller never has to hold the table. The default implementation materializes via
        `extract_rows` — correct, and it keeps every existing dialect (and test stub)
        working unchanged — but a lane that can genuinely stream should override it.
        """
        return iter(self.extract_rows(entity, mappings))

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

    # ── Seam 5: corpus-audit type facts (SAU-516) ───────────────────────────
    # Both hooks exist so gate bullet 0 stays type-agnostic. The moment the audit's
    # comparator learns a type name it becomes the answer key it was written to detect,
    # so every type-specific judgement is asked of the dialect that owns the type.

    def claim_equivalence(self, claimed: str | None, stored: str | None, *,
                          source_udt: str = "", data_type: str = "") -> str | None:
        """Name of the documented type equivalence under which a seed's `claimed`
        literal and the extracted `stored` text denote the same value — or None.

        Default: **None**. A lane declares its equivalences explicitly; an equivalence
        that arrived by inheritance is one nobody wrote down, and an undocumented
        equivalence is indistinguishable from silently accepting drift.
        """
        return None

    # ── Provenance: what source rig produced a readout ──────────────────────
    def rig_descriptor(self) -> dict[str, str]:
        """Where this lane read from, in enough detail to reproduce a readout.

        A gate result is a property of the *environment* as much as of the code —
        the same commit goes RED/GREEN by interpreter and by which seed is behind the
        socket. A readout that does not name its rig is not reproducible, so the
        readout prints this verbatim. Never includes credentials.
        """
        return {}

    def is_seed_declarable(self, facts: dict[str, Any]) -> bool:
        """Whether an INSERT in this source *can* name this column and state a value.

        `facts` are catalog facts from the profile projection: `data_type`, `udt`,
        `is_identity`, `is_computed`, `has_default`.

        This is the discriminator behind bullet 0's third state. The seed cannot tell
        "the trap was never planted" from "the type forbids planting it" — but the
        *catalog* can, because being engine-generated is a property of the type, not an
        observation about our corpus. That is what makes the exemption unabusable: it
        cannot be widened by appending to a list, only by lying about a type.

        Default: everything is declarable except a computed column. **Fails closed** —
        a column whose facts are unknown is treated as declarable, so a genuinely
        unplanted trap still fails rather than being exempted by omission.

        Note what is deliberately NOT here: a DEFAULT. A defaulted column is one an
        INSERT *may* name and this seed did not — the "normal column the seed forgot"
        case, which must keep failing. Row-level DEFAULTs are already handled honestly
        one level down, as an announced weakening of the comparison ("subset (N rows
        took a DEFAULT)"), not as an exemption.
        """
        return not bool(facts.get("is_computed"))


# ── PostgresDialect — the first implementation (SAU-303/305 behavior, verbatim) ──

_REPO = Path(__file__).resolve().parent.parent


def _stream_json_lines(cmd: list[str], *, context: str,
                       keep: "Any" = None) -> Iterator[dict[str, Any]]:
    """Run `cmd` and yield one parsed JSON object per matching stdout line (SAU-511).

    The extraction wire form is already one self-contained JSON object per row, which is
    what makes streaming a read change rather than a protocol change: `subprocess.run(
    capture_output=True)` buffered the entire table into one string (and then `json.loads`
    built the whole list) purely because nothing asked for it incrementally.

    `keep` is an optional predicate selecting data lines (the SQL Server lane must skip
    header/warning lines that some sqlcmd flavours interpose); the default keeps every
    non-blank line, matching the Postgres reader.

    Non-zero exit is raised as `SystemExit` *after* the stream drains, identical to the
    batch readers. The process is always reaped: if the consumer abandons the generator
    part-way (the diagnostic pass stops early on a saturated sample), the `finally` kills
    the child rather than leaking it.
    """
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if line and (keep is None or keep(line)):
                yield json.loads(line)
        rc = proc.wait()
        if rc != 0:
            sys.stderr.write(proc.stderr.read() if proc.stderr else "")
            raise SystemExit(f"{context} failed (exit {rc})")
    finally:
        if proc.poll() is None:      # consumer abandoned the generator
            proc.kill()
            proc.wait()
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None and not pipe.closed:
                pipe.close()

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
    #: sqlglot dialect used to parse this lane's seed file (SAU-513 gate bullet 0).
    sql_read = "postgres"

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

    def rig_descriptor(self) -> dict[str, str]:
        import os
        env = os.environ
        d = {
            "source": "PostgreSQL via psql (libpq env)",
            "PGHOST": env.get("PGHOST", "(default)"),
            "PGPORT": env.get("PGPORT", "(default)"),
            "PGDATABASE": env.get("PGDATABASE", "(default)"),
        }
        try:
            d["server_version"] = self._psql("SELECT version()").strip()
        except SystemExit:
            d["server_version"] = "(unreachable)"
        return d

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

    def _extract_sql(self, entity: str, mappings: list[Any]) -> str:
        """The extraction query: one JSON object per row, PK-ordered."""
        cols = sorted(mappings, key=lambda m: m.column)
        pairs = ",\n      ".join(
            f"'{m.column}', ({self.extraction_expr(m.column, m.reconcile)})" for m in cols
        )
        has_id = any(m.column == "id" for m in cols)
        order = 'ORDER BY "id"' if has_id else ""
        return (
            f"SELECT json_build_object(\n      {pairs}\n    )::text "
            f'FROM "{entity}" {order}'
        )

    def extract_rows(self, entity: str, mappings: list[Any]) -> list[dict[str, Any]]:
        out = self._psql(self._extract_sql(entity, mappings),
                         context=f"psql extract {entity}")
        rows: list[dict[str, Any]] = []
        for line in out.splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
        return rows

    #: Rows per server-side FETCH. psql without this pulls the *entire* result set into
    #: the client before printing a byte, which would move the O(table) buffer from
    #: Python into psql rather than removing it. Setting FETCH_COUNT switches psql to a
    #: cursor, so the table is never fully resident on either side of the pipe.
    FETCH_COUNT = 1000

    def iter_rows(self, entity: str, mappings: list[Any]) -> Iterator[dict[str, Any]]:
        """Stream the extraction, cursor-backed end to end (SAU-511)."""
        cmd = ["psql", "-X", "-tA", "-v", "ON_ERROR_STOP=1",
               "-v", f"FETCH_COUNT={self.FETCH_COUNT}",
               "-c", self._extract_sql(entity, mappings)]
        return _stream_json_lines(cmd, context=f"psql extract {entity}")

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

    # ── Seam 5 ──
    def claim_equivalence(self, claimed: str | None, stored: str | None, *,
                          source_udt: str = "", data_type: str = "") -> str | None:
        """`inet`/`cidr`: an omitted mask denotes the full host mask (SAU-516).

        `inet '203.0.113.42'` and `inet '203.0.113.42/32'` are the same value under the
        type's own equality operator — round-trip stable, nothing lost. The seed writes
        the abbreviated input grammar; the type stores its canonical output form.

        The scope is deliberately the narrowest that covers that: the claim must omit
        the mask **entirely**, and the stored form must carry **exactly** the full host
        mask (/32 IPv4, /128 IPv6). Every other mask difference stays a finding — a
        blanket strip-the-mask rule would also swallow `/24 → /32`, which is real data
        loss, trading a genuine hole in gate coverage for cosmetics.
        """
        if claimed is None or stored is None:
            return None
        if self.physical_token(data_type, source_udt) != PT_NETWORK:
            return None
        if "/" in claimed:                 # the claim states a mask — compare it verbatim
            return None
        host_mask = "/128" if ":" in claimed else "/32"     # IPv6 vs IPv4
        return EQ_INET_HOST_MASK if stored == claimed + host_mask else None


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
#: The engine-assigned subset of _MS_BINARY. A ROWVERSION counter is written by the
#: server on every INSERT/UPDATE and cannot be supplied by one — unlike plain
#: BINARY/VARBINARY, which a seed names like any other column. Same catalog signal that
#: already drives the BINARY(8) mapping, read for a second, narrower question.
_MS_ROWVERSION = ("timestamp", "rowversion")
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
    sql_read = "tsql"

    # ── Seam 1: profiler-SQL template ───────────────────────────────────────
    def profiler_install_sql(self) -> str:
        return (_REPO / "sqlserver" / "profile" / "profile.sqlserver.sql").read_text()

    def profile_query(self, schema: str) -> str:
        # The profiler proc prints one JSON document (FOR JSON) for `schema`.
        return f"EXEC dbo.phase0_profile @target_schema = N'{schema}';"

    def _sqlcmd_argv(self, sql: str, *, input_file: str | None = None) -> list[str]:
        """Build the sqlcmd command line. Shared by the batch and streaming readers so
        both issue byte-identical invocations.

        Connection comes from the standard env the SQL Server rig uses (mirrors
        sqlserver/scripts/setup.sh): MSSQL_HOST / MSSQL_HOST_PORT / MSSQL_SA_PASSWORD /
        MSSQL_DATABASE, with $SQLCMD overriding the binary.
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
        # in `_is_data_line`, so the wire form stays losslessly parseable on either tool.
        cmd = [binary, "-S", f"{host},{port}", "-U", user, "-P", pwd, "-d", db,
               "-C", "-b", "-y", "0"]
        if input_file is not None:
            cmd += ["-i", input_file]
        else:
            cmd += ["-Q", sql]
        return cmd

    def _sqlcmd(self, sql: str, *, context: str = "sqlcmd",
                input_file: str | None = None) -> str:
        """Run T-SQL via sqlcmd and return stdout."""
        cmd = self._sqlcmd_argv(sql, input_file=input_file)
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr)
            raise SystemExit(f"{context} failed (exit {proc.returncode})")
        return proc.stdout

    def run_profiler(self, schema: str = "dbo") -> str:
        """Install the profiler proc then invoke it; return the raw aggregate JSON.

        The proc emits exactly one JSON document, but its per-column statistics run
        `COUNT(DISTINCT …)`/`MIN`/`MAX` over nullable columns, and this rig's sqlcmd
        prints SQL Server's "Null value is eliminated by an aggregate" note to *stdout*,
        ahead of the document. Those are informational: `-b` makes a real error a
        non-zero exit, which `_sqlcmd` already raises on, so selecting the document here
        cannot swallow a failure — the same reason `_is_data_line` filters the
        extraction stream.

        A missing document raises, carrying the output. Returning a truncated or empty
        profile would push a *reader* failure downstream disguised as a thin schema —
        the misattribution SAU-513's unparsed-INSERT guard exists to prevent.
        """
        self._sqlcmd(self.profiler_install_sql(), context="sqlcmd install")
        out = self._sqlcmd(self.profile_query(schema), context="sqlcmd profile")
        for line in out.splitlines():
            if line.strip().startswith("{"):
                return line.strip()
        raise SystemExit(
            "sqlcmd profile returned no JSON document; sqlcmd said: "
            f"{out.strip()[:500] or '(nothing)'}")

    def rig_descriptor(self) -> dict[str, str]:
        import os
        env = os.environ
        d = {
            "source": "SQL Server via sqlcmd",
            "MSSQL_HOST": env.get("MSSQL_HOST", "localhost"),
            "MSSQL_HOST_PORT": env.get("MSSQL_HOST_PORT", "14333"),
            "MSSQL_DATABASE": env.get("MSSQL_DATABASE", "phase1"),
            "SQLCMD": env.get("SQLCMD", "sqlcmd"),
        }
        try:
            out = self._sqlcmd("SET NOCOUNT ON; SELECT @@VERSION")
            d["server_version"] = next(
                (ln.strip() for ln in out.splitlines() if ln.strip().startswith("Microsoft")),
                out.strip().splitlines()[0] if out.strip() else "(empty)")
        except SystemExit:
            d["server_version"] = "(unreachable)"
        return d

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

    def _extract_sql(self, entity: str, mappings: list[Any]) -> str:
        """The extraction query: one FOR JSON object per row, PK-ordered.

        FOR JSON keys off the column alias, so each output line is a parseable row in the
        same {column: text|None} shape the Postgres lane returns. INCLUDE_NULL_VALUES
        keeps NULL keys present (FOR JSON drops them by default), so a NULL is a real
        None in the row rather than a missing column.
        """
        cols = sorted(mappings, key=lambda m: m.column)
        pairs = ",\n      ".join(
            f"({self._wire_expr(m.column, m.pg_type, m.reconcile)}) AS [{m.column}]"
            for m in cols
        )
        has_id = any(m.column == "id" for m in cols)
        order = 'ORDER BY [id]' if has_id else ""
        return (
            "SET NOCOUNT ON;\n"
            f"SELECT (SELECT {pairs} "
            f"FOR JSON PATH, WITHOUT_ARRAY_WRAPPER, INCLUDE_NULL_VALUES) AS row_json\n"
            f"FROM [{entity}] {order}"
        )

    @staticmethod
    def _is_data_line(line: str) -> bool:
        # Each row is one FOR JSON object → one line. Skip any header / separator /
        # warning lines a sqlcmd flavour may interpose: a data line starts with '{'.
        return line.startswith("{")

    def extract_rows(self, entity: str, mappings: list[Any]) -> list[dict[str, Any]]:
        out = self._sqlcmd(self._extract_sql(entity, mappings),
                           context=f"sqlcmd extract {entity}")
        rows: list[dict[str, Any]] = []
        for line in out.splitlines():
            line = line.strip()
            if self._is_data_line(line):
                rows.append(json.loads(line))
        return rows

    def iter_rows(self, entity: str, mappings: list[Any]) -> Iterator[dict[str, Any]]:
        """Stream the extraction (SAU-511). Same wire form, same order, same filter."""
        return _stream_json_lines(
            self._sqlcmd_argv(self._extract_sql(entity, mappings)),
            context=f"sqlcmd extract {entity}",
            keep=self._is_data_line,
        )

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

    # ── Seam 5 ──
    def is_seed_declarable(self, facts: dict[str, Any]) -> bool:
        """ROWVERSION is engine-assigned, so no INSERT can state a claim about it.

        This is a schema fact, not an observation about our seed: the server writes the
        8-byte counter itself on every INSERT/UPDATE, and naming the column in an INSERT
        is an error. The audit's third state is therefore *derived from the type*, and a
        normal column the seed forgot still fails — the dialect reports it declarable.

        IDENTITY is deliberately **not** exempt here: `SET IDENTITY_INSERT` lets an
        INSERT name it, this lane's seed does exactly that, and `company.id` is verified
        by bullet 0 today. An exemption for something that is in fact declarable is the
        list-that-rots failure mode with extra steps.
        """
        if not super().is_seed_declarable(facts):
            return False
        return (facts.get("data_type") or "").lower() not in _MS_ROWVERSION


# Default lane until a run selects another. The whole loop is Postgres today; this keeps
# every existing call site (and the test suite) working without threading a dialect.
DEFAULT_DIALECT: SourceDialect = PostgresDialect()
