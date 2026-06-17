# Connecting the loop to live Snowflake (SAU-308)

Phase 0 proved the §8 GO against a **DuckDB execution proxy** — no live Snowflake.
Phase 1 runs the *same* generated Snowflake SQL against a **real account**. This doc
is the documented, secret-stored connection the loop uses (SAU-308 acceptance).

## How the loop connects

- `core/snowflake_conn.py` — `SnowflakeConfig.from_env()` resolves credentials from
  the environment (the same libpq-style convention the SAU-303 profiler uses for
  Postgres). `connect()` opens a live connection. **No secret is read from, written
  to, or logged in the repo.**
- `scripts/verify_snowflake.py` — the connectivity probe. Exit codes: `0` connected,
  `2` not configured, `3` driver missing, `4` unreachable/auth failed.

```bash
cp .env.example .env && $EDITOR .env     # fill in real values (.env is gitignored)
pip install 'snowflake-connector-python>=3.12'   # optional live extra
python3 scripts/verify_snowflake.py      # expect: CONNECTED + LOOP-READY
```

The probe **auto-loads the repo-root `.env`** (or `SNOWFLAKE_ENV_FILE=<path>`) for any
vars not already exported — so dropping a filled-in `.env` at the repo root is the one
canonical place to land credentials; no manual `source` step is needed. Real env vars
always win over the file, so an injected secret store still takes precedence.

## Required settings

| Variable | Required | Example |
|---|---|---|
| `SNOWFLAKE_ACCOUNT` | yes | `ab12345.eu-central-1` (Account Identifier, **not** the URL) |
| `SNOWFLAKE_USER` | yes | `MIGRATION_SVC` |
| `SNOWFLAKE_PASSWORD` | yes¹ | — |
| `SNOWFLAKE_PRIVATE_KEY_PATH` | yes¹ | `/run/secrets/snowflake_key.p8` (key-pair auth, preferred for automation) |
| `SNOWFLAKE_ROLE` / `WAREHOUSE` / `DATABASE` / `SCHEMA` | no | `SYSADMIN` / `MIGRATION_WH` / `MIGRATION_SPIKE` / `PUBLIC` |

¹ Provide **either** a password **or** a private-key path.

## Where secrets live (never the repo)

In priority order, land credentials in:

1. **The run environment's secret store** (preferred) — inject `SNOWFLAKE_*` as env
   vars into the run; nothing touches disk.
2. **A local `.env` file** — gitignored (`.env`, `.env.local`, `*.pem` are all in
   `.gitignore`). Use only for local/dev runs.

Rotate by replacing the secret in the store; no code change is needed.

## Provisioning the account (the human/budget-gated step)

Account creation requires accepting Snowflake's ToS, an email-verified signup, and a
spend decision — budget/authority that sits with the board, not the loop. Recommended
path (see the linked board approval on SAU-308):

1. **Snowflake 30-day free trial** — Standard edition, ~$400 credits, **no credit card
   and zero budget**, which is ample for this spike. Choose a region close to the run
   environment; record it in the Account Identifier.
2. After activation, provision the minimal objects by running
   [`snowflake/00_provision.sql`](../snowflake/00_provision.sql) in Snowsight. It is
   **role-consistent** (`USE ROLE SYSADMIN` first) so the role the loop connects with
   actually owns/USEs the warehouse and database — creating them implicitly as
   ACCOUNTADMIN would pass connectivity but fail the live run "not authorized".
   `AUTO_SUSPEND = 60` keeps trial credit burn negligible between runs.
3. Land the credentials per **Where secrets live** above, then run
   `python3 scripts/verify_snowflake.py` and confirm exit `0`.

Done = `verify_snowflake.py` reports `CONNECTED` from the run environment. That output
is the evidence that closes SAU-308 and unblocks the live-execution run.
