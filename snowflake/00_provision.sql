-- SAU-308 — Snowflake spike provisioning (copy-paste into Snowsight → Worksheets).
--
-- Role-consistent on purpose: the loop connects with SNOWFLAKE_ROLE=SYSADMIN, so we
-- create the objects AS SYSADMIN. Creating them implicitly as ACCOUNTADMIN (the
-- Snowsight default role) would leave SYSADMIN without USAGE — the live run would then
-- fail "object does not exist or not authorized" AFTER a green connectivity check.
-- USE ROLE SYSADMIN up front avoids that. SYSADMIN exists on every account by default.

USE ROLE SYSADMIN;

CREATE WAREHOUSE IF NOT EXISTS MIGRATION_WH
  WAREHOUSE_SIZE = 'XSMALL'
  AUTO_SUSPEND = 60          -- suspend after 60s idle → negligible trial-credit burn
  AUTO_RESUME = TRUE
  INITIALLY_SUSPENDED = TRUE;

CREATE DATABASE IF NOT EXISTS MIGRATION_SPIKE;

-- Sanity: confirm this role can actually USE both (mirrors verify_snowflake.py).
USE WAREHOUSE MIGRATION_WH;
USE DATABASE MIGRATION_SPIKE;
SELECT CURRENT_ROLE() AS role, CURRENT_WAREHOUSE() AS warehouse, CURRENT_DATABASE() AS database;
