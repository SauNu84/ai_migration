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


def read_gold(con: duckdb.DuckDBPyConnection, entity: str,
              cols: list[str]) -> list[dict[str, Any]]:
    has_id = "id" in cols
    order = 'ORDER BY "id"' if has_id else ""
    quoted = ", ".join(f'"{c}"' for c in cols)
    cur = con.execute(f'SELECT {quoted} FROM "gold_{entity}" {order}')
    out = []
    for row in cur.fetchall():
        out.append({c: row[i] for i, c in enumerate(cols)})
    return out


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


def _checksum(values: list[str | None]) -> str:
    h = hashlib.md5()
    for i, v in enumerate(values):
        h.update(str(i).encode())
        h.update(b"\x1f")
        h.update(b"\x00NULL\x00" if v is None else v.encode("utf-8"))
        h.update(b"\x1e")
    return h.hexdigest()


def reconcile_entity(ec: cg.EntityCode, mappings: list[mp.Mapping],
                     source_rows: list[dict[str, Any]],
                     target_rows: list[dict[str, Any]],
                     udt_by_col: dict[str, str],
                     dialect: "dl.SourceDialect | None" = None) -> EntityResult:
    dialect = dialect or dl.DEFAULT_DIALECT
    cols = sorted(mappings, key=lambda m: m.column)
    col_results: list[ColumnResult] = []
    n = min(len(source_rows), len(target_rows))
    row_match = len(source_rows) == len(target_rows)

    for m in cols:
        udt = udt_by_col.get(m.column, "")
        src_canon = [canonicalize(m.reconcile, r.get(m.column), source_udt=udt, dialect=dialect)
                     for r in source_rows]
        tgt_canon = [canonicalize(m.reconcile, r.get(m.column), source_udt=udt, dialect=dialect)
                     for r in target_rows]
        scs, tcs = _checksum(src_canon), _checksum(tgt_canon)
        # Diagnostics are bounded at COLLECTION time. Appending every mismatching row and
        # slicing to [:10] only at ColumnResult construction inverted the failure mode: a
        # clean run cost nothing while a *systematically broken* mapping — precisely what
        # the gate exists to catch — allocated one dict + one formatted string per row and
        # OOM'd first. The cheap path stayed cheap and the diagnostic path exploded. The
        # retained content is unchanged (the slice already kept the first 10); only the
        # rows past the cap are no longer built. `n_mismatches` keeps the full scale.
        mismatches: list[dict[str, Any]] = []
        red: list[str] = []
        n_mismatch = 0
        for i in range(n):
            sv, tv = src_canon[i], tgt_canon[i]
            if sv == tv:
                continue
            n_mismatch += 1
            if len(mismatches) >= _DIAG_CAP:
                continue  # keep counting, stop allocating
            mismatches.append({"row": i, "source": sv, "target": tv})
            # red-flag classification
            if sv is not None and tv is None:
                red.append(f"row {i}: NOT-NULL source value silently dropped to NULL")
            elif m.reconcile in (mp.RC_EXACT_TEXT, mp.RC_TS_INSTANT, mp.RC_TS_WALL):
                red.append(
                    f"row {i}: exact-class mismatch ({m.semantic_type}) "
                    f"'{sv}' → '{tv}' — silent precision/identity loss"
                )
            elif m.reconcile == mp.RC_APPROX_FLOAT:
                red.append(
                    f"row {i}: float drift beyond epsilon '{sv}' → '{tv}'"
                )
            else:
                red.append(f"row {i}: {m.reconcile} mismatch '{sv}' → '{tv}'")
        match = (scs == tcs) and row_match
        col_results.append(
            ColumnResult(
                entity=m.entity,
                column=m.column,
                reconcile=m.reconcile,
                confidence=m.confidence,
                source_checksum=scs,
                target_checksum=tcs,
                match=match,
                n_compared=n,
                mismatches=mismatches,
                red_flags=red,
                flagged_lossy=("lossy_float" in m.flags),
                n_mismatches=n_mismatch,
            )
        )
    return EntityResult(
        entity=ec.entity,
        source_rows=len(source_rows),
        target_rows=len(target_rows),
        row_match=row_match,
        columns=col_results,
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
            src = extract_table(entity, ms, dialect)
            try:
                target.run_medallion(handle, ec, src)
                tgt = target.read_gold(handle, ec)
            except Exception as exc:  # noqa: BLE001 — a failed transform IS a caught defect
                results.append(EntityResult(
                    entity=entity, source_rows=len(src), target_rows=0,
                    row_match=False, columns=[],
                    exec_error=f"{type(exc).__name__}: {exc}",
                ))
                continue
            udt_by_col = {c: udt_index.get((entity, c), "") for c in ec.text_columns}
            results.append(reconcile_entity(ec, ms, src, tgt, udt_by_col, dialect))
    finally:
        target.disconnect(handle)
    return results
