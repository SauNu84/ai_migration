#!/usr/bin/env bash
# =============================================================================
# Rig adapter (SAU-325): route the SqlServerDialect's host `sqlcmd` invocation
# into the bundled sqlcmd inside the running SQL Server container.
#
# The §8 gate runs the source-swap lane against the SAU-323 seed in the
# containerized SQL Server (the proven target rig). The host has no sqlcmd
# installed, but the official mssql/server:2022 image ships one at
# /opt/mssql-tools18/bin/sqlcmd. This shim forwards the dialect's exact argv to
# that binary via `docker exec`, so the dialect code stays untouched.
#
# Point the dialect at it with:
#   export SQLCMD="$PWD/sqlserver/scripts/sqlcmd-rig.sh"
#   export MSSQL_HOST=localhost MSSQL_HOST_PORT=1433   # container-internal port
#   export MSSQL_SA_PASSWORD=... MSSQL_DATABASE=phase1 MSSQL_USER=sa
#
# Note MSSQL_HOST_PORT=1433: the dialect builds `-S <host>,<port>`, and inside
# the container the server listens on 1433 (the published 14333 is host-side).
# =============================================================================
exec docker exec -i "${MSSQL_CONTAINER:-sau323-mssql}" \
  "${CONTAINER_SQLCMD:-/opt/mssql-tools18/bin/sqlcmd}" "$@"
