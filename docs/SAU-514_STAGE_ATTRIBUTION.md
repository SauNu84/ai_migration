# SAU-514 — stage attribution of the verification loop

**Question asked.** SAU-511 reported 962.5 s for one 10-column entity at 100k rows and did
not say which stage that time was in. Four of the six stages run on the DuckDB *execution
proxy*, which SAU-432's `TargetAdapter` seam exists to swap for live Snowflake. So: split
the wall clock by stage, and tag each stage by whether it survives the swap — before
anyone funds a full-volume campaign that might be measuring a component we intend to
delete.

**How.** `profile/stage_profile.py` instruments the **real** `reconcile.run` — a
`PostgresDialect` subclass times the row pull, a proxy over the DuckDB connection
classifies and times every statement, and an `EntityDigest` subclass times the
canonicalize+checksum fold. No core module is edited and no executed SQL changes. This
matters because since SAU-511 the extraction, the source digest and the Bronze load are
*interleaved* by the streaming tee: there is no point in the program where one ends and
the next begins, so the split has to be accumulated per row at the exact seams. A
re-implemented loop with timers in convenient places would have measured a different
program — the SAU-511 mistake (a synthetic dialect has no reader, so it cannot measure
the reader) repeated on the time axis.

Every run below reconciled **clean** (`row_match=True`, all 10 columns matching), so the
instrumented loop is a real verification, not a timing shell.

Corpus: a 10-column mixed-type `app_user` clone (two timestamps of differing tz-awareness,
JSONB, enum, fixed-width CHAR, two BIGINTs, three VARCHARs) in its own schema, reached via
`search_path`; `public` never touched. Seed integrity **proved, not asserted**: the §8 gate
was re-run afterwards and `gate_result.json` is byte-identical to the pre-probe copy.

Box: macOS, 34.4 GB RAM, Python 3.9.6, DuckDB 1.4.4, `batch=5000`, `FETCH_COUNT=1000`.

---

## 1. The attribution — one stage dominates, and it is on the proxy side

100k rows, 10 columns (clean pass A; shares are stable across every run):

| stage | side | seconds | share | µs/row |
|---|---|---:|---:|---:|
| `bronze_load` — DuckDB `executemany` | **proxy** | 39.89 | **75.3%** | 398.9 |
| `canon_source` — Python digest fold, source | **durable** | 5.24 | 9.9% | 52.4 |
| `canon_target` — Python digest fold, Gold | durable in kind | 4.70 | 8.9% | 47.0 |
| `gold_readback` — query + `fetchmany` + dict | proxy | 1.03 | 1.9% | 10.3 |
| `extract_pull` — psql pipe + `json.loads` | **durable** | 0.92 | 1.7% | 9.2 |
| `silver` — `CREATE TABLE … AS SELECT` | proxy | 0.30 | 0.6% | 3.0 |
| `gold` — `CREATE TABLE … AS SELECT` | proxy | 0.07 | 0.1% | 0.7 |
| `bronze_ddl` | proxy | 0.00 | 0.0% | 0.0 |
| residual (chassis, generators, tee) | — | 0.82 | 1.5% | |

**Durable 14–20%. Proxy 78–85%.** Consistent across all five runs.

Two notes on the tagging, because both were judgement calls and both matter:

* `canon_target` is **not** filed under "proxy" the way the ticket's table has it. The Gold
  *read* is the proxy's and becomes a Snowflake cursor; the digest fold is our
  `canonicalize()` and runs byte-identically on rows that came from Snowflake. Its cost
  changes engines, it does not disappear. Calling it proxy would have understated the
  durable half, which is the one thing this exercise exists not to do.
* `extract_pull` is time Python is *blocked* on the psql pipe. psql runs concurrently, so
  this is the source's contribution to the **critical path** — the right number for "what
  does the run cost", and an under-estimate of the server's total work.

### This reverses the ticket's hypothesis

The ticket reasoned: ~130 s of 962.5 s is the Bronze load, so "**~830 s is somewhere
else** — most plausibly extraction and canonicalization, which are the *durable* half",
and closed "if it holds, the good news is that the expensive stages are the ones worth
optimizing, because they survive Snowflake."

It does not hold. Extraction is **1.7%**. Both canonicalization folds together are 18.8%.
The single dominant stage is the DuckDB `executemany` at 75–83% — the stage that becomes
`COPY INTO` the moment `SnowflakeAdapter(live=True)` is selected.

The arithmetic in the ticket was sound; its premise was the 962.5 s total, and that total
is the thing that did not survive contact (§3).

---

## 2. The n·log n super-linearity hypothesis — killed

Attributed ~19%/decade to the two `ORDER BY id` sorts. Ablated both directly: same
projection, same extraction expressions, same psql flags, same streaming — the `ORDER BY`
removed and nothing else.

| rows | source extract, sorted → unsorted | Gold read-back, sorted → unsorted |
|---:|---|---|
| 10,000 | +0.011 s (+6.9%) | −0.020 s |
| 100,000 | +0.037 s (+3.1%) | +0.026 s |
| 300,000 | **+0.006 s (+0.2%)** | +0.048 s |

At 300k the two sorts together cost ~0.05 s inside a **270 s** run — under 0.02% of wall
clock, and the source-side delta *shrinks* as rows grow, which is the opposite of n·log n.
The hypothesis is dead and should not be carried forward as an explanation for anything.

---

## 3. 962.5 s does not reproduce — and I published it

Same chassis, same 10-column entity, same 100k rows, three independent clean runs:
**53.7 s, 53.0 s, 48.7 s**. The SAU-511 figure is ~18× higher than anything I can now
measure.

Ruled out:

* **`tracemalloc`.** The SAU-511 run was a *memory* probe, so the obvious suspect was
  measurement overhead. Tested it — same probe, same harness, under `tracemalloc`:
  73.6 s at 100k. That is **+37%, not 18×.** Hypothesis wrong, and stated as wrong rather
  than quietly dropped.
* **Row width.** My probe's wire rows average 317 bytes against the live seed's 273 —
  mine are 16% *wider*, so the reconstruction is if anything conservative.

Not ruled out, and now unrecoverable: the original probe schema was dropped after the
SAU-511 run, so that specific execution cannot be re-examined. §4 gives the explanation I
think is most likely, but I cannot close it, and I am not going to dress a plausible story
up as a finding twice in one ticket.

**Practical consequence: treat 962.5 s as withdrawn as a *rate*.** It was one draw from a
distribution I did not know I was sampling.

---

## 4. What is actually super-linear — and why the totals are unstable

Per-row cost across the ladder, two back-to-back passes of the identical script:

| stage (µs/row) | 10k A | 10k B | 100k A | 100k B | 300k A | 300k B |
|---|---:|---:|---:|---:|---:|---:|
| `bronze_load` | 458.5 | 436.0 | 398.9 | 367.2 | **747.8** | **526.6** |
| `canon_source` | 52.4 | 48.2 | 52.4 | 47.4 | 62.3 | 50.7 |
| `extract_pull` | 11.8 | 10.9 | 9.2 | 8.3 | 10.6 | 8.6 |
| **TOTAL** | 592 | 558 | 530 | 487 | **900** | **651** |

10k and 100k agree between passes within 5–9%, and per-row cost *falls* from 10k to 100k —
sub-linear. Then 300k jumps, and the two passes disagree by **38%**. Nearly all of the
excess is `bronze_load`. Even `canon_source` — pure Python CPU, which must be flat — rises.

A pure algorithmic term does not behave like that; it is smooth and it reproduces. A
resource threshold does. The box: **7.75 GB of 9.2 GB swap in use**, load average ~3, ten
Docker containers running (including an Ollama LLM server and this project's own SQL
Server rig).

> **Labelled as what it is: confidence medium.** The swap/contention reading fits every
> symptom — the non-reproducibility, the concentration in the largest in-memory structure,
> the rise in a stage that has no algorithmic reason to rise. It is not proven; I have not
> re-run this on a quiet box, which is the experiment that would settle it.

**The conclusion I will defend:** absolute wall-clock on this box is not a measurement
instrument above ~100k rows. **Relative stage shares are** — they hold across every run
(bronze_load 75–83%) because contention scales all stages roughly together. So the
attribution in §1 is solid; any single absolute total, 962.5 s included, is not.

---

## 5. The honest full-volume claim for SAU-310 / SAU-311

* **The durable half is not the problem and is not close to being the problem.** Source
  extraction plus both canonicalization folds run ~0.11 ms/row and are flat to 100k:
  ≈ 110 s per 1M-row entity, ≈ 30 min for a 16-entity schema. Full-volume row-count +
  checksum reconciliation is **not** blocked by our own code.
* **The cost is the DuckDB proxy's in-memory Bronze load** — 75–83% of wall clock, and the
  stage that degrades unpredictably as the in-memory database grows. It is also precisely
  the stage `SnowflakeAdapter` replaces with `COPY INTO`.
* **My SAU-511 read — "the DuckDB proxy cannot do full volume in any useful sense" — was
  directionally right and quantitatively wrong.** Not 2.7–3.2 h/entity. At the stable 100k
  rate a 1M-row entity is ~8–9 minutes; what actually threatens a full-volume run is the
  proxy's memory behaviour on a contended box, not a per-row rate.

**On the no-1M rule.** The ticket put a 1M run out of scope because it would cost ~3 h per
entity. That premise is void — at the measured stable rate it is single-digit minutes. The
reason to still not run it *here* is different and better: this box cannot measure it
(§4). On a quiet box it is a cheap experiment and I would recommend it. Not doing it
unilaterally; the scope line is the COO's to move.

---

## 6. Written down, deliberately not done

Per the ticket's "no optimization — I want the attribution uncontaminated":

* `executemany` at ~400 µs/row is the single lever worth pulling, and it is **proxy-side**.
  DuckDB alternatives exist (the `appender` API, Arrow ingest, `read_json` over a spooled
  file). All of them optimize a component SAU-432 exists to delete — which is exactly the
  trap this ticket was written to avoid, so it should be a deliberate decision, not a
  drive-by.
* The 300k instability is a box question before it is a code question. Re-measure
  somewhere that is not swapping before anyone tunes anything.
* Nothing here touched a seed, a mapping, or any generated SQL.
