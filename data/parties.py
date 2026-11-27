"""Fictional parties in the synthetic supply chain: their role, delivery profile, materials and signed event types.

All names are fictional and regions are continent-level only. A party may only deliver the kinds its role allows, in
its registered format and mass unit, and only the materials it is registered for.
"""

from dataclasses import dataclass

from .pipeline.deliveries import KIND_ENTITIES
from .pipeline.rules import ENTITY_ROLES, KG_PER_UNIT, MATERIALS, REGIONS


@dataclass(frozen=True)
class Party:
    party_id: str
    name: str
    role: str
    kinds: tuple
    format: str
    unit: str
    region: str
    materials: tuple = ()
    events: tuple = ()
    site: str = ""


ROLE_KINDS = {"supplier": ("lots",), "recycler": ("lots",), "carrier": ("transport",),
              "plant": ("batches", "builds", "consumption", "energy"), "oem": (), "second_life_operator": ()}
ROLE_EVENTS = {"recycler": ("record_recycling_closure",),
               "oem": ("record_soh", "record_ownership_change", "record_end_of_life"),
               "second_life_operator": ("request_repurpose",)}
FLAT_KINDS = ("lots", "transport", "consumption", "energy", "batches")  # CSV cannot carry the series' battery list
PLANT_KINDS = ROLE_KINDS["plant"]
CELL_COMPONENTS = ("manganese", "electrolyte", "copper_foil", "aluminium_foil", "separator")
PARTIES = (
    Party("SUP-NORTHWIND", "Northwind Lithium Refining (fictional)", "supplier", ("lots",), "json", "kg",
          "South America", ("lithium",)),
    Party("SUP-WIDEWORLD", "Wide World Lithium Chemicals (fictional)", "supplier", ("lots",), "csv", "g",
          "South America", ("lithium",)),
    Party("SUP-PROSEWARE", "Proseware Cobalt (fictional)", "supplier", ("lots",), "json", "kg", "Africa",
          ("cobalt",)),
    Party("SUP-WOODGROVE", "Woodgrove Nickel (fictional)", "supplier", ("lots",), "csv", "t", "Asia", ("nickel",)),
    Party("SUP-LITWARE", "Litware Graphite (fictional)", "supplier", ("lots",), "json", "kg", "Asia", ("graphite",)),
    Party("SUP-LUCERNE", "Lucerne Cell Components (fictional)", "supplier", ("lots",), "json", "kg", "Europe",
          CELL_COMPONENTS),
    Party("RECYCLER-TREY", "Trey Recycling (fictional)", "recycler", ("lots",), "json", "kg", "Europe",
          ("cobalt", "lithium", "nickel"), ROLE_EVENTS["recycler"], "Trey recovery site (fictional)"),
    Party("CARRIER-FABRIKAM", "Fabrikam Freight (fictional)", "carrier", ("transport",), "json", "kg", "Europe"),
    Party("PLANT-SE", "Cell plant Sweden (fictional)", "plant", PLANT_KINDS, "json", "kg", "Europe"),
    Party("PLANT-DE", "Cell plant Germany (fictional)", "plant", PLANT_KINDS, "json", "kg", "Europe"),
    Party("PLANT-PL", "Cell plant Poland (fictional)", "plant", PLANT_KINDS, "json", "kg", "Europe"),
    Party("OEM-ADVENTUREWORKS", "Adventure Works Motors (fictional)", "oem", (), "json", "kg", "Europe",
          events=ROLE_EVENTS["oem"]),
    Party("SECONDLIFE-RELECLOUD", "Relecloud Energy Storage (fictional)", "second_life_operator", (), "json", "kg",
          "Europe", events=ROLE_EVENTS["second_life_operator"]),
)
PLANTS = tuple(party.party_id for party in PARTIES if party.role == "plant")
PLANT_COUNTRIES = {"PLANT-SE": "Sweden", "PLANT-DE": "Germany", "PLANT-PL": "Poland"}


def party(party_id, parties=PARTIES):
    return next(item for item in parties if item.party_id == party_id)


def check_profiles(parties=PARTIES):
    """Problems with the party table; an empty list means every profile is usable."""
    problems, seen = [], set()
    for item in parties:
        if item.party_id in seen:
            problems.append(f"{item.party_id}: listed twice")
        seen.add(item.party_id)
        if item.role not in ROLE_KINDS:
            problems.append(f"{item.party_id}: unknown role {item.role}")
            continue
        for kind in item.kinds:
            if kind not in ROLE_KINDS[item.role] or kind not in KIND_ENTITIES:
                problems.append(f"{item.party_id}: a {item.role} cannot deliver {kind}")
            elif any(item.role not in ENTITY_ROLES.get(entity, ("plant",)) for entity in KIND_ENTITIES[kind]):
                problems.append(f"{item.party_id}: {kind} records belong to another role")
            if item.format == "csv" and kind not in FLAT_KINDS:
                problems.append(f"{item.party_id}: {kind} cannot be delivered as CSV")
        if item.format not in ("json", "csv"):
            problems.append(f"{item.party_id}: unsupported format {item.format}")
        if item.unit not in KG_PER_UNIT:
            problems.append(f"{item.party_id}: unsupported unit {item.unit}")
        if item.region not in REGIONS:
            problems.append(f"{item.party_id}: region {item.region} is not a continent")
        if "lots" in item.kinds and not item.materials:
            problems.append(f"{item.party_id}: a lot party needs its materials")
        for material in item.materials:
            if material not in MATERIALS:
                problems.append(f"{item.party_id}: unknown material {material}")
        for event in item.events:
            if event not in ROLE_EVENTS.get(item.role, ()):
                problems.append(f"{item.party_id}: a {item.role} cannot sign {event}")
    return problems


def registry(parties=PARTIES):
    """The JSON-ready registry the pipeline rules check deliveries against."""
    problems = check_profiles(parties)
    if problems:
        raise ValueError("Invalid party profiles: " + "; ".join(problems))
    return {item.party_id: {"name": item.name, "role": item.role, "kinds": list(item.kinds), "format": item.format,
                            "unit": item.unit, "region": item.region, "materials": list(item.materials)}
            for item in parties}
