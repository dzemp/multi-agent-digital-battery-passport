"""Pure passport rules: no database, no files, no network. Both stores and the tests share them."""

import hashlib
import json

SNAPSHOT_SCHEMA = "dbp-passport-snapshot-2"
LABELS = {
    "assurance": "synthetic_unverified",
    "authority": "mvp_passport_record_not_registered",
    "registry": "not_exercised",
    "factors": "illustrative_not_official",
}
READY = "ready"
ELIGIBLE, HELD, BLOCKED = "eligible", "held", "blocked"
INPUTS_INCOMPLETE, METHOD_UNPINNED, COMPOSITION_INVALID = "inputs_incomplete", "method_unpinned", "composition_invalid"
SOURCING_REVIEW_OPEN = "sourcing_review_open"
COMPLIANCE_REVIEW_OPEN = "compliance_review_open"
PASSPORT_CLOSED = "passport_closed"
ACTIVE, SECOND_LIFE_PENDING, END_OF_LIFE = "active", "second_life_pending", "end_of_life"
REPURPOSED, RECYCLED = "repurposed", "recycled"
CLOSED = (REPURPOSED, RECYCLED)
MANUFACTURER = "OEM-ADVENTUREWORKS"
CHEMISTRY = {"NMC811": {"lithium", "nickel", "manganese", "cobalt", "graphite", "electrolyte", "copper_foil",
                        "aluminium_foil", "separator"}}
CRITICAL = {"cobalt", "graphite", "lithium", "nickel"}
RECYCLED_MATERIALS = {"cobalt": "co", "lithium": "li", "nickel": "ni"}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def join_ids(values):
    return ",".join(sorted(set(values)))


def split_ids(text):
    return [v for v in (text or "").split(",") if v]


def batch_row(build_id, batch):
    return {"build_id": build_id, **{k: batch[k] for k in (
        "batch_id", "plant_id", "version", "method_version", "factor_set_version", "allocation_rule", "energy_id",
        "energy_version", "batch_kwh", "battery_count", "grid_factor_id")}}


def lot_row(build_id, lot):
    return {"build_id": build_id, **{k: lot.get(k) for k in (
        "lot_id", "version", "supplier_id", "material", "region", "origin_site", "mass_kg", "recycled_share_pct",
        "pcf_kgco2e_per_kg", "claim_id", "claim_sha256")},
            "required_fields": ",".join(lot["required_fields"]), "present_fields": ",".join(lot["present_fields"]),
            "na_reasons": json.dumps(lot["na_reasons"], sort_keys=True)}


def composition_row(build_id, part):
    return {"build_id": build_id, "battery_id": part["battery_id"], "material": part["material"],
            "lot_id": part["lot_id"], "mass_kg": part["mass_kg"]}


def package_battery_row(build_id, battery, composition, footprint):
    """One row per battery per build, as the package delivered it plus the app's own footprint calculation."""
    return {
        "build_id": build_id,
        "battery_id": battery["battery_id"],
        "series_id": battery.get("series_id"),
        "model_id": battery["model_id"],
        "chemistry": battery.get("chemistry"),
        "rated_kwh": battery.get("rated_kwh"),
        "rated_capacity_ah": battery.get("rated_capacity_ah"),
        "expected_cycles": battery.get("expected_cycles"),
        "plant_id": battery.get("plant_id"),
        "batch_id": battery.get("batch_id"),
        "readiness": battery["readiness"],
        "input_hash": battery["input_hash"],
        "allocated_kwh": battery.get("allocated_kwh"),
        "grid_factor_id": battery.get("grid_factor_id"),
        "lot_ids": join_ids(p["lot_id"] for p in composition),
        "reasons": "; ".join(battery.get("reasons") or [])[:1000],
        "lifecycle_status": ACTIVE,
        "holder_id": MANUFACTURER,
        "footprint_per_kwh": footprint["kg_co2e_per_kwh"] if footprint else None,
        "footprint_verified": 0,
    }


def composition_problems(battery, composition, lots):
    """Defence in depth over the package check: the parts must cover the chemistry and match their lots."""
    problems = []
    expected = CHEMISTRY.get(battery.get("chemistry"))
    materials = {p["material"] for p in composition}
    if expected is None:
        problems.append(f"unknown chemistry {battery.get('chemistry')!r}")
    elif not expected <= materials:
        problems.append(f"composition does not cover {', '.join(sorted(expected - materials))}")
    for part in composition:
        lot = lots.get(part["lot_id"])
        if lot is None or lot["material"] != part["material"] or not part["mass_kg"] or part["mass_kg"] <= 0:
            problems.append(f"{part['material']} from {part['lot_id']} does not match a delivered lot")
    return problems


def eligibility(inputs, open_review_lots, open_case_lots=()):
    """inputs: {battery, batch, composition, lots, method_supported}. Returns (eligibility, coded reason).

    blocked: inputs missing, a method or factor set the app cannot calculate, or an invalid composition.
    held: a lot used by the battery has an open sourcing review, else an open compliance case. eligible: may
    publish."""
    battery, batch = inputs["battery"], inputs["batch"]
    if battery["readiness"] != READY:
        return BLOCKED, INPUTS_INCOMPLETE
    if batch is None or not inputs["method_supported"]:
        return BLOCKED, METHOD_UNPINNED
    if composition_problems(battery, inputs["composition"], inputs["lots"]):
        return BLOCKED, COMPOSITION_INVALID
    if set(split_ids(battery["lot_ids"])) & set(open_review_lots):
        return HELD, SOURCING_REVIEW_OPEN
    if set(split_ids(battery["lot_ids"])) & set(open_case_lots):
        return HELD, COMPLIANCE_REVIEW_OPEN
    return ELIGIBLE, None


def recycled_shares(composition, lots):
    """Mass-weighted recycled share (percent, 0.1) of cobalt, lithium and nickel; None when a material is absent."""
    totals = {key: [0.0, 0.0] for key in RECYCLED_MATERIALS.values()}
    for part in composition:
        key, lot = RECYCLED_MATERIALS.get(part["material"]), lots.get(part["lot_id"])
        if key and lot:
            totals[key][0] += part["mass_kg"]
            totals[key][1] += part["mass_kg"] * lot["recycled_share_pct"] / 100
    return {f"recycled_{key}_pct": round(100 * recycled / mass, 1) if mass else None
            for key, (mass, recycled) in totals.items()}


def snapshot(inputs, footprint, version, decision, committed_by, committed_utc, lifecycle=None):
    """The immutable passport version written to Blob. Its SHA-256 is stored in the SQL ledger first.

    public: what anyone may see. restricted: field groups that access.py projects per stakeholder role.
    lifecycle: {status, holder_id, links, soh_readings, inherited_from} from the lifecycle ledger, when known."""
    battery, batch, composition, lots = inputs["battery"], inputs["batch"], inputs["composition"], inputs["lots"]
    used = sorted({p["lot_id"] for p in composition})
    life = lifecycle or {}
    status = life.get("status") or battery["lifecycle_status"] or ACTIVE
    readings = life.get("soh_readings") or []
    return {
        "schema": SNAPSHOT_SCHEMA,
        "battery_id": battery["battery_id"],
        "passport_version": version,
        "public": {
            "identity": {"battery_id": battery["battery_id"], "model_id": battery["model_id"],
                         "chemistry": battery["chemistry"], "rated_kwh": battery["rated_kwh"],
                         "passport_version": version, "committed_utc": committed_utc},
            "lifecycle": {"status": status, "closed": status in CLOSED},
            "carbon_footprint": {"kg_co2e_per_kwh": footprint["kg_co2e_per_kwh"], "method": footprint["method"],
                                 "factor_set_version": footprint["factor_set_version"],
                                 "assurance": footprint["assurance"], "scope": footprint["scope"],
                                 "verified": False},
            "recycled_content": recycled_shares(composition, lots),
            "links": life.get("links") or [],
        },
        "restricted": {
            "composition": [{"material": p["material"], "lot_id": p["lot_id"], "mass_kg": p["mass_kg"]}
                            for p in sorted(composition, key=lambda p: (p["material"], p["lot_id"]))],
            "calculation": {k: footprint[k] for k in (
                "kg_co2e", "materials_kg_co2e", "electricity_kg_co2e", "allocated_kwh", "grid_factor_id",
                "grid_kgco2e_per_kwh")},
            "manufacturing": {"plant_id": battery["plant_id"], "batch_id": battery["batch_id"],
                              "build_id": battery["build_id"], "input_hash": battery["input_hash"],
                              "allocation_rule": batch["allocation_rule"], "energy_id": batch["energy_id"],
                              "energy_version": batch["energy_version"]},
            "supply": [{"lot_id": lot_id, "supplier_id": lots[lot_id]["supplier_id"],
                        "material": lots[lot_id]["material"], "region": lots[lot_id]["region"],
                        "claim_id": lots[lot_id]["claim_id"]} for lot_id in used],
            "holder": {"holder_id": life.get("holder_id") or battery["holder_id"]},
            "lifecycle": {"latest_soh_pct": readings[-1]["soh_pct"] if readings else None, "soh_readings": readings,
                          "inherited_from": life.get("inherited_from")},
        },
        "decision": decision,
        "committed_by": committed_by,
        "labels": LABELS,
    }


def snapshot_path(battery_id, version):
    return f"{battery_id}/v{version}.json"


def public_view(state, version_row, receipt, links=()):
    """What anyone may see. An MVP choice of fields, not a legal access classification."""
    committed = bool(version_row)
    return {
        "battery_id": state["battery_id"],
        "model_id": state["model_id"],
        "chemistry": state["chemistry"],
        "rated_kwh": state["rated_kwh"],
        "lifecycle_status": state["lifecycle_status"],
        "closed": state["lifecycle_status"] in CLOSED,
        "links": list(links),
        "footprint_per_kwh": state["footprint_per_kwh"] if committed else None,
        "passport_status": "committed" if committed else "not_available",
        "passport_version": version_row["version"] if committed else None,
        "committed_utc": version_row["committed_utc"] if committed else None,
        "snapshot_sha256": version_row["snapshot_sha256"] if committed else None,
        "snapshot_state": ("written" if receipt else "pending") if committed else None,
        "labels": LABELS,
    }


def report_row(state, version_row, receipt, shares, successor_id=None, second_life_value=None, verified=False):
    """One row of report.v_passport_status, the minimized reporting copy that Fabric receives.

    No people, files, suppliers or hashes. footprint_verified: a published footprint whose battery has at least
    one critical lot and every critical lot carries a verified signed claim (see footprint_verified below)."""
    return {
        "battery_id": state["battery_id"],
        "model_id": state["model_id"],
        "plant_id": state["plant_id"],
        "batch_id": state["batch_id"],
        "chemistry": state["chemistry"],
        "rated_kwh": state["rated_kwh"],
        "eligibility": state["eligibility"],
        "hold_reason": state["hold_reason"],
        "lifecycle_status": state["lifecycle_status"],
        "committed_version": state["committed_version"],
        "committed_utc": version_row["committed_utc"] if version_row else None,
        "snapshot_state": ("written" if receipt else "pending") if version_row else "none",
        "footprint_per_kwh": state["footprint_per_kwh"],
        "footprint_verified": 1 if verified else 0,
        "recycled_co_pct": shares.get("recycled_co_pct"),
        "recycled_li_pct": shares.get("recycled_li_pct"),
        "recycled_ni_pct": shares.get("recycled_ni_pct"),
        "successor_id": successor_id,
        "second_life_value_eur_illustrative": second_life_value,
        "registry_status": LABELS["registry"],
        "data_label": "synthetic",
    }


def claim_status(lot, claims, revoked_parties):
    """verified, missing, invalid or revoked for one package lot, from the connector's supplier-claim rows.

    revoked: an accepted claim came from a party now revoked, or a claim was refused because its key was revoked.
    verified: an accepted claim's payload hash equals the claim hash the package lot references.
    invalid: claims arrived for the lot but none matches (refused, or a hash the package does not reference).
    """
    rows = [r for r in claims if r["subject"] == lot["lot_id"]]
    accepted = [r for r in rows if r["result"] == "verified"]
    if any(r["sender"] in revoked_parties for r in accepted) or any(r["reason_code"] == "revoked" for r in rows):
        return "revoked"
    if lot.get("claim_sha256") and any(r["payload_sha256"] == lot["claim_sha256"] for r in accepted):
        return "verified"
    return "invalid" if rows else "missing"


def lot_claim_row(lot, status="missing", case_state="none"):
    """One row of report.v_lot_claim_status. case_state is the status of the lot's latest compliance case (open or
    accepted), or none."""
    return {
        "lot_id": lot["lot_id"],
        "supplier_id": lot["supplier_id"],
        "material": lot["material"],
        "claim_status": status,
        "case_state": case_state,
        "fields_complete": 1 if fields_complete(lot) else 0,
        "data_label": "synthetic",
    }


def fields_complete(lot):
    """Every field the package requires for the lot is present (comma-separated lists in PackageLots)."""
    present = set(split_ids(lot["present_fields"]))
    return all(f in present for f in split_ids(lot["required_fields"]))


def footprint_verified(state, lots, statuses):
    """Published footprint calculated with every critical-lot claim verified. lots: lot_id -> package lot row of the
    battery's build; statuses: lot_id -> claim status. A battery without critical lots (a successor) is not."""
    critical = [k for k in split_ids(state["lot_ids"]) if lots.get(k) and lots[k]["material"] in CRITICAL]
    return bool(state["committed_version"] and state["footprint_per_kwh"] is not None and critical
                and all(statuses.get(k) == "verified" for k in critical))
