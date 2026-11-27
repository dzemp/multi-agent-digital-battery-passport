"""Aftersales lineage and local/Fabric parity. FabricCase executes the published notebook cells, not a cloud job."""

import hashlib
import json
from copy import deepcopy
from functools import partial

from test_fabric import FabricCase
from test_orchestrator import setUpModule, tearDownModule  # noqa: F401

from data import simulator
from data.aftersales import make_source
from data.aftersales_flow import after_bytes, after_delivery
from data.package import read_folder
from dbp_mvp import MvpError, aftersales, supply
from passport.aftersales_import import import_delivery


class AftersalesFlowTests(FabricCase):
    def prepare_series(self, extra=0, tag=None):
        return simulator.prepare(extra, tag, self.output, now=lambda: 1795435200)

    def cloud(self, state, **kwargs):
        return aftersales.run_cloud(self.config, self.az, self.outputs, state, output=self.output,
                                    **self.options(**kwargs))

    def test_main_and_scale_scopes_have_real_batches_shared_recall_and_varied_observations(self):
        seen = set()
        for extra, tag in ((0, None), (25, "SCALE1"), (50, "SCALE2")):
            with self.subTest(series=tag):
                state = self.prepare_series(extra, tag)
                envelope = aftersales.run_local(state, output=self.output)
                rows = envelope["records"]
                packs = {r["battery_id"]: r for r in rows["packs"]}
                catalog = {r["battery_id"]: r["batch_id"] for r in map(
                    json.loads, (simulator.build_paths(self.output, state)["package"] / "batteries.jsonl")
                    .read_bytes().splitlines())}
                self.assertEqual({k: p["batch_id"] for k, p in packs.items()}, catalog)
                self.assertEqual(len(packs), 5 + extra)
                self.assertTrue(seen.isdisjoint(packs))
                seen.update(packs)
                self.assertEqual(hashlib.sha256(json.dumps(rows, separators=(",", ":"), ensure_ascii=True,
                                                           sort_keys=True).encode()).hexdigest(), envelope["sha256"])
                s = partial(simulator.suffixed, tag=tag)
                latest = {r["battery_id"]: r for r in sorted(rows["readings"], key=lambda r: r["measured_utc"])}
                for battery, cycles in ((s("B-003"), 97), (s("B-005"), 106)):
                    self.assertEqual((packs[battery]["oem_id"], packs[battery]["warranty_cycles"]),
                                     ("OEM-ADVENTUREWORKS", 100))
                    self.assertEqual(latest[battery]["actual_cycles"], cycles)
                self.assertIsNone(packs[s("B-004")]["warranty_months"])
                self.assertNotIn(s("B-004"), latest)
                recall = next(r for r in rows["campaigns"] if r["kind"] == "recall" and r["status"] == "open")
                matched = {b for b, p in packs.items()
                           if b in recall["battery_ids"] or p["batch_id"] in recall["batch_ids"]}
                self.assertIn(s("B-003"), matched)
                self.assertIn(s("B-001"), matched)
                self.assertEqual({packs[b]["oem_id"] for b in matched}, {"OEM-FABRIKAM", "OEM-ADVENTUREWORKS"})
                if tag:
                    self.assertGreater(len(matched), 5)
                    self.assertGreater(len({r["actual_cycles"] for b, r in latest.items() if b.startswith("B-1")}), 2)
                self.assertFalse(aftersales.gold_path(state, self.output).exists())

    def test_fabric_notebooks_trace_late_and_replayed_readings_and_resume_without_production_flow(self):
        state = self.prepare_series(25, "SCALE1")
        local = aftersales.run_local(state, output=self.output)
        result = self.cloud(state)
        self.assertEqual(result, local)
        self.assertEqual(self.fake.ran, ["aftersales_bronze", "aftersales_silver", "aftersales_gold"])
        remote = self.lakehouse_files("gold") / "aftersales" / result["delivery_id"]
        staged = aftersales.gold_path(state, self.output)
        self.assertEqual(staged.read_bytes(), (remote / "aftersales.json").read_bytes())
        manifest = json.loads((remote / "manifest.json").read_bytes())
        self.assertEqual(manifest["files"]["aftersales.json"], hashlib.sha256(staged.read_bytes()).hexdigest())
        self.assertEqual(manifest["row_counts"], {k: len(v) for k, v in result["records"].items()})
        self.assertEqual(sum(manifest["row_outcomes"].values()), sum(r["rows"] for r in manifest["receipts"]))
        readings = self.fake.table("silver", "aftersales_rows")
        occurrences = [r for r in readings if r["source_key"] == "READ-003-02-SCALE1"]
        self.assertEqual(sorted(r["status"] for r in occurrences), ["accepted", "duplicate"])
        raw = {r["receipt"]: json.loads(r["receipt_json"]) for r in self.fake.table("bronze", "aftersales_receipts")}
        late = next(r for r in readings if r["source_key"] == "READ-003-01-SCALE1")
        self.assertGreater(raw[late["receipt"]]["received_utc"], raw[occurrences[0]["receipt"]]["received_utc"])
        self.assertEqual(raw[late["receipt"]]["rows"][late["line"] - 1]["reading_id"], late["source_key"])
        # The boundary consumes the actual fetched bytes after importing the tagged production package.
        for name, content in read_folder(simulator.build_paths(self.output, state)["package"]).items():
            self.app.upload_blob(f"{state['build_id']}/{name}", content)
        status, receipt = self.app.service.import_package(state["build_id"], "operator", "test-tagged-import")
        self.assertIn(status, (200, 201), receipt)
        self.app.upload_blob(f"aftersales/{result['delivery_id']}/aftersales.json", staged.read_bytes())
        for expected in (201, 200):
            status, receipt = import_delivery(self.app.service, result["delivery_id"], "operator")
            self.assertEqual(status, expected, receipt)
            self.assertEqual({k: receipt[k] for k in ("delivery_id", "series_id", "as_of_utc", "sha256")},
                             {k: result[k] for k in ("delivery_id", "series_id", "as_of_utc", "sha256")})
        self.assertEqual(self.cloud(state), result)
        self.assertEqual(len(self.fake.runs), 1)
        items = supply.deploy_items(self.client, self.config.names.fabric_workspace, self.outputs["fabricCapacityId"],
                                    self.out)
        self.fake.calls.clear()
        supply.clear_fabric_data(self.config, self.az, self.outputs, **self.options())
        updates = self.posts(r"/notebooks/[^/]+/updateDefinition$")
        self.assertEqual([url.split("/")[-2] for url in updates], [items["notebooks"]["reset_tables"]])
        self.assertFalse(remote.exists())
        self.assertEqual(self.fake.table("silver", "aftersales_rows"), [])

    def test_conflicts_and_invalid_references_are_quarantined_not_silently_accepted(self):
        state = self.prepare_series()
        source = make_source(state)
        late = source["deliveries"][3]
        template = late["rows"][0]
        late["rows"] += [{**template, "actual_cycles": 12345},
                         {**template, "reading_id": "UNKNOWN-SERIAL", "battery_id": "B-999"},
                         {**template, "reading_id": "WRONG-UNIT", "cycle_basis": "design_cycles"}]
        source["deliveries"][-1]["rows"][0]["batch_ids"] = ["NOT-A-PRODUCTION-BATCH"]
        result = self.cloud(state, source=source)
        self.assertEqual(result, aftersales.run_local(state, output=self.output, source=source))
        seen = {r["reading_id"] for r in result["records"]["readings"]}
        self.assertTrue(seen.isdisjoint((template["reading_id"], "UNKNOWN-SERIAL", "WRONG-UNIT")))
        self.assertFalse(any(c["campaign_id"] == "RECALL-01" for c in result["records"]["campaigns"]))
        bad = [r for r in self.fake.table("silver", "aftersales_rows") if r["status"] == "quarantine"]
        self.assertEqual(len(bad), 5)
        self.assertTrue(all(r["reason"] and r["source_delivery_id"] for r in bad))

    def test_changed_receipt_never_overwrites_original_source_and_later_input_needs_no_new_build(self):
        state = self.prepare_series()
        source = make_source(state)
        previous = self.cloud(state)
        new = deepcopy(source)
        new["as_of_utc"] = "2026-11-28T12:00:00Z"
        new["deliveries"].append({**deepcopy(new["deliveries"][3]), "source_delivery_id": "BMS-03",
                                  "received_utc": "2026-11-28T08:00:00Z", "rows": [
                                      {**new["deliveries"][1]["rows"][0], "reading_id": "READ-001-03",
                                       "measured_utc": "2026-11-27T09:00:00Z", "actual_cycles": 35.5}]})
        latest = self.cloud(state, source=new)
        self.assertNotEqual(latest["delivery_id"], previous["delivery_id"])
        self.assertIn("READ-001-03", {r["reading_id"] for r in latest["records"]["readings"]})
        self.assertEqual(json.loads(aftersales.gold_path(state, self.output).read_bytes()), previous)
        landed = self.lakehouse_files("ingest") / "aftersales" / after_delivery(new) / "source.json"
        landed.write_bytes(after_bytes(source))
        with self.assertRaisesRegex(MvpError, "Conflicting immutable"):
            self.cloud(state, source=new)

    def test_failed_job_corrupt_or_missing_fabric_output_never_stage_local_files(self):
        state = self.prepare_series()
        aftersales.run_local(state, output=self.output)
        staged = aftersales.gold_path(state, self.output)
        with self.assertRaisesRegex(MvpError, "Fabric is not deployed"):
            aftersales.run_cloud(self.config, self.az, {}, state, output=self.output, **self.options())
        self.fake.broken.add("aftersales_silver")
        with self.assertRaisesRegex(MvpError, "pipeline run failed"):
            self.cloud(state)
        self.assertFalse(staged.exists())
        self.assertEqual(self.capacity.actions[-1], "suspend")
        self.fake.broken.clear()
        result = self.cloud(state)
        raw = staged.read_bytes()
        remote = self.lakehouse_files("gold") / "aftersales" / result["delivery_id"] / "aftersales.json"
        for replacement in (b"{}", None):
            if replacement is None:
                remote.unlink()
            else:
                remote.write_bytes(replacement)
            with self.assertRaisesRegex(MvpError, "mismatched Fabric Gold"):
                self.cloud(state)
            self.assertEqual(staged.read_bytes(), raw)  # last verified result is not silently replaced
