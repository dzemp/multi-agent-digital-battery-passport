"""Party registry, signed partner messages and the mock connector. Keys are generated per test run."""

import base64
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

import helpers
from passport import domain, exec_views, telemetry
from passport.agent_service import AgentService
from passport.config import Settings
from passport.store import MemoryStore
from passport.web import create_app

NOW = datetime.now(timezone.utc).replace(microsecond=0)
LINK = {"x-local-user": "cora", "x-local-roles": "Passport.Connector"}
OP = {"x-local-user": "olivia", "x-local-roles": "Passport.Operator"}
AUTH = {"x-local-user": "ada", "x-local-roles": "Passport.Authority"}
REV = {"x-local-user": "rafael", "x-local-roles": "Passport.Reviewer"}
PARTIES = {"SUP-A": ("supplier", ["LOT-A"], []), "SUP-B": ("supplier", ["LOT-B"], []),
           "SUP-CO": ("supplier", ["LOT-CO"], []), "OEM-AW": ("oem", [], ["record_soh", "record_end_of_life"])}
KEYS = {party: Ed25519PrivateKey.generate() for party in PARTIES}


def utc(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def registry(revoked=(), only=None):
    entries = []
    for party, (role, lots, events) in PARTIES.items():
        if only is not None and party not in only:
            continue
        raw = KEYS[party].public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        entries.append({"party_id": party, "name": f"{party} (fictional)", "role": role, "alg": "Ed25519",
                        "key_id": f"{party}-KEY-1", "public_key": base64.b64encode(raw).decode("ascii"),
                        "lot_ids": lots, "event_types": events, "revoked": party in revoked})
    return {"schema": "dbp-party-registry-1", "audience": "CONTOSO-BATTERY", "data_label": "synthetic",
            "parties": entries}


def common(schema, issuer, message_id, **changes):
    return {"schema": schema, "issuer": issuer, "audience": "CONTOSO-BATTERY", "message_id": message_id,
            "issued_utc": utc(NOW - timedelta(hours=1)), "expires_utc": utc(NOW + timedelta(days=30)),
            "data_label": "synthetic", **changes}


def claim(lot_id, supplier, version=1, origin="Mine North", **changes):
    return {**common("dbp-supplier-claim-2", supplier, f"MSG-CLAIM-{lot_id}-V{version}"),
            "claim_id": f"CLAIM-{lot_id}", "claim_version": version, "lot_id": lot_id, "supplier_id": supplier,
            "material": "lithium", "mass_kg": 30.0, "origin_site": origin, "region": "Europe",
            "pcf_kgco2e_per_kg": 9.0, "recycled_share_pct": 0.0, "na_reasons": {}, **changes}


def event(issuer, event_type, battery_id, **details):
    message_id = f"MSG-{event_type.upper().replace('_', '-')}-{battery_id}"
    return {**common("dbp-partner-event-1", issuer, message_id), "event_type": event_type,
            "battery_id": battery_id, **details}


def sign(payload, signer=None):
    signer = signer or payload["issuer"]
    value = KEYS[signer].sign(domain.canonical(payload))
    return {"payload": payload, "signature": {"alg": "Ed25519", "key_id": f"{signer}-KEY-1",
                                              "value": base64.b64encode(value).decode("ascii")}}


def agreement(party, types):
    return {"agreement_id": f"AGR-{party}", "sender": party, "purpose": "Synthetic partner messages for tests.",
            "message_types": types, "valid_from": utc(NOW - timedelta(days=1)),
            "valid_until": utc(NOW + timedelta(days=60)), "command_id": f"agr-{party.lower()}-0001"}


class ConnectorTests(unittest.TestCase):
    def setUp(self):
        self.service, self.blobs = helpers.new_service()
        self.lot_a = claim("LOT-A", "SUP-A")
        package = helpers.make_package("BUILD-03", 2, [helpers.battery("BAT-101", "LOT-A"),
                                                       helpers.battery("BAT-103", "LOT-B", "BATCH-B", 35.0)],
                                       lots={"LOT-A": {"supplier_id": "SUP-A",
                                                       "claim_sha256": domain.sha256(domain.canonical(self.lot_a))}})
        helpers.put_package(self.blobs, "BUILD-03", package)
        self.events = []
        sink = telemetry.Telemetry(lambda name, dims, measures: self.events.append((name, dims)))
        self.app = create_app(Settings(auth_mode="local", worker=False, party_registry=registry()),
                              self.service, telemetry=sink)
        self.client = TestClient(self.app)
        imported = self.client.post("/packages/import", headers=OP,
                                    json={"intake_folder": "BUILD-03", "command_id": "con-imp-0001"})
        self.assertEqual(imported.status_code, 200, imported.text)
        for party, types in (("SUP-A", ["supplier_claim"]), ("SUP-B", ["supplier_claim"]),
                             ("SUP-CO", ["supplier_claim"]), ("OEM-AW", ["record_soh", "record_end_of_life"])):
            recorded = self.client.post("/connector/v1/agreements", headers=LINK, json=agreement(party, types))
            self.assertEqual(recorded.status_code, 201, recorded.text)

    def new_registry(self, document):
        """A changed DBP_PARTY_REGISTRY_B64 setting, as the app sees it after the next deployment."""
        self.app.state.connector.registry = document
        self.app.state.connector.sync()

    def send(self, envelope, **extra):
        response = self.client.post("/connector/v1/messages", headers=LINK, json={"envelope": envelope, **extra})
        return response.status_code, response.json()

    def assert_rejected(self, envelope, status, code):
        observed, body = self.send(envelope)
        self.assertEqual((observed, body["reason_code"], body["result"], body["connector"]),
                         (status, code, "rejected", "mock"), body)
        return body

    def test_a_valid_claim_is_verified_stored_once_and_kept_with_its_correlation_id(self):
        status, body = self.send(sign(self.lot_a), correlation_id="CORR-LOT-A-1")
        self.assertEqual((status, body["result"], body["reason_code"], body["connector"]),
                         (201, "verified", "ok", "mock"), body)
        self.assertEqual((body["correlation_id"], body["route"], body["subject"]),
                         ("CORR-LOT-A-1", "evidence", "LOT-A"))
        evidence = self.service.list_evidence()
        self.assertEqual([(e["evidence_id"], e["kind"]) for e in evidence], [(body["evidence_id"], "supplier_claim")])
        again_status, again = self.send(sign(self.lot_a), correlation_id="CORR-LOT-A-1")
        self.assertEqual((again_status, again.pop("replayed")), (status, True))
        self.assertEqual(again, body)
        log = self.client.get("/connector/v1/messages", headers=OP).json()
        self.assertEqual(log["connector"], "mock")
        self.assertEqual([(m["message_id"], m["correlation_id"], m["result"]) for m in log["messages"]],
                         [("MSG-CLAIM-LOT-A-V1", "CORR-LOT-A-1", "verified")])
        self.assertNotIn("payload_json", log["messages"][0])
        self.assertNotIn("Mine North", json.dumps(log))

    def test_a_message_id_reused_with_another_payload_is_refused(self):
        self.assertEqual(self.send(sign(self.lot_a))[0], 201)
        self.assert_rejected(sign(claim("LOT-A", "SUP-A", origin="Mine South")), 409, "message_conflict")
        self.assertEqual(len(self.service.list_evidence()), 1)

    def test_tampered_misattributed_or_misaddressed_messages_are_refused_with_a_code(self):
        tampered = sign(self.lot_a)
        tampered["payload"] = {**tampered["payload"], "origin_site": "Mine South"}
        self.assert_rejected(tampered, 403, "bad_signature")
        self.assert_rejected(sign(claim("LOT-B", "SUP-B"), signer="SUP-A"), 403, "issuer_mismatch")
        self.assert_rejected(sign(claim("LOT-A", "SUP-A", audience="OTHER-OEM")), 403, "wrong_audience")
        self.assert_rejected(sign(claim("LOT-A", "SUP-A", expires_utc=utc(NOW - timedelta(minutes=1)))), 422,
                             "expired")
        self.assert_rejected(sign(claim("LOT-B", "SUP-A")), 403, "lot_not_allowed")
        self.assert_rejected(sign(event("SUP-A", "record_soh", "BAT-101", soh_pct=90, measured_utc=utc(NOW))), 403,
                             "event_type_not_allowed")
        self.assertEqual(self.service.list_evidence(), [])
        reasons = [m["reason_code"] for m in self.client.get("/connector/v1/messages", headers=AUTH).json()["messages"]]
        self.assertEqual(reasons, ["bad_signature", "issuer_mismatch", "wrong_audience", "expired", "lot_not_allowed",
                                   "event_type_not_allowed"])
        self.assertEqual(self.send(sign(self.lot_a))[1]["result"], "verified")  # refusals do not claim the ID

    def test_a_revoked_key_is_refused_and_the_revocation_keeps_its_history(self):
        self.assertEqual(self.send(sign(claim("LOT-CO", "SUP-CO")))[0], 201)
        self.new_registry(registry(revoked={"SUP-A"}, only={"SUP-A", "SUP-B", "OEM-AW"}))
        self.assert_rejected(sign(self.lot_a), 403, "revoked")
        parties = {p["party_id"]: p for p in self.client.get("/parties", headers=OP).json()["parties"]}
        self.assertTrue(parties["SUP-A"]["revoked"] and parties["SUP-CO"]["revoked"])
        self.assertEqual(parties["SUP-A"]["row_version"], 2)
        actions = [(e["action"], e["subject"]) for e in self.service.audit() if e["action"] == "party_revoked"]
        self.assertEqual(sorted(actions), [("party_revoked", "SUP-A"), ("party_revoked", "SUP-CO")])

    def test_lot_claim_status_compares_the_package_hash_with_the_verified_claim(self):
        self.send(sign(self.lot_a))
        self.send(sign(claim("LOT-B", "SUP-B")))  # verified, but the package references another claim hash
        self.send(sign(claim("LOT-CO", "SUP-CO")))
        self.new_registry(registry(revoked={"SUP-CO"}))
        status = {row["lot_id"]: row["claim_status"] for row in self.service.lot_claim_rows()}
        self.assertEqual((status["LOT-A"], status["LOT-B"], status["LOT-CO"], status["LOT-NI"]),
                         ("verified", "invalid", "revoked", "missing"))
        self.assertLessEqual(set(status.values()), {"verified", "missing", "invalid", "revoked"})

    def test_a_database_recreated_under_the_running_app_receives_the_registry_again(self):
        self.service.store = MemoryStore()  # data reset recreates the database while the app keeps running
        recorded = self.client.post("/connector/v1/agreements", headers=LINK,
                                    json=agreement("SUP-A", ["supplier_claim"]))
        self.assertEqual(recorded.status_code, 201, recorded.text)
        listed = self.client.get("/parties", headers=OP).json()["parties"]
        self.assertEqual(sorted(p["party_id"] for p in listed), sorted(PARTIES))
        with mock.patch.object(self.service.store, "transaction", side_effect=AssertionError("write lock taken")):
            self.assertEqual(self.app.state.connector.sync(), 0)  # an unchanged registry needs no write

    def test_a_message_needs_a_valid_agreement_for_its_type(self):
        service, _ = helpers.new_service()
        client = TestClient(create_app(Settings(auth_mode="local", worker=False, party_registry=registry()), service))
        envelope = sign(self.lot_a)
        response = client.post("/connector/v1/messages", headers=LINK, json={"envelope": envelope})
        self.assertEqual((response.status_code, response.json()["reason_code"]), (403, "no_agreement"))
        expired = {**agreement("SUP-A", ["supplier_claim"]), "valid_from": utc(NOW - timedelta(days=2)),
                   "valid_until": utc(NOW - timedelta(hours=2))}
        self.assertEqual(client.post("/connector/v1/agreements", headers=LINK, json=expired).status_code, 201)
        other_type = {**agreement("SUP-A", ["record_soh"]), "agreement_id": "AGR-SUP-A-2",
                      "command_id": "agr-sup-a-0002"}
        self.assertEqual(client.post("/connector/v1/agreements", headers=LINK, json=other_type).status_code, 201)
        response = client.post("/connector/v1/messages", headers=LINK, json={"envelope": envelope})
        self.assertEqual((response.status_code, response.json()["reason_code"]), (403, "no_agreement"))
        unknown = client.post("/connector/v1/agreements", headers=LINK, json=agreement("SUP-NONE", ["supplier_claim"]))
        self.assertEqual((unknown.status_code, unknown.json()["error"]), (422, "unknown_party"))
        payload = event("OEM-AW", "record_soh", "BAT-404", soh_pct=90, measured_utc=utc(NOW))
        self.assert_rejected(sign(payload), 404, "unknown_battery")

    def test_partner_events_from_the_holder_are_routed_and_others_quarantined_at_intake(self):
        status, body = self.send(sign(event("OEM-AW", "record_soh", "BAT-101", soh_pct=91, measured_utc=utc(NOW))))
        self.assertEqual((status, body["result"], body["reason_code"]), (202, "quarantined", "not_current_holder"))
        with self.service.store.transaction() as tx:
            tx.update("PassportState", {"battery_id": "BAT-101", "holder_id": "OEM-AW"},
                      tx.get("PassportState", "BAT-101")["row_version"])
        status, body = self.send(sign(event("OEM-AW", "record_end_of_life", "BAT-101")))
        self.assertEqual((status, body["result"], body["route"]), (201, "routed", "lifecycle"), body)
        self.assertEqual((body["lifecycle_event"]["issuer"], body["lifecycle_event"]["battery_id"]),
                         ("OEM-AW", "BAT-101"))
        self.assertEqual([(e["message_id"], e["reason_code"]) for e in self.service.quarantined_events()],
                         [("MSG-RECORD-SOH-BAT-101", "not_current_holder")])
        self.assertEqual(self.client.get("/lifecycle/quarantine", headers=AUTH).json()["events"][0]["result"],
                         "quarantined")
        self.assertEqual(self.client.get("/lifecycle/quarantine", headers=REV).status_code, 403)

    def test_roles_routes_and_the_removed_partner_claim_route(self):
        self.assertIn(self.client.post("/partner/claims", headers=REV, json={}).status_code, (404, 405))
        for headers in (OP, REV, AUTH):
            response = self.client.post("/connector/v1/messages", headers=headers, json={"envelope": sign(self.lot_a)})
            self.assertEqual(response.status_code, 403, headers)
            self.assertEqual(self.client.post("/connector/v1/agreements", headers=headers,
                                              json=agreement("SUP-A", ["supplier_claim"])).status_code, 403)
        for path in ("/parties", "/connector/v1/messages"):
            self.assertEqual(self.client.get(path, headers=REV).status_code, 403)
            self.assertEqual(self.client.get(path, headers=LINK).status_code, 403)
            self.assertEqual(self.client.get(path, headers=AUTH).status_code, 200)
        parties = self.client.get("/parties", headers=OP).json()
        self.assertEqual(parties["connector"], "mock")
        text = json.dumps(parties).lower()
        self.assertNotIn("private", text)
        self.assertEqual({k for p in parties["parties"] for k in p},
                         {"party_id", "name", "role", "key_id", "public_key", "lot_ids", "event_types", "revoked",
                          "updated_utc", "row_version"})

    def test_the_console_shows_the_connector_section_to_operator_and_authority_only(self):
        self.send(sign(self.lot_a))
        self.send(sign(claim("LOT-B", "SUP-A")))
        for headers, shown in ((OP, True), (AUTH, True), (REV, False), (LINK, False)):
            page = self.client.get("/", headers=headers)
            self.assertEqual(page.status_code, 200)
            self.assertEqual("Connector (mock)" in page.text, shown, headers)
            self.assertEqual("lot_not_allowed" in page.text, shown, headers)
        trail = self.client.get("/trail/BAT-101", headers=OP)
        self.assertEqual(trail.status_code, 200)
        self.assertIn("Connector (mock)", trail.text)

    def test_trail_shows_claim_receipt_provenance_without_exposing_payloads(self):
        receipts = []
        for version in (1, 2):
            status, body = self.send(sign(claim("LOT-A", "SUP-A", version)), correlation_id=f"CORR-A-{version}")
            self.assertEqual(status, 201, body)
            receipts.append(body)
        view = exec_views.trail_view(self.service, "BAT-101")
        steps = {step["receipt_id"]: step for step in view["path"] + view["after"]
                 if step["kind"] == "connector"}
        for version, receipt in enumerate(receipts, 1):
            step = steps[receipt["receipt_id"]]
            details = dict(step["details"])
            self.assertEqual(step["subject_url"], "/lots/LOT-A")
            self.assertEqual(step["text"], "Verified: supplier claim for LOT-A")
            self.assertEqual(details["Sender ID"], "SUP-A")
            self.assertEqual(details["Delivered by"], "cora")
            self.assertEqual(details["Agreement"], "AGR-SUP-A")
            self.assertEqual(details["Receiver"], "CONTOSO-BATTERY")
            self.assertEqual(details["Agreement purpose"], agreement("SUP-A", ["supplier_claim"])["purpose"])
            self.assertEqual(details["Correlation ID"], f"CORR-A-{version}")
            self.assertEqual(details["Stored evidence ID"], receipt["evidence_id"])
            self.assertEqual(details["Evidence file"], f"MSG-CLAIM-LOT-A-V{version}.json")
            self.assertEqual(details["Evidence kind"], "supplier claim" if version == 1 else "corrected claim")
            self.assertEqual(details["Payload SHA-256"], receipt["payload_sha256"])
            self.assertIn("critical materials only", details["Handling"])
        for headers in (OP, REV):
            page = self.client.get("/trail/BAT-101", headers=headers).text
            for text in ('href="/lots/LOT-A"', "Intake result", "Agreement valid from", "Agreement valid until",
                         "Receipt ID", "Correlation ID", "Delivered by", "not a live partner integration",
                         "not the truth of its contents", receipts[1]["evidence_id"], "CORR-A-2"):
                self.assertIn(text, page)
            for hidden in ("Mine North", "pcf_kgco2e_per_kg", "payload_json", "/content"):
                self.assertNotIn(hidden, page)
        for headers in ({}, LINK, AUTH):
            self.assertIn(self.client.get("/trail/BAT-101", headers=headers).status_code, (401, 403))

    def test_trail_distinguishes_routed_quarantined_and_rejected_intake(self):
        status, quarantined = self.send(sign(event("OEM-AW", "record_soh", "BAT-101",
                                                   soh_pct=91, measured_utc=utc(NOW))))
        self.assertEqual((status, quarantined["result"]), (202, "quarantined"))
        with self.service.store.transaction() as tx:
            tx.update("PassportState", {"battery_id": "BAT-101", "holder_id": "OEM-AW"},
                      tx.get("PassportState", "BAT-101")["row_version"])
        status, routed = self.send(sign(event("OEM-AW", "record_end_of_life", "BAT-101")))
        self.assertEqual((status, routed["result"]), (201, "routed"))
        tampered = sign(self.lot_a)
        tampered["payload"]["origin_site"] = "Untrusted origin"
        rejected = self.assert_rejected(tampered, 403, "bad_signature")

        view = exec_views.trail_view(self.service, "BAT-101")
        steps = {step["receipt_id"]: step for step in view["path"] + view["after"]
                 if step["kind"] == "connector"}
        quarantined_step, routed_step, rejected_step = [steps[receipt["receipt_id"]]
                                                        for receipt in (quarantined, routed, rejected)]
        self.assertIn("Quarantined - not applied", quarantined_step["text"])
        self.assertEqual(quarantined_step["reason_code"], "not_current_holder")
        self.assertEqual(quarantined_step["reason"], "not current holder")
        self.assertIn("not routed to an agent or applied", dict(quarantined_step["details"])["Handling"])
        self.assertIn("Routed to Lifecycle", routed_step["text"])
        self.assertEqual(routed_step["subject_url"], "/p/BAT-101")
        self.assertIn("does not mean the event was applied", dict(routed_step["details"])["Handling"])
        details = dict(rejected_step["details"])
        self.assertEqual(details["Claimed sender ID"], "SUP-A")
        self.assertEqual(details["Agreement"], "Not recorded")
        self.assertNotIn("Stored evidence ID", details)
        self.assertIn("untrusted message claims", details["Intake checks"])
        self.assertNotIn("accepted at receipt", details["Intake checks"])
        page = self.client.get("/trail/BAT-101", headers=OP).text
        for text in ("State-of-health reading", "End-of-life handover", "Quarantined - not applied",
                     "Routed to Lifecycle", "Claimed sender ID", "bad_signature", "not_current_holder"):
            self.assertIn(text, page)
        self.assertNotIn("Untrusted origin", page)

    def test_incomplete_rejection_metadata_does_not_invent_provenance(self):
        envelope = sign(self.lot_a)
        envelope["payload"].update(schema="unknown", battery_id="LOT-A")
        envelope["payload"].pop("issuer")
        self.assert_rejected(envelope, 422, "bad_payload")
        view = exec_views.trail_view(self.service, "BAT-101")
        step = next(step for step in view["path"] + view["after"] if step["kind"] == "connector")
        self.assertEqual(step["subject_url"], "/lots/LOT-A")
        self.assertEqual(step["subject_label"], "Supplier lot")
        self.assertEqual(step["actor"], "Connector (mock) - unknown sender")
        details = dict(step["details"])
        self.assertEqual(details["Claimed sender ID"], "Not recorded")
        self.assertEqual(details["Message type"], "Unknown message type")
        self.assertEqual(details["Agreement"], "Not recorded")
        self.assertNotIn("Receiver", details)
        self.assertNotIn("Stored evidence ID", details)
        page = self.client.get("/trail/BAT-101", headers=OP)
        self.assertEqual(page.status_code, 200)
        self.assertIn("bad payload", page.text)
        self.assertNotIn("Signature, sender permissions", page.text)

    def test_connector_telemetry_carries_codes_only(self):
        self.send(sign(self.lot_a))
        self.assert_rejected(sign(claim("LOT-B", "SUP-A")), 403, "lot_not_allowed")
        connector = [dims for name, dims in self.events if name == "connector.message"]
        self.assertEqual([(d["message_type"], d["result"], d["reason_code"]) for d in connector],
                         [("supplier_claim", "verified", "ok"), ("supplier_claim", "rejected", "lot_not_allowed")])
        self.assertFalse(any("Mine" in value for d in connector for value in d.values()))
        with self.assertRaises(ValueError):
            telemetry.build("connector.message", message_type="supplier_claim", result="rejected",
                            reason_code="The signature was wrong")

    def test_the_largest_accepted_claim_fits_the_agent_evidence_limit(self):
        padded = claim("LOT-A", "SUP-A", na_reasons={})
        padded["na_reasons"] = {"note": "x" * (7990 - len(domain.canonical(padded)))}
        self.assertLessEqual(len(domain.canonical(padded)), 8000)
        status, body = self.send(sign(padded))
        self.assertEqual(status, 201, body)
        too_large = {**padded, "message_id": "MSG-CLAIM-LOT-A-V9", "na_reasons": {"note": "x" * 8000}}
        self.assert_rejected(sign(too_large), 413, "too_large")
        with self.service.store.read() as tx:
            evidence = AgentService(self.service)._evidence(tx, "LOT-A")
        self.assertEqual(len(evidence), 1)
        self.assertLessEqual(len(domain.canonical(evidence[0]["content"])), 16000)


if __name__ == "__main__":
    unittest.main()
