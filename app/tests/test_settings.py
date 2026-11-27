"""Settings page: who may see and change it, what is saved, how the app starts with saved values, and that the
Application Insights connection string is never shown again."""

import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import helpers
from passport import telemetry
from passport.config import Settings, check_workbook_link
from passport.settings_store import LocalFile, SettingsAdmin, parse, probe
from passport.web import create_app

ADMIN = {"x-local-user": "ada", "x-local-roles": "Passport.Admin"}
OP = {"x-local-user": "olivia", "x-local-roles": "Passport.Operator"}
REV = {"x-local-user": "rafael", "x-local-roles": "Passport.Reviewer"}
NOBODY = {"x-local-user": "nora", "x-local-roles": ""}
KEY = "00000000-0000-0000-0000-00000000abcd"
SECRET = f"InstrumentationKey={KEY};IngestionEndpoint=https://westeurope-5.in.applicationinsights.azure.com/"
WORKBOOK = ("https://portal.azure.com/#@contoso.onmicrosoft.com/resource/subscriptions/0000/resourceGroups/rg-dbp"
            "/providers/microsoft.insights/workbooks/1111/workbook")
BASE = {"DBP_AUTH_MODE": "local", "DBP_WORKER": "off"}
FORM = {"DBP_STORE": "memory", "DBP_BLOBS": "local"}


def passing(values, client_id=""):
    return [{"name": "Blob Storage", "ok": True, "text": "Connected"}]


def failing(values, client_id=""):
    return [{"name": "Azure SQL Database", "ok": False, "text": "ServiceRequestError: host not found"}]


class SettingsPage(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "settings.json"
        self.service, _ = helpers.new_service()
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.close()
        self.folder.cleanup()

    def start(self, env=None, saved=None, tester=passing):
        """The app as `python -m passport` starts it: deployment values, then any saved file."""
        if saved is not None:
            self.path.write_text(saved if isinstance(saved, str) else json.dumps(saved), encoding="utf-8")
        admin = SettingsAdmin.load({**BASE, "DBP_SETTINGS_FILE": str(self.path), **(env or {})}, tester)
        self.app = create_app(service=self.service, telemetry=telemetry.Telemetry(), settings_admin=admin)
        client = TestClient(self.app)
        self.clients.append(client)
        return client

    def saved_values(self):
        return json.loads(self.path.read_text(encoding="utf-8"))["values"]

    def test_admin_edits_operator_views_others_are_refused(self):
        client = self.start()
        self.assertEqual(client.get("/settings").status_code, 401)
        for headers in (NOBODY, REV):
            self.assertEqual(client.get("/settings", headers=headers).status_code, 403)
            self.assertEqual(client.post("/settings", data=FORM, headers=headers).status_code, 403)
        operator = client.get("/settings", headers=OP)
        self.assertEqual(operator.status_code, 200)
        self.assertIn("View only", operator.text)
        self.assertNotIn('action="/settings"', operator.text)
        self.assertEqual(client.post("/settings", data=FORM, headers=OP).status_code, 403)
        admin = client.get("/settings", headers=ADMIN)
        self.assertIn('action="/settings"', admin.text)
        self.assertIn("Up to date", admin.text)
        self.assertNotIn("Production series", admin.text)  # Admin alone grants no passport data
        self.assertFalse(self.path.exists())

    def test_connection_string_is_masked_and_never_echoed(self):
        client = self.start({"APPLICATIONINSIGHTS_CONNECTION_STRING": SECRET})
        for headers in (ADMIN, OP):
            page = client.get("/settings", headers=headers).text
            self.assertNotIn(KEY, page)
            self.assertIn("key ending abcd", page)
        kept = client.post("/settings", data={**FORM, "skip_test": "1"}, headers=ADMIN, follow_redirects=False)
        self.assertEqual(kept.status_code, 303)
        self.assertEqual(self.saved_values(), {})  # blank keeps the deployment value, so nothing is overridden
        failed = client.post("/settings", data={**FORM, "DBP_SQL_SERVER": "bad host!"}, headers=ADMIN)
        self.assertEqual(failed.status_code, 422)
        self.assertNotIn(KEY, failed.text)
        client.post("/settings", data={**FORM, "clear_APPLICATIONINSIGHTS_CONNECTION_STRING": "1", "skip_test": "1"},
                    headers=ADMIN)
        self.assertEqual(self.saved_values(), {"APPLICATIONINSIGHTS_CONNECTION_STRING": ""})

    def test_save_writes_only_changes_and_asks_for_a_restart(self):
        client = self.start()
        response = client.post("/settings", data={**FORM, "DBP_WORKBOOK_EXECUTIVE": WORKBOOK}, headers=ADMIN,
                               follow_redirects=False)
        self.assertEqual((response.status_code, response.headers["location"]), (303, "/settings?saved=1"))
        document = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(document["values"], {"DBP_WORKBOOK_EXECUTIVE": WORKBOOK})
        self.assertEqual(document["updated_by"], "ada (local:ada)")
        page = client.get("/settings?saved=1", headers=ADMIN).text
        self.assertIn("Settings saved", page)
        self.assertIn("Restart required", page)
        self.assertIn("Stop the app (Ctrl+C)", page)
        self.assertNotIn("portal.azure.com", client.get("/exec", headers=OP).text)  # not in use until a restart

    def test_invalid_values_are_explained_and_not_saved(self):
        client = self.start()
        other_monitor = f"InstrumentationKey={KEY};IngestionEndpoint=https://collector.example.com/"
        cases = ({"DBP_STORE": "sql"}, {"DBP_BLOB_ACCOUNT_URL": "https://st.blob.core.windows.net/x?sv=1"},
                 {"DBP_WORKBOOK_AGENTS": "javascript:alert(1)"}, {"DBP_WORKBOOK_AGENTS": "https://evil.example/wb"},
                 {"APPLICATIONINSIGHTS_CONNECTION_STRING": "not a connection string"},
                 # Hosts that would receive the app's managed identity token must be Azure SQL or Blob Storage.
                 {"DBP_BLOB_ACCOUNT_URL": "https://attacker.example.com"},
                 {"DBP_DIGEST_ACCOUNT_URL": "https://digest.blob.core.windows.net.attacker.example"},
                 {"DBP_DIGEST_ACCOUNT_URL": "https://digest.blob.core.windows.net:8443"},
                 {"DBP_SQL_SERVER": "sql.attacker.example"},
                 {"APPLICATIONINSIGHTS_CONNECTION_STRING": other_monitor},
                 {"APPLICATIONINSIGHTS_CONNECTION_STRING": f"InstrumentationKey={KEY};EndpointSuffix=example.com"})
        for change in cases:
            response = client.post("/settings", data={**FORM, **change, "skip_test": "1"}, headers=ADMIN)
            self.assertEqual(response.status_code, 422, change)
            self.assertIn("Correct the fields marked below", response.text)
        self.assertFalse(self.path.exists())
        for change in ({"DBP_SQL_SERVER": "sql-dbp"}, {"DBP_SQL_SERVER": "sql-dbp.database.windows.net"},
                       {"DBP_BLOB_ACCOUNT_URL": "https://stdbp01.blob.core.windows.net/"},
                       {"APPLICATIONINSIGHTS_CONNECTION_STRING": SECRET}):
            response = client.post("/settings", data={**FORM, **change, "skip_test": "1"}, headers=ADMIN,
                                   follow_redirects=False)
            self.assertEqual(response.status_code, 303, change)

    def test_a_failed_check_shows_the_error_type_not_the_reply(self):
        class Refused(Exception):
            error_code = "AuthorizationFailure"

        def reach():
            raise Refused("<html>internal page content</html>")

        with self.assertLogs("passport", "WARNING"):
            result = probe("Blob Storage", reach)
        self.assertFalse(result["ok"])
        self.assertEqual(result["text"], "Refused (AuthorizationFailure). See the app log for details.")

    def test_a_failed_connection_test_blocks_saving_unless_skipped(self):
        client = self.start(tester=failing)
        tested = client.post("/settings", data={**FORM, "action": "test"}, headers=ADMIN)
        self.assertEqual(tested.status_code, 200)
        self.assertIn("host not found", tested.text)
        blocked = client.post("/settings", data={**FORM, "DBP_PUBLIC_BASE_URL": "https://passport.example.com"},
                              headers=ADMIN)
        self.assertEqual(blocked.status_code, 422)
        self.assertIn("a connection test failed", blocked.text)
        self.assertFalse(self.path.exists())
        client.post("/settings", data={**FORM, "DBP_PUBLIC_BASE_URL": "https://passport.example.com", "skip_test": "1"},
                    headers=ADMIN)
        self.assertEqual(self.saved_values(), {"DBP_PUBLIC_BASE_URL": "https://passport.example.com"})

    def test_reset_returns_to_the_deployment_values(self):
        client = self.start(saved={"schema": 1, "values": {"DBP_PUBLIC_BASE_URL": "https://passport.example.com"}})
        self.assertEqual(client.post("/settings", data={"action": "reset"}, headers=ADMIN,
                                     follow_redirects=False).status_code, 303)
        self.assertEqual(self.saved_values(), {})

    def test_saved_values_apply_at_start_and_workbooks_open_in_a_new_window(self):
        client = self.start(saved={"schema": 1, "values": {"DBP_WORKBOOK_COMPLIANCE": WORKBOOK}})
        self.assertEqual(self.app.state.settings_admin.pending(), [])
        page = client.get("/exec", headers=OP).text
        link = page[page.index('href="https://portal.azure.com'):]
        link = link[:link.index(">")]
        self.assertIn('target="_blank"', link)
        self.assertIn('rel="noopener noreferrer"', link)
        self.assertIn("Compliance", page)
        self.assertIn('href="/settings"', page)
        self.assertNotIn("Add them in Settings", page)

    def test_without_workbooks_monitoring_is_hidden_from_staff_and_hinted_to_admins(self):
        client = self.start()
        self.assertNotIn("Monitoring", client.get("/exec", headers=REV).text)
        self.assertNotIn('href="/settings"', client.get("/exec", headers=REV).text)  # Reviewer has no Settings
        self.assertIn("Add them in Settings", client.get("/settings", headers=ADMIN).text)

    def test_a_broken_or_invalid_file_never_stops_the_app(self):
        for saved in ("{not json", {"schema": 1, "values": {"DBP_STORE": "sql"}},
                      {"schema": 1, "values": {"DBP_SECRET": "x"}}):
            client = self.start(saved=saved)
            self.assertEqual(self.app.state.settings_admin.saved, {})
            self.assertEqual(client.get("/health/live").status_code, 200)
            self.assertIn("Saved settings ignored", client.get("/settings", headers=ADMIN).text)

    def test_saving_is_off_in_a_container_without_a_settings_blob(self):
        admin = SettingsAdmin.load({"CONTAINER_APP_NAME": "ca-dbp", "DBP_AUTH_MODE": "easyauth"})
        self.assertIsNone(admin.file)
        app = create_app(Settings(auth_mode="local", worker=False), self.service, telemetry=telemetry.Telemetry(),
                         settings_admin=admin)
        with TestClient(app) as client:
            self.assertIn("Saving is off", client.get("/settings", headers=ADMIN).text)
            self.assertEqual(client.post("/settings", data={**FORM, "skip_test": "1"}, headers=ADMIN).status_code, 409)


class Values(unittest.TestCase):
    def test_workbook_links_must_point_at_the_azure_portal(self):
        self.assertEqual(check_workbook_link("X", WORKBOOK), WORKBOOK)
        for bad in ("http://portal.azure.com/#x", "https://user@portal.azure.com/", "https://portal.azure.com.evil.io/",
                    "https://portal.azure.com/ x"):
            with self.assertRaises(ValueError, msg=bad):
                check_workbook_link("X", bad)
        with self.assertRaises(ValueError):
            Settings.from_env({"DBP_WORKBOOK_OPERATIONS": "https://example.com/"})

    def test_saved_files_are_read_strictly(self):
        values, meta = parse(json.dumps({"schema": 1, "updated_by": "ada", "values": {"DBP_PUBLIC_BASE_URL": "https://p.example/"}}))
        self.assertEqual((values, meta["updated_by"]), ({"DBP_PUBLIC_BASE_URL": "https://p.example"}, "ada"))
        for bad in ({"schema": 2, "values": {}}, {"schema": 1, "values": {"AZURE_CLIENT_ID": "x"}},
                    {"schema": 1, "values": {"DBP_STORE": "postgres"}}):
            with self.assertRaises(ValueError):
                parse(json.dumps(bad))

    def test_local_file_write_replaces_the_whole_file(self):
        with tempfile.TemporaryDirectory() as folder:
            file = LocalFile(Path(folder) / "nested" / "settings.json")
            self.assertIsNone(file.read())
            file.write("one")
            file.write("two")
            self.assertEqual(file.read(), "two")
            self.assertEqual(sorted(p.name for p in (Path(folder) / "nested").iterdir()), ["settings.json"])


if __name__ == "__main__":
    unittest.main()
