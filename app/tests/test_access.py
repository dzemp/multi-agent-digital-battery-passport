"""Field policy (access.py): one projection for GET /passports/{id}/view and the passport page; viewer screens."""

import json
import unittest

from passport import access, telemetry
from passport.auth import (ADMIN, AUTHORITY, COMPLIANCE, CONNECTOR, OEM, OPERATOR, RECYCLER, REVIEWER,
                           Principal)
from test_screens import AUTH, COMP, NOBODY, OP, REV, Screens

PUBLIC = ["identity", "footprint"]
STAFF = list(access.STAFF_GROUPS)
OEM_H = {"x-local-user": "otto", "x-local-roles": OEM}
REC_H = {"x-local-user": "rita", "x-local-roles": RECYCLER}


def person(*roles):
    return Principal("u1", "someone", frozenset(roles), "local")


class PolicyTests(unittest.TestCase):
    def test_each_role_and_combination_gets_exactly_its_field_groups(self):
        expected = {
            None: (PUBLIC, None), ADMIN: (PUBLIC, None), CONNECTOR: (PUBLIC, None),
            OEM: (PUBLIC + ["performance", "second_life"], None),
            RECYCLER: (PUBLIC + ["performance", "composition"], None),
            AUTHORITY: (STAFF[:-1], None),
            COMPLIANCE: (PUBLIC + ["due_diligence", "decisions", "evidence"], "compliance_case"),
            OPERATOR: (STAFF, "all"), REVIEWER: (STAFF, "all"),
        }
        for role, (groups, scope) in expected.items():
            self.assertEqual(access.grant(person(role) if role else None), (tuple(groups), scope), role)
        self.assertEqual(access.grant(person())[0], tuple(PUBLIC))
        self.assertEqual(access.grant(person("Passport.Unknown"))[0], tuple(PUBLIC))
        self.assertEqual(access.grant(person(OEM, COMPLIANCE)),
                         (tuple(PUBLIC + ["performance", "due_diligence", "decisions", "evidence", "second_life"]),
                          "compliance_case"))
        self.assertEqual(access.grant(person(AUTHORITY, COMPLIANCE)), (tuple(STAFF), "compliance_case"))
        self.assertEqual(access.grant(person(COMPLIANCE, REVIEWER)), (tuple(STAFF), "all"))
        self.assertEqual(access.field_set(*access.grant(person(COMPLIANCE))), "id-fp-dd-dec-evc")
        self.assertEqual(access.role_group(person(RECYCLER, OEM)), "oem-recycler")
        self.assertEqual(access.role_group(None), "public")


class ViewTests(Screens):
    def setUp(self):
        self.sent = []
        self.build(sink=lambda name, dims, measures: self.sent.append((name, dims, measures)))

    def view(self, headers=None, path="/passports/BAT-103/view", **kwargs):
        response = self.client.get(path, headers=headers or {}, **kwargs)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_anonymous_sees_public_groups_with_labels_and_unknown_battery_is_404(self):
        body = self.view()
        self.assertEqual(body["field_groups"], PUBLIC)
        self.assertEqual(body["labels"], ["synthetic", "illustrative"])
        self.assertEqual(body["footprint"]["label"], "illustrative")
        self.assertEqual(body["identity"]["chemistry"], "NMC811")
        for hidden in ("performance", "composition", "supply_chain", "evidence", "ledger"):
            self.assertNotIn(hidden, body)
        self.assertNotIn("LOT-A", json.dumps(body))
        self.assertEqual(self.client.get("/passports/BAT-999/view").status_code, 404)
        self.assertEqual(self.client.get("/passports/BAT-999/view", headers=OP).status_code, 404)

    def test_query_headers_and_body_never_widen_the_view(self):
        tricks = [{"params": {"role": OPERATOR}}, {"params": {"roles": "Passport.Reviewer"}},
                  {"headers": {"X-Role": OPERATOR}}, {"headers": {"x-ms-client-principal-roles": OPERATOR}}]
        for trick in tricks:
            body = self.view(**trick)
            self.assertEqual(body["field_groups"], PUBLIC, trick)
        body = self.client.request("GET", "/passports/BAT-103/view", headers=OEM_H,
                                   params={"role": AUTHORITY}, json={"roles": [OPERATOR]}).json()
        self.assertEqual(body["field_groups"], PUBLIC + ["performance", "second_life"])

    def test_signed_in_roles_get_their_projection(self):
        self.assertEqual(self.view(OEM_H)["field_groups"], PUBLIC + ["performance", "second_life"])
        self.assertNotIn("second_life", self.view(OEM_H, "/passports/BAT-101/view"))
        recycler = self.view(REC_H)
        self.assertNotIn("second_life", recycler)
        self.assertEqual(recycler["composition"]["label"], "synthetic")
        self.assertTrue(recycler["composition"]["materials"])
        authority = self.view(AUTH)
        self.assertEqual(authority["field_groups"], STAFF[:-1])
        self.assertIn("LOT-B", {r["lot_id"] for r in authority["supply_chain"]["lots"]})
        operator = self.view(OP, "/passports/BAT-101/view")
        self.assertEqual(operator["evidence"]["scope"], "all")
        self.assertEqual({r["evidence_id"] for r in operator["evidence"]["references"]}, {self.claim, self.transport})
        self.assertNotIn("content", json.dumps(operator["evidence"]))

    def test_compliance_sees_only_compliance_case_evidence_references(self):
        adverse = self.client.post("/evidence", headers=COMP, data={
            "kind": "adverse_report", "lot_id": "LOT-CO", "command_id": "acc-ev-0001"},
            files={"file": ("report.json", b'{"report_id": "ADV-1"}', "application/json")}).json()["evidence_id"]
        compliance = self.view(COMP)
        self.assertEqual(compliance["evidence"]["references"], [])
        self.client.post("/compliance/cases", headers=COMP, json={
            "lot_id": "LOT-CO", "reason": "Adverse report names the mine", "evidence_ids": [adverse],
            "command_id": "acc-case-0001"})
        compliance = self.view(COMP)
        self.assertEqual(compliance["field_groups"], PUBLIC + ["due_diligence", "decisions", "evidence"])
        self.assertEqual([r["evidence_id"] for r in compliance["evidence"]["references"]], [adverse])
        co = next(r for r in compliance["due_diligence"]["critical_lots"] if r["lot_id"] == "LOT-CO")
        self.assertEqual(co["case_state"], "open")
        operator = {r["evidence_id"] for r in self.view(OP, "/passports/BAT-101/view")["evidence"]["references"]}
        self.assertEqual(operator, {self.claim, self.transport, adverse})

    def test_view_served_carries_only_coded_dimensions(self):
        self.view(COMP)
        self.client.get("/p/BAT-103", headers=OEM_H)
        self.client.get("/p/BAT-103/card")
        served = [(d, m) for n, d, m in self.sent if n == "view.served"]
        self.assertEqual([(d["field_set"], d["role_group"]) for d, _ in served],
                         [("id-fp-dd-dec-evc", "compliance"), ("id-fp-perf-sl", "oem")])
        for dims, _measures in served:
            telemetry.build("view.served",
                            **{k: v for k, v in dims.items() if not k.startswith("dbp.") and k != "event_id"})
            self.assertEqual(dims["battery_id"], "BAT-103")
        self.assertNotIn("otto", json.dumps(self.sent))
        self.assertNotIn("carmen", json.dumps(self.sent))


class PageTests(Screens):
    def test_public_page_shows_identity_and_footprint_and_signed_in_sections(self):
        page = self.client.get("/p/BAT-103").text
        for text in ("Chemistry", "NMC811", "Rated capacity", "Carbon footprint (illustrative)", "Recycled content",
                     "Lifecycle status"):
            self.assertIn(text, page)
        for hidden in ("Performance and holder", "Supply chain", "Current holder"):
            self.assertNotIn(hidden, page)
        recycler = self.client.get("/p/BAT-103", headers=REC_H).text
        self.assertIn("Composition, dismantling and safety", recycler)
        self.assertIn("Current holder", recycler)
        self.assertNotIn("Supply chain", recycler)
        self.assertIn("Supply chain", self.client.get("/p/BAT-103", headers=AUTH).text)

    def test_viewer_roles_get_a_passport_sidebar_and_staff_screens_stay_closed(self):
        for headers in (OEM_H, REC_H, AUTH):
            page = self.client.get("/p/BAT-103", headers=headers)
            self.assertEqual(page.status_code, 200)
            self.assertIn('id="side-passports"', page.text)
            self.assertIn('href="/p/BAT-101"', page.text)
            for path in ("/exec", "/trail/BAT-103", f"/holds/{self.review_id}", "/report/status"):
                self.assertEqual(self.client.get(path, headers=headers).status_code, 403, (path, headers))
        self.assertIn("Market surveillance", self.client.get("/p/BAT-103", headers=AUTH).text)
        self.assertNotIn("Market surveillance", self.client.get("/p/BAT-103", headers=OEM_H).text)
        self.assertEqual(self.client.get("/lifecycle/quarantine", headers=AUTH).status_code, 200)
        self.assertEqual(self.client.get("/lifecycle/quarantine", headers=REC_H).status_code, 403)
        self.assertEqual(self.client.get("/cases", headers=AUTH).status_code, 200)
        self.assertEqual(self.client.get("/cases", headers=OEM_H).status_code, 403)
        self.assertNotIn('id="side-passports"', self.client.get("/p/BAT-103", headers=NOBODY).text)
        self.assertNotIn('id="side-passports"', self.client.get("/p/BAT-103", headers=REV).text)

    def test_trail_shows_the_import_and_the_calculation_versions(self):
        trail = self.client.get("/trail/BAT-103", headers=OP).text
        for text in ("Imported package BUILD-03", "Composition and package import", "Footprint calculation",
                     "Method version", "Factor set version"):
            self.assertIn(text, trail)


if __name__ == "__main__":
    unittest.main()
