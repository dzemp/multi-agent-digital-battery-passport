"""Database ledger verification for the trail screen.

In SQL mode this runs sys.sp_verify_database_ledger against the digests the operator anchored in private Blob
storage (python -m dbp_mvp ledger anchor), then checks whether the passport version's ledger block is covered by an
anchored digest. It never reports success it did not observe. The in-memory store has no ledger.
"""

import json
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

DIGEST_CONTAINER = "sqldbledgerdigests"
VERIFY_SQL = ("DECLARE @digests nvarchar(max) = CONVERT(nvarchar(max), ?); "
              "DECLARE @result int; EXEC @result = sys.sp_verify_database_ledger @digests = @digests; "
              "IF @result <> 0 THROW 51000, 'Ledger verification failed', 1;")
INCARNATION_SQL = "SELECT create_date FROM sys.databases WHERE name = DB_NAME()"
RECORD_BLOCK_SQL = ("SELECT t.block_id FROM dbp.PassportVersions AS v "
                    "JOIN sys.database_ledger_transactions AS t ON t.transaction_id = v.ledger_start_transaction_id "
                    "WHERE v.battery_id = ? AND v.version = ?")
MESSAGES = {
    "verified": "Ledger verified: the database matches the anchored digests and this version is covered.",
    "chain_verified_anchor_pending": "The ledger verified up to the last anchored digest. This version was written "
                                     "after it; anchor a new digest to cover it.",
    "failed": "Ledger verification FAILED. Treat this passport's history as untrusted and investigate.",
    "unavailable": "Ledger verification could not run, so nothing is claimed.",
}


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def incarnation(create_date):
    """The database's creation time (sys.databases.create_date, UTC in Azure SQL) as YYYYMMDDTHHMMSSZ.

    A recreated database (python -m dbp_mvp data reset) gets a new incarnation, so its digests never mix with the
    digests anchored for the database it replaced."""
    if isinstance(create_date, str):
        create_date = datetime.fromisoformat(create_date.replace("Z", "+00:00"))
    if create_date.tzinfo is not None:
        create_date = create_date.astimezone(timezone.utc)
    return create_date.strftime("%Y%m%dT%H%M%SZ")


def digest_prefix(sql_server, database, incarnation_id):
    """Where anchored digests live in the digest container; the operator's tools write the same prefix."""
    return f"manual/{sql_server.split('.')[0]}/{database}/{incarnation_id}/"


def read_incarnation(cursor):
    cursor.execute(INCARNATION_SQL)
    row = cursor.fetchone()
    if not row or row[0] is None:
        raise RuntimeError("sys.databases has no create_date for this database")
    return incarnation(row[0])


def result(status, reason_code, checked_utc=None, performed=False, **detail):
    return {"status": status, "reason_code": reason_code, "message": detail.pop("message", MESSAGES[status]),
            "checked_utc": checked_utc, "performed": performed, **detail}


class UnavailableLedger:
    """Memory store or missing configuration: say so instead of pretending."""

    def __init__(self, reason_code, message):
        self.reason_code, self.message = reason_code, message

    def check(self, battery_id=None, version=None):
        return result("unavailable", self.reason_code, message=self.message)

    def last(self, battery_id=None, version=None):
        return None


class SqlLedger:
    def __init__(self, store, sql_server, sql_database, digest_account_url, ttl_seconds=60, clock=utc_now,
                 monotonic=time.monotonic):
        self.store, self.account_url, self.ttl, self.clock, self.monotonic = \
            store, digest_account_url, ttl_seconds, clock, monotonic
        self.server, self.database = sql_server, sql_database
        self._lock = threading.Lock()
        self._cached, self._cached_at = None, None
        self._records = {}  # (battery_id, version) -> last observed result, for anonymous pages

    def prefix(self):
        """Read on every check: the database may have been recreated while the app kept running."""
        connection = self.store._connect(autocommit=True)
        try:
            cursor = connection.cursor()
            try:
                return digest_prefix(self.server, self.database, read_incarnation(cursor))
            finally:
                cursor.close()
        finally:
            connection.close()

    def _digests(self):
        from azure.storage.blob import ContainerClient

        prefix = self.prefix()
        with ContainerClient(self.account_url, DIGEST_CONTAINER, credential=self.store.credential) as container:
            return [json.loads(container.download_blob(item.name).readall())
                    for item in container.list_blobs(name_starts_with=prefix)]

    def _verify_database(self):
        checked = self.clock()
        try:
            digests = self._digests()
        except Exception as error:  # noqa: BLE001 - network or permission problems are not tamper evidence
            return result("unavailable", "digests_unreadable", checked, message=f"Anchored digests could not be "
                          f"read ({type(error).__name__}). Nothing is claimed.")
        if not digests:
            return result("unavailable", "no_anchored_digest", checked,
                          message="No digest has been anchored yet (python -m dbp_mvp ledger anchor). "
                                  "Nothing is claimed.")
        anchored = max(d.get("block_id", -1) for d in digests)
        connection = self.store._connect(autocommit=True)
        try:
            cursor = connection.cursor()
            try:
                cursor.execute(VERIFY_SQL, json.dumps(digests))
                while cursor.description is None:
                    if not cursor.nextset():
                        break
                row = cursor.fetchone() if cursor.description is not None else None
                while cursor.nextset():  # consume every set so a later THROW is not hidden
                    pass
            except Exception as error:  # noqa: BLE001
                text = str(error).lower()
                if "permission" in text or "denied" in text:
                    return result("unavailable", "permission_missing", checked, message="The app identity lacks "
                                  "VIEW LEDGER CONTENT (sql/grant-app.sql). Nothing is claimed.")
                return result("failed", "verification_failed", checked, performed=True, anchors=len(digests))
            finally:
                cursor.close()
        finally:
            connection.close()
        verified_block = row[0] if row and row[0] is not None else None
        if verified_block is None or verified_block < anchored:
            return result("failed", "anchor_not_covered", checked, performed=True, anchors=len(digests))
        return result("verified", "ok", checked, performed=True, anchors=len(digests), anchored_block=anchored)

    def _record_block(self, battery_id, version):
        connection = self.store._connect(autocommit=True)
        try:
            cursor = connection.cursor()
            cursor.execute(RECORD_BLOCK_SQL, [battery_id, version])
            row = cursor.fetchone()
            cursor.close()
            return row[0] if row else None
        finally:
            connection.close()

    def check(self, battery_id=None, version=None):
        with self._lock:
            fresh = self._cached is not None and self.monotonic() - self._cached_at < self.ttl
            if fresh:
                database = {**self._cached, "performed": False}
            else:
                try:
                    database = self._verify_database()
                except Exception as error:  # noqa: BLE001
                    database = result("unavailable", "sql_unreachable", self.clock(),
                                      message=f"SQL could not be reached ({type(error).__name__}). Nothing is claimed.")
                if database["status"] in ("verified", "failed"):
                    self._cached, self._cached_at = database, self.monotonic()
        if database["status"] != "verified" or not battery_id or not version:
            return self._remember(battery_id, version, database)
        try:
            block = self._record_block(battery_id, version)
        except Exception as error:  # noqa: BLE001
            return {**database, "status": "unavailable", "reason_code": "record_lookup_failed",
                    "message": f"The version's ledger block could not be read ({type(error).__name__})."}
        if block is None:
            return self._remember(battery_id, version, {
                **database, "status": "failed", "reason_code": "record_missing",
                "message": "This passport version has no ledger transaction. Investigate."})
        if block > database["anchored_block"]:
            return self._remember(battery_id, version, {
                **database, "status": "chain_verified_anchor_pending", "reason_code": "anchor_pending",
                "message": MESSAGES["chain_verified_anchor_pending"], "record_block": block})
        return self._remember(battery_id, version, {**database, "record_block": block})

    def _remember(self, battery_id, version, outcome):
        if battery_id and version and outcome["status"] != "unavailable":
            with self._lock:
                if len(self._records) >= 1000:
                    self._records.clear()
                self._records[(battery_id, version)] = {**outcome, "performed": False}
        return outcome

    def last(self, battery_id=None, version=None):
        """The last observed result, for anonymous pages: never runs SQL on their behalf."""
        with self._lock:
            if battery_id:
                found = self._records.get((battery_id, version))
                return dict(found) if found else None
            return dict(self._cached) if self._cached else None


def build_ledger(settings, store):
    if settings.store != "sql":
        return UnavailableLedger("memory_store", "Not available locally: the in-memory store has no SQL ledger.")
    if not settings.digest_account_url or urlsplit(settings.digest_account_url).scheme != "https":
        return UnavailableLedger("digest_store_not_configured",
                                 "DBP_DIGEST_ACCOUNT_URL is not set, so anchored digests cannot be read.")
    return SqlLedger(store, settings.sql_server, settings.sql_database, settings.digest_account_url)
