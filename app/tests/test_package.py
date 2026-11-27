import json
import unittest

import helpers
from passport import calculator, domain
from passport.package import check_package


def errors(files):
    return check_package(files).errors


def has(files, text):
    return any(text in error for error in errors(files))


class PackageCheckTests(unittest.TestCase):
    def test_valid_package_passes(self):
        result = check_package(helpers.standard_package())
        self.assertEqual(result.errors, [])
        self.assertEqual((len(result.batches), len(result.lots), len(result.batteries), len(result.composition)),
                         (2, 10, 3, 27))
        self.assertEqual(len(result.manifest_sha256), 64)

    def test_contract_1_is_rejected(self):
        files = helpers.standard_package()
        manifest = json.loads(files["manifest.json"])
        files["manifest.json"] = helpers.dumps({**manifest, "contract_id": "dbp-passport-package-1"})
        self.assertTrue(has(files, "contract_id is 'dbp-passport-package-1'"))
        self.assertFalse(check_package({"manifest.json": b'{"contract_id": "dbp-passport-package-1", "files": 5}'}).ok)

    def test_missing_manifest_means_unfinished_build(self):
        files = helpers.standard_package()
        del files["manifest.json"]
        self.assertIn("never completed", errors(files)[0])

    def test_changed_extra_and_reordered_files_are_detected(self):
        changed = helpers.standard_package()
        changed["batteries.jsonl"] = changed["batteries.jsonl"].replace(b"27.0", b"28.0")
        self.assertTrue(has(changed, "SHA-256 mismatch"))
        extra = {**helpers.standard_package(), "notes.txt": b"hello"}
        self.assertTrue(has(extra, "file set differs"))
        reordered = helpers.standard_package()
        manifest = json.loads(reordered["manifest.json"])
        manifest["files"].reverse()
        reordered["manifest.json"] = helpers.dumps(manifest)
        self.assertTrue(has(reordered, "in this order"))

    def test_record_counts_and_build_sequence(self):
        files = helpers.standard_package()
        manifest = json.loads(files["manifest.json"])
        manifest["files"][1]["record_count"] += 1
        manifest["build_sequence"] = "2"
        files["manifest.json"] = helpers.dumps(manifest)
        self.assertTrue(has(files, "lots.jsonl: record count"))
        self.assertTrue(has(files, "integer build_sequence"))

    def test_lot_units_shares_and_applicability_reasons(self):
        base = helpers.standard_package()

        def lot_change(**fields):
            return helpers.rewrite(base, "lots.jsonl", lambda rows: rows[0].update(fields))

        self.assertTrue(has(lot_change(recycled_share_pct=100.5), "recycled_share_pct must be 0-100"))
        self.assertTrue(has(lot_change(mass_kg=-1), "mass_kg must be a positive number"))
        self.assertTrue(has(lot_change(region="Narnia"), "region must be a continent"))
        self.assertTrue(has(lot_change(origin_site=None), "origin_site is missing and has no not-applicable reason"))
        self.assertTrue(has(lot_change(na_reasons={"region": "x"}), "region has a value and a not-applicable reason"))
        self.assertTrue(has(lot_change(present_fields=["material"]), "required or present fields"))

    def test_critical_lot_needs_a_claim_reference(self):
        def drop_claim(rows):
            row = next(r for r in rows if r["lot_id"] == "LOT-CO")
            row.update(claim_sha256=None, na_reasons={"claim_sha256": "not supplied"},
                       present_fields=sorted(set(helpers.LOT_FIELDS) - {"claim_sha256"}),
                       required_fields=sorted(set(helpers.LOT_FIELDS) - {"claim_sha256"}))

        self.assertTrue(has(helpers.rewrite(helpers.standard_package(), "lots.jsonl", drop_claim),
                            "LOT-CO: a critical material lot needs a claim reference"))

    def test_mass_reconciliation_against_delivered_lots(self):
        files = helpers.standard_package()
        lot_files = helpers.rewrite(files, "lots.jsonl",
                                    lambda rows: next(r for r in rows if r["lot_id"] == "LOT-A").update(mass_kg=10.0))
        self.assertTrue(has(lot_files, "lot LOT-A: 12.0 kg used, 10.0 kg delivered"))

    def test_composition_must_cover_the_chemistry_and_match_lots(self):
        files = helpers.standard_package()
        missing = helpers.rewrite(files, "composition.jsonl", lambda rows: rows.__setitem__(
            slice(None), [r for r in rows if not (r["battery_id"] == "BAT-101" and r["material"] == "separator")]))
        self.assertTrue(has(missing, "BAT-101: composition does not cover separator"))
        wrong = helpers.rewrite(files, "composition.jsonl", lambda rows: rows[0].update(material="cobalt"))
        self.assertTrue(has(wrong, "is not a listed lot"))
        chemistry = helpers.rewrite(files, "batteries.jsonl", lambda rows: rows[0].update(chemistry="LFP"))
        self.assertTrue(has(chemistry, "BAT-101: composition does not cover known chemistry"))

    def test_energy_allocation_cannot_exceed_the_batch_reading(self):
        files = helpers.rewrite(helpers.standard_package(), "batches.jsonl",
                                lambda rows: rows[0].update(batch_kwh=20.0))
        self.assertTrue(has(files, "BATCH-A: allocated 54.0 kWh exceeds measured 20.0"))

    def test_ready_and_blocked_batteries(self):
        bad = helpers.battery("BAT-101", "LOT-A")
        bad["readiness"] = "maybe"
        self.assertTrue(has(helpers.make_package("BUILD-09", 9, [bad]), "unknown readiness"))
        blocked = helpers.battery("BAT-104", "LOT-B", readiness="blocked")
        blocked["reasons"] = []
        self.assertTrue(has(helpers.make_package("BUILD-09", 9, [blocked]), "needs reasons"))
        fine = helpers.battery("BAT-105", "LOT-B", readiness="blocked")
        self.assertEqual(errors(helpers.make_package("BUILD-09", 9, [helpers.battery("BAT-106", "LOT-B"), fine])), [])

    def test_wrong_shape_is_an_error_not_a_crash(self):
        files = helpers.rewrite(helpers.standard_package(), "lots.jsonl", lambda rows: rows[0].pop("na_reasons"))
        self.assertFalse(check_package(files).ok)


class CalculatorTests(unittest.TestCase):
    def inputs(self, package, battery_id):
        check = check_package(package)
        battery = next(b for b in check.batteries if b["battery_id"] == battery_id)
        batch = next(b for b in check.batches if b["batch_id"] == battery["batch_id"])
        parts = [p for p in check.composition if p["battery_id"] == battery_id]
        return battery, batch, parts, {lot["lot_id"]: lot for lot in check.lots}

    def test_plant_grid_factors_order_the_footprints(self):
        batteries = [helpers.battery("BAT-1", "LOT-A", "BATCH-A"), helpers.battery("BAT-2", "LOT-A", "BATCH-B"),
                     helpers.battery("BAT-3", "LOT-A", "BATCH-C")]
        package = helpers.make_package("BUILD-1", 1, batteries)
        results = {plant: calculator.calculate(*self.inputs(package, battery))["kg_co2e_per_kwh"]
                   for plant, battery in (("PLANT-SE", "BAT-1"), ("PLANT-DE", "BAT-2"), ("PLANT-PL", "BAT-3"))}
        for plant, expected in helpers.EXPECTED_PER_KWH.items():
            self.assertAlmostEqual(results[plant], expected, places=4)
        self.assertLess(results["PLANT-SE"], results["PLANT-DE"])
        self.assertLess(results["PLANT-DE"], results["PLANT-PL"])

    def test_recycled_share_lowers_the_footprint(self):
        battery = [helpers.battery("BAT-1", "LOT-A")]
        primary = calculator.calculate(*self.inputs(helpers.make_package("B-1", 1, battery), "BAT-1"))
        recycled = calculator.calculate(*self.inputs(helpers.make_package(
            "B-1", 1, battery, lots={"LOT-A": {"recycled_share_pct": 50.0}}), "BAT-1"))
        # 6 kg lithium, half recycled: 3 kg x (9.0 - 3.0) less.
        self.assertAlmostEqual(primary["kg_co2e"] - recycled["kg_co2e"], 18.0, places=3)
        self.assertEqual(primary["factor_set_version"], "illustrative-factors-2026-1")

    def test_other_method_or_factor_set_is_refused(self):
        battery = [helpers.battery("BAT-1", "LOT-A")]
        for changes in ({"method": "illustrative-pcf-3"}, {"factor_set": "illustrative-factors-2027-1"}):
            with self.assertRaises(calculator.MethodMismatch):
                calculator.calculate(*self.inputs(helpers.make_package("B-1", 1, battery, **changes), "BAT-1"))

    def test_composition_problems_block_with_a_coded_reason(self):
        battery, batch, parts, lots = self.inputs(helpers.standard_package(), "BAT-101")
        row = {**battery, "lot_ids": domain.join_ids(p["lot_id"] for p in parts)}
        inputs = {"battery": row, "batch": batch, "composition": parts, "lots": lots, "method_supported": True}
        self.assertEqual(domain.eligibility(inputs, set()), (domain.ELIGIBLE, None))
        short = {**inputs, "composition": [p for p in parts if p["material"] != "graphite"]}
        self.assertEqual(domain.eligibility(short, set()), (domain.BLOCKED, domain.COMPOSITION_INVALID))
        self.assertEqual(domain.eligibility({**inputs, "method_supported": False}, set()),
                         (domain.BLOCKED, domain.METHOD_UNPINNED))
        self.assertEqual(domain.eligibility(inputs, {"LOT-A"}), (domain.HELD, domain.SOURCING_REVIEW_OPEN))


if __name__ == "__main__":
    unittest.main()
