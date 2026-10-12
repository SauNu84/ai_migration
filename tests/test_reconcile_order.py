#!/usr/bin/env python3
"""SAU-548 — the reconcile verdict must not depend on scan order.

An entity with no `id` has no ORDER BY on either walk, so the source and Gold can come
back in different orders while every value is intact. Before SAU-548 the per-column
checksum folded the row ordinal in, so that reorder read as a dirty verdict —
indistinguishable from corruption, and certain on Snowflake after a parallel `COPY INTO`.

Three properties, all DB-free (the DuckDB proxy runs in-process, the source is a stub):

  1. **Order-free.** A keyless entity stays clean across >= 5 runs with the source
     emitted in a different permutation each run AND the proxy forced to
     `preserve_insertion_order=false` with threads > 1 — including exact-duplicate rows.
  2. **Teeth.** The same permutations make the pre-SAU-548 ordered digest dirty, so (1)
     is not passing because nothing was reordered.
  3. **Detection kept.** A changed value is still caught on its column, and a value
     swapped between two rows (every column multiset intact) is caught as a row-alignment
     defect — the case a per-column multiset alone would miss.

Run:
    python3 -m unittest tests.test_reconcile_order -v
"""
from __future__ import annotations

import json
import random
import unittest
from pathlib import Path

from core import codegen as cg
from core import dialect as dl
from core import gate
from core import mapper as mp
from core import ontology as ont
from core import reconcile as rc
from core import target as tg

ROOT = Path(__file__).resolve().parent.parent
PROFILE = ROOT / "artifacts" / "profile.json"

N_ROWS = 3_000
N_DUP = 50          # rows repeated verbatim, like `metric_sample`
RUNS = 5


def _keyless_plan():
    """`plan` with its `id` dropped — the shape of a keyless volume entity."""
    profile = json.loads(PROFILE.read_text())
    onto = ont.lift(profile)
    maps = [m for m in mp.map_ontology(onto) if m.entity == "plan" and m.column != "id"]
    udt = {(e.name, p.name): (p.udt or "") for e in onto.entities for p in e.properties}
    return cg.generate(maps), maps, udt


def _rows() -> list[dict[str, str]]:
    base = [{
        "code": f"code-{i}",
        "currency": "usd",
        "features": json.dumps([f"f{i}", "shared"]),
        "monthly_price": f"{i}.99",
        "name": f"Plan {i}",
    } for i in range(N_ROWS)]
    return base + [dict(base[i]) for i in range(N_DUP)]


class _ShuffledDialect(dl.PostgresDialect):
    """Source stub that emits the same multiset in a seed-chosen permutation."""

    name = "shuffled-stub"

    def __init__(self, rows: list[dict[str, str]], seed: int) -> None:
        self.rows = list(rows)
        random.Random(seed).shuffle(self.rows)

    def iter_rows(self, entity, mappings):
        return iter(self.rows)

    def extract_rows(self, entity, mappings):
        return list(self.rows)


class _UnorderedDuckDb(tg.DuckDbAdapter):
    """The proxy with Snowflake-like ordering freedom: no insertion-order promise."""

    def connect(self):
        con = super().connect()
        con.execute("SET preserve_insertion_order=false")
        con.execute("SET threads=8")
        return con


class _TamperingDuckDb(_UnorderedDuckDb):
    """Rewrites Gold on the way out, to model a transform defect."""

    def __init__(self, tamper) -> None:
        self.tamper = tamper

    def iter_gold(self, handle, ec):
        return self.tamper(list(super().iter_gold(handle, ec)))


def _run(dialect, target):
    code, maps, udt = _keyless_plan()
    (res,) = rc.run(code, maps, udt, dialect=dialect, target=target)
    return res


class TestKeylessVerdictIsOrderFree(unittest.TestCase):
    def test_clean_across_permutations_with_unordered_parallel_proxy(self):
        rows = _rows()
        digests = set()
        for seed in range(RUNS):
            res = _run(_ShuffledDialect(rows, seed), _UnorderedDuckDb())
            clean, problems = gate._all_clean([res])
            self.assertTrue(clean, f"seed {seed}: {problems}")
            self.assertEqual(res.source_rows, N_ROWS + N_DUP)
            digests.add(tuple(c.source_checksum for c in res.columns))
        # Different permutations, one digest: the verdict is a function of the multiset.
        self.assertEqual(len(digests), 1)

    def test_the_permutations_would_have_broken_the_ordered_digest(self):
        """Teeth: without SAU-548 these same inputs read as dirty."""
        _, maps, udt = _keyless_plan()
        cols = sorted(m.column for m in maps)
        rows = _rows()
        a = _ShuffledDialect(rows, 0).rows
        b = _ShuffledDialect(rows, 1).rows
        for c in cols:
            if c == "currency":          # constant column: every order is the same list
                continue
            self.assertNotEqual(rc._checksum([r[c] for r in a]),
                                rc._checksum([r[c] for r in b]), c)

    def test_keyed_entity_keeps_the_ordered_digest(self):
        code, maps, udt = _keyless_plan()
        keyed = maps + [mp.Mapping(
            entity="plan", column="id", pg_type="integer", semantic_type="surrogate_key",
            snowflake_type="NUMBER(38,0)", reconcile=mp.RC_EXACT_TEXT, confidence=1.0,
            governance_tags=[], flags=[], notes="")]
        self.assertTrue(rc.EntityDigest(keyed, {}).ordered)
        self.assertFalse(rc.EntityDigest(maps, {}).ordered)


class TestKeylessDetectionIsKept(unittest.TestCase):
    def test_a_changed_value_is_caught_on_its_column(self):
        def corrupt(rows):
            rows[7] = {**rows[7], "monthly_price": rows[7]["monthly_price"] + 1}
            return rows
        res = _run(_ShuffledDialect(_rows(), 0), _TamperingDuckDb(corrupt))
        dirty = {c.column for c in res.columns if not c.match}
        self.assertEqual(dirty, {"monthly_price"})
        (flag,) = next(c for c in res.columns if c.column == "monthly_price").red_flags
        self.assertIn("keyless", flag)   # no fabricated row-paired examples

    def test_a_value_swapped_between_rows_is_a_row_alignment_defect(self):
        """Every column multiset is intact — only the row tuple multiset can see this."""
        def swap(rows):
            i, j = 3, 11
            rows[i], rows[j] = ({**rows[i], "name": rows[j]["name"]},
                                {**rows[j], "name": rows[i]["name"]})
            return rows
        res = _run(_ShuffledDialect(_rows(), 0), _TamperingDuckDb(swap))
        self.assertTrue(all(c.match for c in res.columns))
        self.assertFalse(res.row_alignment_match)
        clean, problems = gate._all_clean([res])
        self.assertFalse(clean)
        self.assertIn("values moved between rows", problems[0])

    def test_duplicates_are_counted_not_cancelled(self):
        """Both copies of a duplicated row become copies of another row, count unchanged.

        Source {X, X, Y} → Gold {Y, Y, Y}. An XOR multiset digest cancels the X pair and
        sees Y on both sides; the additive digest counts every occurrence.
        """
        rows = _rows()
        x, y = rows[0], rows[N_ROWS - 1]       # x is duplicated, y is not

        def overwrite(gold):                   # Gold rows are typed: match on `code`
            gy = next(r for r in gold if r["code"] == y["code"])
            return [dict(gy) if r["code"] == x["code"] else r for r in gold]
        res = _run(_ShuffledDialect(rows, 0), _TamperingDuckDb(overwrite))
        self.assertTrue(res.row_match)
        dirty = {c.column for c in res.columns if not c.match}
        self.assertEqual(dirty, {"code", "features", "monthly_price", "name"})

if __name__ == "__main__":
    unittest.main()
