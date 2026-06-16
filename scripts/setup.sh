#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Phase 0 one-command bring-up (SAU-303).
# Creates the target database, applies the schema + seed, installs the profiler,
# and emits artifacts/profile.json. Reproducible: re-running fully rebuilds.
#
# Connection via standard libpq env vars (override as needed):
#   PGHOST (localhost) PGPORT (5432) PGUSER (postgres) PGPASSWORD PGDATABASE (phase0)
#
# Example (local docker Postgres on 5599):
#   PGPORT=5599 PGPASSWORD=postgres ./scripts/setup.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PGHOST="${PGHOST:-localhost}"
export PGPORT="${PGPORT:-5432}"
export PGUSER="${PGUSER:-postgres}"
export PGDATABASE="${PGDATABASE:-phase0}"

echo "[setup] target: ${PGUSER}@${PGHOST}:${PGPORT}/${PGDATABASE}"

# Create the database if absent (connect to the maintenance DB to do it).
if ! psql -X -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname = '${PGDATABASE}'" | grep -q 1; then
  echo "[setup] creating database ${PGDATABASE}"
  psql -X -d postgres -c "CREATE DATABASE \"${PGDATABASE}\""
fi

echo "[setup] applying schema"
psql -X -v ON_ERROR_STOP=1 -f "${ROOT}/schema/01_schema.sql"

echo "[setup] seeding data"
psql -X -v ON_ERROR_STOP=1 -f "${ROOT}/schema/02_seed.sql"

echo "[setup] profiling → artifacts/profile.json"
python3 "${ROOT}/profile/profile.py"

echo "[setup] done."
