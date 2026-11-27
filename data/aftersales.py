"""Synthetic OEM service, BMS and quality deliveries, independent of production package generation."""

from . import simulator


def make_source(state):
    """A complete aftersales input snapshot for the prepared series, including late and replayed deliveries."""
    tag = state["tag"]
    plan = simulator.battery_plan(state["batteries"], tag)
    def s(text):
        return simulator.suffixed(text, tag)

    catalog = {s(f"B-{n}"): s(batch) for n, (_, batch, _) in plan.items()}
    packs, readings, late = [], [], []
    for index, number in enumerate(plan):
        pack = s(f"B-{number}")
        aw = number in ("001", "003", "005") if index < 5 else index % 2 == 0
        profile = {"001": 0, "002": 1, "003": 2, "004": 3, "005": 4}.get(number, index % 6)
        unknown = profile == 3
        terms = ("SERVICE-EFC-V1", 24, 100)
        if index >= 5 and profile in (0, 1):
            terms = ("SERVICE-SHORT-V1", 1, 80) if profile == 0 else ("SERVICE-EXTENDED-V2", 36, 140)
        packs.append({"battery_id": pack, "oem_id": "OEM-ADVENTUREWORKS" if aw else "OEM-FABRIKAM",
                      "batch_id": catalog[pack], "in_service_date": None if unknown else "2026-10-21",
                      "term_version": None if unknown else terms[0], "warranty_months": None if unknown else terms[1],
                      "warranty_cycles": None if unknown else terms[2]})
        if unknown:
            continue
        cycles = (26, 60, 97, None, 106, 84)[profile]
        health = (99, 96, 88, None, 62, None)[profile]
        latest = "2026-11-03T08:30:00Z" if number == "005" else "2026-10-28T08:30:00Z"
        reading = {"reading_id": s(f"READ-{number}-02"), "battery_id": pack, "measured_utc": latest,
                   "actual_cycles": cycles, "cycle_basis": "equivalent_full_cycles", "soh_pct": health}
        readings.append(reading)
        if profile != 1:  # Some packs intentionally have just one reading; no inferred trend.
            late.append({**reading, "reading_id": s(f"READ-{number}-01"), "measured_utc": "2026-10-24T08:30:00Z",
                         "actual_cycles": cycles - 9, "soh_pct": None if health is None else min(100, health + 3)})
    campaigns = [
        {"campaign_id": s("RECALL-01"), "kind": "recall", "status": "open", "effective_date": "2026-11-01",
         "batch_ids": [catalog[s("B-001")], catalog[s("B-003")]], "battery_ids": [s("B-003")]},
        {"campaign_id": s("SERVICE-01"), "kind": "service_campaign", "status": "open",
         "effective_date": "2026-11-02", "batch_ids": [], "battery_ids": [s("B-005")] + list(catalog)[5::4]},
        {"campaign_id": s("RECALL-00"), "kind": "recall", "status": "closed", "effective_date": "2026-10-25",
         "batch_ids": [catalog[s("B-004")]], "battery_ids": []}]
    def shipment(name, owner, kind, received, rows):
        return {"source_delivery_id": s(name), "source_system": owner, "entity": kind,
                "received_utc": received, "rows": rows}

    current = shipment("BMS-02", "synthetic-bms", "readings", "2026-11-04T09:00:00Z", readings)
    # A repeated source delivery plus an older measurement arriving later. Distinct physical receipts are retained.
    deliveries = [shipment("TERMS-01", "synthetic-oem-service", "packs", "2026-10-24T09:00:00Z", packs),
                  current, {**current, "received_utc": "2026-11-05T09:00:00Z"},
                  shipment("BMS-01", "synthetic-bms", "readings", "2026-11-06T09:00:00Z", late),
                  shipment("QUALITY-01", "synthetic-oem-quality", "campaigns", "2026-11-07T09:00:00Z", campaigns)]
    return {"schema": "dbp-aftersales-source-1", "series_id": s("SERIES-MAIN"), "build_id": state["build_id"],
            "as_of_utc": max("2026-11-08T12:00:00Z", state["completed_utc"]),
            "production_catalog": catalog, "deliveries": deliveries}
