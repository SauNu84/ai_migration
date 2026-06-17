"""SAU-305 — the core IP: lift → map → codegen → verification loop.

Pipeline (all deterministic except the single isolated inference pass in `ontology`):

    profile.json ──lift──▶ ontology IR ──map──▶ Snowflake target mappings
                                                       │
                                          codegen (canonical SQL once)
                                                       │
                              SQLGlot ┌────────────────┴───────────────┐
                                      ▼                                ▼
                              Snowflake SQL                     DuckDB SQL
                          (compile + byte-identical          (executed on a
                           regeneration target)             sample → reconcile)

The reliability lever (per the board brief): **canonical SQL is generated once**,
then SQLGlot transpiles it deterministically. The LLM is never asked to be a
multi-dialect SQL expert — it is isolated to semantic-type inference only.
"""

__all__ = ["dialect", "ontology", "mapper", "codegen", "reconcile", "gate"]
