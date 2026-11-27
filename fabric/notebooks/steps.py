"""The four notebook steps. A generated notebook's code cell holds CONFIG, the rules, the flows, SparkStore and this
file, then calls main(<step>, CONFIG, <parameters>, spark).

run_step takes any store, so the tests run exactly these steps over a FolderStore.
"""

import json

from data.pipeline.deliveries import parse_delivery_set
from data.pipeline.flows import bronze_load, clear_tables, gold_package, reporting_measures, silver_normalise, utc_now

from .spark_store import SparkStore

STEP_LAKEHOUSE = {"bronze_load": "ingest", "silver_normalise": "silver", "gold_package": "gold",
                  "reporting_measures": "reporting", "reset_tables": "reporting"}
STEP_PARAMETERS = {"bronze_load": ("delivery_set",), "silver_normalise": ("delivery_set", "build_id"),
                   "gold_package": ("delivery_set", "build_id", "build_sequence", "completed_utc"),
                   "reporting_measures": ("export_id",), "reset_tables": ()}


def run_step(step, store, config, parameters):
    """Run one step with the pipeline's parameters; raises on anything unexpected, so the activity fails."""
    if step not in STEP_PARAMETERS:
        raise ValueError(f"Unknown step {step!r}")
    missing = [name for name in STEP_PARAMETERS[step] if not parameters.get(name)]
    if missing:
        raise ValueError(f"{step}: the pipeline passed no {', '.join(missing)}")
    if step == "reset_tables":
        return clear_tables(store)
    registry = config["registry"]
    if step == "reporting_measures":
        return reporting_measures(store, parameters["export_id"])
    delivery_set = parse_delivery_set(parameters["delivery_set"])
    if step == "bronze_load":
        return bronze_load(store, delivery_set, registry, utc_now())
    if step == "silver_normalise":
        return silver_normalise(store, delivery_set, parameters["build_id"], registry)
    # The orchestrator passes the simulator's completion time, so this package matches the local one byte for byte.
    return gold_package(store, delivery_set, parameters["build_id"], int(parameters["build_sequence"]), registry,
                        config["code_hash"], parameters["completed_utc"], config["factors"])


def main(step, config, parameters, spark):
    store = SparkStore(spark, config["workspace_id"], config["lakehouses"], STEP_LAKEHOUSE[step])
    result = run_step(step, store, config, parameters)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return result
