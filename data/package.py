"""Independent check of a candidate package folder before it is handed to the passport app.

It applies the consumer rules of contract dbp-passport-package-2 and deliberately does not import the pipeline that
wrote the package. Usage: python -m data.package <package folder>
"""

import hashlib
import json
import math
import re
import sys
from pathlib import Path

CONTRACT = "dbp-passport-package-2"
EXPECTED = {"contract_id": CONTRACT, "completion_state": "completed", "authority": "candidate_input_not_passport",
            "data_label": "synthetic"}
FILE_ORDER = ("batches.jsonl", "lots.jsonl", "batteries.jsonl", "composition.jsonl")
READINESS = ("ready", "blocked")
CHEMISTRY = {"NMC811": {"lithium", "nickel", "manganese", "cobalt", "graphite", "electrolyte", "copper_foil",
                        "aluminium_foil", "separator"}}
CRITICAL = {"cobalt", "graphite", "lithium", "nickel"}
CONTINENTS = {"Africa", "Asia", "Europe", "North America", "Oceania", "South America"}
LOT_FIELDS = ("supplier_id", "material", "region", "origin_site", "mass_kg", "recycled_share_pct", "pcf_kgco2e_per_kg",
              "claim_id", "claim_sha256")
HEX64 = re.compile(r"[0-9a-f]{64}")
LIMIT = 1e-6


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _rows(data):
    return [json.loads(line) for line in data.decode("utf-8").split("\n") if line]


def _number(value, low=0.0, high=math.inf, low_open=False):
    """True for a finite number (not a boolean) within the bounds."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return False
    return (value > low if low_open else value >= low) and value <= high


def package_errors(files):
    """{file name: bytes} of one folder -> a list of reasons to reject it; empty means accept."""
    try:
        return _errors(files)
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        return [f"package does not follow {CONTRACT}: {type(error).__name__} {error}"]


def _errors(files):
    if "manifest.json" not in files:
        return ["no manifest.json: the build did not complete"]
    try:
        manifest = json.loads(files["manifest.json"])
    except ValueError:
        return ["manifest.json is not JSON"]
    errors = [f"{key} is {manifest.get(key)!r}, expected {value!r}"
              for key, value in EXPECTED.items() if manifest.get(key) != value]
    if errors:
        return errors
    if not manifest.get("build_id") or isinstance(manifest.get("build_sequence"), bool) \
            or not isinstance(manifest.get("build_sequence"), int):
        errors.append("build_id and an integer build_sequence are required")
    for key in ("rules_version", "method_version", "factor_set_version"):
        if not isinstance(manifest.get(key), str) or not manifest[key]:
            errors.append(f"{key} is required")
    if not HEX64.fullmatch(str(manifest.get("rules_source_sha256"))):
        errors.append("rules_source_sha256 must be a SHA-256")
    listed = [entry["name"] for entry in manifest["files"]]
    if listed != list(FILE_ORDER):
        errors.append(f"the manifest lists {listed}, expected {list(FILE_ORDER)} in this order")
    if set(files) != set(listed) | {"manifest.json"}:
        errors.append(f"files present {sorted(files)} differ from the manifest {sorted(listed)}")
    content = {}
    for entry in manifest["files"]:
        name = entry["name"]
        if name not in files:
            continue
        if _sha(files[name]) != entry["sha256"]:
            errors.append(f"{name} changed after the manifest was written")
            continue
        content[name] = _rows(files[name])
        if len(content[name]) != entry["record_count"]:
            errors.append(f"{name} has a different record count from the manifest")
    if errors:
        return errors
    batches = _unique(content["batches.jsonl"], "batch_id", errors)
    lots = _unique(content["lots.jsonl"], "lot_id", errors)
    batteries = content["batteries.jsonl"]
    errors += _batch_errors(manifest, batches)
    errors += _lot_errors(manifest, lots)
    errors += _battery_errors(manifest, batteries, batches, lots, content["composition.jsonl"])
    return errors


def _unique(rows, key, errors):
    keyed = {}
    for row in rows:
        if row[key] in keyed:
            errors.append(f"{row[key]} appears more than once")
        keyed[row[key]] = row
    return keyed


def _batch_errors(manifest, batches):
    errors, vector = [], manifest["source_revision_vector"]
    for batch_id, batch in sorted(batches.items()):
        if (batch["method_version"], batch["factor_set_version"]) != (manifest["method_version"],
                                                                      manifest["factor_set_version"]):
            errors.append(f"{batch_id}: method or factor set differs from the manifest")
        if batch["allocation_rule"] != manifest["allocation_rule"]:
            errors.append(f"{batch_id}: allocation rule differs from the manifest")
        if vector.get(f"batch:{batch_id}") != batch["version"]:
            errors.append(f"batch {batch_id} is not pinned in the revision vector")
        if batch["energy_id"] is not None:
            if vector.get(f"batch_energy:{batch['energy_id']}") != batch["energy_version"]:
                errors.append(f"energy {batch['energy_id']} is not pinned in the revision vector")
            if not _number(batch["batch_kwh"], low_open=True):
                errors.append(f"{batch_id}: batch_kwh must be a positive number of kWh")
    return errors


def _lot_errors(manifest, lots):
    errors, vector = [], manifest["source_revision_vector"]
    for lot_id, lot in sorted(lots.items()):
        if vector.get(f"lot:{lot_id}") != lot["version"]:
            errors.append(f"lot {lot_id} is not pinned in the revision vector")
        reasons = lot["na_reasons"]
        absent = [field for field in LOT_FIELDS if lot.get(field) is None]
        for field in absent:
            if field not in reasons:
                errors.append(f"lot {lot_id}: {field} is missing and has no not-applicable reason")
        for field in reasons:
            if field not in absent:
                errors.append(f"lot {lot_id}: {field} has a value and a not-applicable reason")
        required = sorted(field for field in LOT_FIELDS if field not in reasons)
        if lot["required_fields"] != required or lot["present_fields"] != sorted(set(required) - set(absent)):
            errors.append(f"lot {lot_id}: required or present fields do not match the row")
        if lot["material"] in CRITICAL and not HEX64.fullmatch(str(lot.get("claim_sha256"))):
            errors.append(f"lot {lot_id}: a critical material lot needs a claim reference")
        if lot["region"] not in CONTINENTS:
            errors.append(f"lot {lot_id}: region must be a continent")
        if not _number(lot["mass_kg"], low_open=True):
            errors.append(f"lot {lot_id}: mass_kg must be a positive number of kg")
        if not _number(lot["recycled_share_pct"], high=100.0):
            errors.append(f"lot {lot_id}: recycled_share_pct must be 0-100")
        if not _number(lot["pcf_kgco2e_per_kg"]):
            errors.append(f"lot {lot_id}: pcf_kgco2e_per_kg must be a number of at least 0")
    return errors


def _battery_errors(manifest, batteries, batches, lots, composition):
    errors = []
    battery_ids = [row["battery_id"] for row in batteries]
    if len(set(battery_ids)) != len(battery_ids):
        errors.append("duplicate battery rows")
    if battery_ids != manifest["battery_ids"] or len(battery_ids) != manifest["expected_battery_count"]:
        errors.append("battery rows differ from the manifest series")
    rows = {row["battery_id"]: row for row in batteries}
    parts, per_lot = {}, {}
    for part in composition:
        battery, lot = rows.get(part["battery_id"]), lots.get(part["lot_id"])
        if battery is None or battery["readiness"] != "ready":
            errors.append(f"composition for {part['battery_id']}, which is not a ready battery")
        elif lot is None or lot["material"] != part["material"]:
            errors.append(f"{part['battery_id']}: {part['material']} from {part['lot_id']} is not a listed lot")
        elif not _number(part["mass_kg"], low_open=True):
            errors.append(f"{part['battery_id']}: composition mass_kg must be a positive number of kg")
        else:
            parts.setdefault(part["battery_id"], set()).add(part["material"])
            per_lot[part["lot_id"]] = per_lot.get(part["lot_id"], 0.0) + part["mass_kg"]
    for lot_id, total in sorted(per_lot.items()):
        if _number(lots[lot_id]["mass_kg"], low_open=True) and total > lots[lot_id]["mass_kg"] + LIMIT:
            errors.append(f"lot {lot_id}: {round(total, 6)} kg used, {lots[lot_id]['mass_kg']} kg delivered")
    used = {}
    for row in batteries:
        battery_id = row["battery_id"]
        if row["readiness"] not in READINESS:
            errors.append(f"{battery_id}: readiness {row['readiness']!r} is unknown")
        elif row["readiness"] == "blocked":
            if not row["reasons"] or row["allocated_kwh"] is not None:
                errors.append(f"{battery_id}: a blocked battery needs reasons and no allocation")
        else:
            batch = batches.get(row["batch_id"])
            if row["reasons"] or batch is None or batch["energy_id"] is None:
                errors.append(f"{battery_id}: a ready battery needs its batch and energy and no reasons")
                continue
            if row["grid_factor_id"] is None or row["grid_factor_id"] != batch["grid_factor_id"]:
                errors.append(f"{battery_id}: grid factor differs from its batch")
            if not _number(row["allocated_kwh"], low_open=True):
                errors.append(f"{battery_id}: allocated_kwh must be a positive number of kWh")
                continue
            missing = sorted(CHEMISTRY.get(row["chemistry"], {"known chemistry"}) - parts.get(battery_id, set()))
            if missing:
                errors.append(f"{battery_id}: composition does not cover {', '.join(missing)}")
            used[row["batch_id"]] = used.get(row["batch_id"], 0.0) + row["allocated_kwh"]
    for batch_id, total in sorted(used.items()):
        if total > batches[batch_id]["batch_kwh"] + LIMIT:
            errors.append(f"{batch_id}: {round(total, 6)} kWh allocated, {batches[batch_id]['batch_kwh']} measured")
    ready = sum(1 for row in batteries if row["readiness"] == "ready")
    if manifest["totals"] != {"ready": ready, "blocked": len(batteries) - ready}:
        errors.append("manifest totals differ from the battery rows")
    return errors


def read_folder(folder):
    return {path.name: path.read_bytes() for path in sorted(Path(folder).iterdir()) if path.is_file()}


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("Usage: python -m data.package <package folder>", file=sys.stderr)
        return 2
    folder = Path(args[0])
    if not folder.is_dir():
        print(f"REJECT {folder}: not a folder", file=sys.stderr)
        return 1
    files = read_folder(folder)
    errors = package_errors(files)
    if errors:
        print(f"REJECT {folder}")
        for error in errors:
            print(f"  - {error}")
        return 1
    manifest = json.loads(files["manifest.json"])
    counts = {entry["name"]: entry["record_count"] for entry in manifest["files"]}
    print(f"ACCEPT {manifest['build_id']} (sequence {manifest['build_sequence']}): "
          f"{manifest['expected_battery_count']} batteries, {manifest['totals']['ready']} ready, "
          f"{manifest['totals']['blocked']} blocked; {counts['lots.jsonl']} lots, "
          f"{counts['composition.jsonl']} composition rows; factor set {manifest['factor_set_version']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
