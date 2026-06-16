#!/usr/bin/env python3
"""SAU-308 connectivity check — confirm the live Snowflake account is reachable.

This is the acceptance probe for SAU-308: it resolves credentials from the
environment (never the repo), opens a connection, and runs a tiny read-only round
trip (`CURRENT_VERSION/ACCOUNT/REGION/WAREHOUSE`). It prints a redacted summary and
exits:

    0  connected and the round trip succeeded
    2  required settings missing from the environment   (not yet provisioned)
    3  driver not installed                             (pip install the live extra)
    4  settings present but the account is unreachable / auth failed

Usage (settings come from the secret store / env, e.g. a sourced .env):
    python3 scripts/verify_snowflake.py

No secret value is printed by this script.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.snowflake_conn import SnowflakeConfig, SnowflakeConfigError, connect  # noqa: E402


def main() -> int:
    try:
        cfg = SnowflakeConfig.from_env()
    except SnowflakeConfigError as exc:
        print(f"[verify_snowflake] NOT CONFIGURED: {exc}", file=sys.stderr)
        print("[verify_snowflake] See docs/CONNECTING_SNOWFLAKE.md to land credentials.",
              file=sys.stderr)
        return 2

    print(f"[verify_snowflake] settings: {cfg.redacted()}")

    try:
        conn = connect(cfg)
    except RuntimeError as exc:  # driver missing
        print(f"[verify_snowflake] {exc}", file=sys.stderr)
        return 3
    except Exception as exc:  # noqa: BLE001 - any connect/auth failure is the result
        print(f"[verify_snowflake] UNREACHABLE: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 4

    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT CURRENT_VERSION(), CURRENT_ACCOUNT(), CURRENT_REGION(), "
            "CURRENT_WAREHOUSE(), CURRENT_ROLE()"
        )
        version, account, region, warehouse, role = cur.fetchone()
        print("[verify_snowflake] CONNECTED")
        print(f"  snowflake_version : {version}")
        print(f"  account           : {account}")
        print(f"  region            : {region}")
        print(f"  warehouse         : {warehouse}")
        print(f"  role              : {role}")
    except Exception as exc:  # noqa: BLE001
        print(f"[verify_snowflake] CONNECTED but query failed: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 4

    # Loop-readiness: a green auth check is not enough — the configured role must be
    # able to USE the warehouse/database the medallion runs against. This catches the
    # common gotcha where objects created as ACCOUNTADMIN aren't visible to SYSADMIN,
    # which would otherwise fail mid-run *after* a passing connectivity check.
    try:
        if cfg.warehouse:
            cur.execute(f'USE WAREHOUSE "{cfg.warehouse}"')
        if cfg.database:
            cur.execute(f'USE DATABASE "{cfg.database}"')
        if cfg.warehouse or cfg.database:
            print("[verify_snowflake] LOOP-READY: configured warehouse/database are usable")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[verify_snowflake] CONNECTED but warehouse/database NOT usable by role "
              f"'{role}': {type(exc).__name__}: {exc}\n"
              f"  Fix: provision objects as the connecting role — run "
              f"snowflake/00_provision.sql (USE ROLE SYSADMIN).", file=sys.stderr)
        return 4
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
