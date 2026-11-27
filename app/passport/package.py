"""Package check: the app accepts a Fabric gold package (contract dbp-passport-package-2) only if it is
complete, unchanged and consistent.

This deliberately re-implements the consumer rules of the producer's data/package.py instead of
importing producer code: a consumer must not trust the producer's own checker. Contract 1 is rejected.
"""

import hashlib
import json
import math
import re
from dataclasses import dataclass, field

from .domain import CHEMISTRY, CRITICAL

CONTRACT_ID = "dbp-passport-package-2"
EXPECTED = {"contract_id": CONTRACT_ID, "completion_state": "completed", "authority": "candidate_input_not_passport",
            "data_label": "synthetic"}
FILE_ORDER = ("batches.jsonl", "lots.jsonl", "batteries.jsonl", "composition.jsonl")
READINESS = ("ready", "blocked")
CONTINENTS = {"Africa", "Asia", "Europe", "North America", "Oceania", "South America"}
LOT_FIELDS = ("supplier_id", "material", "region", "origin_site", "mass_kg", "recycled_share_pct", "pcf_kgco2e_per_kg",
              "claim_id", "claim_sha256")
HEX64 = re.compile(r"[0-9a-f]{64}")
TOLERANCE = 1e-6


def sha256(data):
    return hashlib.sha256(data).hexdigest()


@dataclass
class PackageCheck:
    errors: list = field(default_factory=list)
    manifest: dict = field(default_factory=dict)
    batches: list = field(default_factory=list)
    lots: list = field(default_factory=list)
    batteries: list = field(default_factory=list)
    composition: list = field(default_factory=list)
    manifest_sha256: str = ""

    @property
    def ok(self):
        return not self.errors


def positive(value, high=math.inf, low_open=True):
    """True for a finite number (not a boolean) above 0 (or at least 0) and at most high."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return False
    return (value > 0 if low_open else value >= 0) and value <= high


def check_package(files):
    """files: {file name: bytes} for everything found in one intake folder."""
    result = PackageCheck()
    try:
        _check(files, result)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        result.errors.append(f"package does not follow contract {CONTRACT_ID}: {type(exc).__name__} {exc}")
    return result


def _rows(data):
    return [json.loads(line) for line in data.decode("utf-8").split("\n") if line]


def _check(files, result):
    if "manifest.json" not in files:
        result.errors.append("no manifest.json: the build never completed; do not consume these files")
        return
    result.manifest_sha256 = sha256(files["manifest.json"])
    try:
        manifest = json.loads(files["manifest.json"])
    except ValueError:
        result.errors.append("manifest.json is not valid JSON")
        return
    result.manifest = manifest
    errors = result.errors
    errors += [f"{key} is {manifest.get(key)!r}, expected {value!r}"
               for key, value in EXPECTED.items() if manifest.get(key) != value]
    if errors:
        return
    if not manifest.get("build_id") or isinstance(manifest.get("build_sequence"), bool) \
            or not isinstance(manifest.get("build_sequence"), int):
        errors.append("manifest needs build_id and an integer build_sequence")
    for key in ("rules_version", "method_version", "factor_set_version"):
        if not isinstance(manifest.get(key), str) or not manifest[key]:
            errors.append(f"{key} is required")
    if not HEX64.fullmatch(str(manifest.get("rules_source_sha256"))):
        errors.append("rules_source_sha256 must be a SHA-256")
    listed = [entry["name"] for entry in manifest["files"]]
    if listed != list(FILE_ORDER):
        errors.append(f"the manifest lists {listed}, expected {list(FILE_ORDER)} in this order")
    if set(files) != set(listed) | {"manifest.json"}:
        errors.append(f"file set differs: present={sorted(files)}, declared={sorted(listed)}")
    content = {}
    for entry in manifest["files"]:
        name = entry["name"]
        if name not in files:
            continue
        if sha256(files[name]) != entry["sha256"]:
            errors.append(f"{name}: SHA-256 mismatch (file changed after the build completed)")
            continue
        content[name] = _rows(files[name])
        if len(content[name]) != entry["record_count"]:
            errors.append(f"{name}: record count differs from the manifest")
    if errors:
        return
    batches = _unique(content["batches.jsonl"], "batch_id", errors)
    lots = _unique(content["lots.jsonl"], "lot_id", errors)
    batteries, composition = content["batteries.jsonl"], content["composition.jsonl"]
    errors += _batch_errors(manifest, batches)
    errors += _lot_errors(manifest, lots)
    errors += _battery_errors(manifest, batteries, batches, lots, composition)
    if not errors:
        result.batches, result.lots = list(batches.values()), list(lots.values())
        result.batteries, result.composition = batteries, composition


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
            errors.append(f"batch {batch_id} version not in revision vector")
        if batch["energy_id"] is not None:
            if vector.get(f"batch_energy:{batch['energy_id']}") != batch["energy_version"]:
                errors.append(f"energy {batch['energy_id']} version not in revision vector")
            if not positive(batch["batch_kwh"]):
                errors.append(f"{batch_id}: batch_kwh must be a positive number of kWh")
    return errors


def _lot_errors(manifest, lots):
    errors, vector = [], manifest["source_revision_vector"]
    for lot_id, lot in sorted(lots.items()):
        if vector.get(f"lot:{lot_id}") != lot["version"]:
            errors.append(f"lot {lot_id} version not in revision vector")
        reasons = lot["na_reasons"]
        absent = [name for name in LOT_FIELDS if lot.get(name) is None]
        errors += [f"lot {lot_id}: {name} is missing and has no not-applicable reason"
                   for name in absent if name not in reasons]
        errors += [f"lot {lot_id}: {name} has a value and a not-applicable reason"
                   for name in reasons if name not in absent]
        required = sorted(name for name in LOT_FIELDS if name not in reasons)
        if lot["required_fields"] != required or lot["present_fields"] != sorted(set(required) - set(absent)):
            errors.append(f"lot {lot_id}: required or present fields do not match the row")
        if lot["material"] in CRITICAL and not HEX64.fullmatch(str(lot.get("claim_sha256"))):
            errors.append(f"lot {lot_id}: a critical material lot needs a claim reference")
        if lot["region"] not in CONTINENTS:
            errors.append(f"lot {lot_id}: region must be a continent")
        if not positive(lot["mass_kg"]):
            errors.append(f"lot {lot_id}: mass_kg must be a positive number of kg")
        if not positive(lot["recycled_share_pct"], high=100.0, low_open=False):
            errors.append(f"lot {lot_id}: recycled_share_pct must be 0-100")
        if not positive(lot["pcf_kgco2e_per_kg"], low_open=False):
            errors.append(f"lot {lot_id}: pcf_kgco2e_per_kg must be a number of at least 0")
    return errors


def _battery_errors(manifest, batteries, batches, lots, composition):
    errors = []
    battery_ids = [row["battery_id"] for row in batteries]
    if len(set(battery_ids)) != len(battery_ids):
        errors.append("a battery appears more than once")
    if battery_ids != manifest["battery_ids"] or len(battery_ids) != manifest["expected_battery_count"]:
        errors.append("battery rows do not match the manifest series")
    rows = {row["battery_id"]: row for row in batteries}
    parts, per_lot = {}, {}
    for part in composition:
        battery, lot = rows.get(part["battery_id"]), lots.get(part["lot_id"])
        if battery is None or battery["readiness"] != "ready":
            errors.append(f"composition for {part['battery_id']}, which is not a ready battery")
        elif lot is None or lot["material"] != part["material"]:
            errors.append(f"{part['battery_id']}: {part['material']} from {part['lot_id']} is not a listed lot")
        elif not positive(part["mass_kg"]):
            errors.append(f"{part['battery_id']}: composition mass_kg must be a positive number of kg")
        else:
            parts.setdefault(part["battery_id"], set()).add(part["material"])
            per_lot[part["lot_id"]] = per_lot.get(part["lot_id"], 0.0) + part["mass_kg"]
    for lot_id, total in sorted(per_lot.items()):
        if positive(lots[lot_id]["mass_kg"]) and total > lots[lot_id]["mass_kg"] + TOLERANCE:
            errors.append(f"lot {lot_id}: {round(total, 6)} kg used, {lots[lot_id]['mass_kg']} kg delivered")
    used = {}
    for row in batteries:
        battery_id = row["battery_id"]
        if row["readiness"] not in READINESS:
            errors.append(f"{battery_id}: unknown readiness {row['readiness']!r}")
        elif row["readiness"] == "blocked":
            if not row["reasons"] or row["allocated_kwh"] is not None:
                errors.append(f"{battery_id}: a blocked battery needs reasons and no partial totals")
        else:
            batch = batches.get(row["batch_id"])
            if row["reasons"] or batch is None or batch["energy_id"] is None:
                errors.append(f"{battery_id}: a ready battery needs its batch and energy and no reasons")
                continue
            if row["grid_factor_id"] is None or row["grid_factor_id"] != batch["grid_factor_id"]:
                errors.append(f"{battery_id}: grid factor differs from its batch")
            if not positive(row["allocated_kwh"]) or not positive(row["rated_kwh"]):
                errors.append(f"{battery_id}: allocated_kwh and rated_kwh must be positive numbers of kWh")
                continue
            missing = sorted(CHEMISTRY.get(row["chemistry"], {"known chemistry"}) - parts.get(battery_id, set()))
            if missing:
                errors.append(f"{battery_id}: composition does not cover {', '.join(missing)}")
            used[row["batch_id"]] = used.get(row["batch_id"], 0.0) + row["allocated_kwh"]
    for batch_id, total in sorted(used.items()):
        if total > batches[batch_id]["batch_kwh"] + TOLERANCE:
            errors.append(f"{batch_id}: allocated {round(total, 6)} kWh exceeds measured "
                          f"{batches[batch_id]['batch_kwh']}")
    ready = sum(1 for row in batteries if row["readiness"] == "ready")
    if manifest["totals"] != {"ready": ready, "blocked": len(batteries) - ready}:
        errors.append("manifest totals do not reconcile with battery rows")
    return errors
