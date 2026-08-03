"""Verification loop (SAU-305) — the whole point.

Drives the generated medallion against a sample and reconciles the result against
the source, byte/precision-exact where the manifest demands it:

  1. **Extract** each table from Postgres as a *lossless text wire form* (the same
     shape a bulk load to Snowflake uses) — no value is coerced before the typing
     decision is made. JSON/array columns are serialized to canonical JSON text so
     scale and leading zeros survive transit.
  2. **Execute** the medallion in DuckDB (the Snowflake-dialect execution proxy):
     load Bronze (all text) → run the SQLGlot-transpiled Silver (typed CASTs) → Gold.
  3. **Reconcile** Gold against the source: per-column canonicalization by the
     mapped reconcile strategy, then row-count + per-column **checksum** equality.
  4. **Red flags** (hard fail regardless of run count): row loss, a NOT-NULL source
     value turning NULL, or any exact-class mismatch (silent precision/identity loss).

**Streaming (SAU-511).** The loop holds no table. Each entity is reconciled in one walk
of the source and one of Gold:

  * `dialect.iter_rows()` streams the source (cursor-backed, so neither Python nor the
    client tool buffers the table), `_tee` folds every row into an `EntityDigest` on its
    way into the batched Bronze load, and `target.iter_gold()` streams Gold back through
    a second digest. A digest is one checksum accumulator per column — O(columns).
  * The verdict (row counts, per-column checksums, `match`) comes entirely from the
    digests. Only when it is already dirty does a third, diagnostic-only pass re-read
    both sides to collect a bounded sample of example mismatches; a clean run never pays
    for it, and the verdict never depends on it.

Measured through the full `run()` chassis (6-column entity, Python-side peak): 1k rows
7 KB, 1M rows 7 KB. The pre-SAU-511 loop materialized the source list, the load's tuple
list and Gold's `fetchall` — ~272 B/cell, extrapolating to ~2.7 GB at 1M x 10 cols.

What is NOT pushed down: `canonicalize()` still runs in Python on both sides. Computing
the digest in-engine would mean reimplementing every reconcile strategy in each target
SQL dialect and proving the two implementations agree bit-for-bit — a correctness risk
that dwarfs the memory win, and pushing down only one side would break comparison parity
outright. Streaming gets the same bound with the comparison semantics untouched.

Why DuckDB and not live Snowflake: Phase 0 has no Snowflake account. DuckDB is an
independent SQL engine (≠ the source) with Snowflake-like DECIMAL/timestamp semantics,
so a transform that only worked by leaning on source-engine behavior is still caught.
The Snowflake DDL/DML is the deliverable and is compile-checked in the Snowflake
dialect (codegen.compile_check). VARIANT/ARRAY are stored as canonical JSON text in
the proxy — a documented limitation; their *content* preservation is still reconciled,
and their VARIANT/ARRAY typing is validated by the Snowflake compile gate.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import duckdb

from . import codegen as cg
from . import dialect as dl
from . import mapper as mp
from . import target as tg

# float comparison tolerances by source physical width
_FLOAT4_SIG = 6      # REAL ~7 significant digits
_FLOAT8_REL = 1e-12  # double precision relative tolerance


# ── 1. Extraction (source → lossless text wire form) ────────────────────────
# The source read is a dialect seam (SAU-322): the per-column extraction expression and
# the row read live in the SourceDialect. This wrapper preserves the original entry
# point and defaults to Postgres so existing callers are unchanged.

def extract_table(entity: str, mappings: list[mp.Mapping],
                  dialect: "dl.SourceDialect | None" = None) -> list[dict[str, Any]]:
    """Extract a table from the source as a list of {column: text|None} rows, PK-ordered."""
    return (dialect or dl.DEFAULT_DIALECT).extract_rows(entity, mappings)


# ── 2. Execute medallion in DuckDB ──────────────────────────────────────────

def run_medallion(con: duckdb.DuckDBPyConnection, ec: cg.EntityCode,
                  rows: list[dict[str, Any]]) -> None:
    """Create Bronze (all text), load the wire rows, run Silver then Gold."""
    cols = ec.text_columns  # name-sorted, matches codegen projection order
    con.execute(ec.bronze_ddl["duckdb"])
    if rows:
        placeholders = ", ".join("?" for _ in cols)
        data = [tuple(r.get(c) for c in cols) for r in rows]
        con.executemany(
            f'INSERT INTO "bronze_{ec.entity}" VALUES ({placeholders})', data
        )
    con.execute(ec.silver_sql["duckdb"])
    con.execute(ec.gold_sql["duckdb"])


def load_medallion(con: duckdb.DuckDBPyConnection, ec: cg.EntityCode,
                   rows: Iterable[dict[str, Any]], *, batch: int = 5_000) -> None:
    """Streaming twin of `run_medallion` (SAU-511).

    Identical statements in an identical order — Bronze DDL, the same parameterized
    INSERT, Silver, Gold — but the wire rows are consumed from an iterable and flushed
    every `batch` rows instead of being turned into one N-row tuple list first. The
    caller may be teeing the same iterator into a checksum accumulator, so `rows` is
    consumed exactly once, in order.
    """
    cols = ec.text_columns  # name-sorted, matches codegen projection order
    con.execute(ec.bronze_ddl["duckdb"])
    placeholders = ", ".join("?" for _ in cols)
    insert = f'INSERT INTO "bronze_{ec.entity}" VALUES ({placeholders})'
    buf: list[tuple] = []
    for r in rows:
        buf.append(tuple(r.get(c) for c in cols))
        if len(buf) >= batch:
            con.executemany(insert, buf)
            buf.clear()
    if buf:
        con.executemany(insert, buf)
    con.execute(ec.silver_sql["duckdb"])
    con.execute(ec.gold_sql["duckdb"])


def _gold_sql(entity: str, cols: list[str]) -> str:
    order = 'ORDER BY "id"' if "id" in cols else ""
    quoted = ", ".join(f'"{c}"' for c in cols)
    return f'SELECT {quoted} FROM "gold_{entity}" {order}'


def read_gold(con: duckdb.DuckDBPyConnection, entity: str,
              cols: list[str]) -> list[dict[str, Any]]:
    cur = con.execute(_gold_sql(entity, cols))
    out = []
    for row in cur.fetchall():
        out.append({c: row[i] for i, c in enumerate(cols)})
    return out


def iter_gold(con: duckdb.DuckDBPyConnection, entity: str, cols: list[str],
              *, batch: int = 5_000) -> Iterator[dict[str, Any]]:
    """Streaming twin of `read_gold` (SAU-511): same query, same order, `fetchmany`."""
    cur = con.execute(_gold_sql(entity, cols))
    while True:
        chunk = cur.fetchmany(batch)
        if not chunk:
            return
        for row in chunk:
            yield {c: row[i] for i, c in enumerate(cols)}


# ── 3. Canonicalization by reconcile strategy ───────────────────────────────

def _canon_json(v: Any) -> str | None:
    if v is None:
        return None
    obj = json.loads(v) if isinstance(v, (str, bytes, bytearray)) else v
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _canon_ts_instant(v: Any, fix_tz) -> str | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        dt = v
    else:
        s = str(v).strip().replace(" ", "T", 1)
        dt = datetime.fromisoformat(fix_tz(s))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _canon_ts_wall(v: Any, fix_tz) -> str | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.replace(tzinfo=None).isoformat()
    s = str(v).strip().replace(" ", "T", 1)
    return datetime.fromisoformat(fix_tz(s)).replace(tzinfo=None).isoformat()


def _canon_float(v: Any, sig: int) -> str | None:
    if v is None or v == "":
        return None
    f = float(v)
    if f == 0:
        return "0"
    return f"{f:.{sig}g}"


def _canon_bool(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("t", "true", "1", "yes"):
        return "true"
    if s in ("f", "false", "0", "no"):
        return "false"
    return s


def _canon_exact(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, Decimal):
        return format(v, "f")  # fixed-point, preserves scale, no sci-notation
    if isinstance(v, (bytes, bytearray)):
        # BINARY columns are landed in the proxy as their canonical '0x…' hex TEXT (the
        # lossless wire form); DuckDB casting that text to BLOB keeps the literal hex
        # characters, so decoding the bytes back to text recovers the exact source hex.
        # The BINARY(N) *typing* is validated by the Snowflake compile gate, not here —
        # the same documented proxy limitation as VARIANT/ARRAY (stored as JSON text).
        return bytes(v).decode("latin-1")
    return str(v)


def canonicalize(reconcile: str, v: Any, *, source_udt: str = "",
                 dialect: "dl.SourceDialect | None" = None) -> str | None:
    """Map a value to its canonical comparison string for the given strategy.

    The tz wire-format and narrow-float-width hooks are source-specific (SAU-322), so
    they are delegated to the `dialect` (default Postgres); the comparison strategies
    themselves are dialect-agnostic.
    """
    dialect = dialect or dl.DEFAULT_DIALECT
    if reconcile == mp.RC_JSON_CANONICAL or reconcile == mp.RC_ARRAY_CANONICAL:
        return _canon_json(v)
    if reconcile == mp.RC_TS_INSTANT:
        return _canon_ts_instant(v, dialect.fix_tz)
    if reconcile == mp.RC_TS_WALL:
        return _canon_ts_wall(v, dialect.fix_tz)
    if reconcile == mp.RC_BOOLEAN:
        return _canon_bool(v)
    if reconcile == mp.RC_APPROX_FLOAT:
        sig = _FLOAT4_SIG if dialect.is_narrow_float(source_udt) else 12
        return _canon_float(v, sig)
    return _canon_exact(v)


# ── 4. Reconcile ────────────────────────────────────────────────────────────

# Diagnostics are *samples*, not a log: only the first `_DIAG_CAP` mismatching rows are
# retained per column. The cap is enforced at COLLECTION time, not at construction — see
# the note on the collection loop in `reconcile_entity` for why that distinction matters.
_DIAG_CAP = 10


@dataclass
class ColumnResult:
    entity: str
    column: str
    reconcile: str
    confidence: float
    source_checksum: str
    target_checksum: str
    match: bool
    n_compared: int
    mismatches: list[dict[str, Any]] = field(default_factory=list)
    red_flags: list[str] = field(default_factory=list)
    flagged_lossy: bool = False
    #: total mismatching rows seen, NOT the length of the retained sample. Counted in
    #: full even after diagnostic collection stops, so capping loses detail, never scale.
    n_mismatches: int = 0


@dataclass
class EntityResult:
    entity: str
    source_rows: int
    target_rows: int
    row_match: bool
    columns: list[ColumnResult]
    exec_error: str | None = None


class ChecksumAccumulator:
    """Incremental, order-sensitive checksum over canonical values (SAU-511).

    Byte-identical to the previous `_checksum(list)` by construction — same md5, same
    index-prefixed framing, same NULL sentinel — but fed one value at a time, so the
    digest never requires the column's values to exist simultaneously. `_checksum` is
    now a thin wrapper over this, which makes the equivalence structural rather than a
    property that has to be re-asserted.
    """

    __slots__ = ("_h", "_n")

    def __init__(self) -> None:
        self._h = hashlib.md5()
        self._n = 0

    def update(self, v: str | None) -> None:
        self._h.update(str(self._n).encode())
        self._h.update(b"\x1f")
        self._h.update(b"\x00NULL\x00" if v is None else v.encode("utf-8"))
        self._h.update(b"\x1e")
        self._n += 1

    @property
    def n(self) -> int:
        return self._n

    def hexdigest(self) -> str:
        return self._h.hexdigest()


def _checksum(values: list[str | None]) -> str:
    acc = ChecksumAccumulator()
    for v in values:
        acc.update(v)
    return acc.hexdigest()


class EntityDigest:
    """One side (source or target) of an entity reconciliation, accumulated row by row.

    Holds exactly one `ChecksumAccumulator` per mapped column plus a row counter, so its
    footprint is O(columns) — independent of row count. Rows are pushed in PK order via
    `add()`; nothing is retained after the canonical value is folded into the digest.
    """

    def __init__(self, mappings: list[mp.Mapping], udt_by_col: dict[str, str],
                 dialect: "dl.SourceDialect | None" = None) -> None:
        self._dialect = dialect or dl.DEFAULT_DIALECT
        self._cols = sorted(mappings, key=lambda m: m.column)
        self._udt = udt_by_col
        self._acc = {m.column: ChecksumAccumulator() for m in self._cols}
        self._n_rows = 0

    def add(self, row: dict[str, Any]) -> None:
        for m in self._cols:
            self._acc[m.column].update(
                canonicalize(m.reconcile, row.get(m.column),
                             source_udt=self._udt.get(m.column, ""),
                             dialect=self._dialect)
            )
        self._n_rows += 1

    @property
    def n_rows(self) -> int:
        return self._n_rows

    def checksum(self, column: str) -> str:
        return self._acc[column].hexdigest()


def _red_flag(m: mp.Mapping, i: int, sv: str | None, tv: str | None) -> str:
    """Classify one mismatching row into its red-flag wording (unchanged from SAU-305)."""
    if sv is not None and tv is None:
        return f"row {i}: NOT-NULL source value silently dropped to NULL"
    if m.reconcile in (mp.RC_EXACT_TEXT, mp.RC_TS_INSTANT, mp.RC_TS_WALL):
        return (f"row {i}: exact-class mismatch ({m.semantic_type}) "
                f"'{sv}' → '{tv}' — silent precision/identity loss")
    if m.reconcile == mp.RC_APPROX_FLOAT:
        return f"row {i}: float drift beyond epsilon '{sv}' → '{tv}'"
    return f"row {i}: {m.reconcile} mismatch '{sv}' → '{tv}'"


def _diagnose(mappings: list[mp.Mapping], columns: list[str],
              pairs: "Iterator[tuple[dict[str, Any], dict[str, Any]]]",
              udt_by_col: dict[str, str],
              dialect: "dl.SourceDialect") -> dict[str, tuple[list, list, int]]:
    """Second, DIAGNOSTIC-ONLY pass: collect bounded mismatch samples for `columns`.

    Runs only when the verdict is already known to be dirty, and only over the columns
    whose checksums actually disagree — a clean run never enters here at all.

    The walk covers every row, but **collection** is capped: past `_DIAG_CAP` a
    mismatching row is counted and discarded, never formatted or retained. So the pass
    is O(rows) in time and O(columns x cap) in memory. Walking to the end rather than
    stopping at saturation is deliberate: memory is the defect under repair, not time,
    and it keeps `n_mismatches` an exact count. A field that means "total" on small
    inputs and "at least" on large ones would be worse than no field — "3.2M of 10M rows
    mismatch" is the number that tells an operator whether a mapping is subtly wrong or
    categorically wrong.

    The pass consumes `pairs` (zipped source/target rows) lazily and retains nothing
    beyond the capped samples.
    """
    wanted = set(columns)
    by_col = {m.column: m for m in mappings if m.column in wanted}
    out: dict[str, tuple[list, list, int]] = {c: ([], [], 0) for c in by_col}
    if not by_col:
        return out
    for i, (srow, trow) in enumerate(pairs):
        for col, m in by_col.items():
            mism, red, n_mis = out[col]
            udt = udt_by_col.get(col, "")
            sv = canonicalize(m.reconcile, srow.get(col), source_udt=udt, dialect=dialect)
            tv = canonicalize(m.reconcile, trow.get(col), source_udt=udt, dialect=dialect)
            if sv == tv:
                continue
            if len(mism) < _DIAG_CAP:      # collect a bounded sample...
                mism.append({"row": i, "source": sv, "target": tv})
                red.append(_red_flag(m, i, sv, tv))
            out[col] = (mism, red, n_mis + 1)   # ...but count in full
    return out


def reconcile_digests(ec: cg.EntityCode, mappings: list[mp.Mapping],
                      src: EntityDigest, tgt: EntityDigest,
                      udt_by_col: dict[str, str],
                      diagnose_pairs: "Callable[[], Iterator[tuple[dict, dict]]] | None" = None,
                      dialect: "dl.SourceDialect | None" = None) -> EntityResult:
    """Build the `EntityResult` from two already-accumulated digests.

    This is the verdict half of reconciliation and it is *streaming-complete*: everything
    the §8 gate consumes — row counts, per-column checksums, `match` — comes from the
    digests, which are O(columns). No row of either side is retained.

    `diagnose_pairs`, when supplied, is a **factory** for a fresh zipped (source, target)
    row walk. It is invoked at most once, and only when the verdict is already dirty, so
    a clean run never pays for it. The verdict never depends on it: a re-read that came
    back different would change the reported *examples*, never the pass/fail.
    """
    dialect = dialect or dl.DEFAULT_DIALECT
    cols = sorted(mappings, key=lambda m: m.column)
    row_match = src.n_rows == tgt.n_rows
    n = min(src.n_rows, tgt.n_rows)

    sums = {m.column: (src.checksum(m.column), tgt.checksum(m.column)) for m in cols}
    dirty = [m.column for m in cols if sums[m.column][0] != sums[m.column][1]]

    diag: dict[str, tuple[list, list, int]] = {}
    if dirty and diagnose_pairs is not None:
        diag = _diagnose(cols, dirty, diagnose_pairs(), udt_by_col, dialect)

    col_results: list[ColumnResult] = []
    for m in cols:
        scs, tcs = sums[m.column]
        mism, red, n_mis = diag.get(m.column, ([], [], 0))
        col_results.append(
            ColumnResult(
                entity=m.entity,
                column=m.column,
                reconcile=m.reconcile,
                confidence=m.confidence,
                source_checksum=scs,
                target_checksum=tcs,
                match=(scs == tcs) and row_match,
                n_compared=n,
                mismatches=mism,
                red_flags=red,
                flagged_lossy=("lossy_float" in m.flags),
                n_mismatches=n_mis,
            )
        )
    return EntityResult(
        entity=ec.entity,
        source_rows=src.n_rows,
        target_rows=tgt.n_rows,
        row_match=row_match,
        columns=col_results,
    )


def reconcile_entity(ec: cg.EntityCode, mappings: list[mp.Mapping],
                     source_rows: list[dict[str, Any]],
                     target_rows: list[dict[str, Any]],
                     udt_by_col: dict[str, str],
                     dialect: "dl.SourceDialect | None" = None) -> EntityResult:
    """List-based reconciliation — the original entry point, preserved verbatim.

    Since SAU-511 it is a thin adapter over the streaming core: it folds both lists into
    digests and defers to `reconcile_digests`. Callers that already hold materialized
    rows (tests, small entities) are unchanged; the point of routing them through the
    same core is that the existing suite exercises the streaming implementation, so
    parity is proven by the tests that already existed rather than asserted separately.
    """
    dialect = dialect or dl.DEFAULT_DIALECT
    src = EntityDigest(mappings, udt_by_col, dialect)
    tgt = EntityDigest(mappings, udt_by_col, dialect)
    for r in source_rows:
        src.add(r)
    for r in target_rows:
        tgt.add(r)
    return reconcile_digests(
        ec, mappings, src, tgt, udt_by_col,
        diagnose_pairs=lambda: zip(source_rows, target_rows),
        dialect=dialect,
    )


# ── 5. Semantic guard (structural detector) ─────────────────────────────────
# Data checksums can be *masked*: a naive mapper that changes both the type AND the
# reconcile assertion to match its wrong belief can make corrupt values "agree" (e.g.
# treating a no-tz timestamp as tz-aware reconciles fine under a UTC session). The
# semantic guard compares the chosen target against the SOURCE physical type directly,
# so a semantic change is surfaced regardless of whether the sample data happens to
# round-trip — satisfying §8's "reconciliation that masks a semantic mismatch — surface
# it, do not hide it."

_NUMERIC_TARGET = ("NUMBER", "DECIMAL", "FLOAT", "INT", "DOUBLE")
_TEMPORAL_TARGET = ("TIMESTAMP", "DATETIME", "DATE", "TIME")


def semantic_guard(prop, m: mp.Mapping,
                   dialect: "dl.SourceDialect | None" = None) -> list[str]:
    """Structural red flags comparing source physical type ↔ chosen Snowflake target.

    Source-type recognition is delegated to the `dialect` (SAU-322): the guard branches
    on the dialect's *canonical* `PT_*` token, never on raw source type strings, so a new
    source lane (SQL Server, Oracle, …) inherits the guard for free instead of bolting on
    a per-dialect string list. Defaults to Postgres so existing call sites are unchanged
    — and the token table makes the Postgres branches byte-equivalent to the old strings.
    """
    dialect = dialect or dl.DEFAULT_DIALECT
    tok = dialect.physical_token(prop.data_type or "", prop.udt or "")
    tgt = m.snowflake_type.upper()
    sem = m.semantic_type
    flags: list[str] = []

    # tz-awareness must not change (no invented/dropped offset).
    if tok == dl.PT_TIMESTAMP_NTZ and "TZ" in tgt and "NTZ" not in tgt:
        flags.append(
            "tz-awareness INVENTED: no-tz source mapped to a tz target — an offset "
            "would be fabricated and instants shifted"
        )
    if tok == dl.PT_TIMESTAMP_TZ and "NTZ" in tgt:
        flags.append("tz DROPPED: tz-aware source mapped to a no-tz target")

    # raw bytes / ROWVERSION must never be read as a temporal type (the SQL Server
    # 'timestamp' synonym is a name trap: it is an 8-byte binary counter, not a time).
    if tok == dl.PT_BINARY and tgt.startswith(_TEMPORAL_TARGET):
        flags.append(
            f"binary/row-version identity MISREAD as a temporal type ({tgt}): an "
            "8-byte counter is not a timestamp — invented chronology, broken identity"
        )

    # textual identity must not become numeric (leading zeros / formatting lost).
    if tok in (dl.PT_TEXT, dl.PT_FIXED_CHAR) and tgt.startswith(_NUMERIC_TARGET):
        flags.append(
            f"textual identity COERCED to numeric ({tgt}): leading zeros / verbatim "
            f"format ({sem}) would be lost"
        )

    # exact numeric must not become binary float.
    if sem in ("currency_decimal", "currency_as_integer", "rate_fraction") and \
            tgt.startswith(("FLOAT", "DOUBLE")):
        flags.append(f"exact numeric ({sem}) mapped to binary FLOAT — precision lost")

    # numeric scale must not be reduced.
    if prop.numeric_scale and tgt.startswith(("NUMBER", "DECIMAL")) and "(" in tgt:
        try:
            inner = tgt[tgt.index("(") + 1:tgt.index(")")]
            t_scale = int(inner.split(",")[1]) if "," in inner else 0
            if t_scale < prop.numeric_scale:
                flags.append(
                    f"scale REDUCED {prop.numeric_scale}→{t_scale}: rounding/truncation"
                )
        except (ValueError, IndexError):
            pass

    # semi-structured / array must not be flattened to a scalar string.
    if sem in ("semi_structured",) and tgt.startswith("VARCHAR"):
        flags.append("JSON structure FLATTENED to VARCHAR — nested keys/queryability lost")
    if sem in ("array", "array_numeric") and tgt.startswith("VARCHAR"):
        flags.append("ARRAY flattened to a delimited VARCHAR — element structure lost")

    return flags


class SourceReadError(Exception):
    """A failure DELIVERING a source row — the extraction itself, not the mapping.

    Streaming puts the source walk *inside* the target's execution call — the rows are
    pulled lazily by the Bronze load. Without this marker the `except Exception` that
    records a transform `exec_error` would also swallow a failed extraction and report
    it as a caught transform defect: the gate would show a red flag it did not actually
    earn, and a genuinely broken source read would look like a successful detection.
    That is the masking failure mode §8 exists to forbid, so source failures are tagged
    here and deliberately re-raised.

    Scope is deliberately narrow: **only pulling the next row** counts. Folding a row
    into the source digest canonicalizes it under the mapping being tested, and a value
    the mapping cannot canonicalize is a *mapping* defect — exactly what the ablation
    run exists to provoke — so it must stay an ordinary exception the target's
    `except Exception` records as a caught defect. Tagging it here instead aborted the
    whole gate on the SQL Server lane's ROWVERSION trap (`audit_trail.row_version`
    forced to `TIMESTAMP_NTZ`: `'0x0000000000002715'` is not an ISO-8601 string). See
    `_tee`.
    """


def _iter_source(dialect: "dl.SourceDialect", entity: str,
                 mappings: list[mp.Mapping]) -> Iterator[dict[str, Any]]:
    """Stream source rows, falling back to the materializing read.

    `SourceDialect` supplies `iter_rows`, but the dialect seam is duck-typed at several
    call sites (test stubs implement only `extract_rows`), so a missing streaming method
    degrades to the batch read rather than exploding.
    """
    it = getattr(dialect, "iter_rows", None)
    if it is not None:
        return iter(it(entity, mappings))
    return iter(extract_table(entity, mappings, dialect))


def _iter_target(target: "tg.TargetAdapter", handle: Any,
                 ec: cg.EntityCode) -> Iterator[dict[str, Any]]:
    """Stream Gold rows, falling back to the materializing read for duck-typed adapters."""
    it = getattr(target, "iter_gold", None)
    if it is not None:
        return iter(it(handle, ec))
    return iter(target.read_gold(handle, ec))


def _load_target(target: "tg.TargetAdapter", handle: Any, ec: cg.EntityCode,
                 rows: Iterable[dict[str, Any]]) -> None:
    """Load the medallion from a row stream, falling back for duck-typed adapters."""
    load = getattr(target, "load_medallion", None)
    if load is not None:
        load(handle, ec, rows)
    else:
        target.run_medallion(handle, ec, list(rows))


def _tee(rows: Iterable[dict[str, Any]],
         sink: "Callable[[dict[str, Any]], None]") -> Iterator[dict[str, Any]]:
    """Yield each row after handing it to `sink` — one walk, two consumers.

    The two halves fail for different reasons and must be classified differently:

    * **Pulling** the row is the source read. A failure there means the source never
      delivered, and recording it as a caught transform defect would be a detection the
      gate did not earn — so it is tagged `SourceReadError` and re-raised.
    * **`sink(r)`** folds the row into the source digest, which canonicalizes it under
      the mapping under test. A failure there is a *mapping* defect (the ablation run
      forces exactly these), so it propagates untagged and the target's
      `except Exception` records it as the caught defect it is.

    `BaseException` (notably the `SystemExit` a failed psql/sqlcmd raises) is left alone
    on both halves — it already bypasses the target's `except Exception`.
    """
    it = iter(rows)
    while True:
        try:
            r = next(it)
        except StopIteration:
            return
        except Exception as exc:
            raise SourceReadError(f"{type(exc).__name__}: {exc}") from exc
        sink(r)
        yield r


def run(code: dict[str, cg.EntityCode], mappings: list[mp.Mapping],
        udt_index: dict[tuple[str, str], str],
        dialect: "dl.SourceDialect | None" = None,
        target: "tg.TargetAdapter | None" = None) -> list[EntityResult]:
    """Full verification run across all entities.

    The execution target is a seam (SAU-432): the medallion is driven through a
    `TargetAdapter` (default the byte-for-byte DuckDB proxy), so the same loop runs on
    the live Snowflake path once creds land — no plumbing change. One handle is opened
    per run and reused across entities (isolation is fresh-handle-per-run).
    """
    dialect = dialect or dl.DEFAULT_DIALECT
    target = target or tg.DEFAULT_TARGET
    by_entity: dict[str, list[mp.Mapping]] = {}
    for m in mappings:
        by_entity.setdefault(m.entity, []).append(m)

    results: list[EntityResult] = []
    handle = target.connect()
    try:
        for entity, ec in code.items():
            ms = by_entity[entity]
            udt_by_col = {c: udt_index.get((entity, c), "") for c in ec.text_columns}
            src_digest = EntityDigest(ms, udt_by_col, dialect)
            try:
                # Pass 1 — ONE walk of the source serves both consumers: each row is
                # folded into the source digest on its way into the Bronze load. Neither
                # the extraction nor the load ever holds the table.
                _load_target(
                    target, handle, ec,
                    _tee(_iter_source(dialect, entity, ms), src_digest.add),
                )
                # Pass 2 — stream Gold back through the target digest.
                tgt_digest = EntityDigest(ms, udt_by_col, dialect)
                for row in _iter_target(target, handle, ec):
                    tgt_digest.add(row)
            except SourceReadError:
                # NOT a transform defect — the source never delivered. Propagate loudly
                # rather than recording it as a caught defect (see SourceReadError).
                raise
            except Exception as exc:  # noqa: BLE001 — a failed transform IS a caught defect
                # `source_rows` is what the digest actually consumed. If the failure was
                # in Silver/Gold the source was fully read and this is the true count; if
                # the Bronze load itself failed it is a partial count, which is the
                # honest number for a run that never finished reading.
                results.append(EntityResult(
                    entity=entity, source_rows=src_digest.n_rows, target_rows=0,
                    row_match=False, columns=[],
                    exec_error=f"{type(exc).__name__}: {exc}",
                ))
                continue
            results.append(reconcile_digests(
                ec, ms, src_digest, tgt_digest, udt_by_col,
                # Pass 3 — diagnostics only, and only if the verdict is already dirty.
                # A fresh zipped re-read of both sides; see `reconcile_digests`.
                diagnose_pairs=lambda ec=ec, ms=ms, entity=entity: zip(
                    _iter_source(dialect, entity, ms), _iter_target(target, handle, ec)
                ),
                dialect=dialect,
            ))
    finally:
        target.disconnect(handle)
    return results
