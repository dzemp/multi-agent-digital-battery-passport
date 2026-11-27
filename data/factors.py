"""The committed illustrative factor set (data/factors.toml): material PCF per kg, plant grid factors, synthetic risk
ratings and second-life values. Illustrative, not official."""

import tomllib
from pathlib import Path

FACTORS = Path(__file__).resolve().parent / "factors.toml"


def load_factors(path=FACTORS):
    """The factor set as a plain dictionary that can travel as JSON (for example in the Fabric step settings)."""
    with open(path, "rb") as handle:
        factors = tomllib.load(handle)
    for section in ("factor_set", "materials", "grid", "second_life"):
        if section not in factors:
            raise ValueError(f"{path.name}: missing [{section}]")
    return factors
