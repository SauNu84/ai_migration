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
        res = _reconcile(5_000, ["a"], broken=True)
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
        res = _reconcile(500, ["a"], broken=True)
        cr = res.columns[0]
        expected = [{"row": i, "source": f"a-{i}", "target": f"a-{i}-x"}
                    for i in range(rc._DIAG_CAP)]
        self.assertEqual(cr.mismatches, expected)
        self.assertEqual(len(cr.red_flags), rc._DIAG_CAP)
        for i, flag in enumerate(cr.red_flags):
            self.assertTrue(flag.startswith(f"row {i}: exact-class mismatch"), flag)

    def test_a_broken_mapping_no_longer_costs_more_than_a_clean_one(self):
        """The inverted failure mode, measured.

        The defect was never "reconciliation uses memory" — it was that a *broken*
        mapping used ~20x what a clean one did (measured on a 10-column table: 1.3 MB
        clean vs 26.2 MB all-mismatch at 50k rows), so the run that had something to
        report died before reporting it.

        The comparison is therefore broken-vs-clean at the SAME row count, which
        isolates the diagnostic term. The rows and canon lists are a linear cost common
        to both arms — that is the streaming half of SAU-511 and is deliberately NOT
        what this test measures.
        """
        def peak(n: int, *, broken: bool) -> int:
            src = _rows(n, ["a"], broken=False)
            tgt = _rows(n, ["a"], broken=broken)
            ms = [_mapping("a")]
            tracemalloc.start()
            try:
                res = rc.reconcile_entity(_Ec(), ms, src, tgt, {"a": "text"})
                mark = tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
            self.assertEqual(res.columns[0].n_mismatches, n if broken else 0)
            return mark

        n = 20_000
        clean, broken = peak(n, broken=False), peak(n, broken=True)
        # Post-fix the two arms differ only by the bounded 10-row sample, so the broken
        # arm must stay within a small constant factor of the clean one. Pre-fix this
        # ratio was ~20x and grew with n.
        self.assertLess(broken, clean * 2,
                        f"broken run still costs more than clean: {clean} → {broken} bytes")


if __name__ == "__main__":
    unittest.main()
