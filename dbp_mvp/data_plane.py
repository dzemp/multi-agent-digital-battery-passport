"""Azure SQL and the ledger digest store, reached only through their private endpoints from the developer VM.

The operator (the SQL Microsoft Entra admin) creates the passport schema and the app identity's database user, then
anchors a ledger digest in the digest storage account and has SQL verify the ledger against every anchored digest.
No firewall rule or public access is ever enabled. Azure packages are imported only when a connection is opened.
"""

import importlib
import json
import re
import sys
import time
import uuid
from contextlib import contextmanager

from . import MvpError
from .config import ROOT

APP = ROOT / "app"
SCHEMA = APP / "sql" / "schema.sql"
GRANTS = APP / "sql" / "grant-app.sql"
GO = re.compile(r"^\s*GO\s*$", re.MULTILINE | re.IGNORECASE)
IDENTITY_NAME = re.compile(r"[A-Za-z0-9-]{3,128}")
BLOCKED_IP = re.compile(r"Client with IP address '([0-9a-fA-F:.]+)' is not allowed")
GENERATE_SQL = "EXEC sys.sp_generate_database_ledger_digest"
TABLES_SQL = ("SELECT t.name, t.ledger_type_desc FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id "
              "WHERE s.name = N'dbp' ORDER BY t.name")
DIGEST_FIELDS = {"database_name", "block_id", "hash"}
FORBIDDEN = ("AuthorizationPermissionMismatch", "AuthorizationFailure")
CONNECT_ATTEMPTS = 10
BLOB_ATTEMPTS = 10


def app_module(name):
    """A module of the passport app (app/passport), which owns the connection string and the digest location."""
    if str(APP) not in sys.path:
        sys.path.insert(0, str(APP))
    return importlib.import_module(f"passport.{name}")


def digest_prefix(connection, outputs):
    """manual/<server>/<database>/<incarnation>/: the app's ledger check reads the same prefix."""
    return app_module("ledger").digest_prefix(outputs["sqlServerName"], outputs["databaseName"],
                                              incarnation(connection))


def sql_batches(script):
    """Split a T-SQL script on GO lines and drop batches that hold only comments."""
    batches = []
    for batch in GO.split(script):
        code = "\n".join(line for line in batch.splitlines() if not line.strip().startswith("--")).strip()
        if code:
            batches.append(batch.strip())
    return batches


def grant_script(identity_name, client_id):
    if not IDENTITY_NAME.fullmatch(identity_name or ""):
        raise MvpError(f"Unexpected app identity name: {identity_name!r}")
    try:
        client_id = str(uuid.UUID(client_id))
    except (TypeError, ValueError) as error:
        raise MvpError(f"Unexpected app identity client ID: {client_id!r}") from error
    text = GRANTS.read_text(encoding="utf-8").replace("__APP_IDENTITY__", identity_name)
    text = text.replace("__APP_CLIENT_ID__", client_id).rstrip() + "\n"
    statements = app_module("store").grant_statements(identity_name)
    return text + "\n-- Per-table grants generated from the app's table manifest (passport/store.py TABLES).\n" + \
        "\n".join(statements) + "\nGO\n"


def connect(outputs, driver=None, credential=None, sleep=time.sleep, out=print):
    """Connect as the signed-in operator over the private endpoint, waiting while a paused database resumes."""
    driver = driver or importlib.import_module("mssql_python")
    if credential is None:
        from azure.identity import AzureCliCredential
        credential = AzureCliCredential()
    sql_store = app_module("sql_store")
    target = sql_store.connection_string(outputs["sqlServerFqdn"], outputs["databaseName"])
    for attempt in range(CONNECT_ATTEMPTS):
        try:
            return driver.connect(target, autocommit=True, token_provider=credential)
        except driver.Error as error:
            message = str(error)
            if BLOCKED_IP.search(message) or "public network" in message.lower():
                raise MvpError("Azure SQL refused the connection because it did not arrive through the private "
                               "endpoint. Run this on the developer VM and check the private DNS zone links. "
                               "Public access and firewall rules stay off.") from error
            if attempt == CONNECT_ATTEMPTS - 1 or not any(marker in message for marker in sql_store.TRANSIENT):
                raise MvpError(f"Could not connect to Azure SQL: {message}") from error
            out("  Waiting for the database (a paused serverless database takes up to a minute to resume) ...")
            sleep(15)


@contextmanager
def sql_errors(action, where="Azure SQL"):
    """Driver errors (including a failed ledger verification, which THROWs) become one readable message."""
    try:
        yield
    except MvpError:
        raise
    except Exception as error:  # noqa: BLE001 - the driver's error classes are only known at run time
        raise MvpError(f"{action} failed in {where}: {error}") from error


def run_script(connection, script):
    cursor = connection.cursor()
    try:
        for batch in sql_batches(script):
            cursor.execute(batch)
    finally:
        cursor.close()


def rows(connection, query):
    """Rows of the first result set as dictionaries."""
    cursor = connection.cursor()
    try:
        cursor.execute(query)
        while cursor.description is None:
            if not cursor.nextset():
                return []
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
    finally:
        cursor.close()


def apply_schema(connection, outputs):
    """Create what is missing (the scripts are rerunnable) and return {table: ledger type}."""
    grants = grant_script(outputs["appIdentityName"], outputs["appIdentityClientId"])
    with sql_errors("Creating the schema and the app's database user"):
        run_script(connection, SCHEMA.read_text(encoding="utf-8"))
        run_script(connection, grants)
        return {row["name"]: row["ledger_type_desc"] for row in rows(connection, TABLES_SQL)}


def incarnation(connection):
    """The database's creation time as YYYYMMDDTHHMMSSZ: a recreated database starts a new digest prefix."""
    with sql_errors("Reading the database creation time"):
        cursor = connection.cursor()
        try:
            return app_module("ledger").read_incarnation(cursor)
        finally:
            cursor.close()


def generate_digest(connection):
    with sql_errors("Generating a ledger digest"):
        cursor = connection.cursor()
        try:
            cursor.execute(GENERATE_SQL)
            while cursor.description is None:
                if not cursor.nextset():
                    raise MvpError("Azure SQL did not return a ledger digest.")
            row = cursor.fetchone()
        finally:
            cursor.close()
    try:
        digest = json.loads(row[0]) if row else None
    except (TypeError, ValueError):
        digest = None
    if not isinstance(digest, dict) or not DIGEST_FIELDS <= digest.keys():
        raise MvpError("Azure SQL returned a ledger digest in an unexpected shape.")
    return digest


def verify(connection, digests, block_id):
    """Have SQL verify the ledger against the digests; the verified block must reach block_id."""
    with sql_errors("Ledger verification"):
        cursor = connection.cursor()
        try:
            cursor.execute(app_module("ledger").VERIFY_SQL, json.dumps(digests))
            while cursor.description is None:
                if not cursor.nextset():
                    raise MvpError("Ledger verification returned no result.")
            row = cursor.fetchone()
            if row is None or row[0] is None or row[0] < block_id:
                raise MvpError(f"Ledger verification did not cover block {block_id}.")
            verified = row[0]
            while cursor.nextset():  # read every result set so a later SQL error is not hidden
                pass
        finally:
            cursor.close()
    return verified


def blob_container(account_url, name):
    from azure.identity import AzureCliCredential
    from azure.storage.blob import ContainerClient

    return ContainerClient(account_url, name, credential=AzureCliCredential())


def _forbidden(error):
    return getattr(error, "status_code", None) == 403 or any(marker in str(error) for marker in FORBIDDEN)


def _blob_retry(action, sleep, out, account="digest storage account", failure="Ledger digest storage"):
    """A new storage role assignment can take minutes to apply; retry 403 for up to five minutes."""
    for attempt in range(BLOB_ATTEMPTS):
        try:
            return action()
        except MvpError:
            raise
        except Exception as error:  # noqa: BLE001 - Azure SDK errors become MvpError unless a 403 is retried
            if _forbidden(error) and attempt < BLOB_ATTEMPTS - 1:
                out("  Waiting for the storage role assignment to take effect ...")
                sleep(30)
                continue
            if _forbidden(error):
                raise MvpError(f"The {account} still refuses the operator after five minutes. The "
                               "infrastructure grants the operator Storage Blob Data Contributor there; check "
                               "that assignment, then rerun.") from error
            raise MvpError(f"{failure} failed: {type(error).__name__}: {error}") from error


def read_digests(outputs, prefix, container=None, sleep=time.sleep, out=print):
    """Digests anchored for this database incarnation only."""
    container = container or blob_container
    name = app_module("ledger").DIGEST_CONTAINER

    def action():
        with container(outputs["digestStorageUrl"], name) as client:
            return [json.loads(client.download_blob(item.name).readall())
                    for item in client.list_blobs(name_starts_with=prefix)]

    return _blob_retry(action, sleep, out)


def anchor(connection, outputs, container=None, sleep=time.sleep, out=print):
    """Generate a digest, store it in the digest container, read every digest back and verify the ledger."""
    container = container or blob_container
    prefix = digest_prefix(connection, outputs)
    digest = generate_digest(connection)
    blob = f"{prefix}{uuid.uuid4()}.json"
    name = app_module("ledger").DIGEST_CONTAINER

    def upload():
        with container(outputs["digestStorageUrl"], name) as client:
            client.upload_blob(blob, json.dumps(digest).encode("utf-8"), overwrite=False)

    _blob_retry(upload, sleep, out)
    digests = read_digests(outputs, prefix, container, sleep, out)
    if digest not in digests:
        raise MvpError("The new ledger digest was not read back from the digest storage account.")
    verified = verify(connection, digests, digest["block_id"])
    return {"block_id": digest["block_id"], "verified_block": verified, "blob": blob, "digests": len(digests)}


def verify_anchored(connection, outputs, container=None, sleep=time.sleep, out=print):
    """Verify the ledger against the digests already anchored, without adding one."""
    digests = read_digests(outputs, digest_prefix(connection, outputs), container, sleep, out)
    if not digests:
        raise MvpError("No ledger digest has been anchored yet. Run python -m dbp_mvp ledger anchor.")
    latest = max(digest.get("block_id", -1) for digest in digests)
    return {"verified_block": verify(connection, digests, latest), "digests": len(digests)}
