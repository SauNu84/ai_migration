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
    ap.add_argument("--schema", default="public")
    ap.add_argument("--out", default=str(REPO_ROOT / "artifacts" / "profile.json"))
    args = ap.parse_args()

    # Profiler-SQL template + invocation are a dialect seam; default lane is Postgres.
    source: dl.PostgresDialect = dl.PostgresDialect()
    raw = source.run_profiler(args.schema)

    profile = json.loads(raw)
    profile["generated_at"] = datetime.now(timezone.utc).isoformat()
    profile["source"] = {
        "host": os.environ.get("PGHOST", "localhost"),
        "port": os.environ.get("PGPORT", "5432"),
        "database": profile.get("database"),
    }

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
