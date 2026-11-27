"""Independent arithmetic check for the illustrative Carbon calculation."""

from decimal import ROUND_HALF_UP, Decimal

from . import calculator, domain

KG = Decimal("0.001")
PER_KWH = Decimal("0.0001")


def verify(inputs, result):
    """Recalculate from pinned inputs without calling calculator.calculate."""
    battery, batch = inputs["battery"], inputs["batch"]
    factors = calculator.FACTOR_SETS.get(batch["factor_set_version"])
    if not factors or batch["method_version"] != calculator.METHOD:
        raise ValueError("The method or factor set is not available to the verifier.")
    grid = factors["grid"].get(battery["plant_id"])
    if not grid or grid["grid_factor_id"] != battery["grid_factor_id"]:
        raise ValueError("The pinned grid factor does not match the battery.")

    materials = Decimal(0)
    for part in sorted(inputs["composition"], key=lambda item: item["lot_id"]):
        factors_for_material = factors["materials"].get(part["material"])
        if not factors_for_material:
            raise ValueError(f"No verifier factor exists for {part['material']}.")
        recycled = Decimal(str(inputs["lots"][part["lot_id"]]["recycled_share_pct"])) / 100
        primary = Decimal(str(factors_for_material["primary_kgco2e_per_kg"]))
        recovered = Decimal(str(factors_for_material["recycled_kgco2e_per_kg"]))
        materials += Decimal(str(part["mass_kg"])) * ((1 - recycled) * primary + recycled * recovered)

    electricity = Decimal(str(battery["allocated_kwh"])) * Decimal(str(grid["kgco2e_per_kwh"]))
    expected = {
        "method": calculator.METHOD,
        "factor_set_version": batch["factor_set_version"],
        "input_hash": battery["input_hash"],
        "materials_kg_co2e": float(materials.quantize(KG, ROUND_HALF_UP)),
        "electricity_kg_co2e": float(electricity.quantize(KG, ROUND_HALF_UP)),
        "kg_co2e": float((materials + electricity).quantize(KG, ROUND_HALF_UP)),
        "kg_co2e_per_kwh": float(((materials + electricity) / Decimal(str(battery["rated_kwh"]))
                                  ).quantize(PER_KWH, ROUND_HALF_UP)),
        "rated_kwh": battery["rated_kwh"],
        "allocated_kwh": battery["allocated_kwh"],
        "grid_factor_id": grid["grid_factor_id"],
        "grid_kgco2e_per_kwh": grid["kgco2e_per_kwh"],
        "assurance": calculator.ASSURANCE,
    }
    if any(result.get(key) != value for key, value in expected.items()):
        raise ValueError("Carbon output differs from the independent calculation.")
    return {
        "status": "verified",
        "assurance": calculator.ASSURANCE,
        "method": expected["method"],
        "factor_set_version": expected["factor_set_version"],
        "input_hash": expected["input_hash"],
        "calculation_sha256": domain.sha256(domain.canonical(expected)),
    }
