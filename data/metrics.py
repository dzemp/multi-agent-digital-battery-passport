"""The four success measures, defined once (synthetic data; footprints and values are illustrative).

Pure functions over plain dict rows: report rows (report.v_passport_status) and lot rows
(report.v_lot_claim_status). Standard library only and no relative imports: the app carries an identical copy
(app/passport/metrics.py, checked by a test) and the reporting flows can call the same functions.

- Passport coverage: produced batteries with a published passport / batteries produced. A successor passport
  from a second life is not a produced battery and counts in neither.
- Verified footprint: published produced batteries whose footprint was calculated with every critical-lot claim
  verified (footprint_verified = 1), over published produced batteries, with their average kg CO2e per kWh.
- Supplier data completeness: lots with every required field present and a verified signed claim / all lots.
- Second life: batteries repurposed into a successor and the sum of their illustrative values in EUR.
"""

LABELS = {"data": "synthetic", "footprint": "illustrative", "value": "illustrative"}


def pct(part, whole):
    """Percentage rounded to one decimal; None when there is nothing to measure."""
    return round(100.0 * part / whole, 1) if whole else None


def _number(value):
    return None if value is None else float(value)


def successor_ids(rows):
    return {r["successor_id"] for r in rows if r.get("successor_id")}


def produced(rows):
    """Report rows of produced batteries: every row except successor passports."""
    successors = successor_ids(rows)
    return [r for r in rows if r["battery_id"] not in successors]


def published(row):
    return bool(row.get("committed_version"))


def verified(row):
    return published(row) and bool(row.get("footprint_verified")) and row.get("footprint_per_kwh") is not None


def coverage(rows):
    made = produced(rows)
    done = sum(1 for r in made if published(r))
    return {"produced": len(made), "published": done, "coverage_pct": pct(done, len(made))}


def verified_footprint(rows):
    made = [r for r in produced(rows) if published(r)]
    good = [_number(r["footprint_per_kwh"]) for r in made if verified(r)]
    return {"published": len(made), "verified": len(good), "verified_pct": pct(len(good), len(made)),
            "avg_kg_co2e_per_kwh": round(sum(good) / len(good), 2) if good else None}


def lot_complete(lot):
    return bool(lot.get("fields_complete")) and lot.get("claim_status") == "verified"


def supplier_completeness(lot_rows):
    groups = {}
    for lot in lot_rows:
        groups.setdefault((lot.get("supplier_id") or "unknown", lot["material"]), []).append(lot)
    by_supplier = [{"supplier_id": supplier, "material": material, "lots": len(lots),
                    "complete": sum(1 for x in lots if lot_complete(x)),
                    "complete_pct": pct(sum(1 for x in lots if lot_complete(x)), len(lots))}
                   for (supplier, material), lots in sorted(groups.items())]
    complete = sum(1 for x in lot_rows if lot_complete(x))
    return {"lots": len(lot_rows), "complete": complete, "complete_pct": pct(complete, len(lot_rows)),
            "by_supplier": by_supplier}


def second_life(rows):
    repurposed = [r for r in rows if r.get("successor_id")]
    values = [_number(r.get("second_life_value_eur_illustrative")) for r in repurposed]
    return {"repurposed": len(repurposed), "value_eur_illustrative": sum(v for v in values if v is not None)}


def per_plant(rows):
    plants = {}
    for row in produced(rows):
        plants.setdefault(row.get("plant_id") or "unknown", []).append(row)
    result = []
    for plant_id, members in sorted(plants.items()):
        cover, footprint = coverage(members), verified_footprint(members)
        result.append({"plant_id": plant_id, "produced": cover["produced"], "published": cover["published"],
                       "coverage_pct": cover["coverage_pct"], "verified_footprint_batteries": footprint["verified"],
                       "avg_footprint_per_kwh": footprint["avg_kg_co2e_per_kwh"]})
    return result


def measures(rows, lot_rows):
    """All four measures, overall and per plant."""
    return {"coverage": coverage(rows), "verified_footprint": verified_footprint(rows),
            "supplier_completeness": supplier_completeness(lot_rows), "second_life": second_life(rows),
            "plants": per_plant(rows), "labels": dict(LABELS)}
