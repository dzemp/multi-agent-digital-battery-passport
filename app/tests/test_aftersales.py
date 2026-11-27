"""Exercise intake, entitlement and last-known exposure through the actual app routes."""

import base64
import json
import unittest

from fastapi.testclient import TestClient

import helpers
from passport.config import Settings
from passport.web import create_app

AW, FAB = "OEM-ADVENTUREWORKS", "OEM-FABRIKAM"


def local(user, role):
    return {"x-local-user": user, "x-local-roles": "Passport." + role}


AW_ROLE, FAB_ROLE = "Passport.Fleet.AdventureWorks", "Passport.Fleet.Fabrikam"
OP, A, F = local("op", "Operator"), local("aw", f"OEM,{AW_ROLE}"), local("fab", f"OEM,{FAB_ROLE}")


class AftersalesTests(unittest.TestCase):
    def setUp(self):
        self.service, self.blobs = helpers.new_service()
        self.service.clock = lambda: "2025-02-24T10:00:00.000Z"
        helpers.put_package(self.blobs, "BUILD-03", helpers.standard_package())
        status, body = self.service.import_package("BUILD-03", helpers.OPERATOR, "produce-001")
        self.assertEqual(status, 200, body)
        self.service.drain_outbox()
        settings = Settings(auth_mode="local", worker=False)
        self.client = TestClient(create_app(settings, self.service))
        self.addCleanup(self.client.close)

    def post(self, records=None, headers=OP, **params):
        return self.client.post(helpers.put_aftersales(self.blobs, records, **params), headers=headers)

    def view(self, battery="BAT-103", headers=A):
        return self.client.get(f"/passports/{battery}/view", headers=headers).json()

    def fleet(self, headers=A, **params):
        return self.client.get("/oem/fleet/view", headers=headers, params=params).json()

    def test_receipt_retry_conflict_and_rollback_leave_production_and_reports_unchanged(self):
        public = self.client.get("/passports/BAT-103").json()
        report = self.client.get("/report/status", headers=OP).json()
        with self.service.store.read() as tx:
            versions = tx.select("PassportVersions")
        response = self.post()
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(self.post().json()["replayed"], True)
        self.assertEqual(self.post(as_of="2025-02-23T12:00:00Z").status_code, 409)
        self.assertEqual(self.post(delivery="DEL-2", as_of="2025-02-20T12:00:00Z").status_code, 409)
        self.assertEqual(self.post(delivery="DEL-2", sha256="0" * 64).status_code, 422)
        later = {"delivery": "DEL-2", "as_of": "2025-02-23T12:00:00Z"}
        for row, field, value, status in (("packs", "batch_id", "BATCH-X", 422),
                                        ("packs", "oem_id", FAB, 409), ("readings", "actual_cycles", 77, 409),
                                        ("readings", "cycle_basis", "design_cycles", 422),
                                        ("campaigns", "battery_ids", ["OTHER-SERIAL"], 422)):
            with self.subTest(field=field):
                records = helpers.aftersales_records()
                records[row][0][field] = value
                self.assertEqual(self.post(records, **later).status_code, status)
        with self.service.store.read() as tx:
            self.assertEqual(len(tx.select("AftersalesReceipts")), 1)
            self.assertEqual(len(tx.select("ServiceReadings")), 2)
            self.assertEqual(tx.select("WarrantyTerms", delivery_id="DEL-2"), [])
            self.assertEqual(versions, tx.select("PassportVersions"))
        self.assertEqual(report, self.client.get("/report/status", headers=OP).json())
        self.assertEqual(public, self.client.get("/passports/BAT-103").json())

    def test_role_principal_pack_and_persona_are_all_enforced_before_drilldown(self):
        url = helpers.put_aftersales(self.blobs)
        for headers, denied in (({}, 401), (A, 403), (local("op", "Reviewer"), 403)):
            self.assertEqual(self.client.post(url, headers=headers).status_code, denied)
        self.assertEqual(self.client.post(url, headers=OP, json={"records": {}}).status_code, 422)
        self.assertEqual(self.client.post(url, headers=OP).status_code, 201)
        ambiguous = local("aw", f"OEM,{AW_ROLE},{FAB_ROLE}")
        qualifier_only = local("aw", "Fleet.AdventureWorks")
        for headers in ({}, F, OP, local("aw", "Recycler"), local("aw", "Authority"), ambiguous, qualifier_only):
            arguments = {"params": {"oem_id": AW}, "headers": {**headers, "x-oem-id": AW}}
            denied = self.client.get("/passports/BAT-103/view", **arguments)
            self.assertEqual(denied.status_code, 200)
            page = self.client.get("/p/BAT-103", **arguments).text
            self.assertTrue({"warranty", "recall", "second_life"}.isdisjoint(denied.json()))
            self.assertNotIn('id="sec-warranty"', page)
            self.assertNotIn("SERVICE-AW", page + denied.text)
        base_only = self.view(headers=local("aw", "OEM"))
        self.assertIn("second_life", base_only)
        self.assertTrue({"warranty", "recall"}.isdisjoint(base_only))
        self.assertNotIn("warranty", self.view("BAT-102"))
        self.assertNotIn("SERVICE-AW", self.client.get("/p/BAT-103/card", headers=A).text)
        for path in ("/oem/fleet", "/oem/fleet/view"):
            for headers, status in (({}, 401), (local("unmapped", "OEM"), 403), (OP, 403),
                                    (ambiguous, 403), (qualifier_only, 403)):
                self.assertEqual(self.client.get(path, headers=headers).status_code, status)
        multi = {**A, "x-local-roles": f"Passport.Recycler,Passport.OEM,Passport.Operator,{AW_ROLE}"}
        self.client.post("/personas", headers=multi, data={"persona": "recycler"})
        self.assertNotIn("warranty", self.view(headers=multi))
        self.assertEqual(self.client.get("/oem/fleet/view", headers=multi).status_code, 403)
        self.client.post("/personas", headers=multi, data={"persona": "oem"})
        self.assertIn("warranty", self.view(headers=multi))
        self.assertEqual(self.client.post(url, headers=multi).status_code, 403)

    def test_aggregate_applies_entitlement_first_and_uses_current_holder(self):
        self.post()
        result = self.fleet()
        self.assertEqual(result["affected_pack_count"], 2)  # duplicate batch, serial and campaign matches count once
        self.assertEqual(self.fleet(F)["affected_pack_count"], 1)
        self.assertNotIn("BAT-102", self.client.get("/oem/fleet", headers=A).text)
        self.assertNotIn("SERVICE-AW", self.client.get("/oem/fleet", headers=F).text)
        self.assertEqual(self.fleet(F, campaign_id="SERVICE-AW")["affected_pack_count"], 0)
        self.assertIsNone(self.fleet(series_id="OTHER")["affected_pack_count"])
        with self.service.store.transaction() as tx:
            state = tx.get("PassportState", "BAT-103")
            tx.update("PassportState", {"battery_id": "BAT-103", "holder_id": FAB}, state["row_version"])
        result = self.fleet(series_id="SERIES-V3")
        self.assertEqual(result["holders"], [{"holder_id": AW, "affected_pack_count": 1},
                                             {"holder_id": FAB, "affected_pack_count": 1}])
        self.assertEqual([p["passport_id"] for p in self.fleet(holder_id=FAB)["packs"]], ["BAT-103"])
        self.assertEqual(self.fleet(F, holder_id=FAB)["packs"], [])
        page = self.client.get("/oem/fleet?holder_id=OEM-FABRIKAM", headers=A).text
        self.assertIn('href="/p/BAT-103?series=SERIES-V3"', page)
        self.assertNotIn("BAT-101", page)

    def test_near_and_exceeded_dates_and_efc_are_separate_from_design_and_trend(self):
        self.post()
        warranty = self.view()["warranty"]
        self.assertEqual((warranty["expected_cycles"], warranty["actual_cycles"], warranty["warranty_cycles"]),
                         (3000, 96, 100))
        self.assertEqual((warranty["time_status"], warranty["cycle_status"], warranty["warranty_end_date"]),
                         ("near_limit", "near_limit", "2025-02-28"))
        self.assertEqual((warranty["soh_trend"]["change_percentage_points"], warranty["soh_trend"]["from_utc"]),
                         (-4, "2025-01-24T09:00:00.000Z"))
        page = self.client.get("/p/BAT-103", headers=A).text
        self.assertIn("96 equivalent full cycles", page)
        self.assertIn("-4 pp", page)
        records = helpers.aftersales_records()
        records["readings"].append({**records["readings"][-1], "reading_id": "EFC-3", "actual_cycles": 100,
                                    "measured_utc": "2025-02-28T00:00:00Z", "soh_pct": None})
        records["campaigns"][0]["status"] = "closed"
        response = self.post(records, delivery="DEL-2", as_of="2025-02-28T12:00:00Z")
        self.assertEqual(response.status_code, 201, response.text)
        self.service.clock = lambda: "2025-02-28T12:30:00.000Z"
        w = self.view()["warranty"]
        self.assertEqual((w["time_status"], w["cycle_status"], w["days_remaining"], w["cycles_remaining"]),
                         ("exceeded", "exceeded", 0, 0))
        self.assertEqual(self.fleet(F)["affected_pack_count"], 0)
        self.assertEqual(self.view("BAT-102", F)["recall"]["exposure"], "no_active_campaign")

    def test_series_selector_narrows_already_entitled_packs(self):
        self.post()
        battery = {**helpers.battery("BAT-201", "LOT-C"), "series_id": "SERIES-NEXT"}
        files = helpers.make_package("BUILD-04", 3, [battery])
        manifest = json.loads(files["manifest.json"])
        files["manifest.json"] = helpers.dumps({**manifest, "series_id": "SERIES-NEXT"})
        helpers.put_package(self.blobs, "BUILD-04", files)
        status, body = self.service.import_package("BUILD-04", helpers.OPERATOR, "produce-002")
        self.assertEqual(status, 200, body)
        records = helpers.aftersales_records()
        records["packs"] = [{**records["packs"][0], "battery_id": "BAT-201"}]
        records["readings"] = []
        records["campaigns"] = [{**records["campaigns"][0], "battery_ids": ["BAT-201"]}]
        result = self.post(records, delivery="DEL-NEXT", series_id="SERIES-NEXT")
        self.assertEqual(result.status_code, 201, result.text)
        self.assertEqual(self.fleet()["affected_pack_count"], 3)
        self.assertEqual(self.fleet(series_id="SERIES-V3")["affected_pack_count"], 2)
        selected = self.fleet(series_id="SERIES-NEXT")
        self.assertEqual([p["physical_battery_id"] for p in selected["packs"]], ["BAT-201"])
        self.assertIsNone(self.fleet(F, series_id="SERIES-NEXT")["affected_pack_count"])

    def test_unknown_and_last_known_are_not_a_fresh_negative_or_a_zero_trend(self):
        self.assertEqual(self.fleet()["feed_status"], "unavailable")
        self.assertIsNone(self.fleet()["affected_pack_count"])
        self.assertEqual(self.view()["oem_data_status"], "unavailable")
        records = helpers.aftersales_records()
        records["packs"][-1].update(in_service_date=None, term_version=None, warranty_months=None, warranty_cycles=None)
        records["readings"].pop()
        records["campaigns"] = []
        self.post(records)
        w = self.view()["warranty"]
        self.assertEqual((w["time_status"], w["cycle_status"], w["soh_trend"]),
                         ("insufficient_data", "insufficient_data", None))
        no_reading = self.view("BAT-101")["warranty"]
        self.assertIsNone(no_reading["actual_cycles"])
        self.assertEqual(no_reading["cycle_status"], "insufficient_data")
        self.assertEqual(self.view()["recall"]["exposure"], "no_active_campaign")
        self.service.clock = lambda: "2025-04-01T10:00:00.000Z"
        recall = self.view()["recall"]
        self.assertEqual((recall["feed_status"], recall["exposure"]), ("stale", "unknown"))
        self.assertEqual(self.fleet()["feed_status"], "stale")

    def test_easy_auth_requires_exactly_one_authentic_fleet_role(self):
        self.post()
        settings = Settings(auth_mode="easyauth", worker=False)

        def headers(*roles):
            principal = {"claims": [{"typ": "roles", "val": role} for role in roles] +
                                   [{"typ": "name", "val": AW_ROLE}, {"typ": "oid", "val": "test-principal"}]}
            return {**A, "x-ms-client-principal": base64.b64encode(json.dumps(principal).encode()).decode()}
        with TestClient(create_app(settings, self.service)) as client:
            for roles in ((FAB_ROLE,), ("Passport.OEM",), ("Passport.OEM", AW_ROLE, FAB_ROLE)):
                self.assertEqual(client.get("/oem/fleet/view", headers=headers(*roles)).status_code, 403)
            fab = headers("Passport.OEM", FAB_ROLE)
            self.assertEqual(client.get("/oem/fleet/view", headers=fab).json()["oem_id"], FAB)
            self.assertNotIn("warranty", client.get("/passports/BAT-103/view", headers=fab).json())
