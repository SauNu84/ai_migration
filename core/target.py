"""Target execution seam (SAU-432): the pluggable *target-half* of the migration loop.

Where `dialect.SourceDialect` abstracts everything specific to the *source* database,
`TargetAdapter` abstracts everything specific to the *execution target* — the engine
the generated medallion actually runs on. Until now there was no target seam at all:
`reconcile.run_medallion` hard-wired DuckDB (the Snowflake execution *proxy*), and
`snowflake_conn.py` was a bare connection factory with no execution path.

This module introduces one interface and two implementations, mirroring the
`SourceDialect` ABC pattern:

  * **`TargetAdapter`** — consumes an `EntityCode` (the codegen deliverable) and drives
    the Bronze-load → Silver → Gold medallion against one engine, then reads Gold back
    for reconciliation. A run opens one handle, reuses it across every entity, closes it.
  * **`DuckDbAdapter`** — the default. Reproduces the pre-SAU-432
    `reconcile.run_medallion` / `read_gold` behaviour *byte-for-byte* (it literally
    delegates to those functions), so the §8 gate stays GREEN through the new seam.
  * **`SnowflakeAdapter`** — the live path via `snowflake_conn.connect()`. It runs the
    **committed Snowflake SQL unedited** (`ec.{bronze_ddl,silver_sql,gold_sql}["snowflake"]`
    — no dialect fixups) behind a COPY-INTO stage load and warehouse-scoped execution.
    Live execution is **guarded**: `connect()` refuses until `live=True` (which SAU-309
    flips once SAU-308 creds land). The load/exec *statement sequencing* is exercised
    DB-free against a mock cursor (see tests/test_target.py) so the whole path is proven
    before a real account exists.

The key Phase-1 property: when creds land, SAU-309 constructs `SnowflakeAdapter(live=True)`
and the loop runs the same committed SQL that the DuckDB proxy validated — no new
plumbing, no SQL edits. VARIANT/ARRAY stay native (`PARSE_JSON`, not JSON-text) and the
TIMESTAMP_NTZ/TZ distinction is preserved verbatim from the codegen output.
"""
from __future__ import annotations

import json
import os
import tempfile
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # avoid importing codegen at module load (it pulls sqlglot)
    from . import codegen as cg


class TargetAdapter(ABC):
    """The target-half of one migration lane. Implementations are stateless; the live
    engine state lives in the opaque handle returned by `connect()`."""

    #: short identifier, e.g. "duckdb", "snowflake".
    name: str = "abstract"

    @abstractmethod
    def connect(self) -> Any:
        """Open an execution handle (a connection/session). The caller owns closing it
        via `disconnect()`; one handle is reused across every entity in a run."""

    @abstractmethod
    def disconnect(self, handle: Any) -> None:
        """Close a handle opened by `connect()`. Tolerates None."""

    @abstractmethod
    def run_medallion(self, handle: Any, ec: "cg.EntityCode",
                      rows: list[dict[str, Any]]) -> None:
        """Create Bronze (all text), load the wire `rows`, then run Silver and Gold."""

    @abstractmethod
    def read_gold(self, handle: Any, ec: "cg.EntityCode") -> list[dict[str, Any]]:
        """Read Gold as a list of {column: value} rows, PK-ordered (matches source order)."""


# ── DuckDbAdapter — the default execution proxy (byte-for-byte with SAU-305) ──
#
# Delegates to `reconcile.run_medallion` / `reconcile.read_gold` verbatim (lazy import
# to avoid a cycle: reconcile imports this module for DEFAULT_TARGET). Nothing about the
# executed SQL, the load, or the Gold read changes — the §8 gate is behaviour-identical
# through the seam.

class DuckDbAdapter(TargetAdapter):
    """DuckDB execution proxy. Reproduces the pre-seam behaviour exactly."""

    name = "duckdb"

    def connect(self) -> Any:
        import duckdb
        con = duckdb.connect(":memory:")
        con.execute("SET TimeZone='UTC'")   # same session pin as the old reconcile.run
        return con

    def disconnect(self, handle: Any) -> None:
        if handle is not None:
            handle.close()

    def run_medallion(self, handle: Any, ec: "cg.EntityCode",
                      rows: list[dict[str, Any]]) -> None:
        from . import reconcile as rc
        rc.run_medallion(handle, ec, rows)

    def read_gold(self, handle: Any, ec: "cg.EntityCode") -> list[dict[str, Any]]:
        from . import reconcile as rc
        return rc.read_gold(handle, ec.entity, ec.text_columns)


# ── SnowflakeAdapter — the live path (guarded until SAU-308 creds land) ───────

class TargetNotLiveError(RuntimeError):
    """Raised when a live target is invoked before it has been explicitly enabled."""


class SnowflakeAdapter(TargetAdapter):
    """Live Snowflake execution.

    Runs the committed Snowflake medallion SQL unedited behind a COPY-INTO stage load,
    scoped to a compute warehouse. `live=False` (the default) keeps the whole thing a
    no-op guard: `connect()` refuses so an accidental selection can never half-run
    against a real account. The statement sequencing is fully exercised DB-free by
    driving `execute_medallion` with a mock cursor.
    """

    name = "snowflake"

    #: JSON file format used for the Bronze stage load. STRIP_OUTER_ARRAY unwraps the
    #: single JSON array we stage; MATCH_BY_COLUMN_NAME maps top-level keys → columns so
    #: the all-VARCHAR Bronze lands the lossless text wire form untouched (Silver then
    #: PARSE_JSONs the VARIANT/ARRAY columns natively — never a JSON-text round-trip).
    _FILE_FORMAT = "(TYPE = JSON STRIP_OUTER_ARRAY = TRUE)"

    def __init__(self, *, live: bool = False, warehouse: str | None = None,
                 stage: str = "migration_bronze_stage") -> None:
        self.live = live
        # Warehouse scoping is explicit: the arg wins, else the standard env var. A run
        # with no warehouse simply omits the USE WAREHOUSE step (uses the session default).
        self.warehouse = warehouse or os.environ.get("SNOWFLAKE_WAREHOUSE")
        self.stage = stage

    # ── connection lifecycle (live-guarded) ─────────────────────────────────
    def connect(self) -> Any:
        if not self.live:
            raise TargetNotLiveError(
                "SnowflakeAdapter live execution is gated: construct SnowflakeAdapter("
                "live=True) once SAU-308 credentials are present (SAU-309 flips this). "
                "The default DuckDB proxy runs the same committed SQL until then."
            )
        from . import snowflake_conn as sc  # optional driver imported only on the live path
        return sc.connect()

    def disconnect(self, handle: Any) -> None:
        if handle is not None:
            handle.close()

    def run_medallion(self, handle: Any, ec: "cg.EntityCode",
                      rows: list[dict[str, Any]]) -> None:
        cur = handle.cursor()
        try:
            self.execute_medallion(cur, ec, rows)
        finally:
            cur.close()

    def read_gold(self, handle: Any, ec: "cg.EntityCode") -> list[dict[str, Any]]:
        cur = handle.cursor()
        try:
            cols = ec.text_columns
            order = 'ORDER BY "id"' if "id" in cols else ""
            quoted = ", ".join(f'"{c}"' for c in cols)
            cur.execute(f'SELECT {quoted} FROM "gold_{ec.entity}" {order}')
            out: list[dict[str, Any]] = []
            for row in cur.fetchall():
                out.append({c: row[i] for i, c in enumerate(cols)})
            return out
        finally:
            cur.close()

    # ── testable execution seam (DB-free with a mock cursor) ─────────────────
    def copy_into_sql(self, ec: "cg.EntityCode") -> str:
        """The COPY-INTO that loads the staged wire rows into the all-text Bronze table.

        MATCH_BY_COLUMN_NAME keys off the JSON object's fields, so the load is
        column-order-independent and every Bronze column receives its lossless text.
        """
        return (
            f'COPY INTO "bronze_{ec.entity}" FROM @{self.stage}/{ec.entity}.json '
            f"FILE_FORMAT = {self._FILE_FORMAT} "
            f"MATCH_BY_COLUMN_NAME = CASE_INSENSITIVE PURGE = TRUE"
        )

    def execute_medallion(self, cursor: Any, ec: "cg.EntityCode",
                          rows: list[dict[str, Any]]) -> None:
        """Drive the full medallion against `cursor`, in strict Snowflake sequence:

            USE WAREHOUSE  →  CREATE bronze  →  CREATE STAGE  →  PUT rows  →
            COPY INTO bronze  →  Silver (committed SQL)  →  Gold (committed SQL)

        Only `cursor.execute()` is used, so a mock cursor exercises the entire ordering
        without a live account. The Silver/Gold statements are the committed Snowflake
        codegen output verbatim — no dialect fixups — so the live run replays exactly
        what the DuckDB proxy validated.
        """
        if self.warehouse:
            cursor.execute(f'USE WAREHOUSE "{self.warehouse}"')
        # Bronze: all-text landing table (committed DDL, unedited).
        cursor.execute(ec.bronze_ddl["snowflake"])
        # Stage + COPY-INTO load of the lossless wire rows.
        cursor.execute(f"CREATE TEMPORARY STAGE IF NOT EXISTS {self.stage}")
        path = self._stage_rows(cursor, ec, rows)
        try:
            cursor.execute(self.copy_into_sql(ec))
        finally:
            if path is not None:
                os.unlink(path)
        # Silver then Gold: the committed Snowflake SQL, byte-identical (native VARIANT/
        # ARRAY via PARSE_JSON, TIMESTAMP_NTZ/TZ distinction preserved).
        cursor.execute(ec.silver_sql["snowflake"])
        cursor.execute(ec.gold_sql["snowflake"])

    def _stage_rows(self, cursor: Any, ec: "cg.EntityCode",
                    rows: list[dict[str, Any]]) -> str | None:
        """Write the wire `rows` to a temp JSON array and PUT it to the Bronze stage.

        Returns the temp path (for the caller to unlink) or None when there are no rows.
        The rows are the lossless {column: text|None} wire form; JSON preserves them
        exactly (including JSON-text values destined for VARIANT/ARRAY columns).
        """
        if not rows:
            return None
        cols = ec.text_columns
        payload = [{c: r.get(c) for c in cols} for r in rows]
        fd, path = tempfile.mkstemp(suffix=f".{ec.entity}.json")
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
        # AUTO_COMPRESS off keeps the staged filename stable so COPY INTO can name it.
        cursor.execute(
            f"PUT file://{path} @{self.stage}/{ec.entity}.json "
            f"OVERWRITE = TRUE AUTO_COMPRESS = FALSE"
        )
        return path


# Default target until a run selects another. Keeps every existing call site (and the
# §8 gate) on the byte-for-byte DuckDB proxy without threading a target through.
DEFAULT_TARGET: TargetAdapter = DuckDbAdapter()
