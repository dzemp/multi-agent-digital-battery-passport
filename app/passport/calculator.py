"""Carbon footprint calculator, method illustrative-pcf-2 (illustrative, not an EU calculation method).

kg CO2e = sum over the battery's composition of mass_kg x ((1 - r) x primary PCF + r x recycled PCF)
          + allocated kWh x the plant's grid factor,
where r is the lot's recycled share and the PCF and grid values come from the pinned factor set (factors.toml, a copy
of the producer's factor set checked by a test). The footprint per kWh divides by the battery's rated kWh.
Rounding: total kg to 0.001 and kg per kWh to 0.0001, half up. A batch that pins another method or factor-set
version is refused rather than calculated with the wrong numbers.
"""

import tomllib
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

METHOD = "illustrative-pcf-2"
FACTORS_FILE = Path(__file__).resolve().parent / "factors.toml"
SCOPE = "cradle-to-gate materials and cell manufacturing electricity only; illustrative factors, not official"
ASSURANCE = "synthetic_unverified"
KG = Decimal("0.001")
PER_KWH = Decimal("0.0001")


class MethodMismatch(ValueError):
    """The batch pins a method or factor set this app does not have."""


def load_factor_sets(path=FACTORS_FILE):
    with open(path, "rb") as handle:
        factors = tomllib.load(handle)
    return {factors["factor_set"]["factor_set_version"]: factors}


FACTOR_SETS = load_factor_sets()


def supported(method_version, factor_set_version):
    sets = FACTOR_SETS.get(factor_set_version)
    return method_version == METHOD and sets is not None and sets["factor_set"]["method_version"] == METHOD


def _dec(value):
    return Decimal(str(value))


def calculate(battery, batch, composition, lots):
    """battery/batch: package rows; composition: the battery's parts; lots: {lot_id: lot row}."""
    if not supported(batch["method_version"], batch["factor_set_version"]):
        raise MethodMismatch(f"batch {batch['batch_id']} pins {batch['method_version']} with "
                             f"{batch['factor_set_version']}; this app calculates {METHOD} only with "
                             f"{', '.join(sorted(FACTOR_SETS))}")
    factors = FACTOR_SETS[batch["factor_set_version"]]
    grid = factors["grid"].get(battery["plant_id"])
    if grid is None or grid["grid_factor_id"] != battery["grid_factor_id"]:
        raise MethodMismatch(f"{battery['battery_id']}: grid factor {battery['grid_factor_id']} is not in the "
                             f"factor set {batch['factor_set_version']}")
    materials = Decimal(0)
    for part in sorted(composition, key=lambda p: p["lot_id"]):
        values = factors["materials"].get(part["material"])
        if values is None:
            raise MethodMismatch(f"{part['material']} has no factor in {batch['factor_set_version']}")
        share = _dec(lots[part["lot_id"]]["recycled_share_pct"]) / 100
        per_kg = (1 - share) * _dec(values["primary_kgco2e_per_kg"]) + share * _dec(values["recycled_kgco2e_per_kg"])
        materials += _dec(part["mass_kg"]) * per_kg
    electricity = _dec(battery["allocated_kwh"]) * _dec(grid["kgco2e_per_kwh"])
    total = (materials + electricity).quantize(KG, ROUND_HALF_UP)
    per_kwh = ((materials + electricity) / _dec(battery["rated_kwh"])).quantize(PER_KWH, ROUND_HALF_UP)
    return {"method": METHOD, "factor_set_version": batch["factor_set_version"],
            "input_hash": battery["input_hash"], "materials_kg_co2e": float(materials.quantize(KG, ROUND_HALF_UP)),
            "electricity_kg_co2e": float(electricity.quantize(KG, ROUND_HALF_UP)), "kg_co2e": float(total),
            "kg_co2e_per_kwh": float(per_kwh), "rated_kwh": battery["rated_kwh"],
            "allocated_kwh": battery["allocated_kwh"], "grid_factor_id": grid["grid_factor_id"],
            "grid_kgco2e_per_kwh": grid["kgco2e_per_kwh"], "scope": SCOPE, "assurance": ASSURANCE,
            "label": factors["factor_set"]["label"]}
