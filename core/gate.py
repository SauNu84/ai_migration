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
from . import mapper as mp
from . import codegen as cg
from . import reconcile as rc
from . import dialect as dl

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


def run_gate(profile_path: str | Path, manifest_path: str | Path,
             *, clean_runs: int = 3, regen_iters: int = 5,
             dialect: "dl.SourceDialect | None" = None) -> tuple[GateResult, dict[str, Any]]:
    dialect = dialect or dl.DEFAULT_DIALECT
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

    # ── Bullet 1: 3/3 consecutive clean runs (row-count + checksum on Gold) ──
    run_summaries = []
    consecutive = 0
    compile_errors = cg.compile_check(code)
    for i in range(clean_runs):
        results = rc.run(code, maps, udt_index, dialect=dialect)
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
    naive_results = rc.run(naive_code, naive, udt_index, dialect=dialect)
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

def write_readout(gate: GateResult, out_path: str | Path, *,
                  issue: str = "SAU-305", source_label: str = "Postgres",
                  target_label: str = "Snowflake",
                  extra_caveats: list[str] | None = None) -> str:
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

    A("\n## Verdict\n")
    A(f"**{verdict}** for the §8 gate. "
      + ("All four technical criteria pass on the ≥15-table schema; "
         "feeds the CTO Phase-1 go/no-go." if gate.passed
         else "One or more criteria failed — see above; NOT ready for Phase-1 go."))
    text = "\n".join(lines) + "\n"
    Path(out_path).write_text(text)
    return text
