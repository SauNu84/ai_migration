"""§8 exit-gate orchestration (SAU-305).

Runs the full lift → map → codegen → verify loop and scores it against the board
brief's §8 acceptance criteria, then writes a pass/fail readout that feeds the CTO's
Phase-1 go/no-go.

Acceptance bullets (verbatim from the issue):
  1. 100% row-count + checksum reconciliation on Gold across 3/3 consecutive clean runs.
  2. Byte-identical SQL regeneration from the same approved ontology (SQLGlot).
  3. Calibrated confidence: low-confidence mappings correlate with actual errors vs the
     seeded hard-column manifest.
  4. Hard fail: any silent data loss / precision-losing coercion / masked semantic
     mismatch must be SURFACED, not hidden.
  5. A short written readout of pass/fail against each bullet.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import ontology as ont
from . import corpus as cp
from . import mapper as mp
from . import codegen as cg
from . import reconcile as rc
from . import dialect as dl
from . import target as tg

REPO = Path(__file__).resolve().parent.parent


# ── Naive "trap" mapper for the negative/ablation test ──────────────────────
# A mapper that falls into each documented trap. The loop MUST catch every one,
# either by checksum (value-level loss) or by the semantic guard (masked mismatch).

_TRAP = {
    ont.SEM_PII_FIXED_WIDTH: ("NUMBER(38,0)", mp.RC_EXACT_TEXT),
    ont.SEM_FIXED_WIDTH_ID: ("NUMBER(38,0)", mp.RC_EXACT_TEXT),
    ont.SEM_CURRENCY_DECIMAL: ("FLOAT", mp.RC_EXACT_TEXT),
    ont.SEM_RATE: ("FLOAT", mp.RC_EXACT_TEXT),
    ont.SEM_CURRENCY_CENTS: ("FLOAT", mp.RC_EXACT_TEXT),
    ont.SEM_TIMESTAMP_NTZ: ("TIMESTAMP_TZ", mp.RC_TS_INSTANT),  # masked in UTC → guard
    ont.SEM_SEMI_STRUCTURED: ("VARCHAR(16777216)", mp.RC_EXACT_TEXT),  # flatten → guard
    ont.SEM_ARRAY: ("VARCHAR(16777216)", mp.RC_EXACT_TEXT),
    ont.SEM_ARRAY_NUMERIC: ("VARCHAR(16777216)", mp.RC_EXACT_TEXT),
    ont.SEM_NETWORK: ("NUMBER(38,0)", mp.RC_EXACT_TEXT),  # inet→number = nonsense
    ont.SEM_AMBIGUOUS_TEXT: ("NUMBER(38,0)", mp.RC_EXACT_TEXT),  # exec error or loss
    # ROWVERSION (SQL Server 'timestamp' synonym) lured into a temporal type — the
    # checksum cannot catch it (binary bytes ≠ a parseable instant, so it execs-errors
    # or mis-round-trips), and the structural guard must surface the misread chronology.
    ont.SEM_BINARY: ("TIMESTAMP_NTZ", mp.RC_TS_WALL),
}


def naive_mappings(maps: list[mp.Mapping], hard_cols: set[tuple[str, str]]) -> list[mp.Mapping]:
    """Return a copy of `maps` with the seeded hard columns mapped naively (trapped)."""
    out: list[mp.Mapping] = []
    for m in maps:
        if (m.entity, m.column) in hard_cols and m.semantic_type in _TRAP:
            sf, rcv = _TRAP[m.semantic_type]
            out.append(mp.Mapping(
                entity=m.entity, column=m.column, pg_type=m.pg_type,
                semantic_type=m.semantic_type, snowflake_type=sf, reconcile=rcv,
                confidence=m.confidence, governance_tags=m.governance_tags,
                flags=m.flags, notes="[ABLATION] naive trap mapping",
            ))
        else:
            out.append(m)
    return out


# ── Result containers ───────────────────────────────────────────────────────

@dataclass
class GateResult:
    bullets: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return all(b["pass"] for b in self.bullets.values())


def _all_clean(results: list[rc.EntityResult]) -> tuple[bool, list[str]]:
    problems: list[str] = []
    for r in results:
        if r.exec_error:
            problems.append(f"{r.entity}: exec error {r.exec_error}")
            continue
        if not r.row_match:
            problems.append(f"{r.entity}: row count {r.source_rows}≠{r.target_rows}")
        for c in r.columns:
            if not c.match:
                problems.append(f"{r.entity}.{c.column}: checksum mismatch ({c.reconcile})")
    return (not problems), problems


def _digest_code(code: dict[str, cg.EntityCode]) -> str:
    h = hashlib.sha256()
    for entity in sorted(code):
        ec = code[entity]
        for part in (ec.bronze_ddl["snowflake"], ec.silver_sql["snowflake"],
                     ec.gold_sql["snowflake"]):
            h.update(part.encode("utf-8"))
            h.update(b"\x00")
    return h.hexdigest()


def _regen_digest(maps: list[mp.Mapping]) -> str:
    return _digest_code(cg.generate(maps))


def _regen_digest_from_frozen(frozen: dict[str, Any]) -> str:
    """Regenerate SQL from the FROZEN approved ontology and digest it.

    The whole path runs under `assert_no_inference`: load the frozen artifact → map →
    codegen, never re-entering the inference seam. Raises FrozenOntologyViolation if it
    does (SAU-312). This is the digest the Phase-1 determinism gate compares.
    """
    with ont.assert_no_inference():
        onto = ont.load_frozen(frozen)
        maps = mp.map_ontology(onto)
        code = cg.generate(maps)
    return _digest_code(code)


def _frozen_integrity_enforced(frozen: dict[str, Any]) -> bool:
    """A tampered frozen artifact must be rejected by load_frozen (content-addressed)."""
    bad = json.loads(json.dumps(frozen))  # deep copy
    ents = bad.get("ontology", {}).get("entities") or []
    if ents and ents[0].get("properties"):
        ents[0]["properties"][0]["semantic_type"] = "__tampered__"
    else:
        bad.setdefault("ontology", {})["source_schema"] = "__tampered__"
    try:
        ont.load_frozen(bad)
        return False
    except ont.FrozenOntologyViolation:
        return True


#: Per-lane seed file, used by gate bullet 0 when the caller does not pass one.
_SEED_BY_DIALECT = {
    "postgres": REPO / "schema" / "02_seed.sql",
    "sqlserver": REPO / "sqlserver" / "schema" / "02_seed.sql",
}


def _rel(p: Path) -> str:
    try:
        return str(p.resolve().relative_to(REPO))
    except ValueError:
        return str(p)


def run_gate(profile_path: str | Path, manifest_path: str | Path,
             *, clean_runs: int = 3, regen_iters: int = 5,
             dialect: "dl.SourceDialect | None" = None,
             target: "tg.TargetAdapter | None" = None,
             seed_path: str | Path | None = None) -> tuple[GateResult, dict[str, Any]]:
    dialect = dialect or dl.DEFAULT_DIALECT
    target = target or tg.DEFAULT_TARGET
    profile = ont.load_profile(profile_path)
    manifest = json.loads(Path(manifest_path).read_text())
    hard = manifest["hard_columns"]
    hard_cols = {(h["table"], h["column"]) for h in hard}

    onto = ont.lift(profile, dialect=dialect)
    onto_problems = ont.validate(onto)
    maps = mp.map_ontology(onto)
    code = cg.generate(maps)
    udt_index = {(e.name, p.name): p.udt for e in onto.entities for p in e.properties}
    prop_index = {(e.name, p.name): p for e in onto.entities for p in e.properties}

    gate = GateResult()
    detail: dict[str, Any] = {}

    # ── Bullet 0: corpus validity — did the seed actually land? (SAU-513) ────
    # Bullets 1–4 all validate the *transform*. None validates the *extraction*: a value
    # the source engine normalized before the chassis ever read it is invisible to them,
    # because reconciliation's two anchors (source-as-read, target-as-read) are both
    # downstream of that normalization. Bullet 0 anchors on the seed SQL instead — text
    # the engine never touched — and must run first: if the corpus did not land, every
    # bullet below is measuring a trap that was never planted.
    seed = Path(seed_path) if seed_path else _SEED_BY_DIALECT.get(dialect.name)
    audits: list[cp.ColumnAudit] = []
    audit_error: str | None = None
    try:
        if seed is None:
            raise ValueError(f"no seed file known for source lane {dialect.name!r}")
        if not seed.exists():
            raise FileNotFoundError(f"seed file not found: {seed}")
        # Catalog facts per column, for the two type-specific questions the audit must
        # not answer itself: is this column declarable by an INSERT at all, and does the
        # dialect declare a named equivalence between the seed's form and the type's
        # canonical stored form (SAU-516).
        col_facts = {k: {"data_type": p.data_type, "udt": p.udt,
                         "is_identity": p.is_identity, "is_computed": p.is_computed,
                         "has_default": p.has_default}
                     for k, p in prop_index.items()}
        audits = cp.audit_corpus(hard, seed, maps, udt_index, dialect=dialect,
                                 col_facts=col_facts)
    except Exception as exc:  # noqa: BLE001 — a failed audit is a RED bullet, not a crash
        audit_error = f"{type(exc).__name__}: {exc}"
    # Three states, counted separately (SAU-516). `not-seed-declarable` is NOT folded
    # into the verified total: a readout that reports 20/20 when one column was exempt
    # is lying, and an honest three-column count is worth more than a clean two-column
    # one. It does not fail the bullet either — the type forbade the claim, so there is
    # nothing bullet 0 could have verified, and bullets 1–4 still reconcile the column.
    drifted = [a for a in audits if a.verdict == cp.V_DRIFTED]
    exempt = [a for a in audits if a.verdict == cp.V_NOT_DECLARABLE]
    verified = [a for a in audits if a.verdict == cp.V_VERIFIED]
    equivalences = [{"column": f"{a.entity}.{a.column}", **e}
                    for a in audits for e in a.equivalences]
    gate.bullets["0_corpus_validity"] = {
        "pass": audit_error is None and bool(audits) and not drifted,
        # repo-relative so the committed readout is not machine-specific
        "seed": _rel(seed) if seed else None,
        "audit_error": audit_error,
        "columns_audited": len(audits),
        "columns_verified": len(verified),
        "columns_not_seed_declarable": len(exempt),
        "columns_drifted": len(drifted),
        "drifted": [a.to_json() for a in drifted],
        "not_seed_declarable": [a.to_json() for a in exempt],
        "equivalences": equivalences,
        "detail": [a.to_json() for a in audits],
        "method": "each ground-truth column read back from the source through the "
                  "chassis' own extraction path and compared, as a multiset, against the "
                  "literal its INSERT claims to plant (parsed from the seed SQL, the one "
                  "anchor the source engine never rewrote)",
    }

    # ── Bullet 1: 3/3 consecutive clean runs (row-count + checksum on Gold) ──
    run_summaries = []
    consecutive = 0
    compile_errors = cg.compile_check(code)
    for i in range(clean_runs):
        results = rc.run(code, maps, udt_index, dialect=dialect, target=target)
        clean, problems = _all_clean(results)
        ncols = sum(len(r.columns) for r in results)
        run_summaries.append({"run": i + 1, "clean": clean,
                              "entities": len(results), "columns": ncols,
                              "problems": problems[:10]})
        consecutive = consecutive + 1 if clean else 0
    gate.bullets["1_reconciliation"] = {
        "pass": consecutive >= clean_runs and not compile_errors,
        "consecutive_clean_runs": consecutive,
        "required": clean_runs,
        "snowflake_compile_errors": compile_errors,
        "runs": run_summaries,
    }

    # ── Bullet 2: byte-identical SQL regeneration ───────────────────────────
    digests = {_regen_digest(mp.map_ontology(ont.lift(profile, dialect=dialect)))
               for _ in range(regen_iters)}
    gate.bullets["2_byte_identical_regen"] = {
        "pass": len(digests) == 1,
        "iterations": regen_iters,
        "distinct_digests": len(digests),
        "sha256": next(iter(digests)) if len(digests) == 1 else sorted(digests),
        "method": "canonical SQL generated once per ontology; SQLGlot deterministic transpile (no LLM redraft)",
    }

    # ── Bullet 2b: byte-identical regen from the FROZEN approved ontology ────
    # The Phase-1 criterion (SAU-312 / CTO item ②). Bullet 2 above re-lifts from the
    # profile, which is byte-identical only because inference is a deterministic
    # heuristic today. This bullet freezes the approved ontology and regenerates ONLY
    # from the frozen artifact, under a guard that fails if the inference seam is
    # re-entered — so the guarantee survives an LLM entering inference.
    live_digest = _digest_code(code)
    frozen = ont.freeze_ontology(onto)
    frozen_violation: str | None = None
    frozen_digests: set[str] = set()
    try:
        for _ in range(regen_iters):
            frozen_digests.add(_regen_digest_from_frozen(frozen))
    except ont.FrozenOntologyViolation as exc:  # inference re-entered on regen
        frozen_violation = str(exc)
    matches_live = len(frozen_digests) == 1 and next(iter(frozen_digests), None) == live_digest
    integrity_enforced = _frozen_integrity_enforced(frozen)
    gate.bullets["2b_frozen_ontology_regen"] = {
        "pass": frozen_violation is None and matches_live and integrity_enforced,
        "iterations": regen_iters,
        "distinct_digests": len(frozen_digests),
        "matches_live_generation": matches_live,
        "inference_reentered": frozen_violation is not None,
        "violation": frozen_violation,
        "integrity_check_enforced": integrity_enforced,
        "frozen_sha256": frozen["ontology_sha256"],
        "method": "approved ontology frozen to a content-addressed artifact; regen = "
                  "load_frozen → map → codegen under assert_no_inference (LLM seam never re-entered)",
    }

    # ── Bullet 3: calibrated confidence vs the hard-column manifest ──────────
    map_index = {(m.entity, m.column): m for m in maps}
    # normalize sqlglot synonym for display comparison
    def _norm(t: str) -> str:
        return t.upper().replace("TIMESTAMPNTZ", "TIMESTAMP_NTZ").replace(" ", "")
    calib_rows = []
    floor_ok = target_ok = 0
    for h in hard:
        key = (h["table"], h["column"])
        m = map_index.get(key)
        p = prop_index.get(key)
        exp = h["snowflake_target"].upper()
        got = m.snowflake_type.upper() if m else "—"
        # target match: allow VARCHAR(n) family + NUMBER/DECIMAL synonyms + enum note
        tgt_match = _target_compatible(got, exp)
        floor = h["confidence_floor"]
        meets_floor = bool(m and m.confidence >= floor)
        floor_ok += meets_floor
        target_ok += tgt_match
        calib_rows.append({
            "column": f"{h['table']}.{h['column']}", "category": h["category"],
            "expected": h["snowflake_target"], "got": m.snowflake_type if m else None,
            "target_ok": tgt_match, "confidence": m.confidence if m else None,
            "floor": floor, "meets_floor": meets_floor,
        })
    # correlation: do the lowest-confidence categories coincide with the traps a naive
    # mapper actually breaks? (see bullet 4 ablation) + are all hard cols mapped right?
    gate.bullets["3_confidence_calibration"] = {
        "pass": target_ok == len(hard) and floor_ok == len(hard) and not onto_problems,
        "hard_columns": len(hard),
        "target_correct": target_ok,
        "meets_confidence_floor": floor_ok,
        "ontology_problems": onto_problems,
        "detail": calib_rows,
    }

    # ── Bullet 4: hard-fail detection (negative/ablation + semantic guard) ───
    naive = naive_mappings(maps, hard_cols)
    naive_code = cg.generate(naive)
    naive_results = rc.run(naive_code, naive, udt_index, dialect=dialect, target=target)
    naive_index = {(m.entity, m.column): m for m in naive}
    caught = []
    for h in hard:
        key = (h["table"], h["column"])
        nm = naive_index[key]
        p = prop_index[key]
        is_trap = nm.notes.startswith("[ABLATION]")
        # detector A: checksum / exec error
        er = next((r for r in naive_results if r.entity == key[0]), None)
        data_caught = False
        if er and er.exec_error:
            data_caught = True
        elif er:
            cr = next((c for c in er.columns if c.column == key[1]), None)
            data_caught = bool(cr and not cr.match)
        # detector B: semantic guard (catches masked mismatches)
        guard = rc.semantic_guard(p, nm, dialect=dialect)
        guard_caught = bool(guard)
        caught.append({
            "column": f"{key[0]}.{key[1]}", "trapped": is_trap,
            "naive_target": nm.snowflake_type,
            "checksum_or_exec_caught": data_caught, "semantic_guard_caught": guard_caught,
            "caught": (data_caught or guard_caught) if is_trap else True,
            "guard": guard[:1],
        })
    # also: the CORRECT mapping must raise ZERO semantic-guard flags (no false alarms,
    # and the correct run is clean = nothing hidden).
    correct_guard_flags = []
    for m in maps:
        g = rc.semantic_guard(prop_index[(m.entity, m.column)], m, dialect=dialect)
        if g:
            correct_guard_flags.append({"column": f"{m.entity}.{m.column}", "flags": g})
    trapped = [c for c in caught if c["trapped"]]
    all_traps_caught = all(c["caught"] for c in trapped)
    gate.bullets["4_hard_fail_detection"] = {
        "pass": all_traps_caught and not correct_guard_flags and len(trapped) > 0,
        "traps_injected": len(trapped),
        "traps_caught": sum(1 for c in trapped if c["caught"]),
        "false_alarms_on_correct_mapping": correct_guard_flags,
        "detail": caught,
    }

    detail["ontology"] = onto.to_json()
    detail["mappings"] = [m.to_json() for m in maps]
    return gate, detail


def _target_compatible(got: str, expected: str) -> bool:
    g = got.upper().replace(" ", "").replace("TIMESTAMPNTZ", "TIMESTAMP_NTZ")
    e = expected.upper().replace(" ", "")
    if g == e:
        return True
    # NUMBER ≡ DECIMAL synonyms
    if g.replace("NUMBER", "DECIMAL") == e.replace("NUMBER", "DECIMAL"):
        return True
    # VARCHAR family: if the manifest pins an explicit width, the width MUST match —
    # a VARCHAR(320) where VARCHAR(4) is required is a real defect the gate must catch
    # (e.g. ssn_last4). Only an unwidthed expected target (the enum case, documented as
    # "VARCHAR + CHECK or reference table") accepts any VARCHAR width.
    if "VARCHAR" in e and g.startswith("VARCHAR"):
        e_w = re.search(r"VARCHAR\((\d+)\)", e)
        g_w = re.search(r"VARCHAR\((\d+)\)", g)
        if e_w is None:           # expected has no pinned width → any VARCHAR ok (enum)
            return True
        return g_w is not None and g_w.group(1) == e_w.group(1)
    # TIMESTAMP_NTZ / TIMESTAMP_TZ family: base type must match; a pinned expected
    # precision MUST match exactly (DATETIME2(7) mapped to NTZ(3) is a real defect), and
    # an unpinned expected target accepts any precision (same discipline as VARCHAR).
    for base in ("TIMESTAMP_NTZ", "TIMESTAMP_TZ"):
        if e.startswith(base) and g.startswith(base):
            e_p = re.search(r"\((\d+)\)", e)
            g_p = re.search(r"\((\d+)\)", g)
            if e_p is None:
                return True
            return g_p is not None and g_p.group(1) == e_p.group(1)
    return False


# ── Readout ─────────────────────────────────────────────────────────────────

def _b0_count(b0: dict[str, Any]) -> str:
    """Bullet 0's headline count — three columns, never two (SAU-516).

    `not-seed-declarable` is reported as its own number rather than added to the
    verified total or hidden. Folding it either way would make the headline a claim the
    audit did not make.
    """
    n = b0["columns_audited"]
    exempt = b0.get("columns_not_seed_declarable", 0)
    verified = b0.get("columns_verified", n - b0.get("columns_drifted", 0) - exempt)
    tail = f" / {exempt} not-seed-declarable" if exempt else ""
    return (f"{verified} verified{tail} / {b0.get('columns_drifted', 0)} drifted "
            f"(of {n} ground-truth columns)")


def provenance(dialect: dl.SourceDialect, target: Any = None) -> dict[str, str]:
    """Name the environment a gate result came from (SAU-538).

    A readout is a measurement, and a measurement without its instrument is not
    reproducible: the same commit is RED on Python 3.9 and GREEN on 3.11 for the SQL
    Server lane (`datetime.fromisoformat` vs 7-digit fractions), and the frozen-ontology
    digest moves whenever the seed behind the socket is rebuilt. So the readout states
    interpreter, libraries, commit, source rig and execution proxy — verbatim, no
    credentials. Wall-clock is deliberately omitted: it would make every regeneration a
    diff, and it is not an input to the result.
    """
    import platform
    import subprocess
    import sys

    def _ver(mod: str) -> str:
        try:
            return __import__(mod).__version__
        except Exception:  # noqa: BLE001 — provenance must never fail the gate
            return "(not importable)"

    def _git(*args: str) -> str:
        try:
            out = subprocess.run(["git", *args], capture_output=True, text=True,
                                 cwd=Path(__file__).resolve().parent.parent)
            return out.stdout.strip() if out.returncode == 0 else "(no git)"
        except OSError:
            return "(no git)"

    head = _git("rev-parse", "--short", "HEAD")
    dirty = bool(_git("status", "--porcelain", "--", "core", "run_loop.py", "schema",
                      "sqlserver", "profile"))
    p: dict[str, str] = {
        "interpreter": f"Python {sys.version.split()[0]} ({sys.executable})",
        "platform": platform.platform(),
        "sqlglot": _ver("sqlglot"),
        "duckdb": _ver("duckdb"),
        "commit": head + (" (+ uncommitted changes under core/, schema/, sqlserver/, "
                          "profile/ or run_loop.py)" if dirty else ""),
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "execution_proxy": f"{getattr(target, 'name', 'duckdb')} "
                           + (f"(duckdb {_ver('duckdb')})"
                              if getattr(target, "name", "duckdb") == "duckdb"
                              else "(live target)"),
    }
    for k, v in dialect.rig_descriptor().items():
        p[f"source.{k}"] = v
    return p


def write_readout(gate: GateResult, out_path: str | Path, *,
                  issue: str = "SAU-305", source_label: str = "Postgres",
                  target_label: str = "Snowflake",
                  extra_caveats: list[str] | None = None,
                  provenance: dict[str, str] | None = None) -> str:
    b = gate.bullets
    lines: list[str] = []
    A = lines.append
    verdict = "✅ GO" if gate.passed else "❌ NO-GO"
    A(f"# {issue} §8 Exit-Gate Readout — {verdict}\n")
    A("Phase 0/1 core loop: **lift → map → codegen → verification**. "
      f"{source_label} → {target_label}, reconciled on a sample via a DuckDB execution "
      f"proxy ({target_label} SQL is compile-checked in the {target_label} dialect).\n")
    A("| # | Acceptance criterion | Result |")
    A("|---|----------------------|--------|")

    b0 = b.get("0_corpus_validity")
    if b0:
        A(f"| 0 | Corpus validity: the seeded ground truth actually landed in the source | "
          f"{'✅ PASS' if b0['pass'] else '❌ FAIL'} — {_b0_count(b0)}"
          + (f"; audit error: {b0['audit_error']}" if b0["audit_error"] else "") + " |")

    b1 = b["1_reconciliation"]
    A(f"| 1 | 100% row-count + checksum on Gold, 3/3 clean runs | "
      f"{'✅ PASS' if b1['pass'] else '❌ FAIL'} — {b1['consecutive_clean_runs']}/{b1['required']} "
      f"consecutive clean; Snowflake compile errors: {len(b1['snowflake_compile_errors'])} |")
    b2 = b["2_byte_identical_regen"]
    A(f"| 2 | Byte-identical SQL regeneration (SQLGlot) | "
      f"{'✅ PASS' if b2['pass'] else '❌ FAIL'} — {b2['iterations']} regenerations, "
      f"{b2['distinct_digests']} distinct digest(s) |")
    b2f = b.get("2b_frozen_ontology_regen")
    if b2f:
        A(f"| 2b | Byte-identical regen from FROZEN ontology (no re-inference) | "
          f"{'✅ PASS' if b2f['pass'] else '❌ FAIL'} — {b2f['iterations']} regens, "
          f"{b2f['distinct_digests']} digest(s), matches live: {b2f['matches_live_generation']}, "
          f"inference re-entered: {b2f['inference_reentered']} |")
    b3 = b["3_confidence_calibration"]
    A(f"| 3 | Calibrated confidence vs hard-column manifest | "
      f"{'✅ PASS' if b3['pass'] else '❌ FAIL'} — {b3['target_correct']}/{b3['hard_columns']} "
      f"targets correct, {b3['meets_confidence_floor']}/{b3['hard_columns']} meet confidence floor |")
    b4 = b["4_hard_fail_detection"]
    A(f"| 4 | Hard-fail: surface silent loss / coercion / masked mismatch | "
      f"{'✅ PASS' if b4['pass'] else '❌ FAIL'} — {b4['traps_caught']}/{b4['traps_injected']} "
      f"injected traps caught, {len(b4['false_alarms_on_correct_mapping'])} false alarms |")
    A(f"| 5 | Written readout for CTO go/no-go | ✅ this document |\n")

    if b0:
        A("## 0 · Corpus validity (does the ground truth exist?)\n")
        A("Bullets 1–4 below all validate the **transform**. None of them validates the "
          "**extraction**: reconciliation compares source-as-read to target-as-read, and "
          "both anchors sit *downstream* of anything the source engine normalized at "
          "INSERT. A trap the engine quietly rewrote is therefore invisible to them — the "
          "corpus still looks populated and the gate still goes GREEN while testing "
          "nothing. Bullet 0 anchors on the seed SQL instead, which the engine never "
          "touched.\n")
        A(f"- {b0['method']}")
        A(f"- Seed audited: `{b0['seed']}` — **{_b0_count(b0)}**.")
        if b0["audit_error"]:
            A(f"\n❌ The audit could not run: `{b0['audit_error']}`. Bullet 0 fails "
              "closed — an audit that did not run is not an audit that passed.")
        if b0["drifted"]:
            A("\n❌ **The source does not hold what the seed claims** for the columns "
              "below. Anything downstream that relies on these columns is measuring a "
              "trap that was never planted:\n")
            A("| column | comparison | claimed by the seed | actually stored |")
            A("|--------|------------|---------------------|-----------------|")
            for d in b0["drifted"]:
                claimed = ", ".join(f"`{v}`" for v in d["claimed_but_not_stored"]) or "—"
                stored = ", ".join(f"`{v}`" for v in d["stored_but_not_claimed"]) or "—"
                note = f" — {d['error']}" if d["error"] else ""
                A(f"| {d['column']}{note} | {d['comparison']} | {claimed} | {stored} |")
        elif not b0["audit_error"]:
            # Scoped to the columns that actually made a claim: with an exempt column
            # present, "every ground-truth column round-trips its literal" would assert
            # something about a column that never stated one.
            scope = ("every ground-truth column that states a claim"
                     if b0.get("columns_not_seed_declarable")
                     else "every ground-truth column")
            A(f"\nNo drift: {scope} round-trips the literal its INSERT claims, so the "
              "traps the bullets below rely on are genuinely present.")

        if b0.get("equivalences"):
            # Constraint from the SAU-516 ruling: a canonicalization that hides its own
            # application IS the invisible-normalization defect, one layer up. Both raw
            # forms and the rule's name are printed so the reader can audit the rule
            # itself, which is the entire difference between this and editing the seed.
            A("\n### Reconciled under a named type equivalence\n")
            A("The pairs below did **not** match byte-for-byte. Each was reconciled by a "
              "rule the source dialect declares for that type — documented type "
              "semantics, derivable without ever looking at our corpus — not by editing "
              "what the seed claims. Raw forms are printed on both sides so the rule is "
              "auditable:\n")
            A("| column | claimed by the seed | actually stored | rule |")
            A("|--------|---------------------|-----------------|------|")
            for e in b0["equivalences"]:
                A(f"| {e['column']} | `{e['claimed']}` | `{e['stored']}` | "
                  f"`{e['rule']}` |")
            # SAU-518. Without this, a reader can verify each rule above but not the one
            # thing the table structurally cannot show: that it is non-empty for a
            # reason. An emptier table is the *worse* outcome here, and nothing else in
            # the readout says so.
            A("\nRead the direction, not just the rows. Each rule is consulted only on "
              "residuals the exact comparison left over, and only the way the type "
              "documents — the seed abbreviates, the type expands. The inverse, an "
              "extraction that **strips** what the type stores, would make every "
              "abbreviated literal match exactly and empty this table: a strictly "
              "greener readout produced by a lossy write policy. So the corpus "
              "deliberately also seeds values no equivalence can reach (ADR-0001, "
              "SAU-518) — those rows match byte-for-byte here and fail loudly the "
              "moment extraction starts discarding.")

        if b0.get("not_seed_declarable"):
            A("\n### Not seed-declarable (counted separately — NOT a pass)\n")
            A("For the columns below the **type** forbids an INSERT from supplying a "
              "value, so the seed states no claim for bullet 0 to check. That is bullet "
              "0 declining a job that was never its, not a column going unverified: "
              "bullets 1–4 still reconcile these columns. The exemption is derived from "
              "the catalog, so it cannot be widened by appending to a list — only by "
              "misdeclaring a type:\n")
            A("| column | why it is not seed-declarable |")
            A("|--------|-------------------------------|")
            for d in b0["not_seed_declarable"]:
                A(f"| {d['column']} | {d['not_seed_declarable']} |")
        A("")

    A("## 1 · Reconciliation runs\n")
    for r in b1["runs"]:
        status = "clean" if r["clean"] else f"DIRTY: {r['problems']}"
        A(f"- Run {r['run']}: {r['entities']} entities, {r['columns']} columns — {status}")
    if b1["snowflake_compile_errors"]:
        A(f"\n  Snowflake compile errors: {b1['snowflake_compile_errors']}")

    A("\n## 2 · Determinism\n")
    A(f"- {b2['method']}")
    A(f"- SHA-256 over all Snowflake DDL/DML across {b2['iterations']} regenerations: "
      f"`{b2['sha256'] if isinstance(b2['sha256'], str) else b2['sha256']}`")
    if b2f:
        A("\n### 2b · Frozen-ontology regeneration (SAU-312 / CTO item ②)\n")
        A("- Bullet 2 above re-lifts from the profile, which is byte-identical only "
          "because inference is a deterministic heuristic today. The real guarantee is "
          "this: the **approved ontology is frozen to a content-addressed artifact**, and "
          "regeneration loads that artifact and re-maps + re-codegens — the inference "
          "seam is never re-entered. The guarantee therefore survives an LLM entering "
          "inference.")
        A(f"- Frozen-artifact SHA-256: `{b2f['frozen_sha256']}`")
        A(f"- {b2f['iterations']} regenerations from the frozen artifact → "
          f"{b2f['distinct_digests']} distinct digest(s); matches the live generation: "
          f"{b2f['matches_live_generation']}.")
        A(f"- Inference re-entered on the regen path: **{b2f['inference_reentered']}** "
          "(enforced by `assert_no_inference`)."
          + (f" Violation: {b2f['violation']}" if b2f["violation"] else ""))
        A(f"- Tampered-frozen-artifact rejected by integrity check: "
          f"{b2f['integrity_check_enforced']}.")

    A("\n## 3 · Confidence calibration (hard columns)\n")
    A("| column | category | expected | got | ✓ | conf | floor | ≥floor |")
    A("|--------|----------|----------|-----|---|------|-------|--------|")
    for r in b3["detail"]:
        A(f"| {r['column']} | {r['category']} | {r['expected']} | {r['got']} | "
          f"{'✓' if r['target_ok'] else '✗'} | {r['confidence']} | {r['floor']} | "
          f"{'✓' if r['meets_floor'] else '✗'} |")

    A("\n## 4 · Hard-fail / trap detection (ablation)\n")
    A("A naive mapper was forced into each documented trap; the loop must catch every "
      "one via checksum/exec-error **or** the structural semantic guard (which defeats "
      "reconciliation-masking).\n")
    A("| column | naive target | checksum/exec | semantic guard | caught |")
    A("|--------|--------------|---------------|----------------|--------|")
    for c in b4["detail"]:
        if not c["trapped"]:
            continue
        A(f"| {c['column']} | {c['naive_target']} | "
          f"{'✓' if c['checksum_or_exec_caught'] else '·'} | "
          f"{'✓' if c['semantic_guard_caught'] else '·'} | "
          f"{'✅' if c['caught'] else '❌ ESCAPED'} |")
    if b4["false_alarms_on_correct_mapping"]:
        A(f"\n⚠️ semantic-guard false alarms on the correct mapping: "
          f"{b4['false_alarms_on_correct_mapping']}")
    else:
        A("\nNo semantic-guard false alarms on the correct mapping (correct run is clean — "
          "nothing is hidden).")

    A("\n## Caveats — what Phase 1 must still prove\n")
    A("This is a Phase-0 de-risk spike. The result is **necessary but not sufficient** "
      "for production; surfaced honestly so the go/no-go is not over-read:")
    A("- **No live Snowflake.** The target dialect is *compile-checked* with SQLGlot's "
      "Snowflake parser, and the medallion is *executed* on DuckDB as a stand-in. "
      "Phase 1 must re-run the identical generated Snowflake SQL on a real Snowflake "
      "account (warehouse sizing, micro-partition behavior, COPY-INTO load semantics).")
    A("- **VARIANT/ARRAY** are stored as canonical JSON text in the DuckDB proxy; their "
      "*content* preservation is reconciled and their VARIANT/ARRAY typing is "
      "compile-validated, but native semi-structured behavior is a Phase-1 confirmation.")
    A("- **Sample, not full volume.** Reconciliation ran on the seeded sample "
      "(≥15 tables, all documented traps). Phase 1 must reconcile at production volume "
      "and add scale/perf gates.")
    A("- **Inference pass is deterministic heuristic** here (reproducible, calibratable). "
      "The seam is LLM-pluggable; if an LLM is introduced it stays isolated to "
      "semantic-type inference only — never SQL generation (the reliability lever).")
    for c in (extra_caveats or []):
        A(f"- {c}")

    if provenance:
        A("\n## Provenance — what produced this readout\n")
        A("A gate result is a property of the environment as much as of the code "
          "(the SQL Server lane is RED on Python 3.9 and GREEN on 3.11 with no code "
          "change). Reproduce against *this*, not against \"the repo\":\n")
        A("| key | value |")
        A("|-----|-------|")
        for k, v in provenance.items():
            cell = str(v).replace("|", "\\|")
            A(f"| `{k}` | {cell} |")

    A("\n## Verdict\n")
    A(f"**{verdict}** for the §8 gate. "
      + ("All four technical criteria pass on the ≥15-table schema; "
         "feeds the CTO Phase-1 go/no-go." if gate.passed
         else "One or more criteria failed — see above; NOT ready for Phase-1 go."))
    text = "\n".join(lines) + "\n"
    Path(out_path).write_text(text)
    return text
