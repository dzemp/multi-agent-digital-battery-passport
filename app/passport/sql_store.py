"""Azure SQL Database store. The passport tables are SQL ledger tables (see ../sql/schema.sql).

* Each write transaction first takes one application lock, so commands run one after another.
  That keeps the rules simple (for example "only one open review per lot") at MVP volumes.
* Reads use a separate connection without the lock.
* The app signs in with its managed identity (no password). On a PC, az login is used instead.
* A serverless database that is paused answers "not currently available" for up to about a
  minute while it resumes, so opening a connection is retried.
"""

import time
from contextlib import contextmanager
from datetime import datetime, timezone

from .store import (
    IDENTITY, MAX_ATTEMPTS, TABLES, ConflictError, F, StoreError, T, check_key, check_row, format_utc, key_of,
)

TRANSIENT = ("40613", "40197", "40501", "49918", "49919", "49920", "not currently available",
             "Login timeout expired", "TCP Provider", "Communication link failure")
RETRY_DELAYS = (2, 4, 8, 15, 30)
APP_LOCK = """DECLARE @r int;
EXEC @r = sp_getapplock @Resource = 'dbp-commands', @LockMode = 'Exclusive', @LockOwner = 'Transaction',
    @LockTimeout = 15000;
IF @r < 0 THROW 50001, 'Could not get the dbp-commands application lock.', 1;"""


def connection_string(server, database):
    host = server if "." in server else f"{server}.database.windows.net"
    return (f"Server=tcp:{host},1433;Database={database};Encrypt=yes;TrustServerCertificate=no;"
            "ConnectRetryCount=3")


def to_sql(kind, value):
    if value is None:
        return None
    if kind == T:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)
    return value


def from_sql(kind, value):
    if value is None:
        return None
    if kind == T:
        return format_utc(value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value)
    if kind == F:
        return float(value)
    return value


def _row(table, columns, values):
    types = TABLES[table][2]
    return {c: from_sql(types[c], v) for c, v in zip(columns, values, strict=True)}


class SqlTx:
    def __init__(self, cursor):
        self.cursor = cursor

    def insert(self, table, row):
        import mssql_python

        check_row(table, row)
        types = TABLES[table][2]
        columns = [c for c in row if c != IDENTITY.get(table)]
        sql = f"INSERT INTO dbp.{table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})"
        try:
            self.cursor.execute(sql, [to_sql(types[c], row[c]) for c in columns])
        except mssql_python.IntegrityError as exc:
            raise ConflictError(f"{table}: key {key_of(table, row)} exists") from exc

    def get(self, table, *key):
        check_key(table, key)
        rows = self.select(table, **dict(zip(TABLES[table][1], key, strict=True)))
        return rows[0] if rows else None

    def select(self, table, **where):
        mode, keys, types = TABLES[table]
        columns = list(types)
        unknown = set(where) - set(columns)
        if unknown:
            raise StoreError(f"{table}: unknown filter {sorted(unknown)}")
        sql = f"SELECT {', '.join(columns)} FROM dbp.{table}"
        if where:
            sql += " WHERE " + " AND ".join(f"{c} = ?" for c in where)
        sql += " ORDER BY " + ", ".join(keys)
        self.cursor.execute(sql, [to_sql(types[c], v) for c, v in where.items()])
        return [_row(table, columns, r) for r in self.cursor.fetchall()]

    def update(self, table, row, expected_row_version):
        mode, keys, types = TABLES[table]
        if mode != "updatable":
            raise StoreError(f"{table} is {mode}; updates are not allowed")
        check_row(table, row)
        changes = [c for c in row if c not in keys and c != "row_version"]
        sql = (f"UPDATE dbp.{table} SET {', '.join(f'{c} = ?' for c in changes)}, row_version = row_version + 1 "
               f"WHERE {' AND '.join(f'{k} = ?' for k in keys)} AND row_version = ?")
        params = [to_sql(types[c], row[c]) for c in changes] + [row[k] for k in keys] + [expected_row_version]
        self.cursor.execute(sql, params)
        if self.cursor.rowcount != 1:
            raise ConflictError(f"{table} {key_of(table, row)}: expected row_version {expected_row_version}")


class SqlStore:
    kind = "sql"

    def __init__(self, server, database, client_id="", credential=None):
        self.connection_string = connection_string(server, database)
        if credential is None:
            from azure.identity import DefaultAzureCredential, ManagedIdentityCredential

            credential = ManagedIdentityCredential(client_id=client_id) if client_id else \
                DefaultAzureCredential(exclude_interactive_browser_credential=True)
        self.credential = credential

    def _connect(self, autocommit):
        import mssql_python

        for delay in (*RETRY_DELAYS, None):
            try:
                return mssql_python.connect(self.connection_string, autocommit=autocommit,
                                            token_provider=self.credential)
            except mssql_python.Error as exc:
                if delay is None or not any(marker in str(exc) for marker in TRANSIENT):
                    raise
                time.sleep(delay)

    @contextmanager
    def transaction(self):
        connection = self._connect(autocommit=False)
        try:
            cursor = connection.cursor()
            cursor.execute(APP_LOCK)
            yield SqlTx(cursor)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def read(self):
        connection = self._connect(autocommit=True)
        try:
            yield SqlTx(connection.cursor())
        finally:
            connection.close()

    def ping(self):
        with self.read() as tx:
            tx.cursor.execute("SELECT 1")
            tx.cursor.fetchall()
        return True

    def schema_version(self):
        """The version recorded in dbp.SchemaInfo, or None for a database without it (an older schema)."""
        with self.read() as tx:
            tx.cursor.execute("SELECT OBJECT_ID(N'dbp.SchemaInfo')")
            if tx.cursor.fetchall()[0][0] is None:
                return None
            row = tx.get("SchemaInfo", "dbp")
        return row["schema_version"] if row else None

    def claim_outbox(self, lease_seconds=60):
        sql = """WITH next_item AS (
    SELECT TOP (1) * FROM dbp.Outbox WITH (UPDLOCK, READPAST, ROWLOCK)
    WHERE status = 'pending' OR (status = 'processing' AND lease_until < SYSUTCDATETIME())
    ORDER BY outbox_id)
UPDATE next_item SET status = 'processing', attempts = attempts + 1,
    lease_until = DATEADD(second, ?, SYSUTCDATETIME())
OUTPUT inserted.outbox_id, inserted.kind, inserted.subject, inserted.payload, inserted.status,
    inserted.attempts, inserted.lease_until, inserted.last_error, inserted.created_utc, inserted.done_utc;"""
        connection = self._connect(autocommit=False)
        try:
            cursor = connection.cursor()
            cursor.execute(sql, [lease_seconds])
            row = cursor.fetchone()
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        return _row("Outbox", list(TABLES["Outbox"][2]), row) if row else None

    def finish_outbox(self, outbox_id, error=None):
        connection = self._connect(autocommit=True)
        try:
            cursor = connection.cursor()
            if error is None:
                cursor.execute("UPDATE dbp.Outbox SET status = 'done', done_utc = SYSUTCDATETIME(), "
                               "last_error = NULL WHERE outbox_id = ?", [outbox_id])
            else:
                cursor.execute("UPDATE dbp.Outbox SET status = CASE WHEN attempts >= ? THEN 'failed' "
                               "ELSE 'pending' END, last_error = ?, lease_until = NULL WHERE outbox_id = ?",
                               [MAX_ATTEMPTS, str(error)[:1000], outbox_id])
        finally:
            connection.close()
