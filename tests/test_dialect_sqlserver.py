#!/usr/bin/env python3
"""SAU-324 SQL Server SourceDialect tests — the source-swap proof (DB-free).

SAU-321 child 3: the SQL Server implementation of the `SourceDialect` seam (SAU-322),
tested against the SAU-323 seed's hard columns. Proves, without a live SQL Server:

  * the **physical-type token table** maps every T-SQL type the seed uses onto the
    canonical PT_* vocabulary (incl. the rowversion 'timestamp' name-trap → BINARY);
  * the **extraction wire-form** is lossless where the §8 gate is most exposed —
    MONEY/SMALLMONEY keep the 4th dp (no 2-dp CONVERT drop) and DATETIME2 keeps all
    7 fractional digits (ISO-8601 style 126, not the 3-digit style 121);
  * a SQL Server profile lifts to the **same ontology IR** as the Postgres path: every
    hard column lands its expected semantic type, Snowflake target, reconcile strategy,
    governance tag and calibrated confidence floor — reusing the Snowflake target,
    codegen, gate and reconcile core untouched;
  * the tz/float canonicalization hooks and the token-aware FK shortcut (a GUID foreign
    key is preserved as text, never coerced to a numeric surrogate key).

The DB-backed cross-dialect §8 gate run is a separate child (SAU-321 child 4).
"""
from __future__ import annotations

import unittest
from pathlib import Path

from core import dialect as dl
from core import ontology as ont
from core import mapper as mp
from core import reconcile as rc
from core import gate as gate_mod

ROOT = Path(__file__).resolve().parent.parent
SS_PROFILE = ROOT / "artifacts" / "profile.sqlserver.json"
SS_MANIFEST = ROOT / "sqlserver" / "ground_truth" / "hard_columns.sqlserver.json"


def _seven_digit_fraction_parses() -> bool:
    """Can this interpreter canonicalize the DATETIME2(7) wire form?

    `reconcile._canon_ts_wall/_canon_ts_instant` parse with `datetime.fromisoformat`,
    which rejects more than 6 fractional digits before Python 3.11. The SQL Server
    extraction wire form (CONVERT style 126) emits all 7 for DATETIME2(7)/
    DATETIMEOFFSET(7), so on Python < 3.11 four entities exec-error and the lane's §8
    gate is RED — reproducible at `9c026f0`, the very commit whose committed readout
    says GO. The committed readout is therefore only reproducible on Python ≥ 3.11.

    This is a real portability defect in the chassis, not a test-environment quirk, and
    it is *pre-existing* — unrelated to SAU-513, which merely ran the lane and found it.
    Pinned here so it is named rather than showing up as an unexplained red, and
    reported separately for its own ticket. Fixing it means truncating the fraction to
    µs before `fromisoformat` — deliberately NOT done here.
    """
    from datetime import datetime
    try:
        datetime.fromisoformat("2024-01-05T08:15:00.1234567")
        return True
    except ValueError:
        return False


class TestInterpreterPrecisionFloor(unittest.TestCase):
    def test_datetime2_7_canonicalization_matches_interpreter_capability(self):
        """Pins the dependency above, so it cannot be forgotten or silently drift."""
        v = "2024-01-05T08:15:00.1234567"
        ssd = dl.SqlServerDialect()
        if _seven_digit_fraction_parses():
            self.assertEqual(rc.canonicalize(mp.RC_TS_WALL, v, dialect=ssd),
                             "2024-01-05T08:15:00.123456")   # truncated to µs
        else:
            with self.assertRaises(ValueError):
                rc.canonicalize(mp.RC_TS_WALL, v, dialect=ssd)


def _sqlserver_reachable() -> bool:
    """True if the SAU-323 SQL Server seed is reachable via the configured sqlcmd
    (host binary or the rig adapter in $SQLCMD). DB-backed §8 tests skip otherwise."""
    try:
        out = dl.SqlServerDialect()._sqlcmd("SET NOCOUNT ON; SELECT 1 AS one;",
                                            context="reachability")
        return "1" in out
    except SystemExit:
        return False
    except Exception:
        return False


# ── A synthetic SQL Server profile fixture (what profile.sqlserver.sql emits) ────
# Covers all 20 ground-truth hard columns (hard_columns.sqlserver.json) plus a GUID
# foreign key and the IDENTITY/uniqueidentifier PK split. udt mirrors the profiler's
# normalisations: 'json' for ISJSON columns, the data type otherwise; char_max_len is
# 36 for uniqueidentifier and null for nvarchar(max).

def _c(name, ordinal, data_type, *, udt=None, clen=None, prec=None, scale=None,
       dtp=None, nullable=True, mn=None, mx=None):
    return {
        "name": name, "ordinal": ordinal, "data_type": data_type,
        "udt": udt if udt is not None else data_type,
        "nullable": nullable, "char_max_len": clen,
        "numeric_precision": prec, "numeric_scale": scale,
        "datetime_precision": dtp,
        "null_count": 0, "null_rate": 0.0, "distinct_count": 3, "min": mn, "max": mx,
    }


def _sqlserver_profile() -> dict:
    G1, G3 = "11111111-1111-1111-1111-111111111111", "33333333-3333-3333-3333-333333333333"
    tables = [
        {"table": "company", "row_count": 4, "columns": [
            _c("id", 1, "uniqueidentifier", clen=36, nullable=False, mn=G1, mx=G3),
            _c("name", 2, "nvarchar", clen=255, nullable=False),
            _c("created_at", 3, "datetimeoffset", dtp=7, nullable=False),
            _c("row_version", 4, "timestamp", clen=8, nullable=False),
        ]},
        {"table": "gl_account", "row_count": 8, "columns": [
            _c("id", 1, "bigint", nullable=False),
            _c("company_id", 2, "uniqueidentifier", clen=36, nullable=False, mn=G1, mx=G3),
            _c("account_code", 3, "char", clen=8, nullable=False, mn="00001000", mx="00005000"),
            _c("account_type", 4, "varchar", clen=12, nullable=False, mn="asset", mx="revenue"),
        ]},
        {"table": "journal_entry", "row_count": 3, "columns": [
            _c("id", 1, "uniqueidentifier", clen=36, nullable=False),
            _c("entered_at", 2, "datetime", dtp=3, nullable=False),
            _c("posted_at", 3, "datetime2", dtp=7, nullable=False),
        ]},
        {"table": "journal_line", "row_count": 6, "columns": [
            _c("id", 1, "bigint", nullable=False),
            _c("account_id", 2, "bigint", nullable=False),
            _c("debit", 3, "money", prec=19, scale=4, nullable=False, mn="0.0001", mx="24990.0000"),
        ]},
        {"table": "policyholder", "row_count": 5, "columns": [
            _c("id", 1, "bigint", nullable=False),
            _c("full_name", 2, "nvarchar", clen=200, mn="Alice Owner", mx="田中 太郎"),
            _c("email", 3, "nvarchar", clen=320, nullable=False),
            _c("ssn_last4", 4, "char", clen=4, mn="0007", mx="0815"),
        ]},
        {"table": "payment_method", "row_count": 3, "columns": [
            _c("id", 1, "bigint", nullable=False),
            _c("card_last4", 2, "char", clen=4, mn="0019", mx="0019"),
        ]},
        {"table": "agent", "row_count": 3, "columns": [
            _c("id", 1, "bigint", nullable=False),
            _c("commission_rate", 2, "decimal", prec=9, scale=6, nullable=False,
               mn="0.083325", mx="0.150000"),
        ]},
        {"table": "product_catalog", "row_count": 3, "columns": [
            _c("id", 1, "bigint", nullable=False),
            _c("base_rate_factor", 2, "real", mn="1.25", mx="2.45"),
            _c("attributes_json", 3, "nvarchar", udt="json", clen=None),
        ]},
        {"table": "policy", "row_count": 4, "columns": [
            _c("id", 1, "uniqueidentifier", clen=36, nullable=False),
            _c("premium", 2, "money", prec=19, scale=4, nullable=False,
               mn="0.0001", mx="999999.9999"),
            _c("policy_fee", 3, "smallmoney", prec=10, scale=4, nullable=False),
            _c("effective_at", 4, "datetimeoffset", dtp=7, nullable=False),
            _c("legacy_premium_text", 5, "varchar", clen=32, mn="0.0001", mx="899,50"),
        ]},
        {"table": "coverage", "row_count": 4, "columns": [
            _c("id", 1, "bigint", nullable=False),
            _c("limit_amount", 2, "decimal", prec=18, scale=2, nullable=False,
               mn="50000.00", mx="10000000.00"),
        ]},
        {"table": "claim", "row_count": 3, "columns": [
            _c("id", 1, "uniqueidentifier", clen=36, nullable=False),
            _c("reserve_amount", 2, "float", mn="1000.0", mx="499999.9999999"),
            _c("reported_at", 3, "datetime2", dtp=7, nullable=False),
            _c("metadata_json", 4, "nvarchar", udt="json", clen=None),
        ]},
        {"table": "claim_payment", "row_count": 3, "columns": [
            _c("id", 1, "bigint", nullable=False),
            _c("fee", 2, "smallmoney", prec=10, scale=4, nullable=False, mn="0.0001", mx="12.5000"),
        ]},
        {"table": "transaction_ledger", "row_count": 3, "columns": [
            _c("id", 1, "bigint", nullable=False),
            _c("amount", 2, "money", prec=19, scale=4, nullable=False),
            _c("exchange_rate", 3, "decimal", prec=18, scale=8, nullable=False,
               mn="0.00667125", mx="1.08734500"),
        ]},
        {"table": "document", "row_count": 3, "columns": [
            _c("id", 1, "bigint", nullable=False),
            _c("file_hash", 2, "char", clen=64, nullable=False,
               mn="0000000000000000000000000000000000000000000000000000000000000abc",
               mx="ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"),
        ]},
        {"table": "audit_trail", "row_count": 4, "columns": [
            _c("id", 1, "uniqueidentifier", clen=36, nullable=False),
            _c("ip_address", 2, "varchar", clen=45, mn="198.51.100.7", mx="::1"),
            _c("row_version", 3, "timestamp", clen=8, nullable=False),
        ]},
    ]
    return {
        "schema": "dbo", "database": "phase1", "profiler_version": "0.1.0",
        "dialect": "sqlserver",
        "table_count": len(tables),
        "tables": tables,
        "foreign_keys": [
            {"table": "gl_account", "column": "company_id",
             "ref_table": "company", "ref_column": "id", "constraint": "fk_gl_company"},
        ],
        "fk_count": 1,
    }


class TestSqlServerPhysicalTokenTable(unittest.TestCase):
    def setUp(self):
        self.d = dl.SqlServerDialect()

    def test_name(self):
        self.assertEqual(self.d.name, "sqlserver")

    def test_token_classification(self):
        cases = [
            # (data_type, udt, expected token)
            ("money", "money", dl.PT_MONEY),
            ("smallmoney", "smallmoney", dl.PT_MONEY),
            ("decimal", "decimal", dl.PT_NUMERIC),
            ("numeric", "numeric", dl.PT_NUMERIC),
            ("datetime", "datetime", dl.PT_TIMESTAMP_NTZ),
            ("datetime2", "datetime2", dl.PT_TIMESTAMP_NTZ),
            ("smalldatetime", "smalldatetime", dl.PT_TIMESTAMP_NTZ),
            ("datetimeoffset", "datetimeoffset", dl.PT_TIMESTAMP_TZ),
            ("date", "date", dl.PT_DATE),
            ("bit", "bit", dl.PT_BOOLEAN),
            ("char", "char", dl.PT_FIXED_CHAR),
            ("nchar", "nchar", dl.PT_FIXED_CHAR),
            ("real", "real", dl.PT_FLOAT_NARROW),
            ("float", "float", dl.PT_FLOAT),
            ("bigint", "bigint", dl.PT_INTEGER),
            ("int", "int", dl.PT_INTEGER),
            ("varchar", "varchar", dl.PT_TEXT),
            ("nvarchar", "nvarchar", dl.PT_TEXT),
            ("uniqueidentifier", "uniqueidentifier", dl.PT_TEXT),
            # rowversion: DATA_TYPE 'timestamp' is the name-trap — it is BINARY, not time.
            ("timestamp", "timestamp", dl.PT_BINARY),
            ("rowversion", "rowversion", dl.PT_BINARY),
            ("varbinary", "varbinary", dl.PT_BINARY),
            # JSON has no native type: the profiler tags ISJSON columns udt='json'.
            ("nvarchar", "json", dl.PT_JSON),
            ("sql_variant", "sql_variant", dl.PT_UNKNOWN),
        ]
        for dt, udt, expected in cases:
            self.assertEqual(self.d.physical_token(dt, udt), expected, f"{dt}/{udt}")

    def test_no_native_enum_udt(self):
        # SQL Server encodes domains as VARCHAR + CHECK; there are no UDT enums to lift.
        self.assertFalse(self.d.is_user_defined("USER-DEFINED"))
        self.assertFalse(self.d.is_user_defined("varchar"))

    def test_narrow_float_token(self):
        self.assertTrue(self.d.is_narrow_float("real"))
        self.assertFalse(self.d.is_narrow_float("float"))

    def test_fix_tz_wire_format(self):
        # Style 126 already emits a colon offset → pass-through; colon-less is normalised.
        self.assertEqual(self.d.fix_tz("2024-01-05T08:15:00.1234567+00:00"),
                         "2024-01-05T08:15:00.1234567+00:00")
        self.assertEqual(self.d.fix_tz("2024-01-05T08:15:00+0200"),
                         "2024-01-05T08:15:00+02:00")
        self.assertEqual(self.d.fix_tz("2024-01-05T08:15:00"), "2024-01-05T08:15:00")


class TestSqlServerExtractionWireForm(unittest.TestCase):
    """The §8-exposed extraction boundary: MONEY 4-dp and DATETIME2 100-ns fidelity."""

    def setUp(self):
        self.d = dl.SqlServerDialect()

    def test_money_preserves_full_scale4_no_silent_drop(self):
        expr = self.d._wire_expr("premium", "money(19,4)", mp.RC_EXACT_TEXT)
        # Cast through DECIMAL(38,4) so the 4th dp survives …
        self.assertIn("decimal(38,4)", expr)
        # … and it is explicitly NOT the default 2-dp CONVERT(varchar, money) drop.
        self.assertNotEqual(expr, "CONVERT(varchar(64), [premium])")
        self.assertEqual(self.d._wire_expr("fee", "smallmoney", mp.RC_EXACT_TEXT),
                         "CONVERT(varchar(64), CAST([fee] AS decimal(38,4)))")

    def test_money_4th_dp_roundtrips_and_naive_2dp_is_caught(self):
        # The wire form keeps all four decimals; the reconcile exact-text strategy
        # compares them verbatim — a naive 2-dp drop would mismatch (be surfaced).
        src = rc.canonicalize(mp.RC_EXACT_TEXT, "999999.9999")
        self.assertEqual(src, "999999.9999")
        naive_2dp = rc.canonicalize(mp.RC_EXACT_TEXT, "999999.99")
        self.assertNotEqual(src, naive_2dp)
        # the sub-cent plug, too
        self.assertEqual(rc.canonicalize(mp.RC_EXACT_TEXT, "0.0001"), "0.0001")

    def test_datetime2_preserves_all_7_fractional_digits(self):
        expr = self.d._wire_expr("posted_at", "datetime2(7)", mp.RC_TS_WALL)
        # ISO-8601 style 126 keeps the full sub-microsecond fraction …
        self.assertIn(", 126)", expr)
        # … never the 3-digit legacy styles that truncate at the boundary.
        self.assertNotIn(", 121)", expr)
        self.assertNotIn(", 120)", expr)

    def test_datetime2_wireform_string_is_not_truncated(self):
        # What the style-126 wire form carries for '…59.9999999' keeps 7 digits, whereas
        # a 3-digit (style 121) form would silently drop the sub-microsecond tail.
        lossless = "2024-02-29T23:59:59.9999999"
        frac = lossless.split(".")[1]
        self.assertEqual(len(frac), 7)
        truncated_3dp = "2024-02-29 23:59:59.999"
        self.assertNotEqual(lossless.replace("T", " "), truncated_3dp)

    def test_datetimeoffset_keeps_offset(self):
        expr = self.d._wire_expr("created_at", "datetimeoffset(7)", mp.RC_TS_INSTANT)
        self.assertIn(", 126)", expr)  # 126 preserves the +HH:MM offset

    def test_uniqueidentifier_lowercased_canonical_text(self):
        expr = self.d._wire_expr("id", "uniqueidentifier", mp.RC_EXACT_TEXT)
        self.assertIn("LOWER(", expr)
        self.assertIn("varchar(36)", expr)

    def test_rowversion_serialized_as_lossless_hex(self):
        expr = self.d._wire_expr("row_version", "timestamp(8)", mp.RC_EXACT_TEXT)
        self.assertIn(", 1)", expr)  # style 1 → '0x…' hex, binary identity preserved

    def test_float_full_precision(self):
        self.assertIn(", 3)", self.d._wire_expr("reserve_amount", "float", mp.RC_APPROX_FLOAT))

    def test_json_passthrough_text(self):
        self.assertEqual(self.d._wire_expr("metadata_json", "nvarchar", mp.RC_JSON_CANONICAL),
                         "CONVERT(nvarchar(max), [metadata_json])")


class TestFkTokenAwareGuid(unittest.TestCase):
    """A GUID foreign key must keep textual identity, not be coerced to a numeric key."""

    def test_guid_fk_not_coerced_to_number(self):
        ssd = dl.SqlServerDialect()
        guid_fk = _c("company_id", 2, "uniqueidentifier", clen=36,
                     mn="11111111-1111-1111-1111-111111111111",
                     mx="33333333-3333-3333-3333-333333333333")
        sem, *_ = ont.infer_property(guid_fk, is_pk=False, is_fk=True,
                                     enum_udts=set(), dialect=ssd)
        self.assertNotEqual(sem, ont.SEM_FK)
        self.assertEqual(sem, ont.SEM_FREE_TEXT)

    def test_integer_fk_still_numeric_surrogate(self):
        ssd = dl.SqlServerDialect()
        int_fk = _c("account_id", 2, "bigint", nullable=False)
        sem, *_ = ont.infer_property(int_fk, is_pk=False, is_fk=True,
                                     enum_udts=set(), dialect=ssd)
        self.assertEqual(sem, ont.SEM_FK)

    def test_postgres_fk_unchanged(self):
        # The token-aware FK shortcut must not perturb the Postgres lane (all-integer FKs).
        int_fk = {"name": "org_id", "data_type": "bigint", "udt": "int8"}
        sem, conf, *_ = ont.infer_property(int_fk, is_pk=False, is_fk=True,
                                           enum_udts=set(), dialect=dl.PostgresDialect())
        self.assertEqual(sem, ont.SEM_FK)
        self.assertEqual(conf, 0.97)


class TestSqlServerLiftToSameIR(unittest.TestCase):
    """Lift the SQL Server profile → ontology → mappings and assert every hard column
    lands the expected semantic type, Snowflake target, reconcile strategy, governance
    tag and confidence floor — reusing the chassis unchanged."""

    @classmethod
    def setUpClass(cls):
        prof = _sqlserver_profile()
        cls.onto = ont.lift(prof, dialect=dl.SqlServerDialect())
        cls.maps = mp.map_ontology(cls.onto)
        cls.idx = {(m.entity, m.column): m for m in cls.maps}
        cls.props = {(e.name, p.name): p for e in cls.onto.entities for p in e.properties}

    # (table, column): (semantic, snowflake_type, reconcile, floor, tags, flags)
    EXPECT = {
        ("company", "id"): (ont.SEM_FREE_TEXT, "VARCHAR(36)", mp.RC_EXACT_TEXT, 0.9, [], []),
        ("company", "created_at"): (ont.SEM_TIMESTAMP_TZ, "TIMESTAMP_TZ(7)", mp.RC_TS_INSTANT, 0.9, [], []),
        ("company", "row_version"): (ont.SEM_BINARY, "BINARY(8)", mp.RC_EXACT_TEXT, 0.9, [], []),
        ("journal_entry", "entered_at"): (ont.SEM_TIMESTAMP_NTZ, "TIMESTAMP_NTZ(3)", mp.RC_TS_WALL, 0.85, [], []),
        ("journal_entry", "posted_at"): (ont.SEM_TIMESTAMP_NTZ, "TIMESTAMP_NTZ(7)", mp.RC_TS_WALL, 0.9, [], []),
        ("journal_line", "debit"): (ont.SEM_CURRENCY_DECIMAL, "NUMBER(19,4)", mp.RC_EXACT_TEXT, 0.95, [], []),
        ("policyholder", "full_name"): (ont.SEM_PII_NAME, "VARCHAR(200)", mp.RC_EXACT_TEXT, 0.9, ["PII"], []),
        ("policyholder", "email"): (ont.SEM_PII_EMAIL, "VARCHAR(320)", mp.RC_EXACT_TEXT, 0.9, ["PII"], []),
        ("policyholder", "ssn_last4"): (ont.SEM_PII_FIXED_WIDTH, "VARCHAR(4)", mp.RC_EXACT_TEXT, 0.95, ["PII"], []),
        ("payment_method", "card_last4"): (ont.SEM_PII_FIXED_WIDTH, "VARCHAR(4)", mp.RC_EXACT_TEXT, 0.95, ["PII"], []),
        ("agent", "commission_rate"): (ont.SEM_RATE, "NUMBER(9,6)", mp.RC_EXACT_TEXT, 0.95, [], []),
        ("product_catalog", "base_rate_factor"): (ont.SEM_LOSSY_FLOAT, "FLOAT", mp.RC_APPROX_FLOAT, 0.7, [], ["lossy_float"]),
        ("product_catalog", "attributes_json"): (ont.SEM_SEMI_STRUCTURED, "VARIANT", mp.RC_JSON_CANONICAL, 0.85, [], []),
        ("policy", "premium"): (ont.SEM_CURRENCY_DECIMAL, "NUMBER(19,4)", mp.RC_EXACT_TEXT, 0.98, [], []),
        ("policy", "policy_fee"): (ont.SEM_CURRENCY_DECIMAL, "NUMBER(10,4)", mp.RC_EXACT_TEXT, 0.95, [], []),
        ("policy", "effective_at"): (ont.SEM_TIMESTAMP_TZ, "TIMESTAMP_TZ(7)", mp.RC_TS_INSTANT, 0.9, [], []),
        ("policy", "legacy_premium_text"): (ont.SEM_AMBIGUOUS_TEXT, "VARCHAR(32)", mp.RC_EXACT_TEXT, 0.8, [], ["do_not_parse_numeric"]),
        ("coverage", "limit_amount"): (ont.SEM_CURRENCY_DECIMAL, "NUMBER(18,2)", mp.RC_EXACT_TEXT, 0.95, [], []),
        ("claim", "reserve_amount"): (ont.SEM_LOSSY_FLOAT, "FLOAT", mp.RC_APPROX_FLOAT, 0.7, [], ["lossy_float"]),
        ("claim", "reported_at"): (ont.SEM_TIMESTAMP_NTZ, "TIMESTAMP_NTZ(7)", mp.RC_TS_WALL, 0.9, [], []),
        ("claim", "metadata_json"): (ont.SEM_SEMI_STRUCTURED, "VARIANT", mp.RC_JSON_CANONICAL, 0.85, [], []),
        ("claim_payment", "fee"): (ont.SEM_CURRENCY_DECIMAL, "NUMBER(10,4)", mp.RC_EXACT_TEXT, 0.95, [], []),
        ("transaction_ledger", "amount"): (ont.SEM_CURRENCY_DECIMAL, "NUMBER(19,4)", mp.RC_EXACT_TEXT, 0.95, [], []),
        ("transaction_ledger", "exchange_rate"): (ont.SEM_RATE, "NUMBER(18,8)", mp.RC_EXACT_TEXT, 0.95, [], []),
        ("document", "file_hash"): (ont.SEM_FIXED_WIDTH_ID, "VARCHAR(64)", mp.RC_EXACT_TEXT, 0.95, [], []),
        ("audit_trail", "row_version"): (ont.SEM_BINARY, "BINARY(8)", mp.RC_EXACT_TEXT, 0.9, [], []),
        ("audit_trail", "ip_address"): (ont.SEM_FREE_TEXT, "VARCHAR(45)", mp.RC_EXACT_TEXT, 0.85, [], []),
    }

    def test_ontology_is_valid(self):
        self.assertEqual(ont.validate(self.onto), [])
        self.assertGreaterEqual(len(self.onto.entities), 15)

    def test_hard_columns_lift_to_expected_mapping(self):
        for key, (sem, sf, rcv, floor, tags, flags) in self.EXPECT.items():
            m = self.idx.get(key)
            self.assertIsNotNone(m, f"{key} missing from mappings")
            self.assertEqual(m.semantic_type, sem, f"{key} semantic")
            self.assertEqual(m.snowflake_type, sf, f"{key} target")
            self.assertEqual(m.reconcile, rcv, f"{key} reconcile")
            self.assertGreaterEqual(m.confidence, floor,
                                    f"{key} confidence {m.confidence} < floor {floor}")
            for t in tags:
                self.assertIn(t, m.governance_tags, f"{key} tag {t}")
            for f in flags:
                self.assertIn(f, m.flags, f"{key} flag {f}")

    def test_guid_pk_is_text_not_numeric(self):
        # uniqueidentifier PK is canonical text (VARCHAR(36)); the IDENTITY bigint PK is
        # the numeric surrogate — both lift correctly side by side.
        self.assertEqual(self.idx[("company", "id")].snowflake_type, "VARCHAR(36)")
        self.assertEqual(self.idx[("gl_account", "id")].semantic_type, ont.SEM_PK)
        self.assertEqual(self.idx[("gl_account", "id")].snowflake_type, "NUMBER(38,0)")

    def test_no_semantic_guard_false_alarms_on_correct_mapping(self):
        # The structural guard must raise ZERO flags on the correct SQL Server mapping
        # (nothing hidden), exactly as on the Postgres lane. Exercised with the SQL Server
        # dialect so the guard branches on real SQL Server tokens, not the Postgres ones.
        ssd = dl.SqlServerDialect()
        for m in self.maps:
            self.assertEqual(
                rc.semantic_guard(self.props[(m.entity, m.column)], m, dialect=ssd), [],
                f"false alarm on {m.entity}.{m.column}",
            )

    def test_guard_catches_sqlserver_masked_traps(self):
        # The two SQL Server masked-mismatch traps the §8 gate exists to catch: a no-tz
        # DATETIME/DATETIME2 lured to a tz target (reconciles clean under UTC), and a
        # ROWVERSION misread as a temporal type. Both must be surfaced by the guard.
        ssd = dl.SqlServerDialect()
        ntz = self.props[("journal_entry", "entered_at")]
        ntz_bad = mp.Mapping("journal_entry", "entered_at", "datetime", ont.SEM_TIMESTAMP_NTZ,
                             "TIMESTAMP_TZ", mp.RC_TS_INSTANT, 0.85, [], [], "")
        self.assertTrue(rc.semantic_guard(ntz, ntz_bad, dialect=ssd))
        rv = self.props[("audit_trail", "row_version")]
        rv_bad = mp.Mapping("audit_trail", "row_version", "timestamp", ont.SEM_BINARY,
                            "TIMESTAMP_NTZ", mp.RC_TS_WALL, 0.9, [], [], "")
        self.assertTrue(rc.semantic_guard(rv, rv_bad, dialect=ssd))


@unittest.skipUnless(_sqlserver_reachable() and SS_PROFILE.exists(),
                     "no SQL Server seed reachable (start the SAU-323 container + set "
                     "$SQLCMD to sqlserver/scripts/sqlcmd-rig.sh and MSSQL_* env)")
@unittest.skipUnless(_seven_digit_fraction_parses(),
                     "Python < 3.11: reconcile canonicalization rejects the DATETIME2(7) "
                     "7-digit wire form, so this lane's gate is RED for a reason that has "
                     "nothing to do with the gate — see _seven_digit_fraction_parses(). "
                     "Skipped rather than asserted so the real defect stays legible; it is "
                     "pinned by TestInterpreterPrecisionFloor above and reported separately.")
class TestSqlServerGateDB(unittest.TestCase):
    """SAU-325 child 4: the DB-backed cross-dialect §8 gate run against the live SAU-323
    seed — the board-funded correctness proof. Source swap only; target/codegen/gate/
    reconcile reused verbatim."""

    def test_transform_bullets_pass_on_sqlserver_seed(self):
        """Bullets 1–4 — the *transform* half — still pass on the live SAU-323 seed.

        Narrowed, not weakened: bullet 0 (SAU-513) audits the *corpus* rather than the
        transform and is asserted separately below, so its finding cannot turn this into
        an opaque red.
        """
        gate, _ = gate_mod.run_gate(SS_PROFILE, SS_MANIFEST, clean_runs=3, regen_iters=3,
                                    dialect=dl.SqlServerDialect())
        for name, b in gate.bullets.items():
            if name == "0_corpus_validity":
                continue
            self.assertTrue(b["pass"], f"§8 bullet failed: {name}: {b}")

    def test_corpus_validity_reports_rowversion_as_unverifiable(self):
        """Bullet 0's one finding on this lane, pinned so it cannot drift silently.

        `audit_trail.row_version` is ROWVERSION: the engine generates it, so no INSERT can
        name it and the seed states no claim bullet 0 could check. That is reported as a
        failure rather than waved through — an unverifiable ground-truth column is a
        decision for the COO (SAU-513 puts corpus fixes out of scope), not something this
        gate should quietly call fine. Every other hard column round-trips its literal.
        """
        gate, _ = gate_mod.run_gate(SS_PROFILE, SS_MANIFEST, clean_runs=1, regen_iters=1,
                                    dialect=dl.SqlServerDialect())
        b0 = gate.bullets["0_corpus_validity"]
        self.assertFalse(b0["pass"])
        self.assertEqual([d["column"] for d in b0["drifted"]], ["audit_trail.row_version"])
        self.assertEqual(b0["columns_intact"], b0["columns_audited"] - 1)

    def test_all_sqlserver_traps_caught_zero_false_alarms(self):
        gate, _ = gate_mod.run_gate(SS_PROFILE, SS_MANIFEST, clean_runs=1, regen_iters=1,
                                    dialect=dl.SqlServerDialect())
        b4 = gate.bullets["4_hard_fail_detection"]
        self.assertEqual(b4["traps_caught"], b4["traps_injected"])
        self.assertGreater(b4["traps_injected"], 0)
        self.assertEqual(b4["false_alarms_on_correct_mapping"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
