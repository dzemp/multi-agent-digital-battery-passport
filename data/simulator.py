"""Synthetic supply chain for one series: party deliveries for the Fabric ingest lakehouse, the equivalent local
package, the signed partner messages with the party registry, and the evidence documents a Reviewer uploads.

Added batteries vary: about a fifth of them carry a seeded data issue (see ISSUES), drawn from the series name and
size, so the same series always gets the same issues. Everything is fictional and deterministic apart from the build
sequence, the completion time and the runtime signing keys. Factors are illustrative, not official.
Usage: python -m data.simulator [--batteries N] [--tag T] [--new-build] [--output DIR]
"""

import argparse
import json
import math
import random
import re
import sys
import time
from collections import Counter
from pathlib import Path

from . import signed_messages
from .factors import load_factors
from .package import package_errors
from .parties import PARTIES, PLANTS, party, registry
from .pipeline import code_hash
from .pipeline.deliveries import make_delivery
from .pipeline.flows import local_package
from .pipeline.rules import METHOD_VERSION, build_identity, digest

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "output"
EVIDENCE = Path(__file__).resolve().parent / "evidence"
MAIN = "MAIN"
TAG = re.compile(r"[A-Z0-9]{1,6}")
MAX_BATTERIES = 50
CREATED_UTC = "2026-10-20T06:00:00Z"
MODEL_ID = "MODEL-NMC811"
CHEMISTRY = "NMC811"
RATED_KWH, RATED_AH, EXPECTED_CYCLES = 75, 200, 2000
LABEL = "synthetic - fictional companies and places"
SIGNATURE_NOTE = ("Readable copy for human review. The signed supplier claim (Ed25519, dbp-supplier-claim-2) is "
                  "delivered separately as a partner message.")
STORY = ("001", "002", "003", "004", "005")
# Material use of one battery in kg (NMC811 cathode, natural-graphite anode and cell components).
USE_KG = {"lithium": 12, "nickel": 40, "manganese": 5, "cobalt": 5, "graphite": 50, "electrolyte": 15,
          "copper_foil": 12, "aluminium_foil": 8, "separator": 3}
# Lot -> (party, material, origin site or None for a manufactured component, recycled share %)
LOTS = {
    "LOT-LI-A": ("SUP-NORTHWIND", "lithium", "Mine North", 0),
    "LOT-LI-B": ("SUP-WIDEWORLD", "lithium", "Mine East", 0),
    "LOT-CO-A": ("SUP-PROSEWARE", "cobalt", "Mine Valley", 0),
    "LOT-CO-B": ("SUP-PROSEWARE", "cobalt", "Mine Ridge", 0),
    "LOT-NI-A": ("SUP-WOODGROVE", "nickel", "Mine West", 0),
    "LOT-GR-A": ("SUP-LITWARE", "graphite", "Mine Highland", 0),
    "LOT-MN-A": ("SUP-LUCERNE", "manganese", None, 0),
    "LOT-EL-A": ("SUP-LUCERNE", "electrolyte", None, 0),
    "LOT-CU-A": ("SUP-LUCERNE", "copper_foil", None, 30),
    "LOT-AL-A": ("SUP-LUCERNE", "aluminium_foil", None, 40),
    "LOT-SP-A": ("SUP-LUCERNE", "separator", None, 0),
    "LOT-LI-R": ("RECYCLER-TREY", "lithium", "Trey recovery site (fictional)", 100),
    "LOT-NI-R": ("RECYCLER-TREY", "nickel", "Trey recovery site (fictional)", 100),
    "LOT-CO-R": ("RECYCLER-TREY", "cobalt", "Trey recovery site (fictional)", 100),
}
DEFAULT_LOTS = {"lithium": "LOT-LI-B", "nickel": "LOT-NI-A", "cobalt": "LOT-CO-A", "graphite": "LOT-GR-A",
                "manganese": "LOT-MN-A", "electrolyte": "LOT-EL-A", "copper_foil": "LOT-CU-A",
                "aluminium_foil": "LOT-AL-A", "separator": "LOT-SP-A"}
LOT_LI_A_KG, LOT_MARGIN = 100, 1.2
CLAIM_ISSUED = {"LOT-LI-A": "2026-10-12T08:00:00Z", "LOT-LI-B": "2026-10-13T08:00:00Z"}
DEFAULT_CLAIM_ISSUED = "2026-10-14T08:00:00Z"
CORRECTED_ISSUED = "2026-10-16T09:15:00Z"
KWH_PER_BATTERY = {"PLANT-SE": 3000, "PLANT-DE": 3100, "PLANT-PL": 3200}
ENERGY_UNIT = {"PLANT-SE": "kWh", "PLANT-DE": "kWh", "PLANT-PL": "MWh"}
STORY_BATCHES = {"001": "BATCH-SE-01", "002": "BATCH-SE-01", "003": "BATCH-DE-01", "004": "BATCH-PL-01",
                 "005": "BATCH-PL-01"}
EXTRA_BATCHES = {"PLANT-SE": "BATCH-SE-02", "PLANT-DE": "BATCH-DE-01", "PLANT-PL": "BATCH-PL-01"}
TRANSPORT = (("TR-LOT-LI-A-0417", "LOT-LI-A", "Mine South", "PLANT-SE", "2026-10-03T06:30:00Z",
              "2026-10-09T14:10:00Z"),
             ("TR-LOT-LI-B-0422", "LOT-LI-B", "Mine East", "PLANT-DE", "2026-10-05T07:45:00Z",
              "2026-10-11T15:20:00Z"))
SOH_PCT = {"003": 88, "005": 62}
# Data issues seeded into added batteries (issue_plan). A lot-level kind uses one issue lot, shared by every battery
# drawn for that kind: kind -> (lot, party, material, site its claim version 1 names, site its transport record names).
ISSUES = {
    "mismatch_corrected": ("LOT-LI-C", "SUP-NORTHWIND", "lithium", "Mine Canyon", "Mine Delta"),
    "mismatch_open": ("LOT-LI-D", "SUP-WIDEWORLD", "lithium", "Mine Harbor", "Mine Summit"),
    "adverse_audited": ("LOT-CO-C", "SUP-PROSEWARE", "cobalt", "Mine Basin", None),
    "adverse_open": ("LOT-CO-D", "SUP-PROSEWARE", "cobalt", "Mine Crest", None),
    "adverse_answered": ("LOT-CO-E", "SUP-PROSEWARE", "cobalt", "Mine Lagoon", None),
    "overdrawn_lot": ("LOT-NI-B", "SUP-WOODGROVE", "nickel", "Mine Plateau", None),
    "unmetered_batch": None,
}
# How the scenario treats each kind: the pipeline blocks the battery before any agent, a human decision releases it,
# or its review or case stays open on purpose. Every other kind is published in prepare.
BLOCKED_KINDS = ("overdrawn_lot", "unmetered_batch")
RELEASED_KINDS = ("mismatch_corrected", "adverse_audited")
OPEN_KINDS = ("mismatch_open", "adverse_open")
ISSUE_SHARE, OVERDRAWN_SHARE = 0.2, 0.5
# A new batch per plant that has no energy record, for unmetered_batch batteries.
UNMETERED_BATCHES = {"PLANT-SE": "BATCH-SE-03", "PLANT-DE": "BATCH-DE-03", "PLANT-PL": "BATCH-PL-03"}
# Issue lot -> (transport record, shipped, delivered); it is delivered to the plant of its first battery.
ISSUE_TRANSPORT = {"LOT-LI-C": ("TR-LOT-LI-C-0431", "2026-10-06T06:15:00Z", "2026-10-12T13:40:00Z"),
                   "LOT-LI-D": ("TR-LOT-LI-D-0436", "2026-10-07T08:05:00Z", "2026-10-13T16:25:00Z")}
# (agent, act, model calls for the five story batteries); estimate() adds the added batteries and their issues.
CALLS = (("supplier", "prepare", 9), ("carbon", "prepare", 5), ("issuance", "prepare", 2),
         ("supplier", "complete", 2), ("issuance", "complete", 3), ("lifecycle", "aftercare", 7))
# (evidence file, upload kind, lot) uploaded after the package import. The signed claims, including LOT-LI-A's
# later correction, arrive as partner messages through the app's mock connector. The adverse report about the mine
# behind LOT-CO-B is uploaded before the Supplier tasks run; the audit report arrives during the review act.
EVIDENCE_UPLOADS = (("lot-li-a-transport-record.json", "transport_record", "LOT-LI-A"),
                    ("lot-li-b-transport-record.json", "transport_record", "LOT-LI-B"),
                    ("lot-co-b-adverse-report.json", "adverse_report", "LOT-CO-B"))
AUDIT_UPLOAD = ("lot-co-b-audit-report.json", "audit_report", "LOT-CO-B")
CRITICAL_MATERIALS = ("lithium", "cobalt", "nickel", "graphite")
AGREEMENT_PURPOSE = "Synthetic supply-chain messages for the Contoso battery passport (mock connector)"
AGREEMENT_VALIDITY = ("2026-10-01T00:00:00Z", "2036-10-01T00:00:00Z")
ADVERSE_REPORT = "lot-co-b-adverse-report.json"
AUDIT_REPORT = AUDIT_UPLOAD[0]


def check_options(batteries, tag):
    if isinstance(batteries, bool) or not isinstance(batteries, int) or not 0 <= batteries <= MAX_BATTERIES:
        raise ValueError(f"--batteries must be a whole number from 0 to {MAX_BATTERIES}.")
    if tag is not None and (not isinstance(tag, str) or not TAG.fullmatch(tag) or tag == MAIN):
        raise ValueError("--tag must be 1-6 capital letters or digits (and not MAIN).")


def suffixed(value, tag):
    return f"{value}-{tag}" if tag else value


def battery_numbers(batteries):
    return list(STORY) + [f"{101 + index:03d}" for index in range(batteries)]


def issue_plan(batteries=0, tag=None):
    """{number: issue kind} for the added batteries that carry a seeded data issue: about ISSUE_SHARE of them, and
    every kind at least once from len(ISSUES) + 1 added batteries. The draw is seeded with the series name and size,
    so a series always gets the same issues; the five story batteries never change."""
    check_options(batteries, tag)
    rng = random.Random(f"{series_name(tag)}:{batteries}")
    kinds = list(ISSUES)
    count = round(ISSUE_SHARE * batteries)
    if batteries > len(kinds):
        count = max(count, len(kinds))
    chosen = rng.sample(battery_numbers(batteries)[len(STORY):], count)
    if count >= len(kinds):
        drawn = kinds + [rng.choice(kinds) for _ in range(count - len(kinds))]
        rng.shuffle(drawn)
    else:
        drawn = rng.sample(kinds, count)
    return dict(sorted(zip(chosen, drawn, strict=True)))


def issue_lots(batteries=0, tag=None):
    """{issue kind: lot} for the lot-level issue kinds this series uses, in the order of ISSUES (lot IDs without
    the tag)."""
    used = set(issue_plan(batteries, tag).values())
    return {kind: issue[0] for kind, issue in ISSUES.items() if issue and kind in used}


def battery_plan(batteries=0, tag=None, clean=False):
    """{number: (plant, batch, {(material, lot): kg})}: the story, then added batteries spread over the plants, with
    the series' seeded data issues unless clean is set."""
    plan, issues = {}, {} if clean else issue_plan(batteries, tag)
    for index, number in enumerate(battery_numbers(batteries)):
        kind = issues.get(number)
        if number in STORY_BATCHES:
            batch = STORY_BATCHES[number]
            plant = "PLANT-" + batch.split("-")[1]
        else:
            plant = PLANTS[(index - len(STORY)) % len(PLANTS)]
            batch = UNMETERED_BATCHES[plant] if kind == "unmetered_batch" else EXTRA_BATCHES[plant]
        lots = dict(DEFAULT_LOTS)
        if number in ("001", "002"):
            lots["lithium"] = "LOT-LI-A"
        if number == "004":
            lots["cobalt"] = "LOT-CO-B"
        if kind and ISSUES[kind]:
            lot, _, material, _, _ = ISSUES[kind]
            lots[material] = lot
        parts = {(material, lots[material]): float(kg) for material, kg in USE_KG.items()}
        if number == "005":
            for material, recycled in (("lithium", "LOT-LI-R"), ("nickel", "LOT-NI-R"), ("cobalt", "LOT-CO-R")):
                half = USE_KG[material] / 2
                parts[(material, lots[material])] = half
                parts[(material, recycled)] = half
        plan[number] = (plant, batch, parts)
    return plan


def lot_specs(tag=None, factors=None, batteries=0):
    """{lot: claim fields} with an illustrative PCF per kg: every lot of LOTS, then the issue lots the series uses.
    Lot masses of LOTS cover the largest clean series, so added batteries leave the lots, and with them the inputs
    of the other batteries, unchanged. An issue lot covers its batteries with the usual margin, except the overdrawn
    lot, which holds only OVERDRAWN_SHARE of their use."""
    factors = factors or load_factors()
    used = {}
    for _, _, parts in battery_plan(MAX_BATTERIES, clean=True).values():
        for (_, lot), kg in parts.items():
            used[lot] = used.get(lot, 0) + kg
    issue_used = {}
    for _, _, parts in battery_plan(batteries, tag).values():
        for (_, lot), kg in parts.items():
            issue_used[lot] = issue_used.get(lot, 0) + kg
    entries = [(lot, supplier, material, site, recycled, None) for lot, (supplier, material, site, recycled)
               in LOTS.items()]
    entries += [(ISSUES[kind][0], ISSUES[kind][1], ISSUES[kind][2], ISSUES[kind][3], 0, kind)
                for kind in issue_lots(batteries, tag)]
    specs = {}
    for lot, supplier, material, site, recycled, kind in entries:
        if lot == "LOT-LI-A":
            mass = LOT_LI_A_KG
        elif kind:
            mass = math.ceil(issue_used[lot] * (OVERDRAWN_SHARE if kind == "overdrawn_lot" else LOT_MARGIN))
        else:
            mass = math.ceil(used.get(lot, 1) * LOT_MARGIN)
        material_factors = factors["materials"][material]
        pcf = ((100 - recycled) * material_factors["primary_kgco2e_per_kg"]
               + recycled * material_factors["recycled_kgco2e_per_kg"]) / 100
        specs[lot] = {"lot_id": suffixed(lot, tag), "supplier_id": supplier, "material": material, "mass_kg": mass,
                      "origin_site": site, "region": party(supplier).region, "pcf_kgco2e_per_kg": round(pcf, 3),
                      "recycled_share_pct": recycled,
                      "na_reasons": {} if site else {"origin_site": "manufactured_component"}}
    return specs


def claim_payloads(tag=None, factors=None, batteries=0):
    """{lot: claim payload} for every lot of the series: version 1 as delivered with the lot records."""
    return {lot: signed_messages.claim_payload(spec, 1, CLAIM_ISSUED.get(lot, DEFAULT_CLAIM_ISSUED))
            for lot, spec in lot_specs(tag, factors, batteries).items()}


def transport_rows(batteries=0, tag=None):
    """[(record, lot, loading site, destination plant, shipped, delivered)]: the story's transport records, then one
    per mismatch issue lot the series uses, delivered to the plant of its first battery."""
    plan, rows = battery_plan(batteries, tag), list(TRANSPORT)
    for kind, lot in issue_lots(batteries, tag).items():
        if lot in ISSUE_TRANSPORT:
            record, shipped, delivered = ISSUE_TRANSPORT[lot]
            plant = next(plant for plant, _, parts in plan.values() if any(key[1] == lot for key in parts))
            rows.append((record, lot, ISSUES[kind][4], plant, shipped, delivered))
    return rows


def corrected_lots(batteries=0, tag=None):
    """The lots whose supplier sends a corrected claim (version 2): LOT-LI-A, then the mismatch_corrected lot if the
    series uses it (lot IDs without the tag)."""
    return ["LOT-LI-A"] + [lot for kind, lot in issue_lots(batteries, tag).items() if kind == "mismatch_corrected"]


def corrected_claim_payload(tag=None, factors=None, lot="LOT-LI-A", batteries=0):
    """A lot's claim version 2, which names the mine its transport record names."""
    site = next(row[2] for row in transport_rows(batteries, tag) if row[1] == lot)
    spec = {**lot_specs(tag, factors, batteries)[lot], "origin_site": site}
    return signed_messages.claim_payload(spec, 2, CORRECTED_ISSUED)


def _units(quantity_kg, unit):
    return {"g": round(quantity_kg * 1000, 6), "kg": quantity_kg, "t": round(quantity_kg / 1000, 6)}[unit]


def story_records(batteries=0, tag=None, factors=None):
    """{party_id: {kind: {file name: records}}} for the default story plus N added batteries spread over the
    plants, with the series' seeded data issues. Each lot record carries the hash of its claim payload, not the
    signed message."""
    check_options(batteries, tag)
    factors = factors or load_factors()

    def s(value):
        return suffixed(value, tag)

    plan, specs = battery_plan(batteries, tag), lot_specs(tag, factors, batteries)
    claims = claim_payloads(tag, factors, batteries)
    records = {item.party_id: {} for item in PARTIES}
    for lot, spec in specs.items():
        supplier = party(spec["supplier_id"])
        record = {"entity": "lot", "key": spec["lot_id"], "version": 1, "supplier_id": spec["supplier_id"],
                  "material": spec["material"], "quantity": _units(spec["mass_kg"], supplier.unit),
                  "unit": supplier.unit, "region": spec["region"], "recycled_share_pct": spec["recycled_share_pct"],
                  "pcf_kgco2e_per_kg": spec["pcf_kgco2e_per_kg"], "claim_id": claims[lot]["claim_id"],
                  "claim_sha256": signed_messages.payload_sha256(claims[lot]), "party": spec["supplier_id"]}
        if spec["origin_site"]:
            record["origin_site"] = spec["origin_site"]
        else:
            record["na_reasons"] = spec["na_reasons"]
        lots = records[supplier.party_id].setdefault("lots", {}).setdefault(f"lots.{supplier.format}", [])
        lots.append(record)
    records["CARRIER-FABRIKAM"]["transport"] = {"transport.json": [
        {"entity": "transport", "key": s(key), "version": 1, "lot_id": s(lot), "origin_site": site,
         "destination_plant_id": plant, "shipped_utc": shipped, "delivered_utc": delivered,
         "party": "CARRIER-FABRIKAM"} for key, lot, site, plant, shipped, delivered in transport_rows(batteries, tag)]}

    factor_set = factors["factor_set"]["factor_set_version"]
    for plant in PLANTS:
        numbers = [number for number, (where, _, _) in plan.items() if where == plant]
        batches = sorted({plan[number][1] for number in numbers})
        builds = [{"entity": "series", "key": s("SERIES-MAIN"), "version": 1,
                   "battery_ids": [s(f"B-{n}") for n in plan], "party": plant}] if plant == PLANTS[0] else []
        builds += [{"entity": "battery", "key": s(f"B-{n}"), "version": 1, "batch_id": s(plan[n][1]),
                    "model_id": MODEL_ID, "plant_id": plant, "chemistry": CHEMISTRY, "rated_kwh": RATED_KWH,
                    "rated_capacity_ah": RATED_AH, "expected_cycles": EXPECTED_CYCLES, "party": plant}
                   for n in numbers]
        consumption = [{"entity": "consumption", "key": s(f"USE-{n}-{lot.removeprefix('LOT-')}"), "version": 1,
                        "battery_id": s(f"B-{n}"), "lot_id": s(lot), "quantity": kg, "unit": "kg", "party": plant}
                       for n in numbers for (_, lot), kg in sorted(plan[n][2].items(), key=lambda item: item[0][1])]
        energy = []
        # An unmetered batch has a batch record but no energy reading, so the pipeline blocks its batteries.
        metered = [batch for batch in batches if batch not in UNMETERED_BATCHES.values()]
        for index, batch in enumerate(metered):
            count = sum(1 for n in numbers if plan[n][1] == batch)
            kwh = KWH_PER_BATTERY[plant] * count
            energy.append({"entity": "batch_energy", "key": s(f"ENERGY-{batch.removeprefix('BATCH-')}"),
                           "version": 1, "batch_id": s(batch),
                           "quantity": kwh / 1000 if ENERGY_UNIT[plant] == "MWh" else kwh,
                           "unit": ENERGY_UNIT[plant], "interval_start": f"2026-10-1{8 + index}T06:00:00Z",
                           "interval_end": f"2026-10-1{8 + index}T22:00:00Z", "party": plant})
        records[plant] = {
            "batches": {"batches.json": [{"entity": "batch", "key": s(batch), "version": 1, "plant_id": plant,
                                          "method_version": METHOD_VERSION, "factor_set_version": factor_set,
                                          "party": plant} for batch in batches]},
            "builds": {"builds.json": builds}, "consumption": {"consumption.json": consumption},
            "energy": {"energy.json": energy}}
    return records


def delivery_id(name, sequence, party_id, kind):
    return f"DLV-{name}-{sequence}-{party_id}-{kind.upper()}"


def make_deliveries(name, sequence, batteries=0, tag=None, factors=None):
    """[(party_id, delivery_id, {file name: bytes})] in party order; each folder lists delivery.json last."""
    profiles, documents = registry(), story_records(batteries, tag, factors)
    return [(item.party_id, delivery_id(name, sequence, item.party_id, kind),
             make_delivery(delivery_id(name, sequence, item.party_id, kind), item.party_id,
                           profiles[item.party_id], kind, documents[item.party_id][kind], CREATED_UTC))
            for item in PARTIES for kind in item.kinds]


def event_payloads(tag=None, factors=None):
    """The partner lifecycle events of the story, in time order: B-003 goes to a second life as B-003-SL1 and
    B-005 reaches its end of life and is recycled."""
    check_options(0, tag)
    factors = factors or load_factors()
    b3, b5 = suffixed("B-003", tag), suffixed("B-005", tag)
    oem, operator, recycler = "OEM-ADVENTUREWORKS", "SECONDLIFE-RELECLOUD", "RECYCLER-TREY"
    second_life = factors["second_life"]
    event = signed_messages.event_payload
    return [
        event(oem, "record_soh", b3, "2026-10-28T09:00:00Z", soh_pct=SOH_PCT["003"],
              measured_utc="2026-10-28T08:30:00Z"),
        event(oem, "record_ownership_change", b3, "2026-10-30T10:00:00Z", from_party_id=oem,
              to_party_id=operator, effective_utc="2026-10-30T10:00:00Z"),
        event(operator, "request_repurpose", b3, "2026-11-02T09:00:00Z",
              successor_battery_id=suffixed("B-003-SL1", tag), intended_use="stationary energy storage",
              safety_assessment_ref=suffixed("SAFETY-B-003-01", tag),
              illustrative_value=round(RATED_KWH * second_life["value_eur_per_rated_kwh"], 2),
              value_currency=second_life["currency"], value_label="illustrative, not a sale price"),
        event(oem, "record_soh", b5, "2026-11-03T09:00:00Z", soh_pct=SOH_PCT["005"],
              measured_utc="2026-11-03T08:30:00Z"),
        event(oem, "record_end_of_life", b5, "2026-11-04T09:00:00Z", reason="soh_below_second_life_minimum",
              handed_to_party_id=recycler),
        event(recycler, "record_recycling_closure", b5, "2026-11-06T09:00:00Z",
              recovered_kg={"cobalt": 4.0, "lithium": 8.5, "nickel": 34.0}, recovery_label="illustrative"),
    ]


def signed_message_set(keys, tag=None, factors=None, batteries=0):
    """{file name: envelope}: every lot claim, the corrected claims and every partner event, each signed by its
    issuer."""
    payloads = list(claim_payloads(tag, factors, batteries).values())
    payloads += [corrected_claim_payload(tag, factors, lot, batteries) for lot in corrected_lots(batteries, tag)]
    payloads += event_payloads(tag, factors)
    return {f"{payload['message_id']}.json": signed_messages.sign(payload, keys[payload["issuer"]])
            for payload in payloads}


def signing_keys(output):
    """One Ed25519 key per lot supplier, recycler and lifecycle partner, reused from <output>/keys."""
    folder = Path(output) / "keys"
    return {item.party_id: signed_messages.party_key(item.party_id, folder)
            for item in PARTIES if "lots" in item.kinds or item.events}


def party_registry(keys, tag=None):
    """The registry lists every lot a supplier may claim, including every issue lot, so one deployed registry
    serves a series of any size."""
    lots = {}
    entries = [(lot, supplier) for lot, (supplier, *_) in LOTS.items()]
    entries += [(issue[0], issue[1]) for issue in ISSUES.values() if issue]
    for lot, supplier in entries:
        lots.setdefault(supplier, []).append(suffixed(lot, tag))
    return signed_messages.party_registry(PARTIES, keys, lots)


def evidence_documents(tag=None, batteries=0):
    """The Reviewer's evidence for the lithium lots (lot A's claim v1 names a different mine from its transport
    record) and the adverse report about the mine of cobalt lot LOT-CO-B with the audit report that answers it,
    then the evidence of the issue lots the series uses."""
    check_options(batteries, tag)
    specs = lot_specs(tag, None, batteries)
    rows = {row[1]: row for row in transport_rows(batteries, tag)}

    def s(value):
        return suffixed(value, tag)

    def claim(lot, version, site, issued):
        spec = specs[lot]
        return {"document_type": "supplier_sourcing_claim", "data_label": LABEL, "claim_id": s(f"CLAIM-{lot}"),
                "claim_version": version, "supplier_id": spec["supplier_id"],
                "supplier_name": party(spec["supplier_id"]).name, "lot_id": s(lot), "material": spec["material"],
                "quantity_kg": spec["mass_kg"], "origin_site": site, "issued_utc": issued,
                "signature_note": SIGNATURE_NOTE}

    def corrected(lot):
        document = {key: value for key, value in claim(lot, 2, rows[lot][2], CORRECTED_ISSUED).items()
                    if key != "signature_note"}
        document.update({"replaces_claim_version": 1,
                         "correction_reason": "Origin site was entered incorrectly in version 1.",
                         "signature_note": SIGNATURE_NOTE})
        return document

    def record(key, lot, site, plant, shipped, delivered):
        return {"document_type": "transport_record", "data_label": LABEL, "record_id": s(key), "lot_id": s(lot),
                "material": specs[lot]["material"], "quantity_kg": specs[lot]["mass_kg"],
                "carrier": party("CARRIER-FABRIKAM").name, "loaded_at_site": site, "loaded_utc": shipped,
                "delivered_to": plant, "delivered_utc": delivered}

    documents = {"lot-li-a-supplier-claim-v1.json": claim("LOT-LI-A", 1, "Mine North", CLAIM_ISSUED["LOT-LI-A"]),
                 "lot-li-a-supplier-claim-v2.json": corrected("LOT-LI-A"),
                 "lot-li-a-transport-record.json": record(*rows["LOT-LI-A"]),
                 "lot-li-b-supplier-claim-v1.json": claim("LOT-LI-B", 1, "Mine East", CLAIM_ISSUED["LOT-LI-B"]),
                 "lot-li-b-transport-record.json": record(*rows["LOT-LI-B"]),
                 ADVERSE_REPORT: adverse_report(tag), AUDIT_REPORT: audit_report(tag)}
    for kind, lot in issue_lots(batteries, tag).items():
        site, name = ISSUES[kind][3], lot.lower()
        if kind.startswith("mismatch_"):
            documents[f"{name}-supplier-claim-v1.json"] = claim(lot, 1, site, DEFAULT_CLAIM_ISSUED)
            if kind == "mismatch_corrected":
                documents[f"{name}-supplier-claim-v2.json"] = corrected(lot)
            documents[f"{name}-transport-record.json"] = record(*rows[lot])
        elif kind.startswith("adverse_"):
            documents[f"{name}-adverse-report.json"] = adverse_report(tag, lot, site)
            if kind != "adverse_open":
                documents[f"{name}-audit-report.json"] = audit_report(tag, lot, site)
    return documents


def _report_id(prefix, site, tag):
    return suffixed(f"{prefix}-{site.upper().replace(' ', '-')}-2026-01", tag)


def audit_report(tag=None, lot="LOT-CO-B", site="Mine Ridge"):
    """A synthetic third-party site audit that answers the adverse report with findings and an action plan."""
    return {
        "document_type": "site_audit_report", "data_label": LABEL,
        "audit_id": _report_id("AUD", site, tag),
        "auditor": "Fabrikam Assurance (fictional auditor)", "audit_window": ["2026-10-20", "2026-10-24"],
        "issued_utc": "2026-10-27T09:00:00Z", "subject_site": site, "material": "cobalt",
        "related_supplier_id": "SUP-PROSEWARE", "related_lot_ids": [suffixed(lot, tag)],
        "responds_to": _report_id("ADV", site, tag),
        "findings": [
            {"topic": "child_labour", "result": "not_observed",
             "summary": "No minors were found on site during the audit; age checks at the gate were incomplete."},
            {"topic": "unsafe_working_conditions", "result": "partly_confirmed",
             "summary": "One shaft lacked wall supports; protective equipment was issued during the audit."}],
        "corrective_actions": [
            {"action": "Introduce documented age verification at every pit entrance.", "due_date": "2027-01-31"},
            {"action": "Install wall supports in the remaining shaft and re-inspect.", "due_date": "2027-01-31"}],
        "conclusion": "Supply may continue under the corrective action plan; re-audit within six months.",
        "note": "Illustrative audit for a synthetic scenario; it names no real place, company or person.",
    }


def adverse_report(tag=None, lot="LOT-CO-B", site="Mine Ridge"):
    """A synthetic NGO-style report about the fictional mine that supplied a cobalt lot (LOT-CO-B in the story)."""
    return {
        "document_type": "adverse_media_report", "data_label": LABEL,
        "report_id": _report_id("ADV", site, tag),
        "publisher": "Fourth Coffee Supply Watch (fictional NGO)", "published_utc": "2026-10-15T07:00:00Z",
        "subject_site": site, "subject_region": "Africa", "material": "cobalt",
        "related_supplier_id": "SUP-PROSEWARE", "related_lot_ids": [suffixed(lot, tag)],
        "allegations": [
            {"topic": "child_labour", "summary": "Interviewees describe minors carrying ore at the mine's artisanal "
                                                 "pits during school terms."},
            {"topic": "unsafe_working_conditions", "summary": "Unsupported pit walls and no protective equipment "
                                                              "were reported in two shafts."}],
        "evidence_basis": "Field interviews and photographs (not attached); not independently verified.",
        "requested_action": "Buyers should ask the supplier for its due-diligence findings for the site.",
        "note": "Fictional report for a synthetic scenario; it names no real place, company or person.",
    }


def evidence_bytes(document):
    return (json.dumps(document, indent=2) + "\n").encode("utf-8")


def evidence_uploads(tag=None, batteries=0):
    """[(file, upload kind, lot_id)] for this series, uploaded at the start: the transport records and the adverse
    report of the story, then each mismatch lot's transport record and each adverse lot's report; an answered
    adverse report is followed by its audit report."""
    uploads = list(EVIDENCE_UPLOADS)
    for kind, lot in issue_lots(batteries, tag).items():
        name = lot.lower()
        if kind.startswith("mismatch_"):
            uploads.append((f"{name}-transport-record.json", "transport_record", lot))
        elif kind.startswith("adverse_"):
            uploads.append((f"{name}-adverse-report.json", "adverse_report", lot))
            if kind == "adverse_answered":
                uploads.append((f"{name}-audit-report.json", "audit_report", lot))
    return [(name, kind, suffixed(lot, tag)) for name, kind, lot in uploads]


def audit_upload(tag=None):
    """(file, upload kind, lot_id) of the story's audit report, which Compliance uploads during the review act."""
    name, kind, lot = AUDIT_UPLOAD
    return name, kind, suffixed(lot, tag)


def audit_uploads(tag=None, batteries=0):
    """[(file, upload kind, lot_id)] of the audit reports Compliance uploads during the review act: the story's,
    then the adverse_audited lot's if the series uses it."""
    lots = [lot for kind, lot in issue_lots(batteries, tag).items() if kind == "adverse_audited"]
    return [audit_upload(tag)] + [(f"{lot.lower()}-audit-report.json", "audit_report", suffixed(lot, tag))
                                  for lot in lots]


def critical_lots(tag=None, batteries=0):
    """The lots of critical raw materials that get a Supplier task, LOT-LI-A first, in the order of LOTS, then the
    series' issue lots apart from the overdrawn one, which the pipeline excludes."""
    lots = [lot for lot, (_, material, _, _) in LOTS.items() if material in CRITICAL_MATERIALS]
    lots += [lot for kind, lot in issue_lots(batteries, tag).items() if kind not in BLOCKED_KINDS]
    return [suffixed(lot, tag) for lot in lots]


def claim_message_id(lot_id, version=1):
    return f"MSG-CLAIM-{lot_id}-V{version}"


def opening_messages(tag=None, batteries=0):
    """The message IDs delivered after the package import: every lot's claim version 1."""
    return [claim_message_id(suffixed(lot, tag)) for lot in lot_specs(tag, None, batteries)]


def correction_message(tag=None):
    return claim_message_id(suffixed("LOT-LI-A", tag), 2)


def correction_messages(tag=None, batteries=0):
    """{lot_id: message ID} of the corrected claims a supplier sends during the review act."""
    return {suffixed(lot, tag): claim_message_id(suffixed(lot, tag), 2) for lot in corrected_lots(batteries, tag)}


def soh_message(tag=None):
    return f"MSG-RECORD-SOH-{suffixed('B-003', tag)}"


def connector_agreements(registry_document):
    """One mock connector agreement per registered party for the message types it may sign. The command ID follows
    from the content, so recording the same agreements again replays."""
    agreements = []
    for item in registry_document["parties"]:
        types = (["supplier_claim"] if item["lot_ids"] else []) + list(item["event_types"])
        if not types:
            continue
        body = {"agreement_id": f"AGR-{item['party_id']}", "sender": item["party_id"],
                "purpose": AGREEMENT_PURPOSE, "message_types": types,
                "valid_from": AGREEMENT_VALIDITY[0], "valid_until": AGREEMENT_VALIDITY[1]}
        body["command_id"] = "agr-" + digest(json.dumps(body, sort_keys=True).encode("utf-8"))[:40]
        agreements.append(body)
    return agreements


def estimate(batteries=0, tag=None):
    """[(agent, act, model calls)] for one pass of the scenario with this series size and its seeded issues: the
    pipeline blocks some added batteries before any agent, issue lots add Supplier tasks, and only the released
    issues come back in the complete act."""
    issues = issue_plan(batteries, tag)
    kinds, lots = Counter(issues.values()), issue_lots(batteries, tag)
    blocked = sum(kinds[kind] for kind in BLOCKED_KINDS)
    held = sum(kinds[kind] for kind in RELEASED_KINDS + OPEN_KINDS)
    extra = {("supplier", "prepare"): sum(1 for kind in lots if kind not in BLOCKED_KINDS),
             ("carbon", "prepare"): batteries - blocked,
             ("issuance", "prepare"): batteries - blocked - held,
             ("supplier", "complete"): sum(1 for kind in lots if kind in RELEASED_KINDS),
             ("issuance", "complete"): sum(kinds[kind] for kind in RELEASED_KINDS)}
    return [(agent, act, base + extra.get((agent, act), 0)) for agent, act, base in CALLS]


def estimate_lines(batteries=0, tag=None):
    rows = estimate(batteries, tag)
    acts = {}
    for _, act, calls in rows:
        acts[act] = acts.get(act, 0) + calls
    lines = [f"Estimated agent model calls for {len(STORY) + batteries} batteries (cost depends on your model "
             "prices):"]
    lines += [f"  {agent:<10} {act:<10} {calls:>4}" for agent, act, calls in rows]
    lines.append("  per act: " + ", ".join(f"{act} {calls}" for act, calls in acts.items())
                 + f"; total {sum(calls for _, _, calls in rows)}")
    kinds = Counter(issue_plan(batteries, tag).values())
    if kinds:
        lines.append(f"Seeded data issues in {sum(kinds.values())} of {batteries} added batteries: "
                     + ", ".join(f"{kind} {kinds[kind]}" for kind in ISSUES if kinds[kind]))
    return lines


def series_name(tag=None):
    return tag or MAIN


def series_folder(output, name):
    return Path(output) / "series" / name


def state_path(output, name):
    return Path(output) / "series" / f"{name}.json"


def load_state(output=OUTPUT, tag=None):
    path = state_path(output, series_name(tag))
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def build_paths(output, state):
    """Where a prepared build's files are: ingest (mirrors the lakehouse Files folder), the package, evidence,
    signed messages and the party registry."""
    series = series_folder(output, state["name"])
    build = series / state["build_id"]
    return {"ingest": build / "ingest", "package": build / "package",
            "evidence": series / "evidence", "messages": series / "messages",
            "registry": series / "party-registry.json"}


def _write(folder, files):
    folder.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (folder / name).write_bytes(data)


def prepare(batteries=0, tag=None, output=OUTPUT, now=time.time, new_build=False, min_sequence=None):
    """Write a series' deliveries, packages, signed messages and evidence. The same series and size reuse the build
    unless new_build is set or the app has seen min_sequence; a new build gets a later sequence than every earlier
    one."""
    check_options(batteries, tag)
    factors = load_factors()
    name = series_name(tag)
    previous = load_state(output, tag)
    reuse = (previous is not None and previous["batteries"] == batteries and not new_build
             and (min_sequence is None or previous["build_sequence"] >= min_sequence))
    if reuse:
        sequence, completed = previous["build_sequence"], previous["completed_utc"]
    else:
        floor = max(previous["build_sequence"] + 1 if previous else 1, min_sequence or 1)
        moment = now()
        sequence = max(int(moment // 60), floor)
        completed = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(moment))
    deliveries = make_deliveries(name, sequence, batteries, tag, factors)
    build_id = build_identity(name, [digest(files["delivery.json"]) for _, _, files in deliveries])
    package = local_package(deliveries, registry(), build_id, sequence, code_hash(), completed, factors)
    errors = package_errors(package)
    if errors:
        raise ValueError("The local package failed its check: " + "; ".join(errors))

    keys = signing_keys(output)
    state = {"name": name, "tag": tag, "batteries": batteries, "build_id": build_id, "build_sequence": sequence,
             "completed_utc": completed, "created_utc": CREATED_UTC,
             "delivery_set": ",".join(f"{party_id}/{delivery}" for party_id, delivery, _ in deliveries),
             "manifest_sha256": digest(package["manifest.json"]),
             "input_hashes": {row["battery_id"]: row["input_hash"]
                              for row in map(json.loads, package["batteries.jsonl"].decode("utf-8").splitlines())},
             "issues": issue_plan(batteries, tag)}
    paths = build_paths(output, state)
    for party_id, delivery, files in deliveries:
        _write(paths["ingest"] / "deliveries" / party_id / delivery, files)
    _write(paths["package"], package)
    _write(paths["evidence"], {name: evidence_bytes(doc) for name, doc in evidence_documents(tag, batteries).items()})
    _write(paths["messages"], {name: evidence_bytes(envelope)
                               for name, envelope in signed_message_set(keys, tag, factors, batteries).items()})
    paths["registry"].write_bytes(evidence_bytes(party_registry(keys, tag)))
    path = state_path(output, name)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return state


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m data.simulator", description=__doc__.split("\n\n")[0])
    parser.add_argument("--batteries", type=int, default=0,
                        help=f"batteries to add (0-{MAX_BATTERIES}); about a fifth carry a seeded data issue")
    parser.add_argument("--tag", help="a separate copy: suffix every record ID with this tag")
    parser.add_argument("--new-build", action="store_true", help="start a new build even if the series exists")
    parser.add_argument("--output", default=str(OUTPUT), help="output folder (default: output/)")
    args = parser.parse_args(argv)
    try:
        for line in estimate_lines(args.batteries, args.tag):
            print(line)
        state = prepare(args.batteries, args.tag, Path(args.output), new_build=args.new_build)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    paths = build_paths(Path(args.output), state)
    print(f"{state['build_id']}: {len(state['delivery_set'].split(','))} deliveries in {paths['ingest']}")
    print(f"Local package: {paths['package']} (checked)")
    print(f"Signed messages: {paths['messages']}; party registry: {paths['registry']}")
    print(f"Evidence: {paths['evidence']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
