"""Corpus-validity audit — §8 gate bullet 0 (SAU-513).

Bullets 1–4 all validate *the transform*. None of them validates *the extraction*.
That gap is not hypothetical; it has been hit twice, in two dialects:

  • SAU-503 — an Oracle INTERVAL literal silently rounded at INSERT
    ('.123456789' → '.123457000'). The corpus still looks populated, so the trap was
    never really planted and the gate still goes GREEN.
  • SAU-499 — Oracle collapses '' → NULL. `reconcile._red_flag` red-flags loss as
    "source not-null, target null", comparing source-as-read to target-as-read. A row
    seeded as '' is already NULL *before* the chassis ever sees it, so both sides agree
    and the gate goes GREEN while testing nothing.

One defect, stated generally: **anything the source engine normalizes upstream of
extraction is invisible to every downstream check.** Reconciliation cannot see it,
because reconciliation's two anchors — source-as-read and target-as-read — are both
*downstream* of the normalization. Comparing them is a tautology with respect to it.

Bullet 0 breaks the tautology by introducing an anchor the engine never touched: the
**seed SQL text itself**. We parse the literal each INSERT claims to plant, read the
column back through the very same extraction path the chassis uses, and assert the two
agree. Claimed-vs-stored is an independent comparison; source-vs-target is not.

Note what is deliberately *not* done here: the expected values are not transcribed into
the manifest by hand. A hand-written expectation is an answer key — it records what
someone believed the engine stored, which is the same belief bullet 0 exists to test
(the SAU-505 lesson). The seed file is the only artifact that states the claim
independently of the engine, so the seed file is the anchor.

SAU-516 adds the two things bullet 0 needed to be *accurate* as well as strict, both of
them asked of the dialect so this module never learns a type name:

  • **Named type equivalences.** `inet '203.0.113.42'` stores as `203.0.113.42/32` —
    the input grammar's abbreviation expanded to the type's canonical output form,
    equal under the type's own equality operator, round-trip stable, nothing lost.
    Filing that beside SAU-503's irreversible rounding costs the real findings their
    credibility, and a bullet that cries wolf gets routed around. Each equivalence is
    named, reported with both raw forms, and passes the test that separates it from
    editing the seed: *would the rule have been written the same way before we saw this
    failure?*

  • **A third state, `not-seed-declarable`.** Some columns cannot be seeded at all —
    ROWVERSION is written by the engine, a computed column is derived — so the seed
    states no claim to check. Permanent RED for a structural reason trains the team to
    route around the gate; a hand-written exemption list is an answer key by another
    name. The discrimination comes from the *catalog* instead (engine-generated is a
    property of the type), so it cannot be widened by editing a list, and it is counted
    separately — never folded into the verified total.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp

from . import reconcile as rc
from . import dialect as dl


# ── The claim side: what the seed SQL says it planted ───────────────────────

def _json_element(e: exp.Expression) -> str:
    """One array element as JSON text, preserving numeric literals unquoted and to scale."""
    if isinstance(e, exp.Null):
        return "null"
    if isinstance(e, exp.Boolean):
        return "true" if e.this else "false"
    if isinstance(e, (exp.Cast, exp.Paren)):
        return _json_element(e.this)
    if isinstance(e, exp.Neg):
        return f"-{_json_element(e.this)}"
    if isinstance(e, exp.Literal) and not e.is_string:
        return str(e.this)                          # 10.00 stays 10.00, not 10.0
    return json.dumps(_literal_text(e), ensure_ascii=False)


def _pg_array_literal(s: str) -> str:
    """Convert a Postgres array literal ('{a,b}', '{}') to its JSON element text.

    The seed writes empty arrays as the string form `'{}'` and populated ones as
    `ARRAY[…]`; extraction renders both as JSON. Quoting/escaping beyond the simple
    unquoted case raises rather than guessing — a literal this cannot read must not be
    scored as agreement.
    """
    body = s.strip()
    if not (body.startswith("{") and body.endswith("}")):
        raise ValueError(f"not a Postgres array literal: {s!r}")
    body = body[1:-1].strip()
    if not body:
        return "[]"
    if '"' in body or "\\" in body:
        raise ValueError(f"quoted/escaped array literal not supported: {s!r}")
    parts = [p.strip() for p in body.split(",")]
    return json.dumps([None if p.upper() == "NULL" else p for p in parts],
                      separators=(",", ":"), ensure_ascii=False)


def _literal_text(e: exp.Expression) -> str | None:
    """Render a seed literal as the text the source is expected to hold.

    Mirrors the *wire* form the extraction produces, not SQL syntax: quotes and type
    prefixes are structural, so `TIMESTAMPTZ '2024-03-01 08:15:00+00'` claims the value
    `2024-03-01 08:15:00+00`. Anything not recognised raises, so an unparsed literal can
    never be silently scored as agreement.
    """
    if isinstance(e, exp.Null):
        return None
    if isinstance(e, exp.Boolean):
        return "true" if e.this else "false"
    if isinstance(e, (exp.Cast, exp.Paren)):        # TIMESTAMPTZ '…', DATE '…', (…)
        return _literal_text(e.this)
    if isinstance(e, exp.Neg):
        inner = _literal_text(e.this)
        return None if inner is None else f"-{inner}"
    if isinstance(e, exp.Array):                    # ARRAY['a','b'] → JSON element text
        return "[" + ",".join(_json_element(x) for x in e.expressions) + "]"
    if isinstance(e, exp.National):                 # T-SQL N'…'
        return e.this.this if isinstance(e.this, exp.Expression) else str(e.this)
    if isinstance(e, exp.Literal):
        return e.this if e.is_string else str(e.this)
    raise ValueError(f"unrecognised seed literal: {type(e).__name__} — {e.sql()}")


def claimed_text(e: exp.Expression, reconcile: str) -> str | None:
    """The seed's claim, rendered for the column's declared comparison strategy."""
    from . import mapper as mp
    text = _literal_text(e)
    if reconcile == mp.RC_ARRAY_CANONICAL and text is not None and not text.startswith("["):
        return _pg_array_literal(text)              # '{}' / '{a,b}' string form
    return text


#: A row whose INSERT omitted the column, so it took a DEFAULT. The seed makes no claim
#: about it, which weakens the comparison — the audit says so rather than hiding it.
_MISSING = object()


#: T-SQL batch separator. It is a client directive, not a statement, and sqlglot cannot
#: parse it — left in place it swallows whole INSERTs into opaque `Command` nodes.
_GO = re.compile(r"^\s*GO\s*;?\s*$", re.IGNORECASE | re.MULTILINE)


def _parse_seed(text: str, *, read: str) -> list[exp.Expression]:
    """Parse a seed file into statements, batch separators removed.

    An INSERT that sqlglot could not parse is raised, never skipped. Skipping it would
    make the audit report "no INSERT names this column" — a *corpus* verdict — when the
    truth is that the reader failed. Misattributing a reader bug to the corpus is the
    same false-confidence this bullet exists to remove.
    """
    stmts = []
    # sqlglot logs a warning whenever it falls back to `Command` — expected here for the
    # seed's non-DML directives (SET TIME ZONE, SET IDENTITY_INSERT). Quieted so it does
    # not drown the test output; an unparsed *INSERT* is still raised below, so nothing
    # that could affect a verdict is being hidden.
    log = logging.getLogger("sqlglot")
    prior = log.level
    log.setLevel(logging.ERROR)
    try:
        for batch in _GO.split(text):
            if batch.strip():
                stmts.extend(sqlglot.parse(batch, read=read))
    finally:
        log.setLevel(prior)
    for s in stmts:
        if isinstance(s, exp.Command) and str(s.this).strip().upper().startswith("INSERT"):
            raise ValueError(
                f"seed INSERT could not be parsed under dialect {read!r}: "
                f"{s.sql()[:120]}…")
    return stmts


def seed_claims(seed_path: str | Path, *, read: str) -> dict[tuple[str, str], list[Any]]:
    """Parse a seed file into {(table, column): [claimed literal per seeded row]}.

    Every seeded row contributes exactly one entry per column of its table, so the
    claimed list and the read-back list are directly comparable as multisets. Rows
    inserted by a statement that did not name the column contribute `_MISSING`.
    """
    per_table: dict[str, list[tuple[list[str], list[exp.Expression]]]] = {}
    for stmt in _parse_seed(Path(seed_path).read_text(), read=read):
        if not isinstance(stmt, exp.Insert):
            continue
        schema = stmt.this
        if not isinstance(schema, exp.Schema):      # INSERT without an explicit col list
            continue
        if not isinstance(stmt.expression, exp.Values):   # INSERT … SELECT: no literals
            continue
        table = schema.this.name                    # strips any dbo./public. qualifier
        cols = [c.name for c in schema.expressions]
        for row in stmt.expression.expressions:
            per_table.setdefault(table, []).append((cols, list(row.expressions)))

    claims: dict[tuple[str, str], list[Any]] = {}
    for table, rows in per_table.items():
        for col in {c for cols, _ in rows for c in cols}:
            claims[(table, col)] = [
                vals[cols.index(col)] if col in cols and cols.index(col) < len(vals)
                else _MISSING
                for cols, vals in rows
            ]
    return claims


# ── The stored side, and the comparison ─────────────────────────────────────

#: The three verdicts a column can carry (SAU-516). `NOT_DECLARABLE` is a third state,
#: not a soft pass: the type forbids an INSERT from stating a claim, so bullet 0 has
#: nothing of its own to verify and says so. It is counted separately and NEVER folded
#: into the verified total — a readout that says 20/20 when one was exempt is lying, and
#: an honest three-column count is worth more than a clean two-column one.
V_VERIFIED = "verified"
V_DRIFTED = "drifted"
V_NOT_DECLARABLE = "not-seed-declarable"


@dataclass
class ColumnAudit:
    entity: str
    column: str
    reconcile: str
    comparison: str = "multiset"
    claimed: list[str | None] = field(default_factory=list)
    stored: list[str | None] = field(default_factory=list)
    missing: list[str | None] = field(default_factory=list)   # claimed, never stored
    unclaimed: list[str | None] = field(default_factory=list)  # stored, never claimed
    #: pairs the dialect reconciled under a named type equivalence, each carrying BOTH
    #: raw forms and the rule's name — see `_apply_equivalences`.
    equivalences: list[dict[str, str | None]] = field(default_factory=list)
    #: engine-generated: the type forbids an INSERT naming it, so no claim exists.
    not_declarable: str | None = None
    error: str | None = None

    @property
    def verdict(self) -> str:
        if self.not_declarable is not None:
            return V_NOT_DECLARABLE
        if self.missing or self.unclaimed or self.error is not None:
            return V_DRIFTED
        return V_VERIFIED

    @property
    def ok(self) -> bool:
        """Not a *failure*. NB: `not-seed-declarable` is also not a pass — anything
        counting outcomes must branch on `verdict`, which is why it is three-valued."""
        return self.verdict != V_DRIFTED

    def to_json(self) -> dict[str, Any]:
        return {
            "column": f"{self.entity}.{self.column}", "reconcile": self.reconcile,
            "comparison": self.comparison, "seeded_rows": len(self.claimed),
            "verdict": self.verdict, "ok": self.ok, "error": self.error,
            "not_seed_declarable": self.not_declarable,
            "claimed_but_not_stored": self.missing[:5],
            "stored_but_not_claimed": self.unclaimed[:5],
            "reconciled_by_equivalence": self.equivalences[:5],
        }


def _canon(reconcile: str, v: Any, *, source_udt: str,
           dialect: "dl.SourceDialect") -> str | None:
    """Canonicalize for the audit — reusing reconcile's strategies, except for structs.

    The strategy is taken from the column's declared `reconcile` class on purpose: that
    class is the fidelity the design *claims*, and asserting more than it claims would
    be a false alarm (`approx_float` columns are seeded with values the engine is
    documented to round).

    The one deliberate exception is JSON/ARRAY. `reconcile._canon_json` parses through
    Python floats, so `10.00` becomes `10.0` — it silently drops the scale that
    `RC_ARRAY_CANONICAL` ("ordered element equality, scale preserved") promises to keep.
    Reusing it here would make bullet 0 blind to exactly the precision loss it exists to
    catch, so structs are parsed with Decimal instead. Both sides get the identical
    treatment, so the comparison stays symmetric.
    """
    from . import mapper as mp
    if reconcile in (mp.RC_JSON_CANONICAL, mp.RC_ARRAY_CANONICAL):
        if v is None:
            return None
        return _dump_struct(json.loads(v, parse_float=Decimal, parse_int=Decimal))
    return rc.canonicalize(reconcile, v, source_udt=source_udt, dialect=dialect)


def _dump_struct(o: Any) -> str:
    """Serialize a parsed struct, keeping numeric scale *and* the number/string split.

    Decimals are emitted unquoted, so a numeric `10.00` and the string `"10.00"` do not
    collapse to the same canonical form — a type change inside an array is drift too.
    """
    if isinstance(o, dict):
        return "{" + ",".join(f"{json.dumps(k, ensure_ascii=False)}:{_dump_struct(v)}"
                              for k, v in sorted(o.items())) + "}"
    if isinstance(o, list):
        return "[" + ",".join(_dump_struct(x) for x in o) + "]"
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, bool) or o is None:
        return json.dumps(o)
    return json.dumps(o, ensure_ascii=False)


def _apply_equivalences(missing: list[Any], unclaimed: list[Any], *,
                        dialect: "dl.SourceDialect", source_udt: str, data_type: str
                        ) -> tuple[list[Any], list[Any], list[dict[str, Any]]]:
    """Pair off residuals the dialect reconciles under a named type equivalence.

    Applied only to what the exact multiset comparison left over, so an equivalence can
    never quietly relax a comparison that already agreed — it is consulted precisely
    where there is a discrepancy, and each application is recorded with both raw forms
    and the rule's name so the readout can print them (SAU-516, decision 1).

    The audit itself stays type-agnostic: it knows *that* a dialect may declare
    equivalences, never *which*. That separation is the whole point — a comparator that
    learns type names is the answer key bullet 0 exists to detect.
    """
    left, pool = list(missing), list(unclaimed)
    applied: list[dict[str, Any]] = []
    kept: list[Any] = []
    for c in left:
        for i, s in enumerate(pool):
            rule = dialect.claim_equivalence(c, s, source_udt=source_udt,
                                             data_type=data_type)
            if rule:
                applied.append({"claimed": c, "stored": s, "rule": rule})
                pool.pop(i)
                break
        else:
            kept.append(c)
    return kept, pool, applied


def _multiset_diff(a: list[Any], b: list[Any]) -> list[Any]:
    """Elements of `a` not covered by `b`, counting multiplicity."""
    pool = list(b)
    out = []
    for x in a:
        if x in pool:
            pool.remove(x)
        else:
            out.append(x)
    return out


def audit_corpus(hard: list[dict[str, Any]], seed_path: str | Path,
                 mappings: list[Any], udt_index: dict[tuple[str, str], str],
                 *, dialect: "dl.SourceDialect | None" = None,
                 read: str | None = None,
                 col_facts: dict[tuple[str, str], dict[str, Any]] | None = None,
                 ) -> list[ColumnAudit]:
    """Read every hard column back from the source and audit it against the seed.

    Returns one `ColumnAudit` per manifest entry. A column the seed never names is an
    audit *failure*, not a skip: the manifest claims a trap is planted there, and
    nothing planting it is exactly the SAU-503 shape. The single exception is a column
    the *type* forbids an INSERT from naming — see the third state below.

    `col_facts` carries the profile's catalog facts per column (`data_type`, `udt`,
    `is_identity`, `is_computed`, `has_default`); the dialect reads them to answer the
    two type-specific questions this audit must not answer itself. Omitting it is safe
    and fails closed: every column is then treated as seed-declarable with no
    equivalences, i.e. the strictest reading.
    """
    dialect = dialect or dl.DEFAULT_DIALECT
    read = read or dialect.sql_read
    if not read:
        raise ValueError(
            f"dialect {dialect.name!r} declares no `sql_read`, so its seed cannot be "
            f"parsed — the corpus audit must not guess a grammar")
    claims = seed_claims(seed_path, read=read)
    by_col = {(m.entity, m.column): m for m in mappings}
    wanted: dict[str, list[Any]] = {}
    for h in hard:
        m = by_col.get((h["table"], h["column"]))
        if m is not None:
            wanted.setdefault(h["table"], []).append(m)

    rows_by_entity: dict[str, Any] = {}
    for entity, ms in wanted.items():
        try:
            rows_by_entity[entity] = dialect.extract_rows(entity, ms)
        except Exception as exc:                     # noqa: BLE001 — reported, not raised
            rows_by_entity[entity] = exc

    audits: list[ColumnAudit] = []
    for h in hard:
        key = (h["table"], h["column"])
        m = by_col.get(key)
        a = ColumnAudit(entity=key[0], column=key[1],
                        reconcile=getattr(m, "reconcile", "—"))
        rows = rows_by_entity.get(key[0])
        if m is None:
            a.error = "column absent from the lifted mappings"
        elif isinstance(rows, Exception):
            a.error = f"source read failed: {rows}"
        elif key not in claims:
            # The seed makes no claim about this column. Two very different causes look
            # identical from the seed alone — the trap was never planted, or the *type*
            # forbids planting it (ROWVERSION, computed) — and the seed cannot tell them
            # apart. The catalog can: being engine-generated is a property of the type,
            # so the question goes to the dialect (SAU-516, decision 2). Unknown facts
            # fail closed, so a normal column the seed forgot is still a failure.
            seeded = any(t == key[0] for t, _ in claims)
            facts = (col_facts or {}).get(key, {})
            if seeded and not dialect.is_seed_declarable(facts):
                a.not_declarable = (
                    f"engine-generated ({facts.get('data_type') or 'unknown type'}): no "
                    f"INSERT can name this column, so the seed states no claim for "
                    f"bullet 0 to verify. Counted separately — NOT as a pass. The column "
                    f"is still reconciled by bullets 1–4."
                )
            else:
                a.error = ("no INSERT names this column, so the seed claims nothing to verify"
                           if seeded else
                           f"the seed contains no parsed INSERT for table {key[0]!r}")
        else:
            udt = udt_index.get(key, "")
            raw = claims[key]
            defaulted = sum(1 for v in raw if v is _MISSING)
            if defaulted:
                # A DEFAULT-ed row makes the count non-comparable; assert containment
                # only, and say so — a weakened check that announces itself.
                a.comparison = f"subset ({defaulted} row(s) took a DEFAULT)"
            try:
                a.claimed = [_canon(m.reconcile, claimed_text(v, m.reconcile),
                                    source_udt=udt, dialect=dialect)
                             for v in raw if v is not _MISSING]
                a.stored = [_canon(m.reconcile, r.get(key[1]),
                                   source_udt=udt, dialect=dialect)
                            for r in rows]
            except Exception as exc:                 # noqa: BLE001
                a.error = f"canonicalization failed: {type(exc).__name__}: {exc}"
            else:
                # Residuals first, then the dialect's named equivalences on what is
                # left. `unclaimed` is still only *reported* when no row defaulted (a
                # DEFAULT makes the counts non-comparable), but it is always computed —
                # a column can carry both a defaulted row and an equivalence, and the
                # pairing needs the stored side either way.
                missing = _multiset_diff(a.claimed, a.stored)
                unclaimed = _multiset_diff(a.stored, a.claimed)
                missing, unclaimed, a.equivalences = _apply_equivalences(
                    missing, unclaimed, dialect=dialect, source_udt=udt,
                    data_type=(col_facts or {}).get(key, {}).get("data_type", ""))
                a.missing = missing
                if not defaulted:
                    a.unclaimed = unclaimed
        audits.append(a)
    return audits
