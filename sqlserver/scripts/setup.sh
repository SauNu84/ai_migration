#!/usr/bin/env bash
# =============================================================================
# Phase 1 SQL Server one-command bring-up (SAU-323) — analogue of the Postgres
# scripts/setup.sh. Starts the containerized SQL Server, waits until it accepts
# connections, creates the target database, and applies the schema + seed.
# Reproducible: re-running drops and rebuilds the schema, reseeds.
#
# Connection (override as needed):
#   MSSQL_HOST       (localhost)          host the server is reachable on
#   MSSQL_HOST_PORT  (14333)              published host port -> container 1433
#   MSSQL_SA_PASSWORD(Phase1_Str0ng!Pass) SA password (dev default; override in prod)
#   MSSQL_DATABASE   (phase1)             target database name
#
# sqlcmd resolution (in order):
#   1. $SQLCMD if set, or a `sqlcmd` already on PATH (uses MSSQL_HOST:MSSQL_HOST_PORT)
#   2. otherwise the sqlcmd bundled in the running server container, via docker exec
#      (the 2022 image ships /opt/mssql-tools18/bin/sqlcmd) — no extra image needed
#
# Usage:
#   ./sqlserver/scripts/setup.sh            # compose up + schema + seed + verify
#   NO_COMPOSE=1 ./sqlserver/scripts/setup.sh   # against an already-running server
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${ROOT}/docker-compose.yml"
CONTAINER="${MSSQL_CONTAINER:-sau323-mssql}"

export MSSQL_HOST="${MSSQL_HOST:-localhost}"
export MSSQL_HOST_PORT="${MSSQL_HOST_PORT:-14333}"
export MSSQL_SA_PASSWORD="${MSSQL_SA_PASSWORD:-Phase1_Str0ng!Pass}"
export MSSQL_DATABASE="${MSSQL_DATABASE:-phase1}"

# ── Pick a docker compose invocation (v2 plugin or legacy v1). ───────────────
if docker compose version >/dev/null 2>&1; then
  DC=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  DC=(docker-compose)
else
  DC=()
fi

# ── sqlcmd runner: emits an argv that runs sqlcmd against the server. ────────
# Two modes; both target the same instance. The container mode shares the
# server's net namespace so -S localhost,1433 works without a published port.
sqlcmd_mode=""
if [[ -n "${SQLCMD:-}" ]] || command -v sqlcmd >/dev/null 2>&1; then
  sqlcmd_mode="host"
  SQLCMD_BIN="${SQLCMD:-sqlcmd}"
else
  sqlcmd_mode="container"
fi
# Path to sqlcmd inside the server container (2022 ships tools18; older ships tools).
CONTAINER_SQLCMD="${CONTAINER_SQLCMD:-/opt/mssql-tools18/bin/sqlcmd}"

# run_sql_query "<tsql>"  → executes inline T-SQL, fails on error (-b).
run_sql_query() {
  local q="$1"
  if [[ "$sqlcmd_mode" == "host" ]]; then
    "$SQLCMD_BIN" -S "${MSSQL_HOST},${MSSQL_HOST_PORT}" -U sa -P "$MSSQL_SA_PASSWORD" -C -b -Q "$q"
  else
    docker exec "$CONTAINER" "$CONTAINER_SQLCMD" \
      -S localhost -U sa -P "$MSSQL_SA_PASSWORD" -C -b -Q "$q"
  fi
}

# run_sql_file "<path>" "<db>"  → executes a .sql file against <db>.
run_sql_file() {
  local f="$1" db="$2"
  if [[ "$sqlcmd_mode" == "host" ]]; then
    "$SQLCMD_BIN" -S "${MSSQL_HOST},${MSSQL_HOST_PORT}" -U sa -P "$MSSQL_SA_PASSWORD" -C -b -d "$db" -i "$f"
  else
    # Feed the .sql via stdin so no bind-mount/copy is needed (sqlcmd reads GO batches from stdin).
    docker exec -i "$CONTAINER" "$CONTAINER_SQLCMD" \
      -S localhost -U sa -P "$MSSQL_SA_PASSWORD" -C -b -d "$db" < "$f"
  fi
}

echo "[setup] sqlcmd mode: ${sqlcmd_mode}; target: sa@${MSSQL_HOST}:${MSSQL_HOST_PORT}/${MSSQL_DATABASE}"

# ── Start the container (unless told to use an external server). ─────────────
if [[ -z "${NO_COMPOSE:-}" ]]; then
  if [[ ${#DC[@]} -eq 0 ]]; then
    echo "[setup] ERROR: docker compose not found. Install Docker, or run with NO_COMPOSE=1 against an existing server." >&2
    exit 1
  fi
  echo "[setup] starting SQL Server container"
  "${DC[@]}" -f "$COMPOSE_FILE" up -d
fi

# ── Wait until the instance answers SELECT 1 (real readiness, not just TCP). ─
echo "[setup] waiting for SQL Server to accept connections"
ready=""
for i in $(seq 1 40); do
  if run_sql_query "SELECT 1" >/dev/null 2>&1; then ready=1; break; fi
  sleep 3
done
if [[ -z "$ready" ]]; then
  echo "[setup] ERROR: SQL Server did not become ready in time." >&2
  exit 1
fi

# ── Create the database if absent (idempotent). ──────────────────────────────
echo "[setup] ensuring database ${MSSQL_DATABASE}"
run_sql_query "IF DB_ID('${MSSQL_DATABASE}') IS NULL CREATE DATABASE [${MSSQL_DATABASE}];"

echo "[setup] applying schema"
run_sql_file "${ROOT}/schema/01_schema.sql" "$MSSQL_DATABASE"

echo "[setup] seeding data"
run_sql_file "${ROOT}/schema/02_seed.sql" "$MSSQL_DATABASE"

# ── Sanity readout: table + row counts prove the seed landed. ────────────────
echo "[setup] verifying"
run_sql_query "USE [${MSSQL_DATABASE}];
SET NOCOUNT ON;
SELECT 'tables' AS metric, COUNT(*) AS n FROM sys.tables WHERE schema_id = SCHEMA_ID('dbo')
UNION ALL SELECT 'companies',    COUNT(*) FROM dbo.company
UNION ALL SELECT 'policies',     COUNT(*) FROM dbo.policy
UNION ALL SELECT 'claims',       COUNT(*) FROM dbo.claim
UNION ALL SELECT 'journal_lines',COUNT(*) FROM dbo.journal_line;"

echo "[setup] done. Source ready at ${MSSQL_HOST},${MSSQL_HOST_PORT} (db: ${MSSQL_DATABASE})."
