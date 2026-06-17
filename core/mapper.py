"""Auto-map stage (SAU-305): ontology property → Snowflake target type.

Each property's inferred semantic type + physical profile is mapped to:
  * `snowflake_type` — the production target physical type (the codegen deliverable),
  * `reconcile` — the assertion class the verification loop applies (exact text,
    approximate float, canonical JSON, timestamp instant vs wall-clock, …),
  * `confidence` — carried from inference, the calibration signal for §8,
  * `governance_tags`, `flags`, `notes` — human-approvable review metadata.

The mapper **preserves source precision/scale and length** — it never infers a type
from a column's *name* when the physical profile already pins it (e.g. JPY money is
kept at the source scale, not re-derived from the currency). That discipline is what
keeps the precision traps from silently coercing.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any

from . import ontology as ont

# Reconciliation strategy tokens — consumed by reconcile.py to pick the assertion.
RC_EXACT_TEXT = "exact_text"          # byte/precision-exact string identity
RC_APPROX_FLOAT = "approx_float"      # within float epsilon; flagged lossy
RC_JSON_CANONICAL = "json_canonical"  # parseable, key-canonicalized JSON equality
RC_ARRAY_CANONICAL = "array_canonical"  # ordered element equality, scale preserved
RC_TS_INSTANT = "timestamp_instant"   # same UTC instant (tz-aware)
RC_TS_WALL = "timestamp_wall"         # same wall-clock value (no tz applied)
RC_BOOLEAN = "boolean"


@dataclass
class Mapping:
    entity: str
    column: str
    pg_type: str
    semantic_type: str
    snowflake_type: str
    reconcile: str
    confidence: float
    governance_tags: list[str]
    flags: list[str]
    notes: str

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def _num_type(prop: ont.Property, default_prec: int = 38, default_scale: int = 0) -> str:
    p = prop.numeric_precision if prop.numeric_precision is not None else default_prec
    s = prop.numeric_scale if prop.numeric_scale is not None else default_scale
    return f"NUMBER({p},{s})"


def _varchar(prop: ont.Property, default_len: int | None = None) -> str:
    n = prop.char_max_len if prop.char_max_len is not None else default_len
    return f"VARCHAR({n})" if n is not None else "VARCHAR"


def map_property(entity: str, prop: ont.Property) -> Mapping:
    """Map a single ontology property to its Snowflake target + reconcile strategy."""
    sem = prop.semantic_type
    tags = list(prop.governance_tags)
    flags: list[str] = []
    notes = ""
    pg_type = prop.data_type + (
        f"({prop.char_max_len})" if prop.char_max_len else
        f"({prop.numeric_precision},{prop.numeric_scale})"
        if prop.numeric_precision is not None and prop.numeric_scale is not None else ""
    )

    if sem in (ont.SEM_PK, ont.SEM_FK, ont.SEM_INTEGER):
        sf, rc = "NUMBER(38,0)", RC_EXACT_TEXT
    elif sem == ont.SEM_CURRENCY_CENTS:
        sf, rc = "NUMBER(38,0)", RC_EXACT_TEXT
        notes = "money stored as integer cents — kept integer, never FLOAT"
    elif sem in (ont.SEM_CURRENCY_DECIMAL, ont.SEM_RATE):
        sf, rc = _num_type(prop, 38, 2), RC_EXACT_TEXT
        notes = "precision/scale preserved from source — no FLOAT coercion"
    elif sem == ont.SEM_LOSSY_FLOAT:
        sf, rc = "FLOAT", RC_APPROX_FLOAT
        flags.append("lossy_float")
        notes = "binary float identity is approximate — reconciled within epsilon, not byte-exact"
    elif sem == ont.SEM_BOOLEAN:
        sf, rc = "BOOLEAN", RC_BOOLEAN
    elif sem == ont.SEM_TIMESTAMP_TZ:
        sf, rc = "TIMESTAMP_TZ", RC_TS_INSTANT
        notes = "tz-aware — same UTC instant must round-trip"
    elif sem == ont.SEM_TIMESTAMP_NTZ:
        sf, rc = "TIMESTAMP_NTZ", RC_TS_WALL
        notes = "no time zone — wall-clock value preserved, no offset invented"
    elif sem == ont.SEM_DATE:
        sf, rc = "DATE", RC_EXACT_TEXT
    elif sem == ont.SEM_BINARY:
        # ROWVERSION / raw bytes — fixed-width BINARY identity, reconciled as exact hex
        # text. Never a timestamp (the legacy 'timestamp' synonym is the trap).
        n = prop.char_max_len if prop.char_max_len else 8
        sf, rc = f"BINARY({n})", RC_EXACT_TEXT
        notes = "row-version/binary identity preserved as BINARY — not interpreted as time"
    elif sem in (ont.SEM_PII_FIXED_WIDTH, ont.SEM_FIXED_WIDTH_ID):
        sf, rc = _varchar(prop), RC_EXACT_TEXT
        notes = "fixed-width identity — must stay textual; numeric coercion drops leading zeros"
    elif sem == ont.SEM_PII_EMAIL:
        sf, rc = _varchar(prop, 320), RC_EXACT_TEXT
    elif sem in (ont.SEM_PII_PHONE, ont.SEM_PII_NAME):
        sf, rc = _varchar(prop), RC_EXACT_TEXT
    elif sem == ont.SEM_NETWORK:
        sf, rc = "VARCHAR(45)", RC_EXACT_TEXT
        notes = "INET has no native Snowflake type — serialized losslessly to text (IPv4+IPv6)"
    elif sem == ont.SEM_ENUM:
        sf, rc = _varchar(prop, 64), RC_EXACT_TEXT
        flags.append("enum_domain")
        notes = "Postgres enum → VARCHAR; domain enforced via CHECK/ref table downstream"
    elif sem == ont.SEM_AMBIGUOUS_TEXT:
        sf, rc = _varchar(prop), RC_EXACT_TEXT
        flags.append("do_not_parse_numeric")
        notes = "numeric-LOOKING text — kept verbatim; must NOT be parsed to a number"
    elif sem == ont.SEM_SEMI_STRUCTURED:
        sf, rc = "VARIANT", RC_JSON_CANONICAL
        notes = "JSON → VARIANT; nested structure + unicode preserved"
    elif sem == ont.SEM_ARRAY:
        sf, rc = "ARRAY", RC_ARRAY_CANONICAL
    elif sem == ont.SEM_ARRAY_NUMERIC:
        sf, rc = "ARRAY", RC_ARRAY_CANONICAL
        notes = "numeric elements keep scale inside the ARRAY"
    else:  # SEM_FREE_TEXT and fallbacks
        sf, rc = _varchar(prop) if prop.char_max_len else "VARCHAR", RC_EXACT_TEXT

    return Mapping(
        entity=entity,
        column=prop.name,
        pg_type=pg_type,
        semantic_type=sem,
        snowflake_type=sf,
        reconcile=rc,
        confidence=prop.confidence,
        governance_tags=tags,
        flags=flags,
        notes=notes,
    )


def map_ontology(onto: ont.Ontology) -> list[Mapping]:
    """Map every property in every entity. Stable order (entity, ordinal)."""
    out: list[Mapping] = []
    for e in onto.entities:
        for p in sorted(e.properties, key=lambda x: x.ordinal):
            out.append(map_property(e.name, p))
    return out


def mappings_for(mappings: list[Mapping], entity: str) -> list[Mapping]:
    return [m for m in mappings if m.entity == entity]
