"""Checks that the SQL schema, the store metadata and the SQL statements agree. No database needed."""

import re
import unittest
from datetime import datetime
from pathlib import Path

import helpers  # noqa: F401 - adds the app to sys.path
from passport import domain
from passport.sql_store import SqlTx, connection_string, from_sql, to_sql
from passport.store import SCHEMA_VERSION, TABLES, ConflictError, MemoryStore, StoreError, grant_statements

SCHEMA = (Path(__file__).resolve().parents[1] / "sql" / "schema.sql").read_text(encoding="utf-8")


def create_blocks():
    blocks = {}
    for match in re.finditer(r"CREATE TABLE dbp\.(\w+) \((.*?)\)\s*(WITH \((.*?)\);|;)", SCHEMA, re.S):
        columns = re.findall(r"^\s+(\w+)\s+(?:varchar|nvarchar|char|int|bigint|decimal|datetime2)", match.group(2),
                             re.M)
        blocks[match.group(1)] = (columns, match.group(4) or "")
    return blocks


def view_columns(name):
    """The output column names of a view: the top-level select list, split on commas outside brackets."""
    body = SCHEMA.split(f"CREATE OR ALTER VIEW report.{name} AS\nSELECT ")[1]
    items, depth, current = [], 0, ""
    for word in re.split(r"(\(|\)|,|\bFROM\b)", body):
        if depth == 0 and word == "FROM":
            break
        depth += (word == "(") - (word == ")")
        if depth == 0 and word == ",":
            items.append(current)
            current = ""
        else:
            current += word
    items.append(current)
    return [re.findall(r"\w+", item)[-1] for item in items]


class FakeCursor:
    def __init__(self, rowcount=1, rows=()):
        self.rowcount, self.rows, self.calls = rowcount, list(rows), []

    def execute(self, sql, params=()):
        self.calls.append((sql, list(params)))

    def fetchall(self):
        return self.rows


class SchemaTests(unittest.TestCase):
    def test_every_table_matches_store_metadata(self):
        blocks = create_blocks()
        self.assertEqual(set(blocks), set(TABLES))
        for table, (mode, _keys, columns) in TABLES.items():
            ddl_columns, options = blocks[table]
            self.assertEqual(ddl_columns, list(columns), table)
            if mode == "append":
                self.assertIn("APPEND_ONLY = ON", options, table)
            elif mode == "updatable":
                self.assertIn("SYSTEM_VERSIONING = ON", options, table)
                self.assertIn("row_version", columns, table)
            else:
                self.assertNotIn("LEDGER", options, table)

    def test_ledger_tables_avoid_unsupported_types(self):
        for table, (_columns, options) in create_blocks().items():
            if "LEDGER" in options:
                block = SCHEMA.split(f"CREATE TABLE dbp.{table} (")[1].split("WITH (")[0]
                self.assertNotRegex(block, r"(?i)\b(xml|sql_variant|max\))", table)

    def test_report_views_match_domain_rows(self):
        state = {k: None for k in ("battery_id", "model_id", "plant_id", "batch_id", "chemistry", "rated_kwh",
                                   "eligibility", "hold_reason", "lifecycle_status", "committed_version",
                                   "footprint_per_kwh", "footprint_verified")}
        self.assertEqual(view_columns("v_passport_status"), list(domain.report_row(state, None, None, {})))
        lot = {"lot_id": "LOT-A", "supplier_id": "SUP-A", "material": "lithium", "required_fields": "a,b",
               "present_fields": "a"}
        self.assertEqual(view_columns("v_lot_claim_status"), list(domain.lot_claim_row(lot)))

    def test_grants_follow_the_table_manifest(self):
        blocks = create_blocks()
        grants = {re.search(r"dbp\.(\w+)", g).group(1): g for g in grant_statements("app-id")}
        self.assertEqual(set(grants), set(blocks))
        for table, (_columns, options) in blocks.items():
            privileges = re.match(r"GRANT (.*) ON OBJECT", grants[table]).group(1)
            expected = "SELECT, INSERT" if "APPEND_ONLY = ON" in options else "SELECT, INSERT, UPDATE"
            self.assertEqual(privileges, expected, table)
        text = " ".join(grants.values())
        self.assertNotRegex(text, r"DELETE|ALTER|CONTROL|report\.")

    def test_schema_refuses_another_version(self):
        guard = SCHEMA.split("CREATE TABLE")[0]
        self.assertRegex(guard, rf"schema_version <> {SCHEMA_VERSION}[\s\S]*THROW 50001")
        self.assertIn(f"VALUES ('dbp', {SCHEMA_VERSION}, SYSUTCDATETIME())", SCHEMA)


class SqlStatementTests(unittest.TestCase):
    def test_insert_skips_identity_and_converts_times(self):
        cursor = FakeCursor()
        SqlTx(cursor).insert("AuditEvents", {"event_utc": "2026-10-23T10:00:00.123Z", "actor": "a",
                                             "action": "x", "subject": "s", "detail": "{}"})
        sql, params = cursor.calls[0]
        self.assertNotIn("event_id", sql)
        self.assertEqual(sql.count("?"), len(params))
        self.assertEqual(params[0], datetime(2026, 10, 23, 10, 0, 0, 123000))

    def test_update_checks_row_version(self):
        cursor = FakeCursor(rowcount=0)
        with self.assertRaises(ConflictError):
            SqlTx(cursor).update("Reviews", {"review_id": "RV-1", "status": "resolved"}, 1)
        sql, params = cursor.calls[0]
        self.assertIn("row_version = row_version + 1", sql)
        self.assertEqual(params, ["resolved", "RV-1", 1])
        with self.assertRaises(StoreError):
            SqlTx(FakeCursor()).update("Evidence", {"evidence_id": "EV-1"}, 1)

    def test_select_and_type_round_trip(self):
        cursor = FakeCursor(rows=[("EV-1", "other", "LOT-A", "a.txt", "text/plain", "0" * 64, 1, "EV-1/a.txt",
                                   "v1", "r", datetime(2026, 10, 23, 10, 0, 0, 5000))])
        row = SqlTx(cursor).select("Evidence", lot_id="LOT-A")[0]
        self.assertEqual(row["uploaded_utc"], "2026-10-23T10:00:00.005Z")
        self.assertIn("WHERE lot_id = ?", cursor.calls[0][0])
        self.assertEqual(from_sql("f", 27), 27.0)
        self.assertIsNone(to_sql("t", None))

    def test_get_rejects_an_incomplete_or_extra_key_before_querying(self):
        cursor = FakeCursor()
        with MemoryStore().read() as memory:
            for tx in (SqlTx(cursor), memory):
                for key in ((), ("B-1",), ("B-1", "B-2", "extra")):
                    with self.subTest(store=type(tx).__name__, key=key), self.assertRaises(StoreError):
                        tx.get("PassportLinks", *key)
        self.assertEqual(cursor.calls, [])

    def test_select_rejects_rows_with_a_different_number_of_columns(self):
        row = ("dbp", 3, datetime(2026, 10, 23))
        for values in (row[:-1], (*row, "extra")):
            with self.subTest(values=values), self.assertRaises(ValueError):
                SqlTx(FakeCursor(rows=[values])).select("SchemaInfo")

    def test_connection_string_has_no_password(self):
        text = connection_string("dbp-sql", "dbp")
        self.assertIn("dbp-sql.database.windows.net", text)
        self.assertNotRegex(text.lower(), r"pwd|password|authentication")


if __name__ == "__main__":
    unittest.main()
