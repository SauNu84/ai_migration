-- ─────────────────────────────────────────────────────────────────────────────
-- Phase 0 profiler (SAU-303) — statistical profiling by SQL pushdown.
--
-- Defines phase0_profile(target_schema) → JSONB. ALL computation runs IN-DATABASE:
-- the function returns only aggregate statistics (counts, rates, distinct counts,
-- min/max as text, type metadata, FK signals) — never raw rows. That satisfies the
-- "no data egress" constraint: the orchestrator (profile.py) ships one compact JSON
-- artifact out, not table contents. Deliberately emits NO value samples so PII columns
-- never leave the database.
--
-- Per-column stats are computed with dynamic SQL wrapped in an exception guard, so a
-- single non-aggregatable column degrades gracefully (stats null + an "error" note)
-- instead of aborting the whole profile.
-- ─────────────────────────────────────────────────────────────────────────────
SET search_path TO public;

CREATE OR REPLACE FUNCTION phase0_profile(target_schema text DEFAULT 'public')
RETURNS jsonb
LANGUAGE plpgsql
AS $fn$
DECLARE
  t            record;
  c            record;
  fk_rows      jsonb;
  tables_arr   jsonb := '[]'::jsonb;
  cols_arr     jsonb;
  col_stats    jsonb;
  row_count    bigint;
  fk_count     int;
  table_count  int;
BEGIN
  -- Foreign keys (relationship signals) for the whole schema.
  SELECT coalesce(jsonb_agg(jsonb_build_object(
            'table',      tc.table_name,
            'column',     kcu.column_name,
            'ref_table',  ccu.table_name,
            'ref_column', ccu.column_name,
            'constraint', tc.constraint_name)
          ORDER BY tc.table_name, kcu.column_name), '[]'::jsonb)
    INTO fk_rows
  FROM information_schema.table_constraints tc
  JOIN information_schema.key_column_usage kcu
    ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
  JOIN information_schema.constraint_column_usage ccu
    ON ccu.constraint_name = tc.constraint_name AND ccu.table_schema = tc.table_schema
  WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = target_schema;

  fk_count := jsonb_array_length(fk_rows);

  -- Iterate base tables.
  FOR t IN
    SELECT table_name
    FROM information_schema.tables
    WHERE table_schema = target_schema AND table_type = 'BASE TABLE'
    ORDER BY table_name
  LOOP
    EXECUTE format('SELECT count(*) FROM %I.%I', target_schema, t.table_name) INTO row_count;
    cols_arr := '[]'::jsonb;

    FOR c IN
      SELECT column_name, ordinal_position, data_type, udt_name, is_nullable,
             character_maximum_length, numeric_precision, numeric_scale,
             -- Generation facts (SAU-516). Emitted as three SEPARATE catalog facts, not
             -- collapsed into one is_generated boolean: they mean different things and
             -- the dialect treats them differently (a computed column cannot be named by
             -- an INSERT; a DEFAULT-ed one can, and this seed simply did not). Collapsing
             -- them here would bake that judgement into the projection, where the layer
             -- that owns the type can no longer revisit it.
             is_identity, identity_generation, is_generated, column_default
      FROM information_schema.columns
      WHERE table_schema = target_schema AND table_name = t.table_name
      ORDER BY ordinal_position
    LOOP
      -- Per-column aggregate stats via pushdown; guard non-aggregatable types.
      BEGIN
        EXECUTE format(
          'SELECT jsonb_build_object(
             ''null_count'', count(*) FILTER (WHERE %1$I IS NULL),
             ''distinct_count'', count(DISTINCT %1$I),
             ''min'', min(%1$I)::text,
             ''max'', max(%1$I)::text)
           FROM %2$I.%3$I',
          c.column_name, target_schema, t.table_name)
        INTO col_stats;
      EXCEPTION WHEN others THEN
        -- Retry without min/max (e.g., types lacking an ordering operator).
        BEGIN
          EXECUTE format(
            'SELECT jsonb_build_object(
               ''null_count'', count(*) FILTER (WHERE %1$I IS NULL),
               ''distinct_count'', count(DISTINCT %1$I),
               ''min'', NULL, ''max'', NULL, ''note'', ''min/max unsupported'')
             FROM %2$I.%3$I',
            c.column_name, target_schema, t.table_name)
          INTO col_stats;
        EXCEPTION WHEN others THEN
          col_stats := jsonb_build_object('error', 'stats unavailable');
        END;
      END;

      cols_arr := cols_arr || jsonb_build_object(
        'name',              c.column_name,
        'ordinal',           c.ordinal_position,
        'data_type',         c.data_type,
        'udt',               c.udt_name,
        'nullable',          (c.is_nullable = 'YES'),
        'char_max_len',      c.character_maximum_length,
        'numeric_precision', c.numeric_precision,
        'numeric_scale',     c.numeric_scale,
        'is_identity',       (c.is_identity = 'YES'),
        'identity_generation', c.identity_generation,
        -- GENERATED ALWAYS AS (…) STORED — no INSERT may supply a value.
        'is_computed',       (c.is_generated = 'ALWAYS'),
        'has_default',       (c.column_default IS NOT NULL),
        'null_count',        (col_stats->>'null_count')::bigint,
        'null_rate',         CASE WHEN row_count > 0 AND col_stats ? 'null_count'
                                  THEN round(((col_stats->>'null_count')::numeric / row_count), 6)
                                  ELSE NULL END,
        'distinct_count',    (col_stats->>'distinct_count')::bigint,
        'min',               col_stats->>'min',
        'max',               col_stats->>'max',
        'stat_note',         col_stats->>'note'
      );
    END LOOP;

    tables_arr := tables_arr || jsonb_build_object(
      'table',     t.table_name,
      'row_count', row_count,
      'column_count', jsonb_array_length(cols_arr),
      'columns',   cols_arr);
  END LOOP;

  table_count := jsonb_array_length(tables_arr);

  RETURN jsonb_build_object(
    'schema',        target_schema,
    'database',      current_database(),
    'profiler_version', '0.1.0',
    'table_count',   table_count,
    'fk_count',      fk_count,
    'tables',        tables_arr,
    'foreign_keys',  fk_rows);
END;
$fn$;
