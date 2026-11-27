"""Lifecycle state machine, second-life approval and recycling closure over the mock connector and the
Lifecycle agent task (a fixed runner proposes the routed event, as the scripted double does)."""

import base64
import json
import re
import unittest
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

import helpers
from passport import domain, lifecycle, telemetry
from passport.agent_contracts import (CONTRACT, EVENT_FROM, POLICY, ROLE_INSTRUCTIONS, ROLES, SECTIONS, SOURCES,
                                      instructions)
from passport.agent_service import AgentService
from passport.config import Settings
from passport.web import create_app

NOW = datetime.now(timezone.utc).replace(microsecond=0)
LINK = {"x-local-user": "cora", "x-local-roles": "Passport.Connector"}
OP = {"x-local-user": "olivia", "x-local-roles": "Passport.Operator"}
REV = {"x-local-user": "rafael", "x-local-roles": "Passport.Reviewer"}
AUTH = {"x-local-user": "ada", "x-local-roles": "Passport.Authority"}
OEM_VIEW = {"x-local-user": "otto", "x-local-roles": "Passport.OEM"}
NOBODY = {"x-local-user": "nora", "x-local-roles": ""}
AGENT = {"x-local-user": "lifecycle-agent", "x-local-roles": "Passport.Agent.Lifecycle"}
OEM, OPERATOR, RECYCLER = domain.MANUFACTURER, "SECONDLIFE-RELECLOUD", "RECYCLER-TREY"
PARTIES = {OEM: ("oem", ["record_soh", "record_ownership_change", "record_end_of_life"]),
           OPERATOR: ("second_life_operator", ["request_repurpose"]),
           RECYCLER: ("recycler", ["record_recycling_closure"])}
KEYS = {party: Ed25519PrivateKey.generate() for party in PARTIES}


def utc(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def registry():
    entries = []
    for party, (role, events) in PARTIES.items():
        raw = KEYS[party].public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        entries.append({"party_id": party, "name": f"{party} (fictional)", "role": role, "alg": "Ed25519",
                        "key_id": f"{party}-KEY-1", "public_key": base64.b64encode(raw).decode("ascii"),
                        "lot_ids": [], "event_types": events, "revoked": False})
    return {"schema": "dbp-party-registry-1", "audience": "CONTOSO-BATTERY", "data_label": "synthetic",
            "parties": entries}


def sign(payload):
    value = KEYS[payload["issuer"]].sign(domain.canonical(payload))
    return {"payload": payload, "signature": {"alg": "Ed25519", "key_id": f"{payload['issuer']}-KEY-1",
                                              "value": base64.b64encode(value).decode("ascii")}}


class Runner:
    """Proposes the action named by the routed event, after reading every section; action overrides it."""

    action = None

    def run(self, role, task_id, context, section, *, attempt_id=None):
        for name in SECTIONS[role]:
            section(task_id, role, name)
        return {"action": self.action or context["event"]["event_type"], "reason": "Record the routed event.",
                "evidence_ids": []}, {"model": "fixed-test-runner"}


class ContractTests(unittest.TestCase):
    def test_contract_two_gives_each_role_its_own_limits_only(self):
        self.assertEqual((CONTRACT, POLICY), ("dbp-agent-2", "synthetic-agent-policy-2"))
        self.assertEqual(EVENT_FROM, lifecycle.ALLOWED_FROM)
        limits = {"supplier": "cannot clear, resolve or decide", "carbon": "Do no arithmetic",
                  "issuance": "no sourcing review or compliance case is open",
                  "lifecycle": "cannot approve or reject a second life", "watch": "Never activate policy."}
        for role in ROLES:
            with self.subTest(role=role):
                text = instructions(role)
                self.assertIn(limits[role], text)
                self.assertIn("synthetic battery passport MVP", text)
                self.assertLessEqual(len(text), 1600)
                for other in set(ROLES) - {role}:
                    self.assertNotIn(ROLE_INSTRUCTIONS[other][:12], text)
        self.assertIn("Cite exactly the routed event ID", instructions("lifecycle"))


class Lifecycle(unittest.TestCase):
    """BUILD-03 with BAT-101..103 published (version 1, holder the manufacturer) before agent mode is enabled."""

    def setUp(self):
        self.service, blobs = helpers.new_service()
        helpers.put_package(blobs, "BUILD-03", helpers.standard_package())
        status, body = self.service.import_package("BUILD-03", helpers.OPERATOR, "import-lc-0001")
        self.assertEqual([b["committed_version"] for b in body["batteries"]], [1, 1, 1], body)
        self.service.agent_mode = True
        self.sent, self.hours = [], 48
        self.runner = Runner()
        sink = telemetry.Telemetry(lambda name, dims, measures: self.sent.append((name, dims)))
        app = create_app(Settings(auth_mode="local", worker=False, agent_mode=True, party_registry=registry()),
                         self.service, agent_runner=self.runner, telemetry=sink)
        self.client = TestClient(app)
        for party, (_, events) in PARTIES.items():
            response = self.client.post("/connector/v1/agreements", headers=LINK, json={
                "agreement_id": f"AGR-{party}", "sender": party, "purpose": "Synthetic lifecycle events for tests.",
                "message_types": events, "valid_from": utc(NOW - timedelta(days=5)),
                "valid_until": utc(NOW + timedelta(days=60)), "command_id": f"agr-{party.lower()}"[:64]})
            self.assertEqual(response.status_code, 201, response.text)

    def tearDown(self):
        self.client.close()

    def test_policy_approval_requires_the_dedicated_role(self):
        self.assertEqual(self.client.get("/policy", headers=OP).status_code, 403)
        approver = {"x-local-user": "pat", "x-local-roles": "Passport.PolicyApprover"}
        self.assertEqual(self.client.get("/policy", headers=approver).status_code, 200)
        for verb in ("activate", "reject"):
            denied = self.client.post(f"/policy/candidates/watch-none-0001/{verb}", headers=OP,
                                      json={"expected_row_version": 1, "reason": "Not my decision."})
            self.assertEqual(denied.status_code, 403, verb)
        missing = self.client.post("/policy/candidates/watch-none-0001/reject", headers=approver,
                                   json={"expected_row_version": 1, "reason": "Nothing to reject."})
        self.assertEqual((missing.status_code, missing.json()["error"]), (404, "policy_candidate_not_found"))

    def test_policy_page_labels_ai_drafts_and_rejects_by_form(self):
        agents = AgentService(self.service)
        task = agents.prepare("watch", "battery-due-diligence", None, helpers.OPERATOR, "watch-page-0001")
        for name in SECTIONS["watch"]:
            agents.section(task["task_id"], "watch", name)
        status, body = agents.finish(task, {
            "action": "recommend", "reason": "Update the synthetic operational guidance.",
            "evidence_ids": [source["id"] for source in SOURCES], "policy_title": "Reviewed guidance",
            "policy_text": "Use the reviewed due-diligence schedule."}, {"hosting": "prompt", "model": "gpt-5.4"})
        self.assertEqual(status, 200, body)
        approver = {"x-local-user": "pat", "x-local-roles": "Passport.PolicyApprover"}
        page = self.client.get("/policy", headers=approver).text
        self.assertIn('data-ai-generated="true"', page)
        self.assertIn("AI-drafted</span>\n      by the Watch agent (gpt-5.4", page)
        self.assertIn('id="reject-watch-page-0001"', page)
        self.assertIn("Baseline written by people", page)
        rejected = self.client.post("/policy/watch-page-0001/reject", headers=approver, follow_redirects=False,
                                    data={"expected_row_version": "1", "reason": "Not justified by the sources."})
        self.assertEqual((rejected.status_code, rejected.headers["location"]), (303, "/policy?done=rejected"))
        self.assertEqual(agents.policy_candidates()[0]["status"], "rejected")
        self.assertEqual(agents.active_policy()["version"], 2)

    def test_provenance_verify_is_limited_to_expert_roles(self):
        body = {"text": "Text this application never generated."}
        self.assertEqual(self.client.post("/provenance/verify", headers=REV, json=body).status_code, 403)
        for headers in (OP, AUTH):
            found = self.client.post("/provenance/verify", headers=headers, json=body)
            self.assertEqual((found.status_code, found.json()["match"]), (200, False))
        extra = self.client.post("/provenance/verify", headers=OP, json={**body, "actor": "x"})
        self.assertEqual((extra.status_code, extra.json()["error"]), (422, "unknown_fields"))

    def payload(self, issuer, event_type, battery_id, name=None, hours=None, **details):
        if hours is None:
            self.hours -= 1
            hours = self.hours
        return {"schema": "dbp-partner-event-1", "issuer": issuer, "audience": "CONTOSO-BATTERY",
                "message_id": f"MSG-{name or event_type.upper().replace('_', '-')}-{battery_id}",
                "issued_utc": utc(NOW - timedelta(hours=hours)), "expires_utc": utc(NOW + timedelta(days=30)),
                "data_label": "synthetic", "event_type": event_type, "battery_id": battery_id, **details}

    def state(self, battery_id):
        return self.service.restricted_passport(battery_id)["state"]

    def apply(self, issuer, event_type, battery_id, **details):
        """Send the signed event; when routed, run its Lifecycle task. Returns the lifecycle outcome."""
        payload = self.payload(issuer, event_type, battery_id, **details)
        response = self.client.post("/connector/v1/messages", headers=LINK, json={"envelope": sign(payload)})
        body = response.json()
        if body["result"] == "quarantined":
            return {"status": "quarantined", "reason_code": body["reason_code"], "http": response.status_code}
        self.assertEqual((response.status_code, body["result"]), (201, "routed"), body)
        return self.run_task(body["lifecycle_event"], f"task-{payload['message_id']}"[:64])

    def run_task(self, event, task_id, headers=OP):
        event = {**event, "expected_passport_version": self.state(event["battery_id"])["committed_version"]}
        response = self.client.post("/agents/lifecycle/run", headers=headers,
                                    json={"task_id": task_id, "subject": event["battery_id"], "event": event})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["outcome"]

    def soh(self, battery_id, pct, **kw):
        return self.apply(OEM, "record_soh", battery_id, soh_pct=pct, measured_utc=utc(NOW - timedelta(days=3)),
                          **kw)

    def to_operator(self, battery_id):
        return self.apply(OEM, "record_ownership_change", battery_id, from_party_id=OEM, to_party_id=OPERATOR)

    def repurpose(self, battery_id, name=None, **changes):
        details = {"successor_battery_id": f"{battery_id}-SL1", "intended_use": "stationary energy storage",
                   "safety_assessment_ref": f"SAFETY-{battery_id}", "illustrative_value": 2700.0,
                   "value_currency": "EUR", "value_label": "illustrative, not a sale price", **changes}
        details = {k: v for k, v in details.items() if v is not None}
        return self.apply(OPERATOR, "request_repurpose", battery_id, name=name, **details)

    def decide(self, request_id, decision, version, headers=REV, command_id=None, reason="Safety assessment read"):
        return self.client.post(f"/second-life/{request_id}/decision", headers=headers, json={
            "decision": decision, "expected_row_version": version, "reason": reason,
            "command_id": command_id or f"sl-{decision}-{version}-0001"})

    def next_card(self):
        page = self.client.get("/exec", headers=REV).text
        return re.search(r'<div class="next">(.*?)</div>', page, re.S).group(1)


class SecondLifeTests(Lifecycle):
    def test_qualified_fleet_tracks_physical_serial_through_approved_successor(self):
        self.assertEqual(self.client.post(helpers.put_aftersales(self.service.blobs), headers=OP).status_code, 201)
        aw = {**OEM_VIEW, "x-local-roles": "Passport.OEM,Passport.Fleet.AdventureWorks"}
        fab = {**OEM_VIEW, "x-local-roles": "Passport.OEM,Passport.Fleet.Fabrikam"}
        self.soh("BAT-103", 88)
        self.to_operator("BAT-103")
        self.repurpose("BAT-103")
        self.decide("SL-0001", "approve", 1)
        fleet = self.client.get("/oem/fleet/view", headers=aw).json()
        self.assertEqual(fleet["affected_pack_count"], 2)
        at_operator = [r for r in fleet["packs"] if r["holder_id"] == OPERATOR]
        self.assertEqual([(r["physical_battery_id"], r["passport_id"]) for r in at_operator],
                         [("BAT-103", "BAT-103-SL1")])
        view = self.client.get("/passports/BAT-103-SL1/view", headers=aw).json()
        self.assertEqual((view["warranty"]["physical_battery_id"], view["warranty"]["actual_cycles"]), ("BAT-103", 96))
        self.assertEqual(view["warranty"]["soh_trend"]["change_percentage_points"], -4)
        self.assertEqual(view["second_life"]["inherited_from"], "BAT-103")
        self.assertNotIn("BAT-103", self.client.get("/oem/fleet/view", headers=fab).text)
        for battery in ("BAT-103", "BAT-103-SL1"):
            other = self.client.get(f"/passports/{battery}/view", headers=fab).json()
            self.assertTrue({"warranty", "recall", "second_life"}.isdisjoint(other))
            self.assertNotIn("SERVICE-AW", self.client.get(f"/p/{battery}", headers=fab).text)

    def test_oem_sees_health_screen_and_recorded_decision_but_not_the_reviewer_record(self):
        def summary(battery="BAT-101"):
            return self.client.get(f"/passports/{battery}/view", headers=OEM_VIEW).json()["second_life"]

        self.assertIsNone(summary()["meets_health_screen"])
        self.soh("BAT-101", 70)
        self.assertTrue(summary()["meets_health_screen"])
        self.assertIsNone(summary()["request_status"])
        self.to_operator("BAT-101")
        self.repurpose("BAT-101")
        pending = summary()
        self.assertEqual((pending["request_status"], pending["safety_reference_supplied"],
                          pending["illustrative_value_eur"], pending["decided_utc"]), ("pending", True, 2700, None))
        self.assertEqual(self.decide("SL-0001", "approve", 1, OEM_VIEW).status_code, 403)
        self.assertEqual(self.client.get("/second-life/SL-0001", headers=OEM_VIEW).status_code, 403)
        self.decide("SL-0001", "approve", 1)
        approved = summary()
        self.assertEqual(approved["request_status"], "approved")
        self.assertTrue(approved["decided_utc"])
        self.assertEqual(summary("BAT-101-SL1"),
                         {**approved, "inherited_from": "BAT-101", "lifecycle_status": "active"})
        page = self.client.get("/p/BAT-101", headers=OEM_VIEW).text
        for text in ("Second-life decision", "Approved", "EUR 2,700.00", "not a sale price", "EUR 45",
                     'href="/p/BAT-101-SL1"'):
            self.assertIn(text, page)
        for private in ("SAFETY-BAT-101", "Safety assessment read", 'name="decision"'):
            self.assertNotIn(private, page + json.dumps(approved))
        for path, headers in (("/p/BAT-101/card", OEM_VIEW), ("/p/BAT-101", {}),
                              ("/p/BAT-101", {"x-local-user": "rita", "x-local-roles": "Passport.Recycler"})):
            self.assertNotIn('id="sec-second_life"', self.client.get(path, headers=headers).text)

    def test_reviewer_approval_closes_the_original_and_creates_a_linked_successor(self):
        self.assertEqual(self.soh("BAT-101", 88)["status"], "recorded")
        self.assertEqual(self.to_operator("BAT-101")["state_after"], "active")
        self.assertEqual(self.state("BAT-101")["holder_id"], OPERATOR)
        requested = self.repurpose("BAT-101")
        self.assertEqual((requested["status"], requested["state_after"]), ("recorded", "second_life_pending"))
        self.assertEqual(requested["request"], {"request_id": "SL-0001", "illustrative_value_eur": 2700.0,
                                                "event_value_matches": True})
        self.assertEqual(self.state("BAT-101")["committed_version"], 4)  # one new version per recorded event
        for headers in (AGENT, OP, AUTH):
            self.assertEqual(self.decide("SL-0001", "approve", 1, headers).status_code, 403, headers)
        self.assertEqual(self.decide("SL-0001", "approve", 7).json()["error"], "version_conflict")
        approved = self.decide("SL-0001", "approve", 1, command_id="sl-approve-0002").json()
        self.assertEqual((approved["status"], approved["successor_id"], approved["passport_version"]),
                         ("approved", "BAT-101-SL1", 5))
        original, successor = self.state("BAT-101"), self.state("BAT-101-SL1")
        self.assertEqual((original["lifecycle_status"], original["eligibility"], original["hold_reason"]),
                         ("repurposed", "blocked", "passport_closed"))
        self.assertEqual((successor["lifecycle_status"], successor["holder_id"], successor["committed_version"]),
                         ("active", OPERATOR, 1))
        self.assertEqual(self.client.get("/passports/BAT-101/links").json()["links"],
                         [{"battery_id": "BAT-101-SL1", "link_type": "successor"}])
        self.assertEqual(self.client.get("/passports/BAT-101-SL1/links").json()["links"],
                         [{"battery_id": "BAT-101", "link_type": "predecessor"}])
        self.service.drain_outbox()
        snap = json.loads(self.service.blobs.get("passports", "BAT-101-SL1/v1.json"))
        self.assertEqual(snap["public"]["lifecycle"]["status"], "active")
        self.assertEqual(self.service.board()["text"], "3 of 3 passports committed; 1 successor passport")
        closed = self.soh("BAT-101", 80, name="SOH-CLOSED")
        self.assertEqual((closed["status"], closed["reason_code"], closed["http"]),
                         ("quarantined", "passport_closed", 202))
        self.assertEqual(self.decide("SL-0001", "reject", 2, command_id="sl-reject-0003").json()["error"],
                         "request_not_pending")
        names = [name for name, _ in self.sent]
        for name in ("lifecycle.event", "second_life.decided", "passport.closed", "passport.successor.created"):
            self.assertIn(name, names)
        results = [(d["event_type"], d["result"], d["reason_code"]) for n, d in self.sent if n == "lifecycle.event"]
        self.assertIn(("record_soh", "quarantined", "passport_closed"), results)
        self.assertNotIn("Safety assessment read", json.dumps(self.sent))

    def test_reject_returns_to_active_and_the_request_gates_quarantine(self):
        self.assertEqual(self.soh("BAT-103", 60)["status"], "recorded")
        self.to_operator("BAT-103")
        self.assertEqual(self.repurpose("BAT-103")["reason_code"], "soh_below_minimum")
        self.soh("BAT-102", 88)
        self.to_operator("BAT-102")
        missing = self.repurpose("BAT-102", name="NOREF", safety_assessment_ref=None)
        self.assertEqual(missing["reason_code"], "safety_reference_missing")
        self.assertEqual(self.state("BAT-102")["committed_version"], 3)  # quarantined events add no version
        self.assertEqual(self.soh("BAT-102", 75, name="SOH-2")["reason_code"], "not_current_holder")
        self.assertEqual(self.client.get("/lifecycle/quarantine", headers=OP).json()["events"][-1]["reason_code"],
                         "not_current_holder")
        self.assertEqual(self.client.get("/lifecycle/quarantine", headers=REV).status_code, 403)
        requested = self.repurpose("BAT-102", name="OK")
        self.assertEqual(requested["request"]["request_id"], "SL-0001")
        rejected = self.decide("SL-0001", "reject", 1).json()
        self.assertEqual((rejected["status"], rejected["lifecycle_status"], rejected["passport_version"]),
                         ("rejected", "active", 5))
        self.assertEqual(self.state("BAT-102")["lifecycle_status"], "active")
        self.assertEqual(self.client.get("/passports/BAT-102/links").json()["links"], [])
        summary = self.client.get("/passports/BAT-102/view", headers=OEM_VIEW).json()["second_life"]
        self.assertEqual((summary["request_status"], summary["meets_health_screen"]), ("rejected", True))

    def test_an_action_outside_the_lifecycle_role_is_refused(self):
        payload = self.payload(OEM, "record_soh", "BAT-101", soh_pct=88, measured_utc=utc(NOW - timedelta(days=3)))
        event = self.client.post("/connector/v1/messages", headers=LINK,
                                  json={"envelope": sign(payload)}).json()["lifecycle_event"]
        self.runner.action = "publish"
        response = self.client.post("/agents/lifecycle/run", headers=OP, json={
            "task_id": "task-scope-0001", "subject": "BAT-101", "event": {
                **event, "expected_passport_version": self.state("BAT-101")["committed_version"]}})
        self.assertEqual((response.status_code, response.json()["error"]), (403, "action_scope"))
        self.assertEqual(self.state("BAT-101")["committed_version"], 1)

    def test_screens_show_the_request_to_reviewer_and_operator_and_only_the_reviewer_decides(self):
        self.soh("BAT-101", 88)
        self.to_operator("BAT-101")
        self.repurpose("BAT-101")
        html = {"accept": "text/html"}
        for path in ("/second-life", "/second-life/SL-0001"):
            for headers in (REV, OP):
                page = self.client.get(path, headers={**headers, **html})
                self.assertEqual(page.status_code, 200, (path, headers))
                self.assertIn("Illustrative value", page.text)
                self.assertIn("Synthetic data", page.text)
            for headers in (AUTH, NOBODY, AGENT):
                self.assertEqual(self.client.get(path, headers={**headers, **html}).status_code, 403)
        self.assertNotIn('name="decision"', self.client.get("/second-life/SL-0001", headers={**OP, **html}).text)
        page = self.client.get("/second-life/SL-0001", headers={**REV, **html}).text
        self.assertIn("SAFETY-BAT-101", page)
        self.assertIn("Second-life approvals", self.client.get("/exec", headers=REV).text)
        card = self.next_card()
        self.assertIn('href="/second-life/SL-0001">Decide the second-life request for BAT-101', card)
        posted = self.client.post("/second-life/SL-0001/decision", headers=REV, follow_redirects=False, data={
            "decision": "approve", "reason": "Safety assessment read",
            "command_id": re.search(r'name="command_id" value="([^"]+)"', page).group(1),
            "expected_row_version": re.search(r'name="expected_row_version" value="([^"]+)"', page).group(1)})
        self.assertEqual(posted.status_code, 303, posted.text)
        done = self.client.get(posted.headers["location"], headers={**REV, **html}).text
        self.assertIn('href="/exec">Back to passport readiness', done)
        self.assertIn('href="/p/BAT-101-SL1">Open the second-life passport BAT-101-SL1', self.next_card())
        public = self.client.get("/p/BAT-101").text
        self.assertIn("Closed - repurposed", public)
        self.assertIn('href="/p/BAT-101-SL1"', public)
        self.assertNotIn("Current holder", public)
        self.assertIn("Current holder", self.client.get("/p/BAT-101-SL1", headers=OP).text)
        trail = self.client.get("/trail/BAT-101", headers=REV).text
        self.assertIn("Approved the second life", trail)
        self.assertIn("Second-life request SL-0001", trail)


class EndOfLifeTests(Lifecycle):
    def test_end_of_life_then_recycling_closure_and_the_forbidden_or_repeated_events(self):
        self.assertEqual(self.apply(RECYCLER, "record_recycling_closure", "BAT-103", name="EARLY")["reason_code"],
                         "state_not_allowed")
        self.assertEqual(self.soh("BAT-103", 62)["status"], "recorded")
        late = self.soh("BAT-103", 64, name="OLD-SOH", hours=200)
        self.assertEqual(late["reason_code"], "event_out_of_order")
        ended = self.apply(OEM, "record_end_of_life", "BAT-103", reason="soh_below_second_life_minimum",
                           handed_to_party_id=RECYCLER)
        self.assertEqual((ended["state_after"], self.state("BAT-103")["holder_id"]), ("end_of_life", RECYCLER))
        self.assertEqual(self.soh("BAT-103", 61, name="SOH-3")["reason_code"], "state_not_allowed")
        payload = self.payload(RECYCLER, "record_recycling_closure", "BAT-103", recovered_kg={"cobalt": 4.0})
        routed = self.client.post("/connector/v1/messages", headers=LINK, json={"envelope": sign(payload)}).json()
        closed = self.run_task(routed["lifecycle_event"], "task-closure-0001")
        self.assertEqual((closed["state_after"], closed["closed"]), ("recycled", True))
        again = self.client.post("/connector/v1/messages", headers=LINK, json={"envelope": sign(payload)})
        self.assertEqual((again.status_code, again.json()["result"]), (201, "routed"))
        self.assertEqual(self.run_task(routed["lifecycle_event"], "task-closure-0002")["status"], "already_recorded")
        versions = [v["version"] for v in self.service.restricted_passport("BAT-103")["versions"]]
        self.assertEqual(versions, [1, 2, 3, 4])
        self.assertIn("Closed - recycled", self.client.get("/p/BAT-103").text)
        summary = self.client.get("/passports/BAT-103/view", headers=OEM_VIEW).json()["second_life"]
        self.assertEqual((summary["soh_pct"], summary["meets_health_screen"], summary["lifecycle_status"]),
                         (62, False, "recycled"))
        self.assertIsNone(summary["request_status"])
        self.assertIsNone(summary["illustrative_value_eur"])
        self.assertEqual(self.service.restricted_passport("BAT-103")["state"]["hold_reason"], "passport_closed")
        self.assertEqual(self.client.post(helpers.put_aftersales(self.service.blobs), headers=OP).status_code, 201)
        qualified = {**OEM_VIEW, "x-local-roles": "Passport.OEM,Passport.Fleet.AdventureWorks"}
        self.assertNotIn("BAT-103", self.client.get("/oem/fleet/view", headers=qualified).text)
        self.assertIn("SERVICE-AW", self.client.get("/passports/BAT-103/view", headers=qualified).text)


if __name__ == "__main__":
    unittest.main()
