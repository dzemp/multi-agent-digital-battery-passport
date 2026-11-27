"""Test helpers: build small dbp-passport-package-2 packages in memory and create a service with local storage."""

import hashlib
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from passport.blobs import LocalBlobs  # noqa: E402
from passport.service import PassportService  # noqa: E402
from passport.store import MemoryStore  # noqa: E402

METHOD, FACTOR_SET = "illustrative-pcf-2", "illustrative-factors-2026-1"
ALLOCATION = "illustrative-equal-share-per-battery-2"
LOT_FIELDS = ("supplier_id", "material", "region", "origin_site", "mass_kg", "recycled_share_pct", "pcf_kgco2e_per_kg",
              "claim_id", "claim_sha256")
# kg per battery; the lithium lot is chosen per battery, the other lots are shared.
PARTS = {"lithium": 6.0, "nickel": 30.0, "manganese": 4.0, "cobalt": 4.0, "graphite": 40.0, "electrolyte": 12.0,
         "copper_foil": 9.0, "aluminium_foil": 5.0, "separator": 2.0}
SHARED_LOTS = {"nickel": "LOT-NI", "manganese": "LOT-MN", "cobalt": "LOT-CO", "graphite": "LOT-GR",
               "electrolyte": "LOT-EL", "copper_foil": "LOT-CU", "aluminium_foil": "LOT-AL", "separator": "LOT-SP"}
BATCHES = {"BATCH-A": "PLANT-SE", "BATCH-B": "PLANT-DE", "BATCH-C": "PLANT-PL"}
# Materials 832 kg CO2e with no recycled content; per kWh with 60 rated kWh and 27 allocated kWh:
EXPECTED_PER_KWH = {"PLANT-SE": 13.8847, "PLANT-DE": 14.0377, "PLANT-PL": 14.1817}


def dumps(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def jsonl(rows):
    return b"".join(dumps(row) + b"\n" for row in rows)


def battery(battery_id, lot_id, batch_id="BATCH-A", kwh=27.0, readiness="ready", revision=1, rated_kwh=60.0):
    ready = readiness == "ready"
    plant = BATCHES[batch_id]
    parts = [(material, lot_id if material == "lithium" else SHARED_LOTS[material], mass)
             for material, mass in PARTS.items()]
    return {
        "battery_id": battery_id, "series_id": "SERIES-V3", "model_id": "MODEL-V3", "chemistry": "NMC811",
        "rated_kwh": rated_kwh, "rated_capacity_ah": 160.0, "expected_cycles": 3000, "plant_id": plant,
        "batch_id": batch_id, "readiness": readiness,
        "input_hash": hashlib.sha256(f"{battery_id}|{lot_id}|{batch_id}|{kwh}|{readiness}|{revision}|{rated_kwh}"
                                     .encode()).hexdigest(),
        "allocated_kwh": kwh if ready else None, "grid_factor_id": f"GF-{plant}-2026" if ready else None,
        "reasons": [] if ready else ["batch_energy_missing"],
        "parts": parts if ready else [],
    }


def lot(lot_id, material, used, **changes):
    row = {"lot_id": lot_id, "version": 1, "supplier_id": "SUP-" + lot_id.split("-")[-1], "material": material,
           "region": "Europe", "origin_site": "Site " + lot_id, "mass_kg": round(used * 2 + 10, 6),
           "recycled_share_pct": 0.0, "pcf_kgco2e_per_kg": 9.0, "claim_id": "CLAIM-" + lot_id,
           "claim_sha256": hashlib.sha256(lot_id.encode()).hexdigest(), "na_reasons": {},
           "required_fields": sorted(LOT_FIELDS), "present_fields": sorted(LOT_FIELDS)}
    return {**row, **changes}


def make_package(build_id, sequence, batteries, lots=None, method=METHOD, factor_set=FACTOR_SET):
    """lots: {lot_id: field changes} applied to the generated lot rows."""
    used, materials = {}, {}
    composition = []
    for b in batteries:
        for material, lot_id, mass in b["parts"]:
            used[lot_id] = used.get(lot_id, 0.0) + mass
            materials[lot_id] = material
            composition.append({"battery_id": b["battery_id"], "material": material, "lot_id": lot_id,
                                "mass_kg": mass})
    lot_rows = [lot(lot_id, materials[lot_id], used[lot_id], **(lots or {}).get(lot_id, {}))
                for lot_id in sorted(used)]
    batch_rows = []
    for batch_id in sorted({b["batch_id"] for b in batteries}):
        members = [b for b in batteries if b["batch_id"] == batch_id]
        ready = [b for b in members if b["readiness"] == "ready"]
        suffix = batch_id[-1]
        batch_rows.append({
            "batch_id": batch_id, "plant_id": BATCHES[batch_id], "version": 1, "method_version": method,
            "factor_set_version": factor_set, "allocation_rule": ALLOCATION,
            "energy_id": f"ENERGY-{suffix}" if ready else None, "energy_version": 1 if ready else None,
            "batch_kwh": sum(b["allocated_kwh"] for b in ready) if ready else None, "battery_count": len(members),
            "grid_factor_id": f"GF-{BATCHES[batch_id]}-2026"})
    battery_rows = [{k: v for k, v in b.items() if k != "parts"} for b in batteries]
    content = {"batches.jsonl": jsonl(batch_rows), "lots.jsonl": jsonl(lot_rows),
               "batteries.jsonl": jsonl(battery_rows), "composition.jsonl": jsonl(composition)}
    counts = {"batches.jsonl": len(batch_rows), "lots.jsonl": len(lot_rows), "batteries.jsonl": len(battery_rows),
              "composition.jsonl": len(composition)}
    vector = {f"batch:{b['batch_id']}": 1 for b in batch_rows} | {f"lot:{k}": 1 for k in used} | \
        {f"batch_energy:{b['energy_id']}": 1 for b in batch_rows if b["energy_id"]}
    ready = sum(1 for b in batteries if b["readiness"] == "ready")
    manifest = {
        "contract_id": "dbp-passport-package-2", "completion_state": "completed",
        "authority": "candidate_input_not_passport", "data_label": "synthetic", "build_id": build_id,
        "build_sequence": sequence, "series_id": "SERIES-V3", "rules_version": "test-rules-2",
        "method_version": method, "factor_set_version": factor_set, "allocation_rule": ALLOCATION,
        "rules_source_sha256": hashlib.sha256(b"test rules").hexdigest(),
        "battery_ids": [b["battery_id"] for b in batteries], "expected_battery_count": len(batteries),
        "source_revision_vector": vector,
        "files": [{"name": n, "sha256": hashlib.sha256(content[n]).hexdigest(), "record_count": counts[n]}
                  for n in content],
        "totals": {"ready": ready, "blocked": len(batteries) - ready},
    }
    return {**content, "manifest.json": dumps(manifest)}


def standard_package(build_id="BUILD-03", sequence=2):
    return make_package(build_id, sequence, [battery("BAT-101", "LOT-A"), battery("BAT-102", "LOT-A"),
                                             battery("BAT-103", "LOT-B", "BATCH-B", 35.0)])


def rows(files, name):
    return [json.loads(line) for line in files[name].decode("utf-8").splitlines() if line]


def rewrite(files, name, change):
    """Change one data file with change(rows) and re-seal the manifest, as a producer bug would."""
    files = dict(files)
    data = rows(files, name)
    change(data)
    files[name] = jsonl(data)
    manifest = json.loads(files["manifest.json"])
    for entry in manifest["files"]:
        if entry["name"] == name:
            entry["sha256"], entry["record_count"] = hashlib.sha256(files[name]).hexdigest(), len(data)
    files["manifest.json"] = dumps(manifest)
    return files


def put_package(blobs, folder, files):
    for name, data in files.items():
        blobs.put("intake", f"{folder}/{name}", data)


def new_service():
    root = tempfile.mkdtemp(prefix="dbp-test-")
    blobs = LocalBlobs(root)
    return PassportService(MemoryStore(), blobs), blobs


REVIEWER = "reviewer (local:rev)"
OPERATOR = "operator (local:op)"


def aftersales_records():
    """All three original packs including overlapping serial/batch targets for two OEMs."""
    packs = [{"battery_id": b["battery_id"], "batch_id": b["batch_id"],
              "oem_id": "OEM-FABRIKAM" if b["battery_id"] == "BAT-102" else "OEM-ADVENTUREWORKS",
              "in_service_date": "2024-02-29", "term_version": "TERM-1", "warranty_months": 12,
              "warranty_cycles": 100}
             for b in rows(standard_package(), "batteries.jsonl")]
    return {"packs": packs, "readings": [
        {"reading_id": "EFC-1", "battery_id": "BAT-103", "measured_utc": "2025-01-24T09:00:00Z",
         "actual_cycles": 78, "soh_pct": 92, "cycle_basis": "equivalent_full_cycles"},
        {"reading_id": "EFC-2", "battery_id": "BAT-103", "measured_utc": "2025-02-21T09:00:00Z",
         "actual_cycles": 96, "soh_pct": 88, "cycle_basis": "equivalent_full_cycles"}],
        "campaigns": [{"campaign_id": "RECALL-1", "kind": "recall", "status": "open",
                       "effective_date": "2025-02-01", "batch_ids": ["BATCH-A"], "battery_ids": ["BAT-101", "BAT-103"]},
                      {"campaign_id": "SERVICE-AW", "kind": "service_campaign", "status": "open",
                       "effective_date": "2025-02-01", "batch_ids": [], "battery_ids": ["BAT-103"]}]}


def put_aftersales(blobs, records=None, delivery="DEL-1", as_of="2025-02-22T12:00:00Z", **overrides):
    records = aftersales_records() if records is None else records
    envelope = {"schema": "dbp-aftersales-1", "delivery_id": delivery, "series_id": "SERIES-V3",
                "as_of_utc": as_of, "records": records, "sha256": hashlib.sha256(dumps(records)).hexdigest(),
                **overrides}
    blobs.put("intake", f"aftersales/{delivery}/aftersales.json", dumps(envelope))
    return "/imports/aftersales/" + delivery
