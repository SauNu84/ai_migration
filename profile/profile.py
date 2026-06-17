#!/usr/bin/env python3
"""Phase 0 profiler orchestrator (SAU-303).

Runs the source dialect's in-database profiling routine and writes the returned
aggregate JSON to `artifacts/profile.json`. All statistics are computed in-database via
SQL pushdown; this script only ships the compact JSON artifact out — no table rows leave
the database (no data egress).

The profiler-SQL template and its invocation are a source dialect seam (SAU-322): the
default Postgres lane reads `profile/profile.sql` and runs it via psql with the standard
libpq environment variables (PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE).

Usage:
    python3 profile/profile.py [--schema public] [--out artifacts/profile.json]
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

import sys
sys.path.insert(0, str(REPO_ROOT))
from core import dialect as dl  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Phase 0 source profiler")
    ap.add_argument("--source", choices=("postgres", "sqlserver"), default="postgres",
                    help="source dialect lane (SAU-322 seam)")
    ap.add_argument("--schema", default=None,
                    help="schema to profile (default: 'public' for postgres, 'dbo' for sqlserver)")
    ap.add_argument("--out", default=str(REPO_ROOT / "artifacts" / "profile.json"))
    args = ap.parse_args()

    # Profiler-SQL template + invocation are a dialect seam (SAU-322).
    if args.source == "sqlserver":
        source: dl.SourceDialect = dl.SqlServerDialect()
        schema = args.schema or "dbo"
        raw = source.run_profiler(schema)
        src_meta = {
            "host": os.environ.get("MSSQL_HOST", "localhost"),
            "port": os.environ.get("MSSQL_HOST_PORT", "14333"),
        }
    else:
        source = dl.PostgresDialect()
        schema = args.schema or "public"
        raw = source.run_profiler(schema)
        src_meta = {
            "host": os.environ.get("PGHOST", "localhost"),
            "port": os.environ.get("PGPORT", "5432"),
        }

    profile = json.loads(raw)
    profile["generated_at"] = datetime.now(timezone.utc).isoformat()
    profile["dialect"] = source.name
    profile["source"] = {**src_meta, "database": profile.get("database")}

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(profile, indent=2, sort_keys=False) + "\n")

    print(
        f"[profile] {profile['table_count']} tables, "
        f"{profile['fk_count']} FKs → {out_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
