-- ─────────────────────────────────────────────────────────────────────────────
-- Phase 1 SQL Server profiler (SAU-324) — the dialect twin of profile/profile.sql.
--
-- Defines dbo.phase0_profile(@target_schema) which PRINTs ONE JSON document with the
-- SAME shape the Postgres profiler emits, so core/ontology.lift consumes either lane
-- unchanged:
--   { schema, database, profiler_version, table_count, fk_count, tables:[...],
--     foreign_keys:[{table,column,ref_table,ref_column,constraint}] }
-- and each column carries: name, ordinal, data_type, udt, nullable, char_max_len,
-- numeric_precision, numeric_scale, null_count, null_rate, distinct_count, min, max.
--
-- ALL statistics are computed IN-DATABASE via dynamic SQL (counts, distincts, min/max
-- as text) — never raw rows; only the compact JSON leaves the server (no data egress).
-- Per-column stats run under TRY/CATCH so a single non-aggregatable column degrades
-- to null stats instead of aborting the whole profile.
--
-- Three SQL-Server-specific signals are normalised here so the dialect token table and
-- the canonical IR stay name-free:
--   * udt = 'json'  for NVARCHAR(MAX) columns guarded by an ISJSON CHECK (→ VARIANT).
--   * char_max_len = 36 synthesised for UNIQUEIDENTIFIER (→ canonical VARCHAR(36)).
--   * CHARACTER_MAXIMUM_LENGTH of -1 (the '(max)' sentinel) is reported as null.
-- DATA_TYPE 'timestamp' is left as-is: it is the ROWVERSION synonym and the dialect
-- token table maps it to BINARY — the name trap the §8 gate exists to catch.
--
-- Idempotent (CREATE OR ALTER). Target: SQL Server 2017+/2019+/2022 (FOR JSON, STRING_ESCAPE).
-- ─────────────────────────────────────────────────────────────────────────────
SET NOCOUNT ON;
GO

CREATE OR ALTER PROCEDURE dbo.phase0_profile
    @target_schema sysname = N'dbo'
AS
BEGIN
    SET NOCOUNT ON;

    -- ── Per-column metadata + computed statistics staging table. ──────────────
    IF OBJECT_ID('tempdb..#cols') IS NOT NULL DROP TABLE #cols;
    CREATE TABLE #cols (
        table_name        sysname,
        column_name       sysname,
        ordinal           int,
        data_type         sysname,
        udt               sysname,
        char_max_len      int      NULL,
        numeric_precision int      NULL,
        numeric_scale     int      NULL,
        nullable          bit,
        null_count        bigint   NULL,
        distinct_count    bigint   NULL,
        min_val           nvarchar(max) NULL,
        max_val           nvarchar(max) NULL
    );

    -- Seed the metadata rows from INFORMATION_SCHEMA, applying the three normalisations.
    INSERT INTO #cols (table_name, column_name, ordinal, data_type, udt, char_max_len,
                       numeric_precision, numeric_scale, nullable)
    SELECT
        c.TABLE_NAME,
        c.COLUMN_NAME,
        c.ORDINAL_POSITION,
        c.DATA_TYPE,
        -- udt: 'json' when an ISJSON CHECK guards the column, else the data type.
        CASE WHEN EXISTS (
                 SELECT 1
                 FROM sys.check_constraints cc
                 JOIN sys.objects o2 ON o2.object_id = cc.parent_object_id
                 JOIN sys.schemas s2 ON s2.schema_id = o2.schema_id
                 WHERE s2.name = c.TABLE_SCHEMA
                   AND o2.name = c.TABLE_NAME
                   AND cc.definition LIKE '%isjson%'
                   -- match the bracket-quoted name SQL Server stores (e.g. [metadata_json])
                   -- so 'data_json' does not false-positive inside 'metadata_json'.
                   AND CHARINDEX(QUOTENAME(c.COLUMN_NAME), cc.definition) > 0)
             THEN N'json'
             ELSE c.DATA_TYPE
        END,
        -- char_max_len: -1 ('(max)') → null; UNIQUEIDENTIFIER → 36 (canonical text width).
        CASE
            WHEN c.DATA_TYPE = 'uniqueidentifier' THEN 36
            WHEN c.CHARACTER_MAXIMUM_LENGTH = -1   THEN NULL
            ELSE c.CHARACTER_MAXIMUM_LENGTH
        END,
        c.NUMERIC_PRECISION,
        c.NUMERIC_SCALE,
        CASE WHEN c.IS_NULLABLE = 'YES' THEN 1 ELSE 0 END
    FROM INFORMATION_SCHEMA.COLUMNS c
    JOIN INFORMATION_SCHEMA.TABLES t
      ON t.TABLE_SCHEMA = c.TABLE_SCHEMA AND t.TABLE_NAME = c.TABLE_NAME
    WHERE c.TABLE_SCHEMA = @target_schema
      AND t.TABLE_TYPE = 'BASE TABLE';

    -- ── Per-column aggregate statistics via dynamic SQL (pushdown, guarded). ──
    DECLARE @tbl sysname, @col sysname, @dt sysname, @sql nvarchar(max);
    DECLARE col_cur CURSOR LOCAL FAST_FORWARD FOR
        SELECT table_name, column_name, data_type FROM #cols;
    OPEN col_cur;
    FETCH NEXT FROM col_cur INTO @tbl, @col, @dt;
    WHILE @@FETCH_STATUS = 0
    BEGIN
        DECLARE @nulls bigint = NULL, @distinct bigint = NULL,
                @mn nvarchar(max) = NULL, @mx nvarchar(max) = NULL;
        -- min/max only for ordered, text-coercible types (skip blobs / xml / json).
        DECLARE @minmax_expr nvarchar(max) =
            CASE
                WHEN @dt IN ('varbinary','binary','image','xml','text','ntext','geography','geometry')
                     THEN NULL
                WHEN @dt IN ('datetime','datetime2','smalldatetime')
                     THEN 'CONVERT(varchar(27), %s, 126)'
                WHEN @dt = 'datetimeoffset'
                     THEN 'CONVERT(varchar(34), %s, 126)'
                WHEN @dt IN ('money','smallmoney')
                     THEN 'CONVERT(varchar(64), CAST(%s AS decimal(38,4)))'
                WHEN @dt = 'uniqueidentifier'
                     THEN 'LOWER(CONVERT(varchar(36), %s))'
                ELSE 'CONVERT(nvarchar(4000), %s)'
            END;

        -- null_count is valid for ANY type; compute it on its own so a non-distinctable
        -- LOB column (text/ntext/image) does not also forfeit its null_count.
        BEGIN TRY
            SET @sql = N'SELECT @n = COUNT(*) - COUNT(' + QUOTENAME(@col) + N') ' +
                       N'FROM ' + QUOTENAME(@target_schema) + N'.' + QUOTENAME(@tbl);
            EXEC sp_executesql @sql, N'@n bigint OUTPUT', @n = @nulls OUTPUT;
        END TRY
        BEGIN CATCH
            SET @nulls = NULL;
        END CATCH
        -- COUNT(DISTINCT) is illegal on LOB types — guard it separately.
        BEGIN TRY
            SET @sql = N'SELECT @d = COUNT(DISTINCT ' + QUOTENAME(@col) + N') ' +
                       N'FROM ' + QUOTENAME(@target_schema) + N'.' + QUOTENAME(@tbl);
            EXEC sp_executesql @sql, N'@d bigint OUTPUT', @d = @distinct OUTPUT;
        END TRY
        BEGIN CATCH
            SET @distinct = NULL;
        END CATCH

        IF @minmax_expr IS NOT NULL
        BEGIN
            BEGIN TRY
                DECLARE @e nvarchar(max) = REPLACE(@minmax_expr, '%s', QUOTENAME(@col));
                SET @sql = N'SELECT @mn = MIN(' + @e + N'), @mx = MAX(' + @e + N') ' +
                           N'FROM ' + QUOTENAME(@target_schema) + N'.' + QUOTENAME(@tbl);
                EXEC sp_executesql @sql, N'@mn nvarchar(max) OUTPUT, @mx nvarchar(max) OUTPUT',
                                   @mn = @mn OUTPUT, @mx = @mx OUTPUT;
            END TRY
            BEGIN CATCH
                SET @mn = NULL; SET @mx = NULL;
            END CATCH
        END

        UPDATE #cols
           SET null_count = @nulls, distinct_count = @distinct,
               min_val = @mn, max_val = @mx
         WHERE table_name = @tbl AND column_name = @col;

        FETCH NEXT FROM col_cur INTO @tbl, @col, @dt;
    END
    CLOSE col_cur; DEALLOCATE col_cur;

    -- ── Per-table row counts. ────────────────────────────────────────────────
    IF OBJECT_ID('tempdb..#rc') IS NOT NULL DROP TABLE #rc;
    CREATE TABLE #rc (table_name sysname, row_count bigint);
    DECLARE @t sysname;
    DECLARE tab_cur CURSOR LOCAL FAST_FORWARD FOR
        SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = @target_schema AND TABLE_TYPE = 'BASE TABLE';
    OPEN tab_cur;
    FETCH NEXT FROM tab_cur INTO @t;
    WHILE @@FETCH_STATUS = 0
    BEGIN
        DECLARE @cnt bigint;
        SET @sql = N'SELECT @c = COUNT_BIG(*) FROM ' +
                   QUOTENAME(@target_schema) + N'.' + QUOTENAME(@t);
        EXEC sp_executesql @sql, N'@c bigint OUTPUT', @c = @cnt OUTPUT;
        INSERT INTO #rc VALUES (@t, @cnt);
        FETCH NEXT FROM tab_cur INTO @t;
    END
    CLOSE tab_cur; DEALLOCATE tab_cur;

    -- ── Foreign keys (relationship signals). ─────────────────────────────────
    DECLARE @fk_json nvarchar(max) = (
        SELECT
            OBJECT_NAME(fk.parent_object_id)        AS [table],
            COL_NAME(fkc.parent_object_id, fkc.parent_column_id)        AS [column],
            OBJECT_NAME(fk.referenced_object_id)    AS [ref_table],
            COL_NAME(fkc.referenced_object_id, fkc.referenced_column_id) AS [ref_column],
            fk.name                                 AS [constraint]
        FROM sys.foreign_keys fk
        JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id
        WHERE SCHEMA_NAME(fk.schema_id) = @target_schema
        ORDER BY OBJECT_NAME(fk.parent_object_id),
                 COL_NAME(fkc.parent_object_id, fkc.parent_column_id)
        FOR JSON PATH);
    SET @fk_json = ISNULL(@fk_json, N'[]');

    -- ── Assemble the tables array (columns nested via JSON_QUERY). ────────────
    DECLARE @tables_json nvarchar(max) = (
        SELECT
            rc.table_name AS [table],
            rc.row_count  AS [row_count],
            (SELECT COUNT(*) FROM #cols x WHERE x.table_name = rc.table_name) AS [column_count],
            JSON_QUERY((
                SELECT
                    c.column_name       AS [name],
                    c.ordinal           AS [ordinal],
                    c.data_type         AS [data_type],
                    c.udt               AS [udt],
                    CAST(c.nullable AS bit) AS [nullable],
                    c.char_max_len      AS [char_max_len],
                    c.numeric_precision AS [numeric_precision],
                    c.numeric_scale     AS [numeric_scale],
                    c.null_count        AS [null_count],
                    CASE WHEN rc.row_count > 0 AND c.null_count IS NOT NULL
                         THEN CAST(ROUND(1.0 * c.null_count / rc.row_count, 6) AS decimal(9,6))
                         ELSE NULL END  AS [null_rate],
                    c.distinct_count    AS [distinct_count],
                    c.min_val           AS [min],
                    c.max_val           AS [max]
                FROM #cols c
                WHERE c.table_name = rc.table_name
                ORDER BY c.ordinal
                FOR JSON PATH)) AS [columns]
        FROM #rc rc
        ORDER BY rc.table_name
        FOR JSON PATH);
    SET @tables_json = ISNULL(@tables_json, N'[]');

    -- ── Emit the single aggregate document. ──────────────────────────────────
    DECLARE @doc nvarchar(max) =
        N'{' +
        N'"schema":"'           + STRING_ESCAPE(@target_schema, 'json') + N'",' +
        N'"database":"'         + STRING_ESCAPE(DB_NAME(), 'json')      + N'",' +
        N'"profiler_version":"0.1.0",' +
        N'"table_count":'       + CAST((SELECT COUNT(*) FROM #rc) AS varchar(12)) + N',' +
        N'"fk_count":'          + CAST((SELECT COUNT(*) FROM sys.foreign_keys
                                        WHERE SCHEMA_NAME(schema_id) = @target_schema) AS varchar(12)) + N',' +
        N'"tables":'            + @tables_json + N',' +
        N'"foreign_keys":'      + @fk_json +
        N'}';

    -- Return the document as a single-row result set (NOT PRINT — PRINT truncates an
    -- nvarchar(max) to 4000 chars, which a whole-schema profile easily exceeds). sqlcmd
    -- is invoked with -h -1 -y 0 so the value streams out untruncated and header-free.
    SELECT @doc AS profile_json;
END
GO
