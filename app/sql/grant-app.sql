-- Lets the app's managed identity read and write passport rows, and nothing else.
-- deploy.py replaces __APP_IDENTITY__ (identity name) and __APP_CLIENT_ID__ (its client ID).
-- Run as the Microsoft Entra admin after schema.sql. deploy.py appends one GRANT per dbp table, generated from the
-- app's table manifest (passport/store.py): SELECT, INSERT on append-only ledger tables; SELECT, INSERT, UPDATE on
-- updatable ledger and ordinary tables. No DDL, no DELETE and nothing on schema report.

IF DATABASE_PRINCIPAL_ID(N'__APP_IDENTITY__') IS NULL
BEGIN
    BEGIN TRY
        EXEC (N'CREATE USER [__APP_IDENTITY__] FROM EXTERNAL PROVIDER');
    END TRY
    BEGIN CATCH
        -- Fallback when the directory lookup is not allowed: create the user from the client ID.
        DECLARE @sid varchar(40) = CONVERT(varchar(40), CAST(CAST(N'__APP_CLIENT_ID__' AS uniqueidentifier) AS varbinary(16)), 1);
        EXEC (N'CREATE USER [__APP_IDENTITY__] WITH SID = ' + @sid + N', TYPE = E');
    END CATCH
END
GO
-- Read-only: lets the trail screen run sys.sp_verify_database_ledger and read ledger transaction blocks.
GRANT VIEW LEDGER CONTENT TO [__APP_IDENTITY__];
GO
