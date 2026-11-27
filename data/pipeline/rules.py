"""Deterministic rules from party records to a candidate battery package (contract dbp-passport-package-2).

Standard library only and no relative imports: the same text runs in the local simulator and, embedded as source,
in the Fabric notebooks. A package is candidate input for the passport app, never a passport. The rules do not
calculate a footprint: the package carries its inputs (lot mass and PCF, allocated energy, grid factor ID) for the
app's calculator.
"""

import csv
import hashlib
import io
import json
import math
import re
from collections import defaultdict

CONTRACT_ID = "dbp-passport-package-2"
RULES_VERSION = "passport-rules-2"
METHOD_VERSION = "illustrative-pcf-2"
ALLOCATION_RULE = "illustrative-equal-share-per-battery-2"
AUTHORITY = "candidate_input_not_passport"
ASSURANCE = "synthetic_unverified"
PACKAGE_FILES = ("batches.jsonl", "lots.jsonl", "batteries.jsonl", "composition.jsonl")
CHEMISTRIES = {"NMC811": ("lithium", "nickel", "manganese", "cobalt", "graphite", "electrolyte", "copper_foil",
                          "aluminium_foil", "separator")}
MATERIALS = CHEMISTRIES["NMC811"]
CRITICAL_MATERIALS = ("cobalt", "graphite", "lithium", "nickel")
REGIONS = ("Africa", "Asia", "Europe", "North America", "Oceania", "South America")
CONTRACTS = {
    "lot": {"supplier_id", "material", "quantity", "unit", "region", "recycled_share_pct", "pcf_kgco2e_per_kg",
            "party"},
    "series": {"battery_ids", "party"},
    "battery": {"batch_id", "model_id", "plant_id", "chemistry", "rated_kwh", "rated_capacity_ah", "expected_cycles",
                "party"},
    "batch": {"plant_id", "method_version", "factor_set_version", "party"},
    "consumption": {"battery_id", "lot_id", "quantity", "unit", "party"},
    "batch_energy": {"batch_id", "quantity", "unit", "interval_start", "interval_end", "party"},
    "transport": {"lot_id", "origin_site", "destination_plant_id", "shipped_utc", "delivered_utc", "party"},
}
OPTIONAL_FIELDS = {"lot": ("origin_site", "claim_id", "claim_sha256")}  # absent only with a not-applicable reason
CLAIM_FIELDS = ("claim_id", "claim_sha256")
LOT_DATA = ("supplier_id", "material", "region", "origin_site", "mass_kg", "recycled_share_pct", "pcf_kgco2e_per_kg",
            "claim_id", "claim_sha256")
PACKAGE_ENTITIES = ("lot", "series", "battery", "batch", "consumption", "batch_energy")
RECORD_KEYS = {"entity", "key", "version", "na_reasons"}
KG_PER_UNIT = {"g": 0.001, "kg": 1.0, "t": 1000.0}
KWH_PER_UNIT = {"kWh": 1.0, "MWh": 1000.0}
ENTITY_ROLES = {"lot": ("supplier", "recycler"), "transport": ("carrier",)}  # every other entity comes from a plant
ROLE_OWNERS = {"supplier": "supplier_data_owner", "recycler": "supplier_data_owner",
               "carrier": "carrier_data_owner", "plant": "plant_data_owner"}
ROLE_ERRORS = {"lot": "lot_requires_supplier_or_recycler_party", "transport": "transport_requires_carrier_party"}
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,100}")
VERSION_LABEL = re.compile(r"[A-Za-z0-9._-]{1,60}")
NA_REASON = re.compile(r"[a-z0-9_]{1,60}")
SHA256 = re.compile(r"[0-9a-f]{64}")
UTC_TIME = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
REVISION = re.compile(r"[1-9][0-9]*")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError(f"Invalid identifier: {value!r}")
    return value


def utc(value):
    if not isinstance(value, str) or not UTC_TIME.fullmatch(value):
        raise ValueError(f"Invalid UTC time: {value!r}")
    return value


def number(value):
    if isinstance(value, bool):
        raise ValueError(f"Expected a number: {value!r}")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"Expected a number: {value!r}")
    return result


def positive(value):
    if number(value) <= 0:
        raise ValueError(f"Expected a positive number: {value!r}")
    return float(value)


def percentage(value):
    result = number(value)
    if not 0 <= result <= 100:
        raise ValueError(f"percentage_out_of_range:{value}")
    return result


def kg(quantity, unit):
    if unit not in KG_PER_UNIT:
        raise ValueError(f"unsupported_mass_unit:{unit}")
    return round(positive(quantity) * KG_PER_UNIT[unit], 6)


def kwh(quantity, unit):
    if unit not in KWH_PER_UNIT:
        raise ValueError(f"unsupported_energy_unit:{unit}")
    return round(positive(quantity) * KWH_PER_UNIT[unit], 6)


def site_text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValueError("site_requires_text")
    return value


def owner_of(entity):
    return ROLE_OWNERS[ENTITY_ROLES.get(entity, ("plant",))[0]]


def decode_document(name, text):
    """Records of one delivered file: a JSON array, or CSV with entity, key and version columns. An empty or null
    value counts as absent."""
    records = list(csv.DictReader(io.StringIO(text))) if name.endswith(".csv") else json.loads(text)
    if not isinstance(records, list) or not records:
        raise ValueError("Expected nonempty source record array")
    decoded = []
    for record in records:
        if not isinstance(record, dict) or record.get("entity") not in CONTRACTS:
            raise ValueError("Unknown source entity")
        record = {field: value for field, value in record.items() if value not in ("", None)}
        identifier(record.get("key"))
        version = record.get("version")
        if isinstance(version, bool) or not REVISION.fullmatch(str(version)):
            raise ValueError("Expected positive source revision")
        record["version"] = int(version)
        decoded.append(record)
    return decoded


def not_applicable(entity, record):
    """{field: reason} from na_reasons, given as an object or as 'field=reason;field=reason' text (CSV)."""
    value = record.get("na_reasons", {})
    if isinstance(value, str):
        pairs = [item.split("=", 1) for item in value.split(";") if item.strip()]
        if any(len(pair) != 2 for pair in pairs):
            raise ValueError("na_reasons_unreadable")
        value = {field.strip(): reason.strip() for field, reason in pairs}
    if not isinstance(value, dict):
        raise ValueError("na_reasons_unreadable")
    for field, reason in value.items():
        if field not in OPTIONAL_FIELDS.get(entity, ()):
            raise ValueError(f"na_reason_not_allowed:{field}")
        if field in record:
            raise ValueError(f"na_reason_for_present_field:{field}")
        if not isinstance(reason, str) or not NA_REASON.fullmatch(reason):
            raise ValueError(f"na_reason_invalid:{field}")
    return dict(sorted(value.items()))


def _check_fields(entity, record):
    fields = set(record) - RECORD_KEYS
    optional = set(OPTIONAL_FIELDS.get(entity, ()))
    unexpected = sorted(fields - CONTRACTS[entity] - optional)
    if unexpected:
        raise ValueError(f"unexpected_field:{unexpected[0]}")
    missing = sorted(CONTRACTS[entity] - fields)
    if missing:
        raise ValueError(f"missing_field:{missing[0]}")
    reasons = not_applicable(entity, record)
    critical = entity == "lot" and record["material"] in CRITICAL_MATERIALS
    for field in sorted(optional - fields):
        if field not in reasons and not (critical and field in CLAIM_FIELDS):
            raise ValueError(f"missing_field:{field}")  # a critical lot without a claim is blocked later instead
    return reasons


def _check_lot(result, party):
    if result["supplier_id"] != result["party"]:
        raise ValueError("supplier_is_not_the_party")
    if result["material"] not in party.get("materials", ()):
        raise ValueError(f"material_not_allowed_for_party:{str(result['material'])[:40]}")
    if result["region"] not in REGIONS:
        raise ValueError("region_not_a_continent")
    result["quantity_kg"] = kg(result["quantity"], result["unit"])
    result["recycled_share_pct"] = percentage(result["recycled_share_pct"])
    result["pcf_kgco2e_per_kg"] = number(result["pcf_kgco2e_per_kg"])
    if result["pcf_kgco2e_per_kg"] < 0:
        raise ValueError("negative_pcf")
    if "origin_site" in result:
        site_text(result["origin_site"])
    if "claim_id" in result:
        identifier(result["claim_id"])
    if "claim_sha256" in result and (not isinstance(result["claim_sha256"], str)
                                     or not SHA256.fullmatch(result["claim_sha256"])):
        raise ValueError("claim_sha256_invalid")
    result["claim_ref_present"] = all(field in result for field in CLAIM_FIELDS)


def validate(record, registry):
    """One current record, checked against its contract and its party. Raises ValueError with a coded reason."""
    entity = record["entity"]
    reasons = _check_fields(entity, record)
    party = registry.get(record["party"]) if isinstance(record["party"], str) else None
    if party is None:
        raise ValueError("unknown_party")
    if party["role"] not in ENTITY_ROLES.get(entity, ("plant",)):
        raise ValueError(ROLE_ERRORS.get(entity, "production_record_requires_plant_party"))
    result = {**record, "na_reasons": reasons}
    for field in ("supplier_id", "batch_id", "model_id", "plant_id", "battery_id", "lot_id", "chemistry",
                  "destination_plant_id"):
        if field in result:
            identifier(result[field])
    if entity in ("battery", "batch") and result["plant_id"] != result["party"]:
        raise ValueError("plant_is_not_the_party")
    if entity == "lot":
        _check_lot(result, party)
    if entity == "consumption":
        result["quantity_kg"] = kg(result["quantity"], result["unit"])
    if entity == "series":
        members = result["battery_ids"]
        if not isinstance(members, list) or not members or len(members) != len(set(map(str, members))):
            raise ValueError("series_requires_distinct_battery_ids")
        for member in members:
            identifier(member)
    if entity == "battery":
        result["rated_kwh"] = positive(result["rated_kwh"])
        result["rated_capacity_ah"] = positive(result["rated_capacity_ah"])
        cycles = positive(result["expected_cycles"])
        if cycles != int(cycles):
            raise ValueError("expected_cycles_not_whole")
        result["expected_cycles"] = int(cycles)
    if entity == "batch":
        for field in ("method_version", "factor_set_version"):
            if not isinstance(result[field], str) or not VERSION_LABEL.fullmatch(result[field]):
                raise ValueError(f"invalid_{field}")
    if entity == "batch_energy":
        result["kwh"] = kwh(result["quantity"], result["unit"])
        if utc(result["interval_end"]) <= utc(result["interval_start"]):
            raise ValueError("invalid_energy_interval")
    if entity == "transport":
        site_text(result["origin_site"])
        if utc(result["delivered_utc"]) < utc(result["shipped_utc"]):
            raise ValueError("delivery_before_shipment")
    return result


def issue(entity, key, version, reason, owner):
    return {"entity": entity, "source_key": key, "source_version": version, "reason": reason, "owner": owner}


def resolve(records, registry):
    """The latest revision per entity and key. Conflicts and invalid latest revisions become issues."""
    revisions = defaultdict(lambda: defaultdict(set))
    for record in records:
        revisions[(record["entity"], record["key"])][record["version"]].add(canonical(record))
    current, issues, vector = {}, [], {}
    for (entity, key), versions in sorted(revisions.items()):
        version = max(versions)
        vector[f"{entity}:{key}"] = version
        contents = versions[version]
        if len(contents) > 1:
            issues.append(issue(entity, key, version, "conflicting_contents_same_revision", owner_of(entity)))
            continue
        try:
            current[(entity, key)] = validate(json.loads(next(iter(contents))), registry)
        except (ValueError, TypeError, KeyError) as error:
            issues.append(issue(entity, key, version, f"invalid_latest_revision:{error}", owner_of(entity)))
    return current, issues, vector


def lot_fields(lot):
    """(fields that apply to the lot, fields it has), both sorted; mass_kg stands for quantity and unit."""
    values = {**lot, "mass_kg": lot["quantity_kg"]}
    required = sorted(field for field in LOT_DATA if field not in lot["na_reasons"])
    return required, sorted(field for field in required if field in values)


def lot_completeness(current):
    """Rows for silver lot_completeness: the applicable and present fields of every valid current lot."""
    rows = []
    for (entity, key), lot in sorted(current.items()):
        if entity == "lot":
            required, present = lot_fields(lot)
            rows.append({"lot_id": key, "supplier_id": lot["supplier_id"], "material": lot["material"],
                         "required_fields": required, "present_fields": present,
                         "claim_ref_present": lot["claim_ref_present"]})
    return rows


def _referenced_by(records, entity, field, value):
    """Keys of every revision of an entity that names the value, including older revisions."""
    return sorted({r["key"] for r in records if r["entity"] == entity and r.get(field) == value})


def _batch_checks(battery, batch, unusable, factors):
    batch_id, reasons = battery["batch_id"], []
    if ("batch", batch_id) in unusable:
        reasons.append(f"batch_unusable:{batch_id}")
    elif batch is None:
        reasons.append(f"missing_batch:{batch_id}")
    else:
        if batch["method_version"] != METHOD_VERSION:
            reasons.append("method_version_unknown")
        if batch["factor_set_version"] != factors["factor_set"]["factor_set_version"]:
            reasons.append("factor_set_mismatch")
        if batch["plant_id"] != battery["plant_id"]:
            reasons.append("batch_plant_mismatch")
    if battery["chemistry"] not in CHEMISTRIES:
        reasons.append(f"unknown_chemistry:{battery['chemistry']}")
    if battery["plant_id"] not in factors["grid"]:
        reasons.append(f"grid_factor_missing:{battery['plant_id']}")
    return reasons


def _energy_for(records, batch_id, energy, unusable):
    """(reason or None, (energy_id, record) or None) for the batch's one usable energy reading."""
    bad = [key for key in _referenced_by(records, "batch_energy", "batch_id", batch_id)
           if ("batch_energy", key) in unusable]
    measured = sorted(key for key, item in energy.items() if item["batch_id"] == batch_id)
    if bad:
        return "batch_energy_unusable:" + ",".join(bad), None
    if len(measured) != 1:
        return f"missing_or_ambiguous_batch_energy:{batch_id}", None
    return None, (measured[0], energy[measured[0]])


def derive(records, as_of_utc, registry, factors):
    """Per-battery readiness, composition by lot and allocated energy for the one series in the records."""
    utc(as_of_utc)
    records = [record for record in records if record["entity"] in PACKAGE_ENTITIES]
    current, issues, vector = resolve(records, registry)

    def by(entity):
        return {key: record for (kind, key), record in current.items() if kind == entity}

    lots, series_records, batteries, batches = by("lot"), by("series"), by("battery"), by("batch")
    uses, energy = by("consumption"), by("batch_energy")
    unusable = {(item["entity"], item["source_key"]) for item in issues}
    if len(series_records) != 1:
        raise ValueError(f"A package expects exactly one valid series record, found {len(series_records)}.")
    series_id, series = next(iter(series_records.items()))
    members = series["battery_ids"]

    blocked_lots = {}
    for lot_id, lot in sorted(lots.items()):
        if lot["material"] in CRITICAL_MATERIALS and not lot["claim_ref_present"]:
            issues.append(issue("lot", lot_id, lot["version"], "claim_missing", owner_of("lot")))
            blocked_lots[lot_id] = f"claim_missing:{lot_id}"
    lot_totals = defaultdict(float)
    for use in uses.values():
        lot_totals[use["lot_id"]] += use["quantity_kg"]
    for lot_id, total in sorted(lot_totals.items()):
        if lot_id in lots and total > lots[lot_id]["quantity_kg"] + 1e-9:
            issues.append(issue("lot", lot_id, lots[lot_id]["version"], "consumption_exceeds_lot_mass",
                                owner_of("consumption")))
            blocked_lots[lot_id] = f"consumption_exceeds_lot_mass:{lot_id}"

    batch_members = defaultdict(list)
    for battery_id in members:
        if battery_id in batteries:
            batch_members[batteries[battery_id]["batch_id"]].append(battery_id)
    batch_energy = {batch_id: _energy_for(records, batch_id, energy, unusable) for batch_id in batch_members}

    rows, composition = [], []
    for battery_id in members:
        reasons, used, allocation, grid_factor_id = [], defaultdict(float), None, None
        battery = batteries.get(battery_id)
        if ("battery", battery_id) in unusable:
            reasons.append("battery_record_unusable")
        elif battery is None:
            reasons.append("missing_battery_record")
        use_keys = _referenced_by(records, "consumption", "battery_id", battery_id)
        bad_use = [key for key in use_keys if ("consumption", key) in unusable]
        if bad_use:
            reasons.append("consumption_unusable:" + ",".join(bad_use))
        good_use = sorted(key for key, use in uses.items() if use["battery_id"] == battery_id)
        if not good_use and not bad_use:
            reasons.append("no_material_consumption")
        for key in good_use:
            lot_id = uses[key]["lot_id"]
            if ("lot", lot_id) in unusable:
                reasons.append(f"lot_unusable:{lot_id}")
            elif lot_id not in lots:
                reasons.append(f"unknown_lot:{lot_id}")
            elif lot_id in blocked_lots:
                reasons.append(blocked_lots[lot_id])
            else:
                used[(lots[lot_id]["material"], lot_id)] += uses[key]["quantity_kg"]
        if battery is not None:
            reasons += _batch_checks(battery, batches.get(battery["batch_id"]), unusable, factors)
            materials = {lots[uses[key]["lot_id"]]["material"] for key in good_use if uses[key]["lot_id"] in lots}
            reasons += [f"material_missing:{material}" for material in CHEMISTRIES.get(battery["chemistry"], ())
                        if material not in materials]
            grid_factor_id = factors["grid"].get(battery["plant_id"], {}).get("grid_factor_id")
            problem, measured = batch_energy[battery["batch_id"]]
            if problem:
                reasons.append(problem)
            else:
                allocation = (measured[0], measured[1]["version"],
                              round(measured[1]["kwh"] / len(batch_members[battery["batch_id"]]), 6))
        ready = not reasons
        parts = [{"battery_id": battery_id, "material": material, "lot_id": lot_id, "mass_kg": round(mass, 6)}
                 for (material, lot_id), mass in sorted(used.items())] if ready else []
        row = {"battery_id": battery_id, "series_id": series_id}
        for field in ("model_id", "chemistry", "rated_kwh", "rated_capacity_ah", "expected_cycles", "plant_id",
                      "batch_id"):
            row[field] = battery[field] if battery else None
        row.update({"allocated_kwh": allocation[2] if ready else None, "grid_factor_id": grid_factor_id,
                    "readiness": "ready" if ready else "blocked", "reasons": sorted(set(reasons))})
        pins = {"lots": sorted([part["lot_id"], lots[part["lot_id"]]["version"],
                                lots[part["lot_id"]].get("claim_sha256")] for part in parts),
                "energy": list(allocation[:2]) if ready else None,
                "batch": batches[battery["batch_id"]]["version"] if ready else None}
        row["input_hash"] = digest(canonical({"battery": row, "composition": parts, "pins": pins,
                                              "rules_version": RULES_VERSION}).encode("utf-8"))
        rows.append(row)
        composition += parts

    batch_rows = []
    for batch_id in sorted(batch_members):
        batch, (_, measured) = batches.get(batch_id), batch_energy[batch_id]
        if batch is None or ("batch", batch_id) in unusable:
            continue
        batch_rows.append({
            "batch_id": batch_id, "version": batch["version"], "plant_id": batch["plant_id"],
            "method_version": batch["method_version"], "factor_set_version": batch["factor_set_version"],
            "grid_factor_id": factors["grid"].get(batch["plant_id"], {}).get("grid_factor_id"),
            "energy_id": measured[0] if measured else None,
            "energy_version": measured[1]["version"] if measured else None,
            "batch_kwh": measured[1]["kwh"] if measured else None, "battery_count": len(batch_members[batch_id]),
            "allocation_rule": ALLOCATION_RULE})
    lot_rows = []
    for lot_id, lot in sorted(lots.items()):
        if lot_id in blocked_lots:
            continue
        required, present = lot_fields(lot)
        lot_rows.append({"lot_id": lot_id, "version": lot["version"], "supplier_id": lot["supplier_id"],
                         "material": lot["material"], "region": lot["region"], "origin_site": lot.get("origin_site"),
                         "mass_kg": lot["quantity_kg"], "recycled_share_pct": lot["recycled_share_pct"],
                         "pcf_kgco2e_per_kg": lot["pcf_kgco2e_per_kg"], "claim_id": lot.get("claim_id"),
                         "claim_sha256": lot.get("claim_sha256"), "required_fields": required,
                         "present_fields": present, "na_reasons": lot["na_reasons"]})
    return {"as_of_utc": as_of_utc, "series_id": series_id, "series_version": series["version"], "batteries": rows,
            "composition": composition, "lots": lot_rows, "batches": batch_rows,
            "issues": sorted(issues, key=canonical), "revision_vector": vector}


def build_suffix(manifest_hashes):
    """The content part of a build ID: the rules version and the accepted deliveries' manifest hashes."""
    return digest(canonical([RULES_VERSION, sorted(manifest_hashes)]).encode("utf-8"))[:12].upper()


def build_identity(name, manifest_hashes):
    return identifier(f"BUILD-{name}-{build_suffix(manifest_hashes)}")


def jsonl(rows):
    return "".join(canonical(row) + "\n" for row in rows).encode("utf-8")


def build_package(result, build_id, build_sequence, code_hash, source_hashes, completed_utc, factors):
    """The package files in write order and the manifest that is written after them."""
    identifier(build_id)
    if isinstance(build_sequence, bool) or not isinstance(build_sequence, int) or build_sequence < 1:
        raise ValueError(f"Invalid build sequence: {build_sequence!r}")
    utc(completed_utc)
    content = {"batches.jsonl": result["batches"], "lots.jsonl": result["lots"],
               "batteries.jsonl": result["batteries"], "composition.jsonl": result["composition"]}
    files = {name: jsonl(content[name]) for name in PACKAGE_FILES}
    ready = sum(1 for row in result["batteries"] if row["readiness"] == "ready")
    manifest = {
        "contract_id": CONTRACT_ID, "build_id": build_id, "build_sequence": build_sequence,
        "series_id": result["series_id"], "series_version": result["series_version"],
        "expected_battery_count": len(result["batteries"]),
        "battery_ids": [row["battery_id"] for row in result["batteries"]],
        "assessment_utc": result["as_of_utc"], "completed_utc": completed_utc, "rules_version": RULES_VERSION,
        "rules_source_sha256": code_hash, "method_version": METHOD_VERSION,
        "factor_set_version": factors["factor_set"]["factor_set_version"], "allocation_rule": ALLOCATION_RULE,
        "source_document_hashes": sorted(source_hashes), "source_revision_vector": result["revision_vector"],
        "issues": result["issues"],
        "files": [{"name": name, "sha256": digest(files[name]), "record_count": len(content[name])}
                  for name in PACKAGE_FILES],
        "totals": {"ready": ready, "blocked": len(result["batteries"]) - ready},
        "completion_state": "completed", "authority": AUTHORITY, "assurance": ASSURANCE, "data_label": "synthetic",
    }
    return files, manifest


def merge_records(state, records):
    """Version outcome of each incoming record against the state seen so far, in arrival order."""
    state = {key: {"version": value["version"], "payloads": dict(value["payloads"])} for key, value in state.items()}
    log, touched = [], set()
    for position, record in enumerate(records):
        key = (record["entity"], record["key"])
        record_hash = digest(canonical(record).encode("utf-8"))
        known = state.get(key)
        previous = known["version"] if known else None
        if known is None:
            outcome = "new"
            state[key] = {"version": record["version"], "payloads": {record_hash: canonical(record)}}
        elif record["version"] > known["version"]:
            outcome = "replaced"
            state[key] = {"version": record["version"], "payloads": {record_hash: canonical(record)}}
        elif record["version"] < known["version"]:
            outcome = "older"
        elif record_hash in known["payloads"]:
            outcome = "duplicate"
        else:
            outcome = "conflict"
            known["payloads"][record_hash] = canonical(record)
        if outcome in ("new", "replaced", "conflict"):
            touched.add(key)
        log.append({"position": position, "entity": record["entity"], "source_key": record["key"],
                    "incoming_version": record["version"], "previous_version": previous,
                    "record_hash": record_hash, "outcome": outcome})
    return state, log, touched
