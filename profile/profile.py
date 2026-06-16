#!/usr/bin/env python3
"""Phase 0 profiler orchestrator (SAU-303).

Runs the in-database `phase0_profile()` function (profile/profile.sql) and writes the
returned aggregate JSON to `artifacts/profile.json`. All statistics are computed in
Postgres via SQL pushdown; this script only ships the compact JSON artifact out — no
table rows leave the database (no data egress).

Connection comes from the standard libpq environment variables (PGHOST, PGPORT,
PGUSER, PGPASSWORD, PGDATABASE), which `psql` reads natively. Stdlib only — no driver
dependency — so it runs anywhere `psql` is on PATH.

Usage:
    python3 profile/profile.py [--schema public] [--out artifacts/profile.json]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def run_psql(sql: str) -> str:
    """Execute SQL via psql and return stdout (tuples-only, unaligned)."""
    cmd = ["psql", "-X", "-tA", "-v", "ON_ERROR_STOP=1", "-c", sql]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise SystemExit(f"psql failed (exit {proc.returncode})")
    return proc.stdout.strip()


def main() -> int:
    ap = argparse.ArgumentParser(description="Phase 0 Postgres profiler")
    ap.add_argument("--schema", default="public")
    ap.add_argument("--out", default=str(REPO_ROOT / "artifacts" / "profile.json"))
    args = ap.parse_args()

    # Ensure the profiling function is installed (idempotent), then invoke it.
    profile_sql = (REPO_ROOT / "profile" / "profile.sql").read_text()
    run_psql(profile_sql)
    raw = run_psql(f"SELECT phase0_profile('{args.schema}')")

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
