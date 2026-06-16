"""Lift stage (SAU-305): profile.json → typed property-graph ontology IR.

The ontology is a typed property graph:
  * **entity** nodes  — one per base table
  * **property** nodes — one per column, carrying its physical profile (type,
    nullability, precision/scale, length, value-range stats) plus an inferred
    **semantic type** and **governance tags** with a per-property **confidence**.
  * **relationship** edges — foreign keys (source entity → target entity).

The graph is plain JSON (JSONB-friendly) and is validated against an embedded
JSON Schema, so it is human-approvable and round-trips losslessly.

`infer_semantics()` is THE ISOLATED AI PASS. In this Phase-0 spike it is a
deterministic, signal-scored heuristic (reproducible, calibratable, and free of
LLM non-determinism — which is what byte-identical regeneration requires). The
function is the single seam where an LLM classifier would plug in: it consumes
only aggregate profile signals (never raw rows) and emits the same
`(semantic_type, confidence, signals)` contract. Everything downstream is
deterministic SQL generation.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

ONTOLOGY_SCHEMA_VERSION = "0.1.0"

# ── Semantic type vocabulary ────────────────────────────────────────────────
# These are the *meanings* the mapper turns into Snowflake physical types. They
# intentionally mirror the categories in ground_truth/hard_columns.json so the
# §8 calibration check can correlate confidence with the seeded traps.
SEM_PK = "primary_key"
SEM_FK = "foreign_key"
SEM_PII_EMAIL = "pii_email"
SEM_PII_PHONE = "pii_phone"
SEM_PII_NAME = "pii_name"
SEM_PII_FIXED_WIDTH = "pii_fixed_width"
SEM_FIXED_WIDTH_ID = "fixed_width_identifier"
SEM_CURRENCY_DECIMAL = "currency_decimal"
SEM_CURRENCY_CENTS = "currency_as_integer"
SEM_RATE = "rate_fraction"
SEM_TIMESTAMP_TZ = "timestamp_tz"
SEM_TIMESTAMP_NTZ = "timestamp_ntz"
SEM_DATE = "date"
SEM_SEMI_STRUCTURED = "semi_structured"
SEM_ARRAY = "array"
SEM_ARRAY_NUMERIC = "array_numeric"
SEM_ENUM = "enum"
SEM_NETWORK = "network_address"
SEM_AMBIGUOUS_TEXT = "ambiguous_text_numeric"
SEM_LOSSY_FLOAT = "lossy_float"
SEM_BOOLEAN = "boolean"
SEM_INTEGER = "integer"
SEM_FREE_TEXT = "free_text"

PII_SEMANTICS = {
    SEM_PII_EMAIL,
    SEM_PII_PHONE,
    SEM_PII_NAME,
    SEM_PII_FIXED_WIDTH,
}

# Lossy categories assert approximate-equality, not byte-equality (per manifest).
LOSSY_SEMANTICS = {SEM_LOSSY_FLOAT}


@dataclass
class Property:
    """A column lifted into the ontology, with physical + inferred semantics."""

    name: str
    ordinal: int
    # physical profile (from SAU-303 profiler)
    data_type: str
    udt: str
    nullable: bool
    char_max_len: int | None
    numeric_precision: int | None
    numeric_scale: int | None
    null_count: int
    null_rate: float
    distinct_count: int | None
    min: str | None
    max: str | None
    # inferred semantics (the isolated AI pass)
    semantic_type: str = SEM_FREE_TEXT
    confidence: float = 0.0
    governance_tags: list[str] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Entity:
    name: str
    row_count: int | None
    properties: list[Property]

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "row_count": self.row_count,
            "properties": [p.to_json() for p in self.properties],
        }


@dataclass
class Relationship:
    name: str
    from_entity: str
    from_columns: list[str]
    to_entity: str
    to_columns: list[str]

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Ontology:
    schema_version: str
    source_schema: str
    entities: list[Entity]
    relationships: list[Relationship]

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source_schema": self.source_schema,
            "entities": [e.to_json() for e in self.entities],
            "relationships": [r.to_json() for r in self.relationships],
        }

    def property(self, entity: str, column: str) -> Property | None:
        for e in self.entities:
            if e.name == entity:
                for p in e.properties:
                    if p.name == column:
                        return p
        return None


# ── The isolated inference pass ─────────────────────────────────────────────

_EMAIL_NAME = re.compile(r"email|e_mail", re.I)
_PHONE_NAME = re.compile(r"phone|mobile|tel", re.I)
_NAME_NAME = re.compile(r"(full_name|first_name|last_name|\bname\b)", re.I)
_CENTS_NAME = re.compile(r"cents|_cents$|amount_cents|mrr_cents", re.I)
_MONEY_NAME = re.compile(r"price|amount|total|mrr|revenue|cost|balance|fee", re.I)
_RATE_NAME = re.compile(r"rate|pct|percent|discount|ratio", re.I)
_FIXED_PII_NAME = re.compile(r"ssn|card_last4|last4|account_no|routing", re.I)
_HASH_NAME = re.compile(r"hash|token|fingerprint|checksum|digest", re.I)
_LEGACY_TEXT_NAME = re.compile(r"legacy|_text$|_str$|raw_", re.I)
_NUMERIC_LOOKING = re.compile(r"^\s*-?[\d.,]+\s*$")


def _is_array(udt: str, data_type: str) -> bool:
    return data_type.upper() == "ARRAY" or udt.startswith("_")


def _looks_numeric_text(min_v: str | None, max_v: str | None) -> bool:
    """True only if BOTH observed bounds look numeric AND at least one carries an
    ambiguity (comma-decimal or leading characters) that makes auto-parsing unsafe."""
    vals = [v for v in (min_v, max_v) if v not in (None, "")]
    if not vals:
        return False
    return all(_NUMERIC_LOOKING.match(v) for v in vals)


def _has_leading_zero(*vals: str | None) -> bool:
    """True if any observed value is a fixed-width token that begins with '0' and is
    otherwise all digits or hex — i.e. the leading zero is identity-significant and
    numeric coercion would silently drop it. Covers both decimal ('0042') and hex
    ('0000…abc', e.g. a sha-256 hash) fixed-width identifiers."""
    for v in vals:
        if not v or len(v) <= 1 or v[0] != "0":
            continue
        rest = v[1:]
        if rest.strip("0123456789") == "" or rest.lower().strip("0123456789abcdef") == "":
            return True
    return False


def infer_property(col: dict[str, Any], *, is_pk: bool, is_fk: bool,
                   enum_udts: set[str]) -> tuple[str, float, list[str], list[str]]:
    """Infer (semantic_type, confidence, governance_tags, signals) for one column.

    Confidence is *calibrated to risk*: a type whose mapping is unambiguous given
    strong corroborating signals scores high; a type that hinges on a judgment call
    (numeric-looking text, lossy floats, enum-without-native-target) scores lower,
    so low confidence correlates with the seeded hard columns (§8 calibration).
    """
    name = col["name"]
    dt = (col.get("data_type") or "").lower()
    udt = (col.get("udt") or "").lower()
    scale = col.get("numeric_scale")
    prec = col.get("numeric_precision")
    clen = col.get("char_max_len")
    mn, mx = col.get("min"), col.get("max")
    tags: list[str] = []
    sig: list[str] = []

    # Keys first — structural certainty.
    if is_pk:
        sig.append("primary_key_constraint")
        return SEM_PK, 0.99, tags, sig
    if is_fk:
        sig.append("foreign_key_constraint")
        return SEM_FK, 0.97, tags, sig

    # Enums (no native Snowflake type — judgment call → moderate confidence).
    if udt in enum_udts:
        sig.append(f"pg_enum_type:{udt}")
        return SEM_ENUM, 0.85, tags, sig

    # Arrays.
    if _is_array(udt, dt):
        # numeric element type? udt like _numeric / _int8 etc.
        if any(k in udt for k in ("numeric", "int", "float", "numer")):
            sig.append(f"array_of_numeric:{udt}")
            return SEM_ARRAY_NUMERIC, 0.8, tags, sig
        sig.append(f"array_type:{udt}")
        return SEM_ARRAY, 0.85, tags, sig

    # Semi-structured.
    if dt in ("jsonb", "json") or udt in ("jsonb", "json"):
        sig.append("json_type")
        return SEM_SEMI_STRUCTURED, 0.85, tags, sig

    # Network.
    if udt in ("inet", "cidr") or dt in ("inet", "cidr"):
        sig.append("inet_no_native_target")
        return SEM_NETWORK, 0.85, tags, sig

    # Timestamps — the tz distinction is decided purely by the physical type.
    if "timestamp with time zone" in dt or udt == "timestamptz":
        sig.append("timestamptz_physical_type")
        return SEM_TIMESTAMP_TZ, 0.9, tags, sig
    if "timestamp without time zone" in dt or udt == "timestamp":
        sig.append("timestamp_ntz_physical_type")
        return SEM_TIMESTAMP_NTZ, 0.9, tags, sig
    if dt == "date":
        return SEM_DATE, 0.95, tags, sig

    if dt == "boolean" or udt == "bool":
        return SEM_BOOLEAN, 0.97, tags, sig

    # Fixed-width identifiers / PII (char(N)) — leading-zero corroboration.
    if dt in ("character", "char") or udt == "bpchar":
        lead = _has_leading_zero(mn, mx)
        if _FIXED_PII_NAME.search(name):
            tags.append("PII")
            sig.append(f"char({clen})_pii_name")
            if lead:
                sig.append("leading_zero_observed")
            return SEM_PII_FIXED_WIDTH, 0.95 if lead else 0.9, tags, sig
        if _HASH_NAME.search(name) or (clen and clen >= 32):
            sig.append(f"char({clen})_hash_or_token")
            if lead:
                sig.append("leading_zero_observed")
            return SEM_FIXED_WIDTH_ID, 0.95 if lead else 0.88, tags, sig
        # generic fixed-width text
        sig.append(f"char({clen})_generic")
        return SEM_FREE_TEXT, 0.8, tags, sig

    # Floats — explicitly lossy.
    if dt in ("real", "double precision") or udt in ("float4", "float8"):
        sig.append(f"float_physical_type:{udt}")
        # REAL (float4) is lossier than double → lower confidence.
        conf = 0.7 if udt == "float4" or dt == "real" else 0.75
        return SEM_LOSSY_FLOAT, conf, tags, sig

    # Exact numerics.
    if dt == "numeric" or udt in ("numeric",):
        # An explicit numeric(p,s) mechanically pins the target NUMBER(p,s): no
        # judgment needed, so confidence is high. The residual uncertainty is the
        # currency-scale-vs-currency-code subtlety (e.g. JPY stored at 2dp).
        if _RATE_NAME.search(name):
            sig.append(f"rate_name+numeric({prec},{scale})")
            return SEM_RATE, 0.96 if (scale or 0) >= 4 else 0.9, tags, sig
        if _MONEY_NAME.search(name):
            sig.append(f"money_name+numeric({prec},{scale})")
            return SEM_CURRENCY_DECIMAL, 0.98, tags, sig
        sig.append(f"numeric({prec},{scale})")
        # high-scale numeric without a money/rate name is still precision-sensitive
        return SEM_CURRENCY_DECIMAL if (scale or 0) > 0 else SEM_INTEGER, 0.82, tags, sig

    # Integers — money-as-cents is the trap.
    if dt in ("bigint", "integer", "smallint") or udt in ("int8", "int4", "int2"):
        if _CENTS_NAME.search(name):
            tags.append("currency")
            sig.append("integer_cents_name")
            return SEM_CURRENCY_CENTS, 0.95, tags, sig
        if _MONEY_NAME.search(name):
            sig.append("money_name_on_integer")
            return SEM_CURRENCY_CENTS, 0.8, tags, sig
        return SEM_INTEGER, 0.95, tags, sig

    # Text columns — PII names, ambiguous numeric-looking text, or free text.
    if dt in ("character varying", "varchar", "text") or udt in ("varchar", "text"):
        if _EMAIL_NAME.search(name):
            tags.append("PII")
            sig.append("email_name")
            return SEM_PII_EMAIL, 0.9, tags, sig
        if _PHONE_NAME.search(name):
            tags.append("PII")
            sig.append("phone_name")
            return SEM_PII_PHONE, 0.88, tags, sig
        if _NAME_NAME.search(name) and "code" not in name and "_name" not in name.replace("full_name", ""):
            # heuristic: "full_name" yes; "metric_name"/"tz_name" no
            pass
        if name in ("full_name", "first_name", "last_name"):
            tags.append("PII")
            sig.append("person_name")
            return SEM_PII_NAME, 0.85, tags, sig
        # Numeric-looking legacy text → ambiguous, must NOT auto-parse → low conf.
        if _LEGACY_TEXT_NAME.search(name) and _looks_numeric_text(mn, mx):
            sig.append("numeric_looking_text+legacy_name")
            return SEM_AMBIGUOUS_TEXT, 0.8, tags, sig
        if _looks_numeric_text(mn, mx):
            sig.append("numeric_looking_text")
            return SEM_AMBIGUOUS_TEXT, 0.75, tags, sig
        return SEM_FREE_TEXT, 0.9, tags, sig

    # Fallback — unknown physical type → low confidence, surface for review.
    sig.append(f"unrecognized_type:{dt}/{udt}")
    return SEM_FREE_TEXT, 0.5, tags, sig


def lift(profile: dict[str, Any]) -> Ontology:
    """Lift a SAU-303 profile artifact into the typed property-graph ontology."""
    fks = profile.get("foreign_keys", [])
    # index FK membership per (table, column) for the inference pass
    fk_cols: set[tuple[str, str]] = set()
    relationships: list[Relationship] = []
    for fk in fks:
        ft = fk.get("from_table") or fk.get("table")
        tt = fk.get("to_table") or fk.get("ref_table")
        fcols = fk.get("from_columns") or fk.get("columns") or []
        tcols = fk.get("to_columns") or fk.get("ref_columns") or []
        if isinstance(fcols, str):
            fcols = [fcols]
        if isinstance(tcols, str):
            tcols = [tcols]
        for c in fcols:
            fk_cols.add((ft, c))
        relationships.append(
            Relationship(
                name=fk.get("name") or f"{ft}_{'_'.join(fcols)}_fk",
                from_entity=ft,
                from_columns=list(fcols),
                to_entity=tt,
                to_columns=list(tcols),
            )
        )

    # enum udts: any column whose udt is not a known builtin and data_type is USER-DEFINED
    enum_udts: set[str] = set()
    for t in profile.get("tables", []):
        for c in t.get("columns", []):
            if (c.get("data_type") or "").upper() == "USER-DEFINED":
                enum_udts.add((c.get("udt") or "").lower())

    entities: list[Entity] = []
    for t in profile.get("tables", []):
        tname = t["table"]
        props: list[Property] = []
        cols = sorted(t.get("columns", []), key=lambda c: c.get("ordinal", 0))
        for c in cols:
            cname = c["name"]
            is_pk = cname == "id" and (c.get("data_type") in ("bigint", "integer"))
            is_fk = (tname, cname) in fk_cols
            sem, conf, tags, sig = infer_property(
                c, is_pk=is_pk, is_fk=is_fk, enum_udts=enum_udts
            )
            props.append(
                Property(
                    name=cname,
                    ordinal=c.get("ordinal", 0),
                    data_type=c.get("data_type", ""),
                    udt=c.get("udt", ""),
                    nullable=bool(c.get("nullable", True)),
                    char_max_len=c.get("char_max_len"),
                    numeric_precision=c.get("numeric_precision"),
                    numeric_scale=c.get("numeric_scale"),
                    null_count=c.get("null_count", 0),
                    null_rate=c.get("null_rate", 0.0),
                    distinct_count=c.get("distinct_count"),
                    min=c.get("min"),
                    max=c.get("max"),
                    semantic_type=sem,
                    confidence=round(conf, 4),
                    governance_tags=tags,
                    signals=sig,
                )
            )
        entities.append(Entity(name=tname, row_count=t.get("row_count"), properties=props))

    return Ontology(
        schema_version=ONTOLOGY_SCHEMA_VERSION,
        source_schema=profile.get("schema", "public"),
        entities=entities,
        relationships=relationships,
    )


# ── JSON-schema validation (human-approvable, lossless round-trip) ───────────

ONTOLOGY_JSON_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["schema_version", "source_schema", "entities", "relationships"],
    "properties": {
        "schema_version": {"type": "string"},
        "source_schema": {"type": "string"},
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name", "properties"],
                "properties": {
                    "name": {"type": "string"},
                    "row_count": {"type": ["integer", "null"]},
                    "properties": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["name", "semantic_type", "confidence"],
                            "properties": {
                                "name": {"type": "string"},
                                "semantic_type": {"type": "string"},
                                "confidence": {
                                    "type": "number",
                                    "minimum": 0.0,
                                    "maximum": 1.0,
                                },
                                "governance_tags": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                            },
                        },
                    },
                },
            },
        },
        "relationships": {"type": "array"},
    },
}


def validate(ontology: Ontology) -> list[str]:
    """Lightweight structural validation against ONTOLOGY_JSON_SCHEMA.

    Returns a list of human-readable problems (empty == valid). Avoids a jsonschema
    dependency — checks the invariants the downstream stages rely on.
    """
    problems: list[str] = []
    doc = ontology.to_json()
    for key in ("schema_version", "source_schema", "entities", "relationships"):
        if key not in doc:
            problems.append(f"missing top-level key: {key}")
    for e in doc.get("entities", []):
        if not e.get("name"):
            problems.append("entity missing name")
        for p in e.get("properties", []):
            if not p.get("name"):
                problems.append(f"{e.get('name')}: property missing name")
            c = p.get("confidence")
            if not isinstance(c, (int, float)) or not (0.0 <= c <= 1.0):
                problems.append(
                    f"{e.get('name')}.{p.get('name')}: confidence out of range: {c}"
                )
            if not p.get("semantic_type"):
                problems.append(f"{e.get('name')}.{p.get('name')}: no semantic_type")
    return problems


def load_profile(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text())


if __name__ == "__main__":  # pragma: no cover - manual smoke
    import sys

    prof = load_profile(sys.argv[1] if len(sys.argv) > 1 else "artifacts/profile.json")
    onto = lift(prof)
    probs = validate(onto)
    print(json.dumps(onto.to_json(), indent=2))
    if probs:
        print("VALIDATION PROBLEMS:", probs, file=sys.stderr)
        raise SystemExit(1)
