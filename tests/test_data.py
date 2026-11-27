"""The synthetic supply chain: the simulator, the signed partner messages, the delivery and package rules shared with
the Fabric notebooks, the independent package check, the app's own reading of it and the reporting export. Local only;
the Fabric runs are covered in test_fabric.py."""

import ast
import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from collections import Counter, defaultdict
from pathlib import Path

from test_orchestrator import ROOT

from data import metrics, package, signed_messages, simulator
from data.factors import load_factors
from data.parties import PARTIES, Party, check_profiles, registry
from data.pipeline import SOURCES, code_hash, embedded_source
from data.pipeline.deliveries import DELIVERY_MANIFEST, check_delivery, parse_delivery_set
from data.pipeline.flows import (EXPORT_FILE, LOT_CLAIMS_FILE, STATUS_COLUMNS, FolderStore, bronze_load,
                                 local_package, make_export, read_export, reporting_measures)
from data.pipeline.rules import (build_identity, build_package, canonical, derive, digest, kg, kwh, merge_records,
                                 percentage)
from fabric.notebooks import steps

if str(ROOT / "app") not in sys.path:
    sys.path.append(str(ROOT / "app"))
from passport.package import check_package as app_check_package  # noqa: E402

NOW = 6000.0  # a fixed clock: build sequence 100, completed 1970-01-01T01:40:00Z
FACTORS = load_factors()
BUILD_ID = r"BUILD-MAIN-[0-9A-F]{12}"


def clock(moment=NOW):
    return lambda: moment


def rows(files, name):
    return [json.loads(line) for line in files[name].decode("utf-8").splitlines()]


def batteries_of(files):
    return {row["battery_id"]: row for row in rows(files, "batteries.jsonl")}


def story_list(batteries=0):
    story = simulator.story_records(batteries)
    return [record for kinds in story.values() for documents in kinds.values()
            for records in documents.values() for record in records]


def build_id_of(name, deliveries):
    return build_identity(name, [digest(files[DELIVERY_MANIFEST]) for _, _, files in deliveries])


class TempFolder(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)


class SimulatorTests(TempFolder):
    def prepare(self, batteries=0, **kwargs):
        return simulator.prepare(batteries, output=kwargs.pop("output", self.folder),
                                 now=kwargs.pop("now", clock()), **kwargs)

    def package(self, state, kind="package", output=None):
        return package.read_folder(simulator.build_paths(output or self.folder, state)[kind])

    def test_a_series_is_prepared_once_and_reused(self):
        state = self.prepare()
        self.assertRegex(state["build_id"], f"^{BUILD_ID}$")
        self.assertEqual((state["build_sequence"], state["completed_utc"]), (100, "1970-01-01T01:40:00Z"))
        files = self.package(state)
        self.assertEqual(package.package_errors(files), [])
        self.assertEqual(state["manifest_sha256"], digest(files["manifest.json"]))
        self.assertEqual(json.loads(files["manifest.json"])["totals"], {"ready": 5, "blocked": 0})
        self.assertEqual(len(state["delivery_set"].split(",")), 20)
        self.assertEqual(self.prepare(now=clock(NOW * 2)), state)
        self.assertEqual(self.package(state), files)

    def test_the_same_series_gives_the_same_packages_whatever_the_signing_keys(self):
        first = self.prepare()
        other = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, other, ignore_errors=True)
        second = self.prepare(output=other)
        self.assertEqual(second["build_id"], first["build_id"])
        self.assertEqual(self.package(second, "package", other), self.package(first))
        keys = [json.loads(simulator.build_paths(folder, first)["registry"].read_bytes())["parties"][0]["public_key"]
                for folder in (self.folder, other)]
        self.assertNotEqual(keys[0], keys[1])

    def test_a_new_build_comes_after_every_earlier_one_and_flags_changes(self):
        first = self.prepare()
        second = self.prepare(new_build=True)
        self.assertEqual(second["build_sequence"], 101)
        self.assertNotEqual(second["build_id"], first["build_id"])
        self.assertEqual(second["input_hashes"], first["input_hashes"])
        third = self.prepare(2)
        changed = [key for key, value in third["input_hashes"].items() if first["input_hashes"].get(key) != value]
        self.assertEqual(changed, ["B-101", "B-102"])
        self.assertEqual(self.prepare(2, min_sequence=500)["build_sequence"], 500)
        self.assertEqual(self.prepare(2, min_sequence=400)["build_sequence"], 500)

    def test_a_tag_makes_a_separate_series(self):
        main = self.prepare()
        tagged = self.prepare(tag="R2")
        self.assertRegex(tagged["build_id"], r"^BUILD-R2-[0-9A-F]{12}$")
        self.assertIn("SUP-NORTHWIND/DLV-R2-100-SUP-NORTHWIND-LOTS", tagged["delivery_set"])
        self.assertEqual(list(batteries_of(self.package(tagged))), [f"B-00{n}-R2" for n in range(1, 6)])
        self.assertEqual(simulator.load_state(self.folder), main)
        self.assertEqual(simulator.evidence_uploads("R2")[0],
                         ("lot-li-a-transport-record.json", "transport_record", "LOT-LI-A-R2"))
        self.assertEqual(simulator.critical_lots("R2")[:2], ["LOT-LI-A-R2", "LOT-LI-B-R2"])
        self.assertEqual(len(simulator.critical_lots()), 9)
        self.assertEqual(simulator.correction_message("R2"), "MSG-CLAIM-LOT-LI-A-R2-V2")
        messages = simulator.build_paths(self.folder, tagged)["messages"]
        for message_id in [*simulator.opening_messages("R2"), simulator.correction_message("R2"),
                           simulator.soh_message("R2")]:
            self.assertTrue((messages / f"{message_id}.json").is_file(), message_id)

    def test_connector_agreements_cover_what_each_party_may_sign(self):
        registry = {"parties": [{"party_id": "SUP-X", "lot_ids": ["LOT-X"], "event_types": []},
                                {"party_id": "OEM-X", "lot_ids": [], "event_types": ["record_soh"]},
                                {"party_id": "CARRIER-X", "lot_ids": [], "event_types": []}]}
        agreements = simulator.connector_agreements(registry)
        self.assertEqual([(a["sender"], a["message_types"]) for a in agreements],
                         [("SUP-X", ["supplier_claim"]), ("OEM-X", ["record_soh"])])
        self.assertEqual(agreements, simulator.connector_agreements(registry))

    def test_added_batteries_spread_over_the_plants_and_every_lot_covers_its_use(self):
        files = self.package(self.prepare(simulator.MAX_BATTERIES))
        self.assertEqual(package.package_errors(files), [])
        batteries = batteries_of(files)
        self.assertEqual(Counter(row["plant_id"] for row in batteries.values()),
                         {"PLANT-SE": 2 + 17, "PLANT-DE": 1 + 17, "PLANT-PL": 2 + 16})
        issues = simulator.issue_plan(simulator.MAX_BATTERIES)
        self.assertEqual({b for b, row in batteries.items() if row["readiness"] != "ready"},
                         {f"B-{n}" for n, kind in issues.items() if kind in simulator.BLOCKED_KINDS})
        self.assertEqual({row["readiness"] for row in batteries.values()}, {"ready", "blocked"})
        lots, used = {row["lot_id"]: row for row in rows(files, "lots.jsonl")}, defaultdict(float)
        parts = rows(files, "composition.jsonl")
        for part in parts:
            used[part["lot_id"]] += part["mass_kg"]
        self.assertEqual([lot for lot, mass in used.items() if mass > lots[lot]["mass_kg"]], [])
        materials = defaultdict(set)
        for part in parts:
            materials[part["battery_id"]].add(part["material"])
        self.assertEqual({len(found) for found in materials.values()}, {9})
        recycled = {part["lot_id"] for part in parts if part["battery_id"] == "B-005"} - set(simulator.DEFAULT_LOTS
                                                                                            .values())
        self.assertEqual(recycled, {"LOT-CO-R", "LOT-LI-R", "LOT-NI-R"})
        self.assertEqual({lots[lot]["recycled_share_pct"] for lot in recycled}, {100.0})

    def test_every_claim_and_partner_event_is_signed_with_its_registry_key(self):
        state = self.prepare()
        paths = simulator.build_paths(self.folder, state)
        document = json.loads(paths["registry"].read_bytes())
        parties = {entry["party_id"]: entry for entry in document["parties"]}
        self.assertEqual({entry["role"] for entry in parties.values()},
                         {"supplier", "recycler", "oem", "second_life_operator"})
        envelopes = [json.loads(path.read_bytes()) for path in sorted(paths["messages"].iterdir())]
        self.assertEqual(len(envelopes), 14 + 1 + 6)
        for envelope in envelopes:
            payload, entry = envelope["payload"], parties[envelope["payload"]["issuer"]]
            with self.subTest(message=payload["message_id"]):
                self.assertTrue(signed_messages.verify(envelope, entry["public_key"]))
                self.assertEqual(envelope["signature"]["key_id"], entry["key_id"])
                self.assertEqual((payload["audience"], payload["data_label"]), ("CONTOSO-BATTERY", "synthetic"))
                self.assertLess(payload["issued_utc"], payload["expires_utc"])
                if payload["schema"] == signed_messages.CLAIM_SCHEMA:
                    self.assertIn(payload["lot_id"], entry["lot_ids"])
                else:
                    self.assertIn(payload["event_type"], entry["event_types"])
        self.assertEqual({envelope["payload"]["issuer"] for envelope in envelopes}, set(parties))
        tampered = {**envelopes[0], "payload": {**envelopes[0]["payload"], "issued_utc": "2026-10-01T00:00:00Z"}}
        self.assertFalse(signed_messages.verify(tampered, parties[tampered["payload"]["issuer"]]["public_key"]))
        claims = {e["payload"]["lot_id"]: e["payload"] for e in envelopes if e["payload"].get("claim_version") == 1}
        self.assertEqual({lot["lot_id"]: lot["claim_sha256"] for lot in rows(self.package(state), "lots.jsonl")},
                         {lot: signed_messages.payload_sha256(claim) for lot, claim in claims.items()})
        self.prepare(new_build=True)
        self.assertEqual(json.loads(paths["registry"].read_bytes()), document)

    def test_producer_signed_messages_verify_in_the_app(self):
        from passport import parties as app_parties, signed_messages as app_messages
        state = self.prepare(tag="X1")
        paths = simulator.build_paths(self.folder, state)
        registry = app_parties.check_registry(json.loads(paths["registry"].read_bytes()))
        now = app_messages.parse_utc("2026-11-12T00:00:00Z")
        for path in sorted(paths["messages"].iterdir()):
            envelope = json.loads(path.read_bytes())
            with self.subTest(message=path.name):
                payload, entry = app_messages.verify(envelope, registry, now)
                self.assertEqual(entry["party_id"], envelope["payload"]["issuer"])
                if payload["schema"] == signed_messages.CLAIM_SCHEMA:
                    self.assertEqual(signed_messages.payload_sha256(payload),
                                     app_messages.domain.sha256(app_messages.domain.canonical(payload)))
        forged = json.loads((paths["messages"] / f"{simulator.correction_message('X1')}.json").read_bytes())
        forged["payload"]["origin_site"] = "Mine North"
        with self.assertRaises(app_messages.Rejected) as refused:
            app_messages.verify(forged, registry, now)
        self.assertEqual(refused.exception.code, "bad_signature")

    def test_options_are_checked(self):
        for batteries in (-1, 51, True, "3", 2.0):
            with self.subTest(batteries=batteries), self.assertRaisesRegex(ValueError, "--batteries must be"):
                simulator.check_options(batteries, None)
        for tag in ("", "r2", "MAIN", "TOOLONG", "R-2", 7):
            with self.subTest(tag=tag), self.assertRaisesRegex(ValueError, "--tag must be"):
                simulator.check_options(0, tag)

    def test_the_estimate_counts_model_calls_per_agent_and_act(self):
        self.assertEqual(simulator.estimate_lines(), [
            "Estimated agent model calls for 5 batteries (cost depends on your model prices):",
            "  supplier   prepare       9", "  carbon     prepare       5", "  issuance   prepare       2",
            "  supplier   complete      2", "  issuance   complete      3", "  lifecycle  aftercare     7",
            "  per act: prepare 16, complete 5, aftercare 7; total 28"])
        self.assertEqual(simulator.estimate_lines(5)[-2:], [
            "  per act: prepare 26, complete 7, aftercare 7; total 40",
            "Seeded data issues in 1 of 5 added batteries: adverse_audited 1"])

    def test_added_batteries_carry_seeded_issues_the_same_way_every_time(self):
        self.assertEqual((simulator.issue_plan(0), simulator.issue_plan(2, "S3")), ({}, {}))
        plan = simulator.issue_plan(8, "S3")
        self.assertEqual(plan, simulator.issue_plan(8, "S3"))
        self.assertEqual(sorted(plan.values()), sorted(simulator.ISSUES))
        self.assertNotEqual(plan, simulator.issue_plan(8, "S4"))
        large = simulator.issue_plan(simulator.MAX_BATTERIES, "S3")
        self.assertEqual(len(large), round(simulator.ISSUE_SHARE * simulator.MAX_BATTERIES))
        self.assertEqual(set(large.values()), set(simulator.ISSUES))
        self.assertFalse(set(large) & set(simulator.STORY))
        state = self.prepare(8, tag="S3")
        files = self.package(state)
        self.assertEqual(json.loads(files["manifest.json"])["totals"], {"ready": 11, "blocked": 2})
        lots = {row["lot_id"] for row in rows(files, "lots.jsonl")}
        self.assertTrue({"LOT-LI-C-S3", "LOT-LI-D-S3", "LOT-CO-C-S3", "LOT-CO-D-S3", "LOT-CO-E-S3"} <= lots)
        self.assertEqual(simulator.critical_lots("S3", 8)[-5:],
                         ["LOT-LI-C-S3", "LOT-LI-D-S3", "LOT-CO-C-S3", "LOT-CO-D-S3", "LOT-CO-E-S3"])
        parties = {entry["party_id"]: entry["lot_ids"] for entry in json.loads(
            simulator.build_paths(self.folder, state)["registry"].read_bytes())["parties"]}
        self.assertIn("LOT-NI-B-S3", parties["SUP-WOODGROVE"])
        self.assertEqual(list(simulator.correction_messages("S3", 8)), ["LOT-LI-A-S3", "LOT-LI-C-S3"])
        self.assertEqual([lot for _, _, lot in simulator.audit_uploads("S3", 8)], ["LOT-CO-B-S3", "LOT-CO-C-S3"])
        documents = simulator.evidence_documents("S3", 8)
        claim = json.loads(simulator.evidence_bytes(documents["lot-li-c-supplier-claim-v1.json"]))
        record = json.loads(simulator.evidence_bytes(documents["lot-li-c-transport-record.json"]))
        self.assertNotEqual(claim["origin_site"], record["loaded_at_site"])

    def test_the_command_line_reports_errors_with_exit_code_2(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(simulator.main(["--output", str(self.folder)]), 0)
            self.assertEqual(simulator.main(["--batteries", "99", "--output", str(self.folder)]), 2)
        self.assertRegex(out.getvalue(), f"{BUILD_ID}: 20 deliveries in ")
        self.assertEqual(err.getvalue(), "error: --batteries must be a whole number from 0 to 50.\n")

    def test_the_published_evidence_files_match_the_generator(self):
        expected = {name: simulator.evidence_bytes(document)
                    for name, document in simulator.evidence_documents().items()}
        published = {name: data for name, data in package.read_folder(simulator.EVIDENCE).items()
                     if name.endswith(".json")}
        self.assertEqual(published, expected)
        claim = json.loads(expected["lot-li-a-supplier-claim-v1.json"])
        record = json.loads(expected["lot-li-a-transport-record.json"])
        self.assertNotEqual(claim["origin_site"], record["loaded_at_site"])
        self.assertEqual(json.loads(expected["lot-li-a-supplier-claim-v2.json"])["origin_site"],
                         record["loaded_at_site"])
        report = json.loads(expected[simulator.ADVERSE_REPORT])
        self.assertEqual(report["related_lot_ids"], ["LOT-CO-B"])
        self.assertLessEqual(len(expected[simulator.ADVERSE_REPORT]), 16 * 1024)
        audit = json.loads(expected[simulator.AUDIT_REPORT])
        self.assertEqual((audit["related_lot_ids"], audit["responds_to"]), (["LOT-CO-B"], report["report_id"]))
        self.assertLessEqual(len(expected[simulator.AUDIT_REPORT]), 16 * 1024)
        self.assertEqual(simulator.evidence_uploads()[-1], (simulator.ADVERSE_REPORT, "adverse_report", "LOT-CO-B"))
        self.assertEqual(simulator.audit_upload("R2"), (simulator.AUDIT_REPORT, "audit_report", "LOT-CO-B-R2"))


class DeliveryTests(unittest.TestCase):
    PARTY, ID = "SUP-NORTHWIND", "DLV-MAIN-1-SUP-NORTHWIND-LOTS"

    def files(self):
        return dict(next(files for party, delivery, files in simulator.make_deliveries("MAIN", 1)
                         if delivery == self.ID))

    def resign(self, delivery, **changes):
        manifest = {**json.loads(delivery[DELIVERY_MANIFEST]), **changes}
        return {**delivery, DELIVERY_MANIFEST: (canonical(manifest) + "\n").encode("utf-8")}

    def test_every_simulated_delivery_is_accepted(self):
        for party, delivery, files in simulator.make_deliveries("MAIN", 1, batteries=2):
            with self.subTest(delivery=delivery):
                self.assertEqual(check_delivery(party, delivery, files, registry())["status"], "accepted")

    def test_a_delivery_is_rejected_as_a_whole_with_a_coded_reason(self):
        lots = json.loads(self.files()["lots.json"])
        other = (canonical([{**lots[0], "party": "SUP-WIDEWORLD"}]) + "\n").encode("utf-8")
        cases = {
            "missing_manifest": lambda f: {"lots.json": f["lots.json"]},
            "bad_manifest:delivery_id": lambda f: self.resign(f, delivery_id="DLV-OTHER"),
            "party_mismatch:SUP-WIDEWORLD": lambda f: self.resign(f, party_id="SUP-WIDEWORLD"),
            "kind_not_allowed:transport": lambda f: self.resign(f, kind="transport"),
            "profile_mismatch:csv/kg": lambda f: self.resign(f, format="csv"),
            "unlisted_file:extra.json": lambda f: {**f, "extra.json": b"[]"},
            "hash_mismatch:lots.json": lambda f: {**f, "lots.json": f["lots.json"] + b" "},
            "record_party_mismatch:lots.json:1": lambda f: self.resign(
                {**f, "lots.json": other}, files=[{"name": "lots.json", "sha256": digest(other), "records": 1}]),
        }
        for reason, change in cases.items():
            with self.subTest(reason=reason):
                result = check_delivery(self.PARTY, self.ID, change(self.files()), registry())
                self.assertEqual((result["status"], result["reason"], result["records"]), ("rejected", reason, []))
        self.assertEqual(check_delivery("SUP-UNKNOWN", self.ID, self.resign(self.files(), party_id="SUP-UNKNOWN"),
                                        registry())["reason"], "unknown_party:SUP-UNKNOWN")

    def test_a_delivery_set_lists_each_delivery_once(self):
        self.assertEqual(parse_delivery_set(f" {self.PARTY}/{self.ID} ,"), [(self.PARTY, self.ID)])
        for text, message in (("", "empty"), ("SUP-NORTHWIND/../x", "Invalid delivery set item"),
                              (f"{self.PARTY}/{self.ID},SUP-WIDEWORLD/{self.ID}", "Delivery listed twice")):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, message):
                parse_delivery_set(text)

    def test_party_profiles_must_fit_their_role(self):
        self.assertEqual(check_profiles(), [])
        broken = PARTIES + (Party("SUP-NORTHWIND", "Duplicate", "supplier", ("lots",), "json", "kg", "Europe",
                                  ("lithium",)),
                            Party("PLANT-CSV", "Plant", "plant", ("builds",), "csv", "kg", "Europe"),
                            Party("CARRIER-X", "Carrier", "carrier", ("lots",), "json", "lb", "Atlantis"),
                            Party("OEM-X", "Motors", "oem", (), "json", "kg", "Europe", events=("request_repurpose",)),
                            Party("SUP-X", "Metals", "supplier", ("lots",), "json", "kg", "Asia", ("uranium",)))
        self.assertEqual(check_profiles(broken), [
            "SUP-NORTHWIND: listed twice", "PLANT-CSV: builds cannot be delivered as CSV",
            "CARRIER-X: a carrier cannot deliver lots", "CARRIER-X: unsupported unit lb",
            "CARRIER-X: region Atlantis is not a continent", "CARRIER-X: a lot party needs its materials",
            "OEM-X: a oem cannot sign request_repurpose", "SUP-X: unknown material uranium"])
        with self.assertRaisesRegex(ValueError, "Invalid party profiles"):
            registry(broken)


class RuleTests(unittest.TestCase):
    def derive(self, records, factors=FACTORS):
        return derive(records, simulator.CREATED_UTC, registry(), factors)

    def outcome(self, records, factors=FACTORS):
        """({battery: reasons} of the blocked batteries, [(source key, reason)] of the issues)."""
        result = self.derive(records, factors)
        return ({row["battery_id"]: row["reasons"] for row in result["batteries"] if row["readiness"] == "blocked"},
                [(item["source_key"], item["reason"]) for item in result["issues"]])

    @staticmethod
    def change(records, key, **fields):
        """The records with one key's fields changed; a field set to None is removed."""
        changed = []
        for record in records:
            if record["key"] == key:
                record = {name: value for name, value in {**record, **fields}.items() if value is not None}
            changed.append(record)
        return changed

    def test_versions_merge_by_revision_and_content(self):
        lot = {"entity": "lot", "key": "LOT-1", "version": 2, "quantity": 1}
        _, log, touched = merge_records({}, [lot, {**lot, "version": 3}, {**lot, "version": 1}, {**lot, "version": 3},
                                            {**lot, "version": 3, "quantity": 2}])
        self.assertEqual([entry["outcome"] for entry in log], ["new", "replaced", "older", "duplicate", "conflict"])
        self.assertEqual(touched, {("lot", "LOT-1")})

    def test_units_and_shares_are_normalised_and_bounded(self):
        self.assertEqual((kg(1500, "g"), kg(2.5, "kg"), kg(0.0805, "t"), kwh(6.4, "MWh"), kwh(3000, "kWh")),
                         (1.5, 2.5, 80.5, 6400.0, 3000.0))
        self.assertEqual((percentage(0), percentage("100")), (0.0, 100.0))
        for call, message in ((lambda: kg(1, "lb"), "unsupported_mass_unit:lb"),
                              (lambda: kwh(1, "GJ"), "unsupported_energy_unit:GJ"),
                              (lambda: kg(0, "kg"), "positive"), (lambda: percentage(100.5), "out_of_range"),
                              (lambda: percentage(-1), "out_of_range")):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                call()

    def test_the_story_derives_the_same_result_in_any_order(self):
        records = story_list()
        result = self.derive(records)
        self.assertEqual(result, self.derive(list(reversed(records))))
        self.assertEqual(result["issues"], [])
        self.assertEqual({lot["lot_id"]: lot["mass_kg"] for lot in result["lots"]},
                         {lot: float(spec["mass_kg"]) for lot, spec in simulator.lot_specs().items()})
        batteries = {row["battery_id"]: row for row in result["batteries"]}
        self.assertEqual({key: (row["readiness"], row["allocated_kwh"], row["grid_factor_id"])
                          for key, row in batteries.items()},
                         {"B-001": ("ready", 3000.0, "GF-PLANT-SE-2026"),
                          "B-002": ("ready", 3000.0, "GF-PLANT-SE-2026"),
                          "B-003": ("ready", 3100.0, "GF-PLANT-DE-2026"),
                          "B-004": ("ready", 3200.0, "GF-PLANT-PL-2026"),
                          "B-005": ("ready", 3200.0, "GF-PLANT-PL-2026")})
        self.assertEqual({row["batch_id"]: (row["batch_kwh"], row["battery_count"]) for row in result["batches"]},
                         {"BATCH-SE-01": (6000.0, 2), "BATCH-DE-01": (3100.0, 1), "BATCH-PL-01": (6400.0, 2)})

    def test_problems_block_batteries_with_coded_reasons(self):
        records = story_list()
        everyone = {f"B-00{n}": ["lot_unusable:LOT-SP-A", "material_missing:separator"] for n in range(1, 6)}
        no_grid = {**FACTORS, "grid": {key: value for key, value in FACTORS["grid"].items() if key != "PLANT-PL"}}
        energy = next(r for r in records if r["key"] == "ENERGY-DE-01")
        cases = {
            "consumption above lot mass": (self.change(records, "USE-001-LI-A", quantity=90), FACTORS, (
                {battery: ["consumption_exceeds_lot_mass:LOT-LI-A"] for battery in ("B-001", "B-002")},
                [("LOT-LI-A", "consumption_exceeds_lot_mass")])),
            "critical lot without claim": (self.change(records, "LOT-CO-B", claim_id=None, claim_sha256=None),
                                           FACTORS, ({"B-004": ["claim_missing:LOT-CO-B"]},
                                                     [("LOT-CO-B", "claim_missing")])),
            "component lot without claim or reason": (
                self.change(records, "LOT-SP-A", claim_id=None, claim_sha256=None), FACTORS,
                (everyone, [("LOT-SP-A", "invalid_latest_revision:missing_field:claim_id")])),
            "component lot with not-applicable reasons": (
                self.change(records, "LOT-SP-A", claim_id=None, claim_sha256=None, na_reasons={
                    "origin_site": "manufactured_component", "claim_id": "no_claim_for_component",
                    "claim_sha256": "no_claim_for_component"}), FACTORS, ({}, [])),
            "share above 100": (self.change(records, "LOT-SP-A", recycled_share_pct=101), FACTORS, (
                everyone, [("LOT-SP-A", "invalid_latest_revision:percentage_out_of_range:101")])),
            "reason for a present field": (self.change(records, "LOT-SP-A", origin_site="Plant"), FACTORS, (
                everyone, [("LOT-SP-A", "invalid_latest_revision:na_reason_for_present_field:origin_site")])),
            "missing applicable field": (self.change(records, "LOT-SP-A", region=None), FACTORS, (
                everyone, [("LOT-SP-A", "invalid_latest_revision:missing_field:region")])),
            "unsupported unit": (self.change(records, "LOT-SP-A", unit="lb"), FACTORS, (
                everyone, [("LOT-SP-A", "invalid_latest_revision:unsupported_mass_unit:lb")])),
            "material not consumed": ([r for r in records if r["key"] != "USE-003-SP-A"], FACTORS, (
                {"B-003": ["material_missing:separator"]}, [])),
            "unknown method": (self.change(records, "BATCH-DE-01", method_version="other-method"), FACTORS, (
                {"B-003": ["method_version_unknown"]}, [])),
            "other factor set": (self.change(records, "BATCH-DE-01", factor_set_version="other-factors"), FACTORS, (
                {"B-003": ["factor_set_mismatch"]}, [])),
            "plant without grid factor": (records, no_grid, (
                {"B-004": ["grid_factor_missing:PLANT-PL"], "B-005": ["grid_factor_missing:PLANT-PL"]}, [])),
            "batch without energy": ([r for r in records if r["key"] != "ENERGY-DE-01"], FACTORS, (
                {"B-003": ["missing_or_ambiguous_batch_energy:BATCH-DE-01"]}, [])),
            "conflicting energy": (records + [{**energy, "quantity": 1}], FACTORS, (
                {"B-003": ["batch_energy_unusable:ENERGY-DE-01"]},
                [("ENERGY-DE-01", "conflicting_contents_same_revision")])),
        }
        for name, (changed, factors, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.outcome(changed, factors), expected)
        blocked = self.derive(cases["critical lot without claim"][0])
        self.assertNotIn("LOT-CO-B", {lot["lot_id"] for lot in blocked["lots"]})
        self.assertEqual({part["battery_id"] for part in blocked["composition"]}, {"B-001", "B-002", "B-003", "B-005"})

    def test_embedded_modules_do_not_redefine_each_others_names(self):
        owners = {}
        files = [ROOT / "data" / "pipeline" / name for name in SOURCES]
        files += [ROOT / "data" / "metrics.py"]
        files += [ROOT / "fabric" / "notebooks" / name for name in ("spark_store.py", "steps.py")]
        for path in files:
            for node in ast.parse(path.read_text(encoding="utf-8")).body:
                names = ([node.name] if isinstance(node, (ast.FunctionDef, ast.ClassDef)) else
                         [t.id for t in getattr(node, "targets", []) if isinstance(t, ast.Name)])
                for name in names:
                    owners.setdefault(name, []).append(path.name)
        repeated = {name: places for name, places in owners.items() if len(places) > 1}
        self.assertEqual(repeated, {})
        compile(embedded_source([ROOT / "fabric" / "notebooks" / "steps.py"]), "embedded", "exec")
        self.assertRegex(code_hash(), r"^[0-9a-f]{64}$")


class PipelineTests(TempFolder):
    COMPLETED = "2026-11-12T08:00:00Z"

    def setUp(self):
        super().setUp()
        self.store = FolderStore(self.folder / "lake")
        self.config = {"registry": registry(), "code_hash": code_hash(), "factors": FACTORS}

    def deliver(self, sequence, batteries=0):
        deliveries = simulator.make_deliveries("MAIN", sequence, batteries)
        for party, delivery, files in deliveries:
            for name, data in files.items():
                self.store.write_file("ingest", f"deliveries/{party}/{delivery}/{name}", data)
        return deliveries, ",".join(f"{party}/{delivery}" for party, delivery, _ in deliveries)

    def build(self, sequence, batteries=0):
        deliveries, delivery_set = self.deliver(sequence, batteries)
        parameters = {"delivery_set": delivery_set, "build_id": build_id_of("MAIN", deliveries),
                      "build_sequence": str(sequence), "completed_utc": self.COMPLETED}
        for step in ("bronze_load", "silver_normalise", "gold_package"):
            result = steps.run_step(step, self.store, self.config, parameters)
        folder = self.store.root / "gold" / "Files" / "packages" / parameters["build_id"]
        return deliveries, parameters, result, package.read_folder(folder)

    def test_the_notebook_steps_build_the_local_package(self):
        deliveries, parameters, result, files = self.build(1)
        self.assertEqual(result["status"], "written")
        build_id = parameters["build_id"]
        self.assertEqual(files, local_package(deliveries, registry(), build_id, 1, code_hash(), self.COMPLETED,
                                              FACTORS))
        self.assertEqual(package.package_errors(files), [])
        completeness = self.store.read_table("silver", "lot_completeness")
        separator = next(row for row in completeness if row["lot_id"] == "LOT-SP-A")
        self.assertEqual(json.loads(separator["required_fields"]),
                         ["claim_id", "claim_sha256", "mass_kg", "material", "pcf_kgco2e_per_kg",
                          "recycled_share_pct", "region", "supplier_id"])
        deliveries, parameters, _, second = self.build(2, batteries=1)
        self.assertEqual(second, local_package(deliveries, registry(), parameters["build_id"], 2, code_hash(),
                                               self.COMPLETED, FACTORS))
        self.assertEqual(steps.run_step("gold_package", self.store, self.config, {
            "delivery_set": "PLANT-SE/DLV-X", "build_id": build_id, "build_sequence": "1",
            "completed_utc": self.COMPLETED})["status"], "exists")
        with self.assertRaisesRegex(ValueError, "does not match its deliveries"):
            local_package(deliveries, registry(), "BUILD-MAIN-000000000000", 2, code_hash(), self.COMPLETED, FACTORS)
        with self.assertRaisesRegex(ValueError, "gold_package: the pipeline passed no build_sequence"):
            steps.run_step("gold_package", self.store, self.config, {"delivery_set": "x", "build_id": "y"})

    def test_bronze_keeps_accepted_deliveries_and_retries_rejected_ones(self):
        deliveries, delivery_set = self.deliver(1)
        party, delivery, files = deliveries[0]
        path = f"deliveries/{party}/{delivery}/lots.json"
        self.store.write_file("ingest", path, files["lots.json"] + b" ")
        first = bronze_load(self.store, parse_delivery_set(delivery_set), registry(), self.COMPLETED)
        self.assertEqual([(r["status"], r.get("reason")) for r in first][:2],
                         [("rejected", "hash_mismatch:lots.json"), ("accepted", None)])
        self.store.write_file("ingest", path, files["lots.json"])
        second = bronze_load(self.store, parse_delivery_set(delivery_set), registry(), "2026-11-12T09:00:00Z")
        self.assertEqual([r["loaded"] for r in second], [True] + [False] * (len(deliveries) - 1))
        self.assertEqual({row["status"] for row in self.store.read_table("bronze", "deliveries")}, {"accepted"})
        self.store.write_file("ingest", f"deliveries/{party}/{delivery}/{DELIVERY_MANIFEST}",
                              files[DELIVERY_MANIFEST] + b"\n")
        with self.assertRaisesRegex(ValueError, f"delivery_changed:{delivery} was accepted"):
            bronze_load(self.store, parse_delivery_set(delivery_set), registry(), "2026-11-12T10:00:00Z")


class PackageCheckTests(TempFolder):
    def setUp(self):
        super().setUp()
        self.state = simulator.prepare(output=self.folder, now=clock())
        self.path = simulator.build_paths(self.folder, self.state)["package"]
        self.files = package.read_folder(self.path)

    def edit(self, name, change):
        """The package with one file's rows changed (a function of the row list) and its manifest entry updated."""
        data = b"".join((canonical(row) + "\n").encode("utf-8") for row in change(rows(self.files, name)))
        manifest = json.loads(self.files["manifest.json"])
        for entry in manifest["files"]:
            if entry["name"] == name:
                entry.update(sha256=digest(data), record_count=len(data.splitlines()))
        return {**self.files, name: data, "manifest.json": (canonical(manifest) + "\n").encode("utf-8")}

    def lot(self, lot_id, **fields):
        return self.edit("lots.jsonl", lambda lots: [{**row, **fields} if row["lot_id"] == lot_id else row
                                                     for row in lots])

    def test_tampering_incomplete_builds_and_other_contracts_are_rejected(self):
        manifest = json.loads(self.files["manifest.json"])
        without_claim = sorted(field for field in package.LOT_FIELDS if field != "claim_sha256")
        cases = {
            "no manifest.json: the build did not complete": {
                name: data for name, data in self.files.items() if name != "manifest.json"},
            "batteries.jsonl changed after the manifest was written": {
                **self.files, "batteries.jsonl": self.files["batteries.jsonl"] + b" "},
            "files present": {**self.files, "notes.txt": b"x"},
            "contract_id is 'dbp-passport-package-1'": {**self.files, "manifest.json": json.dumps(
                {**manifest, "contract_id": "dbp-passport-package-1"}).encode("utf-8")},
            "authority is 'passport'": {**self.files, "manifest.json": json.dumps(
                {**manifest, "authority": "passport"}).encode("utf-8")},
            "recycled_share_pct must be 0-100": self.lot("LOT-CU-A", recycled_share_pct=120),
            "mass_kg must be a positive number of kg": self.lot("LOT-CU-A", mass_kg="792 kg"),
            "origin_site is missing and has no not-applicable reason": self.lot("LOT-NI-A", origin_site=None),
            "origin_site has a value and a not-applicable reason": self.lot(
                "LOT-NI-A", na_reasons={"origin_site": "manufactured_component"}),
            "a critical material lot needs a claim reference": self.lot(
                "LOT-CO-B", claim_sha256=None, na_reasons={"claim_sha256": "not_supplied"},
                required_fields=without_claim, present_fields=without_claim),
            "region must be a continent": self.lot("LOT-CU-A", region="Sweden"),
            "kg used, 1 kg delivered": self.lot("LOT-LI-A", mass_kg=1),
            "B-003: composition does not cover separator": self.edit("composition.jsonl", lambda parts: [
                row for row in parts if (row["battery_id"], row["material"]) != ("B-003", "separator")]),
            "kWh allocated": self.edit("batteries.jsonl", lambda batteries: [
                {**row, "allocated_kwh": 99999.0} for row in batteries]),
        }
        for reason, files in cases.items():
            with self.subTest(reason=reason):
                errors = package.package_errors(files)
                self.assertTrue(any(reason in error for error in errors), errors)

    def test_the_command_line_accepts_or_rejects_a_folder(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(package.main([str(self.path)]), 0)
            (self.path / "lots.jsonl").write_bytes(b"{}\n")
            self.assertEqual(package.main([str(self.path)]), 1)
            self.assertEqual(package.main([]), 2)
        self.assertRegex(out.getvalue(), f"ACCEPT {BUILD_ID} \\(sequence 100\\): 5 batteries, 5 ready, 0 blocked; "
                                         r"14 lots, 48 composition rows; factor set illustrative-factors-2026-1")
        self.assertIn("lots.jsonl changed after the manifest was written", out.getvalue())

    def test_the_app_reads_the_contract_2_package_itself(self):
        self.assertEqual(app_check_package(self.files).errors, [])
        records = RuleTests.change(story_list(), "LOT-CO-B", claim_id=None, claim_sha256=None)
        result = derive(records, simulator.CREATED_UTC, registry(), FACTORS)
        files, blocked_manifest = build_package(result, "BUILD-MAIN-1", 1, code_hash(), [], "2026-11-12T08:00:00Z",
                                                FACTORS)
        blocked = {**files, "manifest.json": canonical(blocked_manifest).encode("utf-8")}
        self.assertEqual(app_check_package(blocked).errors, [])
        b4 = batteries_of(blocked)["B-004"]
        self.assertEqual((b4["readiness"], b4["reasons"]), ("blocked", ["claim_missing:LOT-CO-B"]))

    def test_the_app_carries_the_same_factor_set(self):
        self.assertEqual((ROOT / "app" / "passport" / "factors.toml").read_text(encoding="utf-8"),
                         (ROOT / "data" / "factors.toml").read_text(encoding="utf-8"))

    def test_the_app_carries_the_same_measures(self):
        self.assertEqual((ROOT / "app" / "passport" / "metrics.py").read_text(encoding="utf-8"),
                         (ROOT / "data" / "metrics.py").read_text(encoding="utf-8"))

    def test_measures_leave_successors_out_of_production(self):
        def row(battery, plant, version, verified=1, footprint=60.0, successor=None, value=None):
            return {"battery_id": battery, "plant_id": plant, "committed_version": version,
                    "footprint_verified": verified, "footprint_per_kwh": footprint, "successor_id": successor,
                    "second_life_value_eur_illustrative": value}
        rows = [row("B-1", "P-A", 1, successor="B-1-SL1", value=3375), row("B-2", "P-A", 0, verified=0),
                row("B-3", "P-B", 2, footprint=70.0), row("B-4", "P-B", 1, verified=0),
                row("B-1-SL1", None, 1, footprint=None)]
        lots = [{"supplier_id": "S-1", "material": "lithium", "fields_complete": 1, "claim_status": "verified"},
                {"supplier_id": "S-1", "material": "lithium", "fields_complete": 0, "claim_status": "verified"},
                {"supplier_id": "S-2", "material": "cobalt", "fields_complete": 1, "claim_status": "missing"}]
        result = metrics.measures(rows, lots)
        self.assertEqual(result["coverage"], {"produced": 4, "published": 3, "coverage_pct": 75.0})
        self.assertEqual(result["verified_footprint"], {"published": 3, "verified": 2, "verified_pct": 66.7,
                                                        "avg_kg_co2e_per_kwh": 65.0})
        self.assertEqual((result["supplier_completeness"]["complete"], result["supplier_completeness"]["complete_pct"],
                          [g["complete"] for g in result["supplier_completeness"]["by_supplier"]]), (1, 33.3, [1, 0]))
        self.assertEqual(result["second_life"], {"repurposed": 1, "value_eur_illustrative": 3375.0})
        self.assertEqual([(p["plant_id"], p["produced"], p["published"], p["coverage_pct"])
                          for p in result["plants"]], [("P-A", 2, 1, 50.0), ("P-B", 2, 2, 100.0)])
        self.assertIsNone(metrics.measures([], [])["coverage"]["coverage_pct"])


class ExportTests(TempFolder):
    ROW = {"battery_id": "B-001", "model_id": simulator.MODEL_ID, "plant_id": "PLANT-SE", "batch_id": "BATCH-1",
           "chemistry": "NMC811", "rated_kwh": 75.0, "eligibility": "eligible", "hold_reason": None,
           "lifecycle_status": "active", "committed_version": 1, "committed_utc": "2026-11-12T08:00:00Z",
           "snapshot_state": "written", "footprint_per_kwh": 61.5, "footprint_verified": 1, "recycled_co_pct": 12.0,
           "recycled_li_pct": 4.0, "recycled_ni_pct": 8.0, "successor_id": None,
           "second_life_value_eur_illustrative": None, "registry_status": "not_registered", "data_label": "synthetic"}
    LOT = {"lot_id": "LOT-LI-A", "supplier_id": "SUP-LI", "material": "lithium", "claim_status": "verified",
           "case_state": "none", "fields_complete": 1, "data_label": "synthetic"}
    ID = "EXP-20261112T080000Z"

    def setUp(self):
        super().setUp()
        self.store = FolderStore(self.folder / "lake")
        state = simulator.prepare(output=self.folder, now=clock())
        ingest = simulator.build_paths(self.folder, state)["ingest"]
        for path in ingest.rglob("*"):
            if path.is_file():
                self.store.write_file("ingest", path.relative_to(ingest).as_posix(), path.read_bytes())
        config = {"registry": registry(), "code_hash": code_hash(), "factors": FACTORS}
        parameters = {key: str(state[key]) for key in ("delivery_set", "build_id", "build_sequence", "completed_utc")}
        for step in ("bronze_load", "silver_normalise", "gold_package"):
            steps.run_step(step, self.store, config, parameters)

    def export(self, export_id, rows_, generated="2026-11-12T08:00:00Z", lots=None):
        for name, data in make_export(rows_, [self.LOT] if lots is None else lots, export_id, generated).items():
            self.store.write_file("reporting", f"exports/{export_id}/{name}", data)
        return reporting_measures(self.store, export_id)

    def story(self, committed=("B-001",)):
        return [{**self.ROW, "battery_id": battery, "plant_id": plant, "committed_version": int(battery in committed),
                 "footprint_verified": int(battery in committed)}
                for battery, plant in (("B-001", "PLANT-SE"), ("B-002", "PLANT-SE"), ("B-003", "PLANT-DE"),
                                       ("B-004", "PLANT-PL"), ("B-005", "PLANT-PL"))]

    @staticmethod
    def resealed(files, name, data):
        """The export with one file replaced and the manifest's hash and count updated to match."""
        manifest = json.loads(files["manifest.json"])
        for entry in manifest["files"]:
            if entry["name"] == name:
                entry.update(sha256=digest(data), record_count=len(data.splitlines()))
        return {**files, name: data, "manifest.json": (canonical(manifest) + "\n").encode("utf-8")}

    def test_an_export_v2_round_trips_with_both_files_and_the_manifest_last(self):
        files = make_export([self.ROW], [self.LOT], self.ID, "2026-11-12T08:00:00Z")
        self.assertEqual(list(files), [EXPORT_FILE, LOT_CLAIMS_FILE, "manifest.json"])
        manifest, rows_, lots = read_export(files, self.ID)
        self.assertEqual((rows_, lots, manifest["schema_version"]), ([self.ROW], [self.LOT], "2"))
        self.assertEqual([(f["name"], f["record_count"]) for f in manifest["files"]],
                         [(EXPORT_FILE, 1), (LOT_CLAIMS_FILE, 1)])
        self.assertEqual(manifest["columns"], list(STATUS_COLUMNS))
        self.assertEqual(read_export(make_export([self.ROW], [], self.ID, "2026-11-12T08:00:00Z"), self.ID)[2], [])

    def test_exports_are_checked_before_use(self):
        files = make_export([self.ROW], [self.LOT], self.ID, "2026-11-12T08:00:00Z")
        manifest = json.loads(files["manifest.json"])
        old = {**files, "manifest.json": canonical({**manifest, "schema_version": "1"}).encode("utf-8")}
        moved = json.dumps({key: self.ROW[key] for key in reversed(STATUS_COLUMNS)}).encode("utf-8") + b"\n"
        extra = json.dumps({**self.LOT, "evidence_file": "claim.pdf"}).encode("utf-8") + b"\n"
        columns = {**files, "manifest.json": canonical({**manifest, "columns": ["battery_id"]}).encode("utf-8")}
        for name, changed, message in (
                ("schema 1", old, "schema version '1' is not read any more.*report publish"),
                ("no lot claims", {k: v for k, v in files.items() if k != LOT_CLAIMS_FILE},
                 f"{LOT_CLAIMS_FILE} is missing or changed"),
                ("changed hash", {**files, EXPORT_FILE: files[EXPORT_FILE] + b"{}\n"}, "missing or changed"),
                ("no manifest", {EXPORT_FILE: files[EXPORT_FILE]}, "no manifest.json"),
                ("other ID", files, "does not follow schema"),
                ("wrong columns", columns, "does not follow schema"),
                ("column order", self.resealed(files, EXPORT_FILE, moved), "exactly its columns in order"),
                ("forbidden field", self.resealed(files, LOT_CLAIMS_FILE, extra), "exactly its columns")):
            export_id = "EXP-20261112T090000Z" if name == "other ID" else self.ID
            with self.subTest(name), self.assertRaisesRegex(ValueError, message):
                read_export(changed, export_id)
        for name, rows_, lots, export_id, message in (
                ("no rows", [], [], self.ID, "no rows"),
                ("bad ID", [self.ROW], [], "EXP-1", "Invalid export ID"),
                ("reviewer", [{**self.ROW, "reviewer": "x"}], [], self.ID, "exactly the columns"),
                ("evidence", [self.ROW], [{**self.LOT, "evidence_name": "a.pdf"}], self.ID, "exactly the columns"),
                ("prompt", [{**self.ROW, "prompt": "x"}], [], self.ID, "exactly the columns")):
            with self.subTest(name), self.assertRaisesRegex(ValueError, message):
                make_export(rows_, lots, export_id, "2026-11-12T08:00:00Z")

    def test_the_measure_tables_follow_the_newest_export_with_the_app_measure_definitions(self):
        first = self.export(self.ID, self.story(committed=()))
        self.assertEqual([(p["plant_id"], p["produced"], p["published"]) for p in first["measures_by_plant"]],
                         [("PLANT-DE", 1, 0), ("PLANT-PL", 2, 0), ("PLANT-SE", 2, 0)])
        story = self.story() + [{**self.ROW, "battery_id": "B-001-S1", "plant_id": "PLANT-SE",
                                 "lifecycle_status": "active", "committed_version": 1}]
        story[0].update(successor_id="B-001-S1", second_life_value_eur_illustrative=3375.0,
                        lifecycle_status="repurposed")
        lots = [self.LOT, {**self.LOT, "lot_id": "LOT-LI-B", "claim_status": "missing"}]
        later = self.export("EXP-20261112T090000Z", story, "2026-11-12T09:00:00Z", lots)
        expected = metrics.measures(story, lots)
        [success] = later["success_measures"]
        self.assertEqual((success["produced"], success["published"], success["coverage_pct"],
                          success["supplier_completeness_pct"], success["second_life_count"],
                          success["second_life_value_eur_illustrative"], success["export_id"]),
                         (5, 1, 20.0, 50.0, 1, 3375.0, "EXP-20261112T090000Z"))
        self.assertEqual(success["coverage_pct"], expected["coverage"]["coverage_pct"])
        self.assertEqual([{k: p[k] for k in expected["plants"][0]} for p in later["measures_by_plant"]],
                         expected["plants"])
        self.assertEqual([(p["plant_id"], p["second_life_count"]) for p in later["measures_by_plant"]],
                         [("PLANT-DE", 0), ("PLANT-PL", 0), ("PLANT-SE", 1)])
        self.assertEqual([(s["supplier_id"], s["lots"], s["complete"]) for s in later["supplier_completeness"]],
                         [("SUP-LI", 2, 1)])
        self.assertEqual(self.store.read_table("reporting", "success_measures"), later["success_measures"])
        self.assertEqual(len(self.store.read_table("reporting", "passport_status_current")), 6)
        self.export("EXP-20261112T090000Z", story, "2026-11-12T09:00:00Z", lots)  # a replay changes nothing
        self.assertEqual(len(self.store.read_table("reporting", "passport_status_history")), 11)
        self.assertEqual(len(self.store.read_table("reporting", "lot_claim_status_history")), 3)
        self.export(self.ID, self.story(committed=()))  # an older export does not become current again
        self.assertEqual(self.store.read_table("reporting", "success_measures"), later["success_measures"])

    def test_unknown_batteries_reject_and_packaged_batteries_missing_from_the_export_are_flagged(self):
        with self.assertRaisesRegex(ValueError, r"1 batteries are not in a completed Fabric package \(B-999\)"):
            self.export(self.ID, [{**self.ROW, "battery_id": "B-999"}])
        result = self.export(self.ID, [self.ROW])
        self.assertEqual((result["not_in_export_count"], result["not_in_export"]),
                         (4, ["B-002", "B-003", "B-004", "B-005"]))
        self.assertEqual(result["success_measures"][0]["not_in_export"], 4)


if __name__ == "__main__":
    unittest.main()
