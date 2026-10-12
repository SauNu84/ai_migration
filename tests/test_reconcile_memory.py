#!/usr/bin/env python3
"""SAU-511 — reconciliation memory behaviour.

Two independent properties, both DB-free and both measured rather than asserted by
inspection:

  1. **Bounded diagnostics.** `mismatches` / `red_flags` are *samples* capped at
     collection time. Before SAU-511 they were appended once per mismatching row and
     sliced to [:10] only at `ColumnResult` construction, so a systematically broken
     mapping — exactly what the §8 gate exists to catch — allocated one dict plus one
     formatted string per row and OOM'd before it could report. The failure mode was
     inverted: the correct run was cheap, the diagnostic run exploded.

  2. **Content parity.** Capping must lose *scale*, never *detail*: the retained sample
     is byte-identical to what the old slice produced, and `n_mismatches` carries the
     full count that the sample no longer implies.

Row-paired diagnostics only exist for a key-ordered entity (SAU-548: a keyless entity's
two walks have no common order to pair by), so the tests that read the sample or the
full count use an `id` column.

Run:
    python3 -m unittest tests.test_reconcile_memory -v
"""
from __future__ import annotations

import tracemalloc
import unittest

from core import mapper as mp
from core import reconcile as rc


class _Ec:
    """Minimal EntityCode stand-in — `reconcile_entity` only reads `.entity`."""

    def __init__(self, entity: str = "t") -> None:
        self.entity = entity


def _mapping(column: str, reconcile: str = mp.RC_EXACT_TEXT) -> mp.Mapping:
    return mp.Mapping(
        entity="t", column=column, pg_type="text", semantic_type="ambiguous_text",
        snowflake_type="VARCHAR(16777216)", reconcile=reconcile, confidence=0.9,
        governance_tags=[], flags=[], notes="",
    )


def _rows(n: int, cols: list[str], *, broken: bool) -> list[dict[str, str]]:
    """`broken=True` makes EVERY row mismatch on every column (the pathological gate case)."""
    out = []
    for i in range(n):
        out.append({c: (f"{c}-{i}-x" if broken else f"{c}-{i}") for c in cols})
    return out


def _reconcile(n_rows: int, cols: list[str], *, broken: bool) -> rc.EntityResult:
    ms = [_mapping(c) for c in cols]
    src = _rows(n_rows, cols, broken=False)
    tgt = _rows(n_rows, cols, broken=broken)
    return rc.reconcile_entity(_Ec(), ms, src, tgt, {c: "text" for c in cols})


class TestDiagnosticsAreBounded(unittest.TestCase):
    def test_sample_never_exceeds_the_cap_however_broken_the_mapping(self):
        for n in (5, 50, 5_000):
            res = _reconcile(n, ["a", "b"], broken=True)
            for cr in res.columns:
                self.assertLessEqual(len(cr.mismatches), rc._DIAG_CAP, f"n={n}")
                self.assertLessEqual(len(cr.red_flags), rc._DIAG_CAP, f"n={n}")

    def test_full_mismatch_count_survives_the_cap(self):
        res = _reconcile(5_000, ["id"], broken=True)
        cr = res.columns[0]
        # The sample is bounded; the *scale* of the breakage is not lost with it.
        self.assertEqual(len(cr.mismatches), rc._DIAG_CAP)
        self.assertEqual(cr.n_mismatches, 5_000)
        self.assertEqual(cr.n_compared, 5_000)
        self.assertFalse(cr.match)

    def test_clean_run_reports_zero_mismatches(self):
        res = _reconcile(1_000, ["a", "b"], broken=False)
        for cr in res.columns:
            self.assertTrue(cr.match)
            self.assertEqual(cr.n_mismatches, 0)
            self.assertEqual(cr.mismatches, [])
            self.assertEqual(cr.red_flags, [])

    def test_retained_sample_is_byte_identical_to_the_old_slice(self):
        """Capping changed *where* the bound is applied, not *what* it keeps.

        The pre-SAU-511 code kept `mismatches[:10]` / `red[:10]` — i.e. the first ten
        mismatching rows in row order. This reconstructs that expectation independently
        and asserts equality, so the cheap fix cannot silently change gate diagnostics.
        """
        res = _reconcile(500, ["id"], broken=True)
        cr = res.columns[0]
        expected = [{"row": i, "source": f"id-{i}", "target": f"id-{i}-x"}
                    for i in range(rc._DIAG_CAP)]
        self.assertEqual(cr.mismatches, expected)
        self.assertEqual(len(cr.red_flags), rc._DIAG_CAP)
        for i, flag in enumerate(cr.red_flags):
            self.assertTrue(flag.startswith(f"row {i}: exact-class mismatch"), flag)

    def test_reconciliation_peak_is_flat_in_row_count(self):
        """The headline SAU-511 property, measured on both arms.

        Reconciliation's *own* allocation must not grow with row count. The input rows
        are built outside the tracemalloc window on purpose: what is under test is what
        reconciliation allocates given rows, not the cost of holding them (that is the
        extraction/read half, fixed by streaming in `run()`).

        Pre-SAU-511 both arms were linear — one canonical list per column per side, plus
        one dict + one formatted string per mismatching row. Measured on a 10-column
        table: 0.3 MB clean / 5.2 MB all-mismatch at 10k rows, rising to 1.3 MB / 26.2 MB
        at 50k. Now the digests are O(columns) and the sample is capped, so a 20x
        increase in rows must produce no meaningful increase in peak.
        """
        def peak(n: int, *, broken: bool) -> int:
            src = _rows(n, ["id"], broken=False)
            tgt = _rows(n, ["id"], broken=broken)
            ms = [_mapping("id")]
            tracemalloc.start()
            try:
                res = rc.reconcile_entity(_Ec(), ms, src, tgt, {"id": "text"})
                mark = tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
            self.assertEqual(res.columns[0].n_mismatches, n if broken else 0)
            return mark

        for broken in (False, True):
            small = peak(1_000, broken=broken)
            large = peak(20_000, broken=broken)
            arm = "all-mismatch" if broken else "clean"
            # 20x the rows. Linear behaviour would show ~20x here; flat behaviour shows
            # ~1x. A 2x allowance absorbs allocator noise at these small absolute sizes
            # while still failing decisively on any per-row retention.
            self.assertLess(large, small * 2,
                            f"{arm} arm still scales with rows: "
                            f"1k={small}B → 20k={large}B")

    def test_broken_and_clean_arms_cost_the_same_order(self):
        """The inverted failure mode itself.

        The defect was never "reconciliation uses memory" — it was that a *broken*
        mapping cost ~20x a clean one (30.5x measured on this 1-column fixture), so the
        run that had something to report died before reporting it. The two arms now
        differ only by the bounded 10-row sample: a constant, not a multiple of n.
        """
        def arms(n: int) -> tuple[int, int]:
            out = []
            for broken in (False, True):
                src = _rows(n, ["a"], broken=False)
                tgt = _rows(n, ["a"], broken=broken)
                tracemalloc.start()
                try:
                    rc.reconcile_entity(_Ec(), [_mapping("a")], src, tgt, {"a": "text"})
                    out.append(tracemalloc.get_traced_memory()[1])
                finally:
                    tracemalloc.stop()
            return out[0], out[1]

        clean_small, broken_small = arms(1_000)
        clean_large, broken_large = arms(20_000)
        # The gap between the arms is the capped sample. It must be a CONSTANT: growing
        # the input 20x must not grow the gap. Pre-fix the gap was ~linear in n.
        gap_small = broken_small - clean_small
        gap_large = broken_large - clean_large
        self.assertLess(gap_large, max(gap_small * 2, 4096),
                        f"broken/clean gap still scales: 1k={gap_small}B → "
                        f"20k={gap_large}B")


if __name__ == "__main__":
    unittest.main()
