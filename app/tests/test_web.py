import base64
import json
import unittest

from fastapi.testclient import TestClient

import helpers
from passport.config import Settings
from passport.service import PassportService
from passport.store import MemoryStore
from passport.web import create_app

OP = {"x-local-user": "olivia", "x-local-roles": "Passport.Operator"}
REV = {"x-local-user": "rafael", "x-local-roles": "Passport.Reviewer"}
NOBODY = {"x-local-user": "nora", "x-local-roles": ""}


def easy_auth_header(roles, name="Olivia Operator", oid="0000-oid"):
    claims = [{"typ": "name", "val": name},
              {"typ": "http://schemas.microsoft.com/identity/claims/objectidentifier", "val": oid}]
    claims += [{"typ": "roles", "val": r} for r in roles]
    body = {"auth_typ": "aad", "name_typ": "name", "role_typ": "roles", "claims": claims}
    return base64.b64encode(json.dumps(body).encode()).decode()


class LocalModeTests(unittest.TestCase):
    def setUp(self):
        self.service, self.blobs = helpers.new_service()
        helpers.put_package(self.blobs, "BUILD-03", helpers.standard_package())
        app = create_app(Settings(auth_mode="local", worker=False), self.service)
        self.client = TestClient(app)

    def test_public_and_restricted_access(self):
        self.assertEqual(self.client.get("/passports").status_code, 200)
        self.assertEqual(self.client.get("/passports/BAT-101/restricted").status_code, 401)
        self.assertEqual(self.client.get("/passports/BAT-101/restricted", headers=NOBODY).status_code, 403)
        self.assertEqual(self.client.post("/packages/import", json={}, headers=REV).status_code, 403)
        self.assertEqual(self.client.get("/me").json(), {"signed_in": False})

    def test_reviewer_and_operator_flow(self):
        upload = self.client.post("/evidence", headers=REV, data={"kind": "supplier_claim", "lot_id": "LOT-A",
                                                                  "command_id": "web-ev-0001"},
                                  files={"file": ("claim.json", b'{"origin": "Mine North"}', "application/json")})
        self.assertEqual(upload.status_code, 201, upload.text)
        evidence_id = upload.json()["evidence_id"]
        review = self.client.post("/reviews", headers=REV, json={
            "lot_id": "LOT-A", "reason": "Origin differs", "evidence_ids": [evidence_id], "command_id": "web-rv-0001"})
        self.assertEqual(review.status_code, 201, review.text)
        imported = self.client.post("/packages/import", headers=OP,
                                    json={"intake_folder": "BUILD-03", "command_id": "web-imp-0001"})
        self.assertEqual(imported.json()["outcome"], "imported")
        self.assertEqual(self.client.get("/passports").json()["board"]["text"], "1 of 3 passports committed")
        drained = self.client.post("/admin/outbox/drain", headers=OP, json={})
        self.assertEqual(len(drained.json()["processed"]), 1)
        restricted = self.client.get("/passports/BAT-101/restricted", headers=REV).json()
        self.assertEqual(restricted["state"]["hold_reason"], "sourcing_review_open")
        report = self.client.get("/report/status", headers=OP).json()["rows"]
        self.assertNotIn("supplier_ids", report[0])

    def test_browser_form_redirects_and_message_is_escaped(self):
        response = self.client.post("/reviews", headers=REV, data={
            "lot_id": "LOT-A", "reason": "<script>x</script>", "evidence_ids": "EV-NONE",
            "command_id": "web-rv-0002", "return_to": "/"}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        page = self.client.get(response.headers["location"], headers=REV)
        self.assertIn("evidence_not_found", page.text)
        self.assertNotIn("<script>", page.text)
        self.assertIn("default-src 'none'", page.headers["content-security-policy"])

    def test_form_resolve_route_and_bad_row_version(self):
        response = self.client.post("/reviews/resolve", headers=REV, data={
            "review_id": "RV-NOPE", "expected_row_version": "x", "command_id": "web-rs-0001"})
        self.assertEqual(response.status_code, 422)
        response = self.client.post("/reviews/resolve", headers=REV, data={
            "review_id": "RV-NOPE", "expected_row_version": "1", "reason": "Because", "evidence_ids": "EV-1",
            "command_id": "web-rs-0002"})
        self.assertEqual(response.status_code, 404)

    def test_dashboard_shows_forms_by_role(self):
        self.assertNotIn("Upload evidence", self.client.get("/").text)
        self.assertIn("Upload evidence", self.client.get("/", headers=REV).text)
        self.assertNotIn("Upload evidence", self.client.get("/", headers=OP).text)
        self.assertIn("Snapshot queue", self.client.get("/", headers=OP).text)

    def test_console_uses_the_branded_layout_by_role(self):
        staff = self.client.get("/", headers=OP).text
        self.assertIn("Contoso Battery", staff)
        self.assertIn('<nav class="side"', staff)
        self.assertRegex(staff, r'href="/" aria-current="page"')
        self.assertIn("Latest audit events", staff)
        anonymous = self.client.get("/").text
        self.assertIn("Contoso Battery", anonymous)
        self.assertNotIn('<nav class="side"', anonymous)
        self.assertIn("Battery passports", anonymous)
        self.assertNotIn("Sourcing reviews", anonymous)
        roleless = self.client.get("/", headers=NOBODY).text
        self.assertIn("no app role", roleless)
        self.assertNotIn('<nav class="side"', roleless)
        admin = self.client.get("/", headers={"x-local-user": "ada", "x-local-roles": "Passport.Admin"}).text
        self.assertIn('<nav class="side"', admin)
        self.assertNotIn("Latest audit events", admin)
        self.assertNotIn("Upload evidence", admin)

    def test_console_result_panel(self):
        done = self.client.get("/?msg=" + "201%20%7B%22outcome%22%3A%20%22imported%22%7D", headers=OP).text
        self.assertIn("Done (HTTP 201)", done)
        failed = self.client.get("/?msg=409%20%7B%22message%22%3A%20%22Already%20used%22%7D", headers=OP).text
        self.assertIn("Not done (HTTP 409)", failed)
        self.assertIn("Already used", failed)

    def test_health(self):
        self.assertEqual(self.client.get("/health/live").status_code, 200)
        self.assertEqual(self.client.get("/health/ready").json()["store"], "memory")

    def test_another_schema_version_is_not_ready_and_refuses_writes(self):
        service = PassportService(MemoryStore(schema_version=3), self.blobs)
        client = TestClient(create_app(Settings(auth_mode="local", worker=False), service))
        ready = client.get("/health/ready")
        self.assertEqual((ready.status_code, ready.json()["error"]), (503, "schema_mismatch"))
        imported = client.post("/packages/import", headers=OP,
                               json={"intake_folder": "BUILD-03", "command_id": "web-imp-0009"})
        self.assertEqual((imported.status_code, imported.json()["error"]), (503, "schema_mismatch"))
        aftersales = client.post("/imports/aftersales/DEL-1", headers=OP)
        self.assertEqual((aftersales.status_code, aftersales.json()["error"]), (503, "schema_mismatch"))
        self.assertEqual(service.store.schema_version(), 3)
        with service.store.read() as tx:
            self.assertEqual((tx.select("Packages"), tx.select("Commands")), ([], []))

    def test_report_export_v2(self):
        self.client.post("/packages/import", headers=OP, json={"intake_folder": "BUILD-03", "command_id": "web-imp-2"})
        self.assertEqual(self.client.get("/report/status", headers=REV).status_code, 403)
        self.assertEqual(self.client.get("/report/lot-claims", headers=OP).status_code, 404)
        export = self.client.get("/report/status", headers=OP).json()
        self.assertEqual(set(export), {"schema_version", "export_id", "generated_utc", "rows", "lot_claims",
                                       "data_label"})
        self.assertEqual((export["schema_version"], export["data_label"]), ("2", "synthetic"))
        self.assertRegex(export["export_id"], r"^EXP-[0-9]{8}T[0-9]{6}Z$")
        self.assertRegex(export["generated_utc"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(len(export["rows"]), 3)
        self.assertEqual(len(export["lot_claims"]), 10)
        self.assertEqual({(r["claim_status"], r["case_state"]) for r in export["lot_claims"]}, {("missing", "none")})


class EasyAuthModeTests(unittest.TestCase):
    def setUp(self):
        service, _ = helpers.new_service()
        self.client = TestClient(create_app(Settings(auth_mode="easyauth", worker=False), service))

    def test_principal_and_roles_from_easy_auth_header(self):
        me = self.client.get("/me", headers={"x-ms-client-principal": easy_auth_header(["Passport.Operator"])}).json()
        self.assertEqual(me["roles"], ["Passport.Operator"])
        self.assertEqual(me["user_id"], "0000-oid")

    def test_local_identity_headers_are_ignored(self):
        self.assertEqual(self.client.get("/me", headers=OP).json(), {"signed_in": False})
        for bad in ("not-base64!", base64.b64encode(b"[1, 2]").decode()):
            self.assertEqual(self.client.get("/me", headers={"x-ms-client-principal": bad}).json(),
                             {"signed_in": False})

    def test_cross_site_form_post_is_blocked(self):
        header = {"x-ms-client-principal": easy_auth_header(["Passport.Operator"])}
        foreign = self.client.post("/admin/worker", json={"paused": True},
                                   headers={**header, "origin": "https://evil.example"})
        self.assertEqual(foreign.status_code, 403)
        cross = self.client.post("/admin/worker", json={"paused": True},
                                 headers={**header, "sec-fetch-site": "cross-site"})
        self.assertEqual(cross.status_code, 403)
        same = self.client.post("/admin/worker", json={"paused": True},
                                headers={**header, "origin": "http://testserver"})
        self.assertEqual(same.json()["worker"]["paused"], True)
        script = self.client.post("/admin/worker", json={"paused": False}, headers=header)
        self.assertEqual(script.json()["worker"]["paused"], False)


class SettingsTests(unittest.TestCase):
    def test_local_auth_refused_inside_container_apps(self):
        with self.assertRaises(ValueError):
            Settings.from_env({"DBP_AUTH_MODE": "local", "CONTAINER_APP_NAME": "dbp-app"})

    def test_sql_needs_server_and_database(self):
        with self.assertRaises(ValueError):
            Settings.from_env({"DBP_STORE": "sql"})
        self.assertEqual(Settings.from_env({}).auth_mode, "easyauth")


if __name__ == "__main__":
    unittest.main()
